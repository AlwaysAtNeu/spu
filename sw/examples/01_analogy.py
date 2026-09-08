"""Classic VSA symbolic reasoning: "What is the dollar of Mexico?"

Records are role/filler bundles:  USA = maj(NAME*USA, CURRENCY*DOLLAR, CAPITAL*DC)
Query:  DOLLAR * USA * MEXICO  ->  cleanup over the item memory  ->  PESO
(binding is XOR so unbinding is binding; the noisy result is cleaned up with an
associative search over all atomic symbols).  Everything below the host loop runs
as one SPU program: the host only reads back indices.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from spuc.dsl import Program
from spuc.runtime import get_device, Session

dev = get_device(sys.argv[1] if len(sys.argv) > 1 else "sim")
p = Program(name="analogy")
roles = {r: p.symbol(r, ns="role") for r in ("NAME", "CURRENCY", "CAPITAL")}
atoms = ["USA", "DOLLAR", "WASHINGTON", "MEXICO", "PESO", "MEXICO_CITY", "JAPAN", "YEN", "TOKYO"]
sym = {a: p.symbol(a, ns="atom") for a in atoms}
item_mem = p.symbol_table("ITEMS", [sym[a] for a in atoms])
out = p.buffer("OUT", 64, kind="output")

with p.scope():
    def record(rec, name, currency, capital):
        """rec = maj(NAME*name, CURRENCY*currency, CAPITAL*capital); temporaries die with the scope."""
        with p.scope():
            comps = [p.bind(p.gen(roles["NAME"]), p.gen(sym[name])),
                     p.bind(p.gen(roles["CURRENCY"]), p.gen(sym[currency])),
                     p.bind(p.gen(roles["CAPITAL"]), p.gen(sym[capital]))]
            p.bundle(comps, rec)
    usa = p.hv("USA"); record(usa, "USA", "DOLLAR", "WASHINGTON")
    mex = p.hv("MEX"); record(mex, "MEXICO", "PESO", "MEXICO_CITY")
    # "dollar of mexico":  DOLLAR (x) USA (x) MEXICO  ~  PESO
    q = p.bind(p.gen(sym["DOLLAR"]), usa)
    p.bind(q, mex, q)
    idx = p.cleanup(q, item_mem, len(atoms))
    p.sw(idx, out, 0)
    p.sw(p.csrr(p.s(), "SR_DIST"), out, 4)
    p.sw(p.csrr(p.s(), "SR_DIST2"), out, 8)
    # "capital of usa": CAPITAL (x) USA ~ WASHINGTON
    q2 = p.bind(p.gen(roles["CAPITAL"]), usa)
    idx2 = p.cleanup(q2, item_mem, len(atoms))
    p.sw(idx2, out, 12)

binary = p.compile()
s = Session(dev, binary).run()
r = s.get_u32s("OUT", 4)
print("dollar of Mexico  ->", atoms[r[0]], "(distance %d, runner-up distance %d, D=%d)" % (r[1], r[2], p.cfg.D))
print("capital of USA    ->", atoms[r[3]])
print("program: %d instructions, %s" % (len(binary.words), s.report()))
