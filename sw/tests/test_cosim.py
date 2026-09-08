"""RTL co-simulation: every program runs on the Verilator model of the SPU and on the
Python golden model; registers, CSRs and memory must match bit for bit.
Skips when verilator is not installed."""
import os
import random
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest

from spuc.isa import DEFAULT_CONFIG as cfg
from spuc import hv as H
from spuc.sim import Memory
from spuc.cosim import cosim, rtl_available
from test_sim_basic import PROG_GEN_BIND, PROG_ACC_SEARCH, PROG_SCALAR_LOOP, acc_search_memory

pytestmark = pytest.mark.skipif(not rtl_available(), reason="verilator not on PATH")
D = cfg.D


def check(text, mem=None, **kw):
    sim, rtl, bad = cosim(text, mem, **kw)
    assert rtl.done, rtl.stdout[-2000:]
    assert not bad, bad
    return sim, rtl


def test_gen_bind_dist():
    check(PROG_GEN_BIND)


def test_acc_bundle_and_search():
    mem, _ = acc_search_memory()
    check(PROG_ACC_SEARCH, mem)


def test_scalar_loop_and_branches():
    check(PROG_SCALAR_LOOP)


def test_rotate_mask_edge_cases():
    check("""
        HGEN h1, 7
        HGEN h2, 8
        HROT h1, h1, 1000    ; in-place rotate (buffered path)
        HROT h2, h2, 8191
        HROT h3, h1, 512     ; whole-chunk offsets
        HROT h4, h1, 8192    ; identity
        HROT h5, h1, s0, -1
        HNOT h8, h2
        HAND h9, h1, h2
        HOR  h10, h1, h2
        HMASK h11, -5
        HMASK h12, 9000
        HMASK h13, 8192
        HMASK h14, 511
        HMASK h15, 513
        HMASK h16, 0
        HALT""")


def test_search_unaligned_dout_and_errors():
    mem = Memory()
    rng = random.Random(3)
    tbl = 0x40000 - 0x40   # table straddles a 4 KiB boundary on purpose
    for k in range(9):
        mem.write(tbl + k * cfg.HV_BYTES, H.to_bytes(H.hgen(100 + k, D), D))
    check("""
        LI s1, %d
        LI s2, 9
        LI s3, 0x50004        ; DOUT not 64-byte aligned
        CSRW DOUT, s3
        HGEN h1, 104
        HROT h1, h1, 3
        HSEARCH.WD s4, h1, s1, s2
        CSRR s5, SR_DIST
        CSRR s6, SR_IDX2
        CSRR s7, SR_DIST2
        LI s2, 0              ; empty search
        HSEARCH s8, h1, s1, s2
        CSRR s9, SR_DIST
        LI s2, 17             ; more than 16 -> two distance beats
        LI s3, 0x50040
        CSRW DOUT, s3
        HSEARCH.WD s10, h1, s1, s2
        LW s11, 0(s3)
        LW s12, 64(s3)
        SB s2, 3(s3)
        LB s13, 3(s3)
        SW s2, 0x1000(s3)
        LW s14, 0x1000(s3)
        HALT""" % tbl, mem)


def test_illegal_opcode_sets_error():
    from spuc.isa import Instr
    from spuc.asm import assemble
    words = assemble("LI s1, 5\nHALT") + []
    words.insert(1, Instr(op=0x3E).encode())          # illegal opcode
    sim, rtl, bad = cosim(words)
    assert sim.err and rtl.err and not bad


def test_acc_weights_and_thresholds():
    check("""
        HGEN h1, 1
        HGEN h2, 2
        HGEN h3, 3
        LI s1, -7
        LI s2, 32767
        ACC.CLR 5
        ACC.ADD 5, h1, s1, 0      ; weight -7
        ACC.ADD 5, h2, s2, 0      ; weight 32767 (saturates)
        ACC.ADD 5, h2, s2, 0
        ACC.SUB 5, h3, 5
        ACC.SUB 5, h3, s2, 1      ; weight -32768 negated
        ACC.THR h4, 5, 0
        ACC.THR h5, 5, s1, 3      ; threshold -4
        ACC.THR h6, 5, 32000
        ACC.THR h7, 5, -32768
        LI s3, 0x60000
        ACC.ST 5, 0(s3)
        ACC.CLR 15
        ACC.LD 15, 0(s3)
        ACC.THR h8, 15, 0
        ACC.LD 6, 0x20(s3)        ; unaligned address -> low bits ignored
        ACC.THR h9, 6, 0
        HALT""")


