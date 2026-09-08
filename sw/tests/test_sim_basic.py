import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from spuc.isa import DEFAULT_CONFIG as cfg, CSR
from spuc.asm import assemble, disassemble
from spuc.sim import SPUSim
from spuc import hv as H

D = cfg.D


def run(text, sim=None):
    sim = sim or SPUSim()
    sim.load_program(assemble(text))
    st = sim.run(max_instr=10_000_000)
    assert st == "halt", (st, sim.pc)
    return sim


PROG_GEN_BIND = """
        HGEN h1, 7
        HGEN h2, 8
        HXOR h3, h1, h2
        HXOR h4, h3, h2      ; unbind -> h1
        HDIST s1, h4, h1     ; 0
        HDIST s2, h1, h2     ; ~D/2
        HROT h5, h1, 5
        HROT h6, h5, s0, -5  ; back
        HDIST s3, h6, h1
        LI s4, 100
        HMASK h7, s4, 28     ; 128 bits
        HDIST s5, h7, h0     ; popcount(h7) = 128 (h0 is 0)
        HALT"""

PROG_ACC_SEARCH = """
        LI s1, 0x10000
        HLD h1, 0(s1)
        HLD h2, 1024(s1)
        HLD h3, 2048(s1)
        ACC.CLR 2
        ACC.ADD 2, h1, 1
        ACC.ADD 2, h2, 1
        ACC.ADD 2, h3, 1
        ACC.THR h4, 2, 0
        LI s2, 4
        LI s3, 0x20000
        CSRW DOUT, s3
        HSEARCH.WD s4, h4, s1, s2
        CSRR s5, SR_DIST
        CSRR s6, SR_IDX2
        HST h4, 4096(s1)
        ACC.ST 2, 0x100(s3)
        ACC.SUB 2, h1, 3
        ACC.CLR 3
        ACC.LD 3, 0x100(s3)
        ACC.THR h5, 3, 0
        HALT"""

PROG_SCALAR_LOOP = """
        LI s1, 0        ; i
        LI s2, 10       ; n
        LI s3, 0        ; sum
        LI s10, 0x3000
    loop:
        ADD s3, s3, s1
        SW s3, 0(s10)
        ADDI s10, s10, 4
        ADDI s1, s1, 1
        BLT s1, s2, loop
        SUBI s4, s0, 1  ; -1
        SRAI s5, s4, 4  ; -1
        SHRI s6, s4, 4  ; 0x0fffffff
        SLT s7, s4, s0  ; 1
        SLTU s8, s4, s0 ; 0
        MULI s9, s2, 7  ; 70
        LB s11, 1(s10)  ; byte 1 of the word after the loop -> 0
        LW s12, -4(s10) ; last stored = 45
        HALT"""


def acc_search_memory():
    """Initial memory image for PROG_ACC_SEARCH: 4 HVs at 0x10000."""
    from spuc.sim import Memory
    mem = Memory()
    hvs = [H.hgen(s, D) for s in (1, 2, 3)] + [H.hgen(999, D)]
    for k, x in enumerate(hvs):
        mem.write(0x10000 + k * cfg.HV_BYTES, H.to_bytes(x, D))
    return mem, hvs


def test_gen_bind_dist():
    sim = run(PROG_GEN_BIND)
    assert sim.h[1] == H.hgen(7, D) and sim.h[2] == H.hgen(8, D)
    assert sim.s[1] == 0 and sim.s[3] == 0
    assert abs(sim.s[2] - D // 2) < D // 16
    assert sim.h[5] == H.rotl(sim.h[1], 5, D)
    assert sim.s[5] == 128


def test_acc_bundle_and_search():
    mem, hvs4 = acc_search_memory()
    hvs, tb = hvs4[:3], hvs4[3]
    sim = SPUSim(mem=mem)
    run(PROG_ACC_SEARCH, sim)
    b = H.bundle(hvs, D)
    assert sim.h[4] == b
    dists = [H.hamming(b, x) for x in hvs + [tb]]
    assert sim.s[4] == int(np.argmin(dists))
    assert sim.s[5] == min(dists)
    got = [sim.mem.read_u32(0x20000 + 4 * k) for k in range(4)]
    assert got == dists
    assert H.from_bytes(sim.mem.read(0x10000 + 4096, cfg.HV_BYTES)) == b
    assert sim.h[5] == b                          # ACC.ST / ACC.LD roundtrip
    exp = sum(H.to_pm1(x, D).astype(np.int32) for x in hvs) - 3 * H.to_pm1(hvs[0], D).astype(np.int32)
    assert np.array_equal(sim.acc[2], exp.astype(np.int16))


def test_scalar_loop_and_branches():
    sim = run(PROG_SCALAR_LOOP)
    assert sim.s[3] == 45 and sim.s[1] == 10
    assert sim.s[5] == 0xFFFFFFFF and sim.s[6] == 0x0FFFFFFF and sim.s[7] == 1 and sim.s[8] == 0 and sim.s[9] == 70
    assert sim.s[12] == 45 and sim.s[11] == 0
    assert [sim.mem.read_u32(0x3000 + 4 * k) for k in range(10)] == [sum(range(k + 1)) for k in range(10)]


def test_disassemble_roundtrip():
    text = """
        ADDI s1, s0, -7
        LW s2, 8(s1)
        SW s2, -8(s1)
        BNE s1, s2, 0
        HROT h3, h4, s5, 9
        ACC.THR h1, 3, s2, -1
        HSEARCH.WD s1, h2, s3, s4
        CSRW DOUT, s3
        HALT"""
    words = assemble(text)
    assert assemble(disassemble(words, with_addr=False)) == words


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
