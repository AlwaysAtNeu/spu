"""Resonator-style factorisation on the SPU: given the composite symbol
S = X_i (x) Y_j (x) Z_k with three codebooks of N symbols each, recover (i, j, k)
by iterating   x <- cleanup_X(S (x) y (x) z),  y <- cleanup_Y(S (x) x (x) z),  ...
starting from the superposition (bundle) of every codebook entry.  This is the
core of parsing / structure decoding in vector symbolic architectures; the whole
iteration (bind, cleanup, convergence test) runs as one SPU program.

usage: python3 05_resonator.py [sim|rtl] [N] [iterations]
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from spuc.dsl import Program
from spuc.runtime import get_device, Session

dev_name = sys.argv[1] if len(sys.argv) > 1 else "sim"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 10
ITERS = int(sys.argv[3]) if len(sys.argv) > 3 else 8
rng = np.random.default_rng(1)

p = Program(name="resonator")
books = [p.symbol_family(nm, N) for nm in ("X", "Y", "Z")]
tables = [p.symbol_table("T" + b.name, b) for b in books]
truth = [int(v) for v in rng.integers(0, N, size=3)]
out = p.buffer("OUT", 64, kind="output")

with p.scope():
    # composite to decode (built on the device from the true factors)
    S = p.gen(books[0][truth[0]], name="S")
    p.bind(S, p.gen(books[1][truth[1]]), S)
    p.bind(S, p.gen(books[2][truth[2]]), S)
    # initial estimates: bundle of each whole codebook (device-side, via accumulators)
    est = [p.hv("est_" + b.name) for b in books]
    for b, e in zip(books, est):
        with p.scope():
            members = [p.gen(m) for m in b]
            p.bundle(members, e)
    idx = [p.s(0xFFFFFFFF, name="i" + b.name) for b in books]
    prev = [p.s(0xFFFFFFFE, name="prev" + b.name) for b in books]
    n_iter = p.s(0, name="n_iter")
    with p.loop(ITERS) as it:
        for f in range(3):
            o1, o2 = [g for g in range(3) if g != f]
            with p.scope():
                u = p.bind(S, est[o1], name="u")      # S (x) other estimates ~ factor f
                p.bind(u, est[o2], u)
                p.cleanup(u, tables[f], N, dst=idx[f])
                p.load_idx(tables[f], idx[f], dst=est[f])   # hard cleanup: snap to the codebook entry
        p.set(n_iter, n_iter + 1)
        # converged when all three indices are unchanged
        with p.if_(idx[0], "==", prev[0]):
            with p.if_(idx[1], "==", prev[1]):
                with p.if_(idx[2], "==", prev[2]):
                    p.break_()
        for f in range(3):
            p.set(prev[f], idx[f])
    for f in range(3):
        p.sw(idx[f], out, 4 * f)
    p.sw(n_iter, out, 12)

binary = p.compile()
s = Session(get_device(dev_name), binary).run()
r = s.get_u32s("OUT", 4)
print("codebooks: 3 x %d symbols -> %d possible composites" % (N, N ** 3))
print("true factors  :", truth)
print("decoded       :", r[:3], "in %d iterations" % r[3], "(correct)" if r[:3] == truth else "(WRONG)")
print("program: %d instructions |" % len(binary.words), s.report())