def _random_program(seed, n_ops=60):
    rng = random.Random(seed)
    tbl, scratch, dout = 0x100000, 0x200000, 0x300000
    lines = ["LI s20, %d" % tbl, "LI s21, %d" % scratch, "LI s22, %d" % dout, "CSRW DOUT, s22"]
    for k in range(6):
        lines += ["HGEN h%d, %d" % (k + 1, rng.randrange(1 << 32)), "HST h%d, %d(s20)" % (k + 1, k * cfg.HV_BYTES)]
    for _ in range(n_ops):
        op = rng.choice(["gen", "xor", "and", "or", "not", "rot", "mask", "ld", "st", "dist", "search", "acc",
                         "thr", "salu", "lw", "sw", "lb", "sb"])
        rd, ra, rb = rng.randrange(1, 32), rng.randrange(0, 32), rng.randrange(0, 32)
        sd, s1, s2 = rng.randrange(1, 20), rng.randrange(0, 20), rng.randrange(0, 20)
        if op == "gen":
            lines.append("HGEN h%d, s%d, %d" % (rd, s1, rng.randrange(-100, 100)))
        elif op in ("xor", "and", "or"):
            lines.append("H%s h%d, h%d, h%d" % (op.upper(), rd, ra, rb))
        elif op == "not":
            lines.append("HNOT h%d, h%d" % (rd, ra))
        elif op == "rot":
            lines.append("HROT h%d, h%d, s%d, %d" % (rd, ra, rng.choice([0, s1]), rng.randrange(-9000, 9000)))
        elif op == "mask":
            lines.append("HMASK h%d, s%d, %d" % (rd, rng.choice([0, s1]), rng.randrange(-100, 9000)))
        elif op == "ld":
            lines.append("HLD h%d, %d(s20)" % (rd, rng.randrange(0, 8) * cfg.HV_BYTES + rng.randrange(0, 64)))
        elif op == "st":
            lines.append("HST h%d, %d(s20)" % (rd, rng.randrange(0, 8) * cfg.HV_BYTES))
        elif op == "dist":
            lines.append("HDIST s%d, h%d, h%d" % (sd, ra, rb))
        elif op == "search":
            lines += ["LI s19, %d" % rng.randrange(0, 9), "HSEARCH%s s%d, h%d, s20, s19" % (rng.choice(["", ".WD"]), sd, ra)]
        elif op == "acc":
            k = rng.randrange(0, cfg.NACC)
            lines.append(rng.choice(["ACC.CLR %d" % k,
                                     "ACC.ADD %d, h%d, %d" % (k, ra, rng.randrange(-40, 40)),
                                     "ACC.SUB %d, h%d, s%d, %d" % (k, ra, s1, rng.randrange(-3, 3)),
                                     "ACC.ST %d, %d(s21)" % (k, rng.randrange(0, 4) * 16384),
                                     "ACC.LD %d, %d(s21)" % (k, rng.randrange(0, 4) * 16384)]))
        elif op == "thr":
            lines.append("ACC.THR h%d, %d, %d" % (rd, rng.randrange(0, cfg.NACC), rng.randrange(-10, 10)))
        elif op == "salu":
            fn = rng.choice(["ADD", "SUB", "AND", "OR", "XOR", "SHL", "SHR", "SRA", "MUL", "SLT", "SLTU"])
            if rng.random() < 0.5:
                lines.append("%s s%d, s%d, s%d" % (fn, sd, s1, s2))
            else:
                imn = {"SLT": "SLTI", "SLTU": "SLTIU"}.get(fn, fn + "I")
                lines.append("%s s%d, s%d, %d" % (imn, sd, s1, rng.randrange(-1000, 1000)))
        elif op == "lw":
            lines.append("LW s%d, %d(s21)" % (sd, rng.randrange(0, 256) * 4))
        elif op == "sw":
            lines.append("SW s%d, %d(s21)" % (s1, rng.randrange(0, 256) * 4))
        elif op == "lb":
            lines.append("LB s%d, %d(s21)" % (sd, rng.randrange(0, 1024)))
        elif op == "sb":
            lines.append("SB s%d, %d(s21)" % (s1, rng.randrange(0, 1024)))
    lines.append("HALT")
    return "\n".join(lines)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_differential(seed):
    mem = Memory()
    rng = random.Random(seed)
    for k in range(8):
        mem.write(0x100000 + k * cfg.HV_BYTES, H.to_bytes(H.hgen(rng.randrange(1 << 32), D), D))
    mem.write(0x200000, bytes(rng.randrange(256) for _ in range(4096)))
    check(_random_program(seed), mem, seed=seed, backpressure=rng.choice([0, 30, 70]))


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_") and name != "test_random_differential":
            f(); print("ok", name)
    for s in (1, 2, 3):
        test_random_differential(s); print("ok random", s)
