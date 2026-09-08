"""Vector-symbolic knowledge base: facts, pattern queries and Horn rules on the SPU.

Representation
--------------
Every entity, relation and argument role is an atomic symbol (random HV).  A fact
rel(a_0, ..., a_{n-1}) is encoded as the majority bundle

    F = maj( REL (x) ARG_0 (x) A_0,  ...,  REL (x) ARG_{n-1} (x) A_{n-1},  REL (x) TAG )

(+ the program's tie-break symbol when the count is even).  Each component can be
recovered by unbinding: F (x) REL (x) ARG_j ~ A_j with similarity ~0.75 (3-way
majority), which a cleanup search over the entity table turns back into a symbol.

Inference
---------
A pattern such as parent(alice, ?Y) is evaluated by (1) searching the fact table
with the single bound component REL (x) ARG_0 (x) ALICE - matching facts sit at
Hamming distance ~D/4, unrelated ones at ~D/2 - (2) verifying the other bound
components of each candidate by unbinding, and (3) extracting the unbound
arguments by unbinding + cleanup.  A Horn rule is a chain of such pattern
evaluations sharing variable bindings (nested loops on the SPU); derived head
facts are encoded, de-duplicated against the table and appended, so repeated
passes compute a (bounded) forward-chaining fixpoint.  The whole evaluation is
one SPU program; the host only decodes indices back to names.
"""
from contextlib import ExitStack
from math import comb
from ..isa import Config, DEFAULT_CONFIG
from ..dsl import Program, CompileError
from .. import hv as H


class Var:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "?" + self.name


class Entity:
    __slots__ = ("name", "index", "symbol")

    def __init__(self, name, index, symbol):
        self.name, self.index, self.symbol = name, index, symbol

    def __repr__(self):
        return self.name


class Relation:
    __slots__ = ("name", "arity", "index", "symbol")

    def __init__(self, name, arity, index, symbol):
        self.name, self.arity, self.index, self.symbol = name, arity, index, symbol

    def __call__(self, *terms):
        if len(terms) != self.arity:
            raise ValueError("%s expects %d arguments" % (self.name, self.arity))
        return Atom(self, terms)

    def __repr__(self):
        return self.name


class Atom:
    __slots__ = ("rel", "terms")

    def __init__(self, rel, terms):
        self.rel, self.terms = rel, tuple(terms)

    def __repr__(self):
        return "%s(%s)" % (self.rel.name, ", ".join(map(repr, self.terms)))


class Neq:
    """Built-in constraint X != Y between two variables (or a variable and an entity)."""
    __slots__ = ("a", "b")

    def __init__(self, a, b):
        self.a, self.b = a, b

    def __repr__(self):
        return "%r != %r" % (self.a, self.b)


class Rule:
    def __init__(self, head, body):
        self.head, self.body = head, list(body)
        vs = set(t for a in self.body if isinstance(a, Atom) for t in a.terms if isinstance(t, Var))
        for t in head.terms:
            if isinstance(t, Var) and t not in vs:
                raise CompileError("unsafe rule: head variable %r not bound in the body" % t)

    def __repr__(self):
        return "%r :- %s" % (self.head, ", ".join(map(repr, self.body)))


