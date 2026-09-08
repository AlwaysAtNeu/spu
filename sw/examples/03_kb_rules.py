"""Symbolic knowledge base on the SPU: facts, pattern queries and Horn rules
(forward chaining to a fixpoint) executed as SPU programs.

usage: python3 03_kb_rules.py [sim|rtl]
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from spuc.runtime import get_device, Session
from spuc.frontends.logic import KnowledgeBase, Var, Rule, Neq

dev_name = sys.argv[1] if len(sys.argv) > 1 else "sim"
kb = KnowledgeBase(max_facts=256, max_answers=128)
parent, lives_in, located_in = kb.relation("parent"), kb.relation("lives_in"), kb.relation("located_in")
E = {n: kb.entity(n) for n in "alice bob carol dave erin frank grace london paris uk france europe".split()}
for a, b in [("alice", "bob"), ("alice", "carol"), ("bob", "dave"), ("bob", "erin"), ("carol", "frank"), ("dave", "grace")]:
    kb.fact(parent(E[a], E[b]))
for a, b in [("alice", "london"), ("bob", "paris"), ("carol", "london"), ("dave", "paris")]:
    kb.fact(lives_in(E[a], E[b]))
for a, b in [("london", "uk"), ("paris", "france"), ("uk", "europe"), ("france", "europe")]:
    kb.fact(located_in(E[a], E[b]))

X, Y, Z, P, C = (Var(n) for n in "XYZPC")
grandparent, sibling, ancestor, lives_in_region = (kb.relation(n) for n in ("grandparent", "sibling", "ancestor", "lives_in_region"))
rules = [
    Rule(grandparent(X, Z), [parent(X, Y), parent(Y, Z)]),
    Rule(sibling(X, Y), [parent(P, X), parent(P, Y), Neq(X, Y)]),
    Rule(ancestor(X, Y), [parent(X, Y)]),
    Rule(ancestor(X, Z), [parent(X, Y), ancestor(Y, Z)]),
    Rule(lives_in_region(X, C), [lives_in(X, Y), located_in(Y, C)]),
    Rule(lives_in_region(X, C), [lives_in_region(X, Y), located_in(Y, C)]),
]

print("== pattern query: parent(alice, ?Y) ==")
bq = kb.compile_query(parent(E["alice"], Y))
s = Session(get_device(dev_name), bq).run()
print("  answers:", kb.decode_answers(s, bq), "|", s.report())

print("== forward chaining with %d rules ==" % len(rules))
br = kb.compile_rules(rules, iterations=8)
print("  program: %d instructions, %d facts in the initial KB" % (br.stats["n_instr"], len(kb.facts)))
s = Session(get_device(dev_name), br).run()
derived = kb.decode_derived(s, br)
by_rel = {}
for d in derived:
    by_rel.setdefault(d[0], []).append(d[1:])
for r, rows in by_rel.items():
    print("  %-16s %d: %s" % (r, len(rows), " ".join("(%s)" % ",".join(a) for a in rows)))
print("  total derived: %d  final KB size: %d  |" % (len(derived), s.get_u32s("NFACTS", 1)[0]), s.report())
ref = set((kb.relations[r].name,) + tuple(kb.entities[i].name for i in a) for r, a in kb.evaluate_ref(rules))
print("  matches host Datalog evaluator:", set(derived) == ref)
