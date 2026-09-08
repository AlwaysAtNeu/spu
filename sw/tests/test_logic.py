import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from spuc.isa import DEFAULT_CONFIG as cfg
from spuc.runtime import SimDevice, Session
from spuc.frontends.logic import KnowledgeBase, Var, Rule, Neq


def family_kb():
    kb = KnowledgeBase(max_facts=128, max_answers=64)
    parent = kb.relation("parent", 2)
    people = "alice bob carol dave erin frank grace heidi ivan judy".split()
    E = {n: kb.entity(n) for n in people}
    for a, b in [("alice", "bob"), ("alice", "carol"), ("bob", "dave"), ("bob", "erin"),
                 ("carol", "frank"), ("dave", "grace"), ("erin", "heidi"), ("frank", "ivan"), ("grace", "judy")]:
        kb.fact(parent(E[a], E[b]))
    return kb, parent, E


def test_query_pattern():
    kb, parent, E = family_kb()
    X, Y = Var("X"), Var("Y")
    for atom in (parent(E["alice"], Y), parent(X, E["heidi"]), parent(X, Y), parent(E["bob"], E["dave"]), parent(E["bob"], E["judy"])):
        b = kb.compile_query(atom)
        s = Session(SimDevice(), b).run()
        got = sorted(kb.decode_answers(s, b))
        ref = sorted(tuple(kb.entities[i].name for i in r) for r in kb.query_ref(atom))
        if not kb._vars(atom):     # ground query: one empty-ish row per match
            assert len(got) == len(ref), (atom, got, ref)
        else:
            assert got == ref, (atom, got, ref)


def test_rules_forward_chaining():
    kb, parent, E = family_kb()
    X, Y, Z, P = Var("X"), Var("Y"), Var("Z"), Var("P")
    grandparent = kb.relation("grandparent", 2)
    sibling = kb.relation("sibling", 2)
    ancestor = kb.relation("ancestor", 2)
    rules = [
        Rule(grandparent(X, Z), [parent(X, Y), parent(Y, Z)]),
        Rule(sibling(X, Y), [parent(P, X), parent(P, Y), Neq(X, Y)]),
        Rule(ancestor(X, Y), [parent(X, Y)]),
        Rule(ancestor(X, Z), [parent(X, Y), ancestor(Y, Z)]),
    ]
    b = kb.compile_rules(rules, iterations=6)
    s = Session(SimDevice(), b).run()
    got = set(kb.decode_derived(s, b))
    ref = set((kb.relations[r].name,) + tuple(kb.entities[i].name for i in a) for r, a in kb.evaluate_ref(rules))
    assert got == ref, (got ^ ref)
    assert s.get_u32s("NFACTS", 1)[0] == len(kb.facts) + len(ref)
    print("derived", len(got), "facts;", b.stats)


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
