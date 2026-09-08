#!/usr/bin/env python3
"""Measure RTL cycles per instruction type on the Verilator model (zero AXI back-pressure,
minimum memory latency), and compare with the golden model's cost estimate.
usage: cd sw && python3 ../hw/tb/measure_cycles.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sw"))
from spuc.sim import SPUSim, Memory
from spuc.asm import assemble
from spuc.cosim import run_rtl
from spuc import hv as H
from spuc.isa import DEFAULT_CONFIG as cfg

PRE = "LI s1, 0x10000\nLI s2, 8\nLI s3, 0x20000\nCSRW DOUT, s3\nHGEN h1, 1\nHGEN h2, 2\n"
CASES = [
    ("ADDI (scalar ALU)", "ADDI s4, s1, 1"),
    ("BEQ not taken", "BEQ s1, s2, skip\nNOP\nskip:"),
    ("BEQ taken", "BEQ s1, s1, skip\nNOP\nskip:"),
    ("MULI", "MULI s4, s1, 3"),
    ("LW", "LW s4, 0(s1)"),
    ("SW", "SW s4, 0(s1)"),
    ("CSRR", "CSRR s4, SR_IDX"),
    ("HXOR", "HXOR h3, h1, h2"),
    ("HROT (rd != ra)", "HROT h3, h1, 5"),
    ("HROT (rd == ra, buffered)", "HROT h1, h1, 5"),
    ("HGEN", "HGEN h3, 3"),
    ("HMASK", "HMASK h3, 100"),
    ("HLD", "HLD h3, 0(s1)"),
    ("HST", "HST h1, 0(s1)"),
    ("HDIST", "HDIST s4, h1, h2"),
    ("HSEARCH 8 candidates", "HSEARCH s4, h1, s1, s2"),
    ("HSEARCH.WD 8 candidates", "HSEARCH.WD s4, h1, s1, s2"),
    ("ACC.CLR", "ACC.CLR 1"),
    ("ACC.ADD", "ACC.ADD 1, h1, 1"),
    ("ACC.THR", "ACC.THR h3, 1, 0"),
    ("ACC.ST", "ACC.ST 1, 0(s3)"),
    ("ACC.LD", "ACC.LD 1, 0(s3)"),
]


def run(text):
    mem = Memory()
    for k in range(8):
        mem.write(0x10000 + k * cfg.HV_BYTES, H.to_bytes(H.hgen(50 + k, cfg.D), cfg.D))
    init = Memory()
    for b, n in mem.touched_ranges():
        init.write(b, mem.read(b, n))
    words = assemble(text)
    sim = SPUSim(mem=mem); sim.load_program(words); sim.run()
    rtl = run_rtl(words, init, backpressure=0, max_lat=4)
    return sim.cycles, rtl.cycles


base_sim, base_rtl = run(PRE + "HALT")
print("%-28s %8s %8s" % ("instruction", "RTL cyc", "model"))
for name, ins in CASES:
    s, r = run(PRE + ins + "\nHALT")
    print("%-28s %8d %8d" % (name, r - base_rtl, s - base_sim))
