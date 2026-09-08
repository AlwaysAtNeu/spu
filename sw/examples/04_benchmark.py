"""Cycle-count benchmark (simulator cost model or RTL) for the three workloads.

usage: python3 04_benchmark.py [sim|rtl] [scale]
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from spuc.runtime import get_device, Session
from spuc.frontends.hdc import HDCModel, synthetic_dataset
from spuc.frontends.logic import KnowledgeBase, Var, Rule

dev_name = sys.argv[1] if len(sys.argv) > 1 else "sim"
scale = int(sys.argv[2]) if len(sys.argv) > 2 else 1
F_MHZ = 250.0
rows = []

def bench(name, binary, inputs, work_units):
    s = Session(get_device(dev_name), binary)
    for k, v in inputs.items():
        s.set_input(k, v)
    s.run()
    cyc = s.dev.cycles()
    rows.append((name, binary.stats["n_instr"], s.dev.instret(), cyc, cyc / F_MHZ / 1e3, work_units, cyc / work_units))
    return s

# 1. HDC training / inference
n_tr, n_te, n_cls, n_feat, Q = 200 * scale, 100 * scale, 8, 32, 16
Xtr, ytr, Xte, yte = synthetic_dataset(n_tr, n_te, n_cls, n_feat, Q, noise=0.8, seed=7)
m = HDCModel(n_classes=n_cls, n_features=n_feat, n_levels=Q)
s = bench("HDC train (%d samples x %d feat, 2 epochs)" % (n_tr, n_feat), m.compile_train(n_tr, epochs=2),
          {"X": m.pack_X(Xtr), "Y": m.pack_y(ytr)}, n_tr * 2)
protos = s.get_output("PROTO")
si = bench("HDC infer (%d samples)" % n_te, m.compile_infer(n_te), {"X": m.pack_X(Xte), "PROTO": protos}, n_te)
acc = (np.array(si.get_u32s("PRED")) == yte).mean()

# 2. knowledge base: chain of parent facts, ancestor closure
n_people = 12 * scale
kb = KnowledgeBase(max_facts=64 + n_people * n_people, max_answers=n_people * n_people)
parent, ancestor = kb.relation("parent"), kb.relation("ancestor")
ppl = [kb.entity("p%d" % i) for i in range(n_people)]
for i in range(n_people - 1):
    kb.fact(parent(ppl[i], ppl[i + 1]))
X, Y, Z = Var("X"), Var("Y"), Var("Z")
rules = [Rule(ancestor(X, Y), [parent(X, Y)]), Rule(ancestor(X, Z), [parent(X, Y), ancestor(Y, Z)])]
br = kb.compile_rules(rules, iterations=n_people + 1)
s = bench("KB ancestor closure (%d parent facts)" % (n_people - 1), br, {}, 1)
n_der = s.get_u32s("NDERIVED", 1)[0]
rows[-1] = rows[-1][:5] + (n_der, rows[-1][3] / max(1, n_der))

print("device=%s  clock assumed %.0f MHz  (HDC test accuracy %.3f, KB derived %d facts)" % (dev_name, F_MHZ, acc, n_der))
print("%-48s %6s %9s %11s %9s %8s %10s" % ("workload", "instr", "instret", "cycles", "ms", "units", "cyc/unit"))
for r in rows:
    print("%-48s %6d %9d %11d %9.3f %8d %10.0f" % r)
