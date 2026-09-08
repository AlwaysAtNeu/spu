"""RTL co-simulation harness: run a program on the Verilator model of the SPU and
compare every architectural side effect against the Python golden model.

    from spuc.cosim import run_rtl, compare, rtl_available
    sim = SPUSim(); sim.load_program(words); ...; sim.run()
    rtl = run_rtl(words, initial_memory)          # initial_memory: spuc.sim.Memory before the run
    mismatches = compare(sim, rtl)                # [] when the RTL matches

The Verilator binary is built on demand with `make` in hw/tb (cached in hw/tb/obj_dir).
"""
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from .isa import DEFAULT_CONFIG, CSR, NCSR
from .asm import to_hex
from .sim import Memory, SPUSim

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TB_DIR = os.path.join(ROOT, "hw", "tb")
RTL_DIR = os.path.join(ROOT, "hw", "rtl")
BINARY = os.path.join(TB_DIR, "obj_dir", "tb_spu")
# CSRs that legitimately differ between the cost model and the RTL
_SKIP_CSR = {CSR["CYCLES_LO"], CSR["CYCLES_HI"], CSR["INSTRET_LO"], CSR["INSTRET_HI"]}


def rtl_available():
    return shutil.which("verilator") is not None


def build(force=False, quiet=True):
    """Build (or rebuild when sources changed) the Verilator model; returns the binary path."""
    if not rtl_available():
        raise RuntimeError("verilator not found on PATH")
    args = ["make", "-s"] + (["-B"] if force else [])
    r = subprocess.run(args, cwd=TB_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0 or not os.path.exists(BINARY):
        raise RuntimeError("verilator build failed:\n" + r.stdout[-4000:])
    if not quiet and r.stdout.strip():
        print(r.stdout)
    return BINARY


@dataclass
class RtlResult:
    sregs: list
    hregs: list
    csrs: list
    cycles: int
    instret: int
    status: int
    irq: int
    mem: Memory
    stdout: str = ""
    workdir: str = ""
    timed_out: bool = False

    @property
    def done(self):
        return bool(self.status & 0x2)

    @property
    def err(self):
        return bool(self.status & 0x4)


def parse_mem_hex(path):
    mem = Memory()
    addr = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("@"):
                addr = int(line[1:], 16)
                continue
            data = bytes.fromhex(line.zfill(128))[::-1]
            mem.write(addr * 64, data)
            addr += 1
    return mem


def run_rtl(prog_words, mem: Memory, workdir=None, timeout_cycles=5_000_000, seed=1,
            backpressure=30, max_lat=24, keep=False, binary=None):
    """Run the program on the RTL with the given initial memory image."""
    binary = binary or build()
    tmp = workdir or tempfile.mkdtemp(prefix="spu_cosim_")
    os.makedirs(tmp, exist_ok=True)
    files = {k: os.path.join(tmp, k) for k in ("prog.hex", "mem.hex", "mem_out.hex", "sregs.txt", "hregs.txt", "csrs.txt", "cycles.txt")}
    with open(files["prog.hex"], "w") as f:
        f.write(to_hex(prog_words))
    with open(files["mem.hex"], "w") as f:
        f.write(mem.dump_hex())
    cmd = [binary, "+prog=" + files["prog.hex"], "+mem=" + files["mem.hex"], "+mem_out=" + files["mem_out.hex"],
           "+sregs=" + files["sregs.txt"], "+hregs=" + files["hregs.txt"], "+csrs=" + files["csrs.txt"],
           "+cycles=" + files["cycles.txt"], "+timeout=%d" % timeout_cycles, "+verilator+seed+%d" % seed,
           "+backpressure=%d" % backpressure, "+max_lat=%d" % max_lat]
    r = subprocess.run(cmd, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.exists(files["cycles.txt"]):
        raise RuntimeError("RTL run failed (rc=%d):\n%s" % (r.returncode, r.stdout[-4000:]))
    sregs = [int(l, 16) for l in open(files["sregs.txt"]).read().split()]
    hregs = [int(l, 16) for l in open(files["hregs.txt"]).read().split()]
    csrs = [int(l, 16) for l in open(files["csrs.txt"]).read().split()]
    kv = dict(l.split() for l in open(files["cycles.txt"]).read().strip().splitlines())
    res = RtlResult(sregs=sregs, hregs=hregs, csrs=csrs, cycles=int(kv["cycles"]), instret=int(kv["instret"]),
                    status=int(kv["status"]), irq=int(kv["irq"]), mem=parse_mem_hex(files["mem_out.hex"]),
                    stdout=r.stdout, workdir=tmp, timed_out="TIMEOUT" in r.stdout)
    if not keep and workdir is None:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def compare(sim: SPUSim, rtl: RtlResult, verbose=True, check_hregs=True):
    """Return a list of mismatch descriptions (empty when RTL == golden model)."""
    bad = []
    cfg = sim.cfg
    if rtl.timed_out:
        bad.append("RTL timed out")
    if rtl.err != bool(sim.err):
        bad.append("error flag: rtl=%s sim=%s" % (rtl.err, bool(sim.err)))
    for i in range(cfg.NSREG):
        if rtl.sregs[i] != sim.s[i]:
            bad.append("s%d: rtl=%08x sim=%08x" % (i, rtl.sregs[i], sim.s[i]))
    if check_hregs:
        for i in range(cfg.NHREG):
            if rtl.hregs[i] != sim.h[i]:
                diff = bin(rtl.hregs[i] ^ sim.h[i]).count("1")
                bad.append("h%d differs in %d bits" % (i, diff))
    for i in range(NCSR):
        if i in _SKIP_CSR:
            continue
        if rtl.csrs[i] != sim.csr[i]:
            bad.append("csr%d: rtl=%08x sim=%08x" % (i, rtl.csrs[i], sim.csr[i]))
    # memory: every page the simulator touched, plus anything non-zero the RTL wrote elsewhere
    seen = set()
    for base, n in sim.mem.touched_ranges():
        seen.add(base)
        a = sim.mem.read(base, n)
        b = rtl.mem.read(base, n)
        if a != b:
            first = next(i for i in range(n) if a[i] != b[i])
            nbad = sum(1 for i in range(n) if a[i] != b[i])
            bad.append("memory page 0x%x: %d bytes differ, first at 0x%x (sim=%02x rtl=%02x)" % (base, nbad, base + first, a[first], b[first]))
    for base, n in rtl.mem.touched_ranges():
        if base not in seen and any(rtl.mem.read(base, n)):
            bad.append("RTL wrote page 0x%x which the model never touched" % base)
    if verbose:
        print("cosim: rtl cycles=%d instret=%d | model cycles=%d instret=%d | %s" %
              (rtl.cycles, rtl.instret, sim.cycles, sim.instret, "OK" if not bad else "%d MISMATCH(ES)" % len(bad)))
        for b in bad[:40]:
            print("  ", b)
    return bad


def cosim(text_or_words, mem=None, cfg=DEFAULT_CONFIG, **kw):
    """Convenience: assemble (if text), run model + RTL, compare.  Returns (sim, rtl, mismatches)."""
    from .asm import assemble
    words = assemble(text_or_words) if isinstance(text_or_words, str) else list(text_or_words)
    mem = mem if mem is not None else Memory()
    # snapshot of the initial memory for the RTL
    init = Memory()
    for base, n in mem.touched_ranges():
        init.write(base, mem.read(base, n))
    sim = SPUSim(cfg, mem=mem)
    sim.load_program(words)
    sim.run(max_instr=50_000_000)
    rtl = run_rtl(words, init, **kw)
    return sim, rtl, compare(sim, rtl)