def majority_agreement(k):
    """P(component agrees with a k-way majority of independent random components), k odd."""
    return sum(comb(k - 1, j) for j in range((k - 1) // 2, k)) / 2.0 ** (k - 1)


class KnowledgeBase:
    def __init__(self, cfg: Config = DEFAULT_CONFIG, max_entities=1024, max_relations=64, max_arity=4,
                 max_facts=512, max_answers=256):
        self.cfg, self.D = cfg, cfg.D
        self.max_entities, self.max_relations, self.max_arity = max_entities, max_relations, max_arity
        self.max_facts, self.max_answers = max_facts, max_answers
        self.entities, self.relations, self.facts = [], [], []
        self._ent_by_name, self._rel_by_name = {}, {}
        self._sym = self._symbols(Program(cfg, "kb_symbols"))

    # ------------------------------------------------------------ symbols
    def _symbols(self, p):
        p.namespace("sym")
        p.namespace("internal")
        p.tie_break()
        self.ent_fam = p.symbol_family("ent", self.max_entities)
        self.rel_fam = p.symbol_family("rel", self.max_relations)
        self.arg_fam = p.symbol_family("arg", self.max_arity)
        self.tag = p.symbol("tag")
        return p

    # ------------------------------------------------------------- schema
    def entity(self, name):
        if name in self._ent_by_name:
            return self._ent_by_name[name]
        if len(self.entities) >= self.max_entities:
            raise CompileError("too many entities")
        e = Entity(name, len(self.entities), self.ent_fam[len(self.entities)])
        self.entities.append(e)
        self._ent_by_name[name] = e
        return e

    def relation(self, name, arity=2):
        if name in self._rel_by_name:
            return self._rel_by_name[name]
        if len(self.relations) >= self.max_relations or arity > self.max_arity:
            raise CompileError("too many relations or arity too large")
        r = Relation(name, arity, len(self.relations), self.rel_fam[len(self.relations)])
        self.relations.append(r)
        self._rel_by_name[name] = r
        return r

    def fact(self, atom):
        if any(isinstance(t, Var) for t in atom.terms):
            raise CompileError("facts must be ground")
        key = (atom.rel.index, tuple(t.index for t in atom.terms))
        if key not in set((r, a) for r, a in self.facts):
            if len(self.facts) >= self.max_facts:
                raise CompileError("fact table full")
            self.facts.append(key)
        return atom

    # ------------------------------------------------------ host encoding
    def _components(self, rel_idx, arg_idx):
        D = self.D
        R = self.rel_fam[rel_idx].value(D)
        comps = [R ^ self.arg_fam[j].value(D) ^ self.ent_fam[a].value(D) for j, a in enumerate(arg_idx)]
        comps.append(R ^ self.tag.value(D))
        return comps

    def encode_fact(self, rel_idx, arg_idx):
        return H.bundle(self._components(rel_idx, arg_idx), self.D, tie_break=self._sym.tie_break().value(self.D))

    def n_components(self, arity):
        k = arity + 1
        return k + 1 if k % 2 == 0 else k

    def match_threshold(self, arity):
        """Hamming threshold separating 'component present' (~(1-p)D) from chance (D/2)."""
        p = majority_agreement(self.n_components(arity))
        return int(self.D * (1.5 - p) / 2)

    def dup_threshold(self):
        return self.D // 8

    def facts_image(self):
        return b"".join(H.to_bytes(self.encode_fact(r, a), self.D) for r, a in self.facts)

    # ----------------------------------------------------- host reference
    def query_ref(self, atom, facts=None):
        facts = self.facts if facts is None else facts
        out = []
        for r, args in facts:
            if r != atom.rel.index:
                continue
            env = {}
            ok = True
            for t, a in zip(atom.terms, args):
                if isinstance(t, Var):
                    if t in env and env[t] != a:
                        ok = False
                        break
                    env[t] = a
                elif t.index != a:
                    ok = False
                    break
            if ok:
                out.append(tuple(env[v] for v in self._vars(atom)))
        return out

    def evaluate_ref(self, rules, iterations=None):
        """Naive forward chaining on the host; returns the derived facts (in order)."""
        facts = list(self.facts)
        known = set(facts)
        derived = []
        it = 0
        while iterations is None or it < iterations:
            it += 1
            new = []
            for rule in rules:
                for env in self._match_body(rule.body, {}, facts):
                    f = (rule.head.rel.index, tuple(env[t] if isinstance(t, Var) else t.index for t in rule.head.terms))
                    if f not in known:
                        known.add(f)
                        new.append(f)
                        facts.append(f)
                        derived.append(f)
            if not new:
                break
        return derived

    def _match_body(self, body, env, facts):
        if not body:
            yield dict(env)
            return
        first, rest = body[0], body[1:]
        if isinstance(first, Neq):
            a = env[first.a] if isinstance(first.a, Var) else first.a.index
            b = env[first.b] if isinstance(first.b, Var) else first.b.index
            if a != b:
                for e in self._match_body(rest, env, facts):
                    yield e
            return
        for r, args in list(facts):
            if r != first.rel.index:
                continue
            env2 = dict(env)
            ok = True
            for t, a in zip(first.terms, args):
                if isinstance(t, Var):
                    if t in env2 and env2[t] != a:
                        ok = False
                        break
                    env2[t] = a
                elif t.index != a:
                    ok = False
                    break
            if ok:
                for e in self._match_body(rest, env2, facts):
                    yield e

    @staticmethod
    def _vars(atom):
        seen = []
        for t in atom.terms:
            if isinstance(t, Var) and t not in seen:
                seen.append(t)
        return seen

    # ---------------------------------------------------------- device code
    def _layout(self, p, extra_facts=0):
        n_ent = len(self.entities)
        self.ENT = p.hv_table("ENT", [e.symbol.value(self.D) for e in self.entities])
        fimg = self.facts_image()
        cap = min(self.max_facts, len(self.facts) + extra_facts)
        self.FACTS = p.buffer("FACTS", cap * self.cfg.HV_BYTES, kind="table", init=fimg)
        self.NFACTS = p.buffer("NFACTS", 64, kind="output", init=len(self.facts).to_bytes(4, "little"))
        self.fact_cap = cap
        self.n_ent = n_ent
        self._dists = {}

    def _dist_buf(self, p, depth):
        if depth not in self._dists:
            self._dists[depth] = p.buffer("DISTS%d" % depth, 4 * self.fact_cap, kind="internal")
        return self._dists[depth]

    def _ent_hv(self, p, term, env):
        """HV register for an entity constant or a bound variable."""
        if isinstance(term, Entity):
            return p.gen(term.symbol, name=term.name)
        return env[term][1]

    def _eval_atom(self, p, atom, env, depth, cont):
        """Evaluate one body atom; call cont(env2) inside the innermost match block."""
        rel = atom.rel
        thr = self.match_threshold(rel.arity)
        bound = [(j, t) for j, t in enumerate(atom.terms) if isinstance(t, Entity) or t in env]
        unbound = [(j, t) for j, t in enumerate(atom.terms) if not (isinstance(t, Entity) or t in env)]
        with p.scope():
            R = p.gen(rel.symbol, name="REL")
            q = p.hv("q")
            if bound:
                j0, t0 = bound[0]
                with p.scope():
                    a = p.gen(self.arg_fam[j0], name="ARG")
                    p.bind(R, a, q)
                    p.bind(q, self._ent_hv(p, t0, env), q)
            else:
                with p.scope():
                    p.bind(R, p.gen(self.tag, name="TAG"), q)
            dists = self._dist_buf(p, depth)
            n = p.s(self.nf, name="nsnap")                 # snapshot: facts appended later are not in dists
            p.search(q, self.FACTS, n, wrdist=dists)
            p.free(q)
            with p.loop(n, name="f%d" % depth) as f:
                d = p.lw(p.s(name="d"), p.addr(dists, f, stride=4))
                with p.if_(d, "<", thr):
                    F = p.load_idx(self.FACTS, f, name="F")
                    env2 = dict(env)
                    with ExitStack() as st:
                        for j, t in bound[1:]:                     # verify remaining bound components
                            U = p.bind(F, R, name="U")
                            with p.scope():
                                p.bind(U, p.gen(self.arg_fam[j]), U)
                            dd = p.dist(U, self._ent_hv(p, t, env), name="dd")
                            p.free(U)
                            st.enter_context(p.if_(dd, "<", thr))
                        seen_vars = {}
                        for j, t in unbound:                       # extract unbound arguments
                            U = p.bind(F, R, name="U")
                            with p.scope():
                                p.bind(U, p.gen(self.arg_fam[j]), U)
                            if t in seen_vars:                     # same variable twice in one atom
                                dd = p.dist(U, env2[t][1], name="dd")
                                p.free(U)
                                st.enter_context(p.if_(dd, "<", thr))
                                continue
                            idx = p.cleanup(U, self.ENT, self.n_ent, name=t.name)
                            dd = p.csrr(p.s(name="dd"), "SR_DIST")
                            p.free(U)
                            st.enter_context(p.if_(dd, "<", thr))
                            e = p.load_idx(self.ENT, idx, name=t.name)
                            env2[t] = (idx, e)
                            seen_vars[t] = True
                        cont(env2)

    def _eval_body(self, p, body, env, depth, cont):
        if not body:
            cont(env)
            return
        first, rest = body[0], body[1:]
        if isinstance(first, Neq):
            a = env[first.a][0] if isinstance(first.a, Var) else first.a.index
            b = env[first.b][0] if isinstance(first.b, Var) else first.b.index
            with p.if_(a, "!=", b):
                self._eval_body(p, rest, env, depth, cont)
            return
        self._eval_atom(p, first, env, depth, lambda env2: self._eval_body(p, rest, env2, depth + 1, cont))

    def _emit_encode_fact(self, p, rel, terms, env, dst):
        """Encode head fact rel(terms) on the device (entities from env / constants)."""
        with p.scope():
            R = p.gen(rel.symbol, name="REL")
            comps = []
            for j, t in enumerate(terms):
                c = p.bind(R, self._ent_hv(p, t, env), name="c%d" % j)
                with p.scope():
                    p.bind(c, p.gen(self.arg_fam[j]), c)
                comps.append(c)
            with p.scope():
                comps.append(p.bind(R, p.gen(self.tag), name="ctag"))
                p.bundle(comps, dst)
        return dst

    def compile_query(self, atom, name="kb_query"):
        """Answers of a pattern (variables in first-occurrence order).
        Outputs: NANS (u32), ANS (rows of len(vars) u32)."""
        p = self._symbols(Program(self.cfg, name))
        self._layout(p)
        vs = self._vars(atom)
        row = max(1, len(vs))
        NANS = p.buffer("NANS", 64, kind="output")
        ANS = p.buffer("ANS", 4 * row * self.max_answers, kind="output")
        with p.scope():
            self.nf = p.lw(p.s(name="nf"), self.NFACTS, 0)
            na = p.s(0, name="na")

            def emit_answer(env):
                with p.if_(na, "<", self.max_answers):
                    with p.scope():
                        a = p.addr(ANS, na, stride=4 * row)
                        for k, v in enumerate(vs):
                            p.sw(env[v][0], a, 4 * k)
                    p.set(na, na + 1)
            self._eval_atom(p, atom, {}, 0, emit_answer)
            p.sw(na, NANS, 0)
        p.halt()
        b = p.compile()
        b.meta = {"vars": [v.name for v in vs], "row": row}
        return b

    def compile_rules(self, rules, iterations=4, name="kb_rules", max_new=None):
        """Forward chaining: apply the rules repeatedly (at most `iterations` passes,
        stopping early when a pass derives nothing).  Derived facts are appended to
        FACTS on the device.  Outputs: NFACTS, NDERIVED, DERIVED (rows: rel, arg0..arg{max_arity-1})."""
        max_new = self.max_answers if max_new is None else max_new
        p = self._symbols(Program(self.cfg, name))
        self._layout(p, extra_facts=max_new)
        row = 1 + self.max_arity
        NDER = p.buffer("NDERIVED", 64, kind="output")
        DER = p.buffer("DERIVED", 4 * row * max_new, kind="output")
        thr_dup = self.dup_threshold()
        with p.scope():
            self.nf = p.lw(p.s(name="nf"), self.NFACTS, 0)
            nd = p.s(0, name="nd")
            added = p.s(0, name="added")
            with p.loop(iterations, name="pass"):
                p.set(added, 0)
                for rule in rules:
                    def derive(env, rule=rule):
                        with p.if_(self.nf, "<", self.fact_cap):
                            Fh = p.hv("Fh")
                            self._emit_encode_fact(p, rule.head.rel, rule.head.terms, env, Fh)
                            with p.scope():
                                p.search(Fh, self.FACTS, self.nf)           # de-duplicate
                                dd = p.csrr(p.s(name="dd"), "SR_DIST")
                                with p.if_(dd, ">=", thr_dup):
                                    p.store_idx(Fh, self.FACTS, self.nf)
                                    with p.scope():
                                        a = p.addr(DER, nd, stride=4 * row)
                                        p.sw(p.s(rule.head.rel.index), a, 0)
                                        for k, t in enumerate(rule.head.terms):
                                            v = env[t][0] if isinstance(t, Var) else p.s(t.index)
                                            p.sw(v, a, 4 * (k + 1))
                                    p.set(nd, nd + 1)
                                    p.set(self.nf, self.nf + 1)
                                    p.set(added, 1)
                            p.free(Fh)
                    self._eval_body(p, rule.body, {}, 0, derive)
                with p.if_(added, "==", 0):
                    p.break_()
            p.sw(self.nf, self.NFACTS, 0)
            p.sw(nd, NDER, 0)
        p.halt()
        b = p.compile()
        b.meta = {"row": row}
        return b

    # ------------------------------------------------------------- decode
    def decode_answers(self, session, binary):
        n = session.get_u32s("NANS", 1)[0]
        row = binary.meta["row"]
        vals = session.get_u32s("ANS", n * row) if n else []
        return [tuple(self.entities[vals[i * row + k]].name for k in range(row)) for i in range(n)]

    def decode_derived(self, session, binary):
        n = session.get_u32s("NDERIVED", 1)[0]
        row = binary.meta["row"]
        vals = session.get_u32s("DERIVED", n * row) if n else []
        out = []
        for i in range(n):
            r = self.relations[vals[i * row]]
            args = tuple(self.entities[vals[i * row + 1 + k]].name for k in range(r.arity))
            out.append((r.name,) + args)
        return out
