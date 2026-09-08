import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from spuc.isa import DEFAULT_CONFIG as cfg
from spuc.dsl import Program
from spuc.runtime import SimDevice, Session
from spuc import hv as H

D = cfg.D


def test_symbols_bind_permute_cleanup():
    p = Program(name="t1")
    a, b, c = (p.symbol(n) for n in "abc")
    items = [p.symbol("item%d" % i) for i in range(40)]
    tbl = p.symbol_table("items", items)
    out = p.buffer("out", 64, kind="output")
    with p.scope():
        ha, hb = p.gen(a), p.gen(b)
        x = p.bind(ha, hb)                  # a*b
        y = p.permute(x, 3)
        z = p.permute(y, -3)                # back to a*b
        u = p.bind(z, hb)                   # a
        d0 = p.dist(u, ha)
        p.sw(d0, out, 0)
        # cleanup of a noisy version of item 7 (flip 1000 bits with a mask) -> index 7
        h7 = p.gen(items[7])
        m = p.mask(1000)
        noisy = p.bind(h7, m)
        idx = p.cleanup(noisy, tbl, len(items))
        p.sw(idx, out, 4)
        dist = p.csrr(p.s(), "SR_DIST")
        p.sw(dist, out, 8)
        idx2 = p.csrr(p.s(), "SR_IDX2")
        p.sw(idx2, out, 12)
    b = p.compile()
    s = Session(SimDevice(), b).run()
    r = s.get_u32s("out", 4)
    assert r[0] == 0 and r[1] == 7 and r[2] == 1000 and r[3] != 7


def test_bundle_and_loops_scalars():
    p = Program(name="t2")
    fam = p.symbol_family("f", 5)
    out = p.buffer("out", 1024 + 64, kind="output")
    counts = p.buffer("counts", 64, kind="output")
    with p.scope():
        hs = [p.gen(m) for m in fam]
        bnd = p.bundle(hs)                     # odd count -> exact majority
        p.store(bnd, out, 0)
        # device-side loop generating the same family via HGEN(s + base) and summing distances to bnd
        acc = p.s(0)
        with p.loop(5) as i:
            g = p.gen((i, fam.base))
            d = p.dist(g, bnd)
            p.set(acc, acc + d)
            with p.if_(d, "<", 3000):
                p.set(acc, acc + 1000000)
        p.sw(acc, out, 1024)
        # nested loop with if/else and expression evaluation
        n = p.s(0)
        with p.loop(4) as i:
            with p.loop(3) as j:
                with p.if_else(i, "==", j) as (then, els):
                    with then():
                        p.set(n, n + 100)
                    with els():
                        p.set(n, n + (i * 3 + j))
        p.sw(n, counts, 0)
        e = p.eval((n << 2) - 7 + (n & 0xF))
        p.sw(e, counts, 4)
    b = p.compile()
    s = Session(SimDevice(), b).run()
    hvs = [m.value(D) for m in fam]
    exp = H.bundle(hvs, D)
    assert H.from_bytes(s.get_output("out", 1024)) == exp
    dsum = sum(H.hamming(x, exp) for x in hvs)
    got = s.get_u32s("out")[256]
    assert got == dsum + 5 * 1000000, (got, dsum)
    n_exp = 0
    for i in range(4):
        for j in range(3):
            n_exp += 100 if i == j else i * 3 + j
    c = s.get_u32s("counts", 2)
    assert c[0] == n_exp and c[1] == (((n_exp << 2) - 7 + (n_exp & 0xF)) & 0xFFFFFFFF)


def test_search_wrdist_and_tables():
    p = Program(name="t3")
    fam = p.symbol_family("e", 12)
    tbl = p.symbol_table("tbl", fam)
    dists = p.buffer("dists", 12 * 4, kind="output")
    res = p.buffer("res", 16, kind="output")
    with p.scope():
        q = p.gen(fam[9])
        m = p.mask(2500)
        q = p.bind(q, m)
        idx = p.search(q, tbl, 12, wrdist=dists)
        p.sw(idx, res, 0)
        # load entry idx from the table by index and compare with the generated symbol
        e = p.load_idx(tbl, idx)
        other = p.gen(fam[3])            # must not clobber e (regression: load_idx freed its dst)
        g = p.gen(fam[9])
        d = p.dist(e, g)
        p.free(other)
        p.sw(d, res, 4)
        p.store_idx(q, tbl, idx)         # overwrite entry 9 with the noisy query
    b = p.compile()
    s = Session(SimDevice(), b).run()
    r = s.get_u32s("res", 2)
    assert r == [9, 0]
    ds = s.get_u32s("dists")
    exp = [H.hamming(fam[9].value(D) ^ H.hmask(2500, D), m.value(D)) for m in fam]
    assert ds == exp
    entry9 = H.from_bytes(s.dev.read(tbl.offset + 9 * 1024, 1024))
    assert entry9 == fam[9].value(D) ^ H.hmask(2500, D)
    assert b.stats["n_instr"] < 40


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
