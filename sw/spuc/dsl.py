"""SPU compiler front half: a structured program builder ("DSL").

The DSL is the level at which the symbolic frontends (spuc.frontends.*) describe
computations: hypervector registers, scalar registers, memory buffers, symbols,
and structured control flow.  `Program.compile()` lowers everything to machine
code, lays out memory, resolves labels / buffer addresses and returns a `Binary`
that any `spuc.runtime.Device` can execute.

Register allocation is scoped and deterministic: `with p.scope():` frees every
register allocated inside the block; there is no spilling (frontends are written
so that <= 31 live scalars / 32 live hypervectors suffice).
"""
from contextlib import contextmanager
import zlib
from .isa import (Config, DEFAULT_CONFIG, OP, ALU_FN, BR_FN, ACC_FN, CSR, SEARCH_WRDIST, Instr)
from . import hv as H
from .asm import disassemble

M32 = 0xFFFFFFFF


class CompileError(Exception):
    pass


# ---------------------------------------------------------------------------
# handles
# ---------------------------------------------------------------------------
class _Handle:
    """Register handle; using it after its scope freed the register is a compile error."""
    __slots__ = ("p", "_r", "name", "alive")
    kind = "?"

    def __init__(self, p, r, name=""):
        self.p, self._r, self.name, self.alive = p, r, name, True

    @property
    def r(self):
        if not self.alive:
            raise CompileError("use of freed %s register %s%d%s (allocate it outside the scope that freed it)"
                               % (self.kind, self.kind, self._r, "(%s)" % self.name if self.name else ""))
        return self._r

    def __repr__(self):
        return "%s%d%s" % (self.kind, self._r, "(%s)" % self.name if self.name else "")


class S(_Handle):
    """Scalar register handle (physical register, allocated for a scope)."""
    __slots__ = ()
    kind = "s"

    # arithmetic builds Expr trees, lowered by Program.eval()
    def __add__(self, o):  return Expr("ADD", self, o)
    def __radd__(self, o): return Expr("ADD", self, o)
    def __sub__(self, o):  return Expr("SUB", self, o)
    def __rsub__(self, o): return Expr("SUB", o, self)
    def __mul__(self, o):  return Expr("MUL", self, o)
    def __rmul__(self, o): return Expr("MUL", self, o)
    def __lshift__(self, o): return Expr("SHL", self, o)
    def __rshift__(self, o): return Expr("SHR", self, o)
    def __and__(self, o):  return Expr("AND", self, o)
    def __or__(self, o):   return Expr("OR", self, o)
    def __xor__(self, o):  return Expr("XOR", self, o)


class Expr:
    __slots__ = ("op", "a", "b")

    def __init__(self, op, a, b):
        self.op, self.a, self.b = op, a, b

    def __add__(self, o):  return Expr("ADD", self, o)
    def __sub__(self, o):  return Expr("SUB", self, o)
    def __mul__(self, o):  return Expr("MUL", self, o)
    def __lshift__(self, o): return Expr("SHL", self, o)
    def __rshift__(self, o): return Expr("SHR", self, o)
    def __and__(self, o):  return Expr("AND", self, o)
    def __or__(self, o):   return Expr("OR", self, o)


class HV(_Handle):
    """Hypervector register handle."""
    __slots__ = ()
    kind = "h"


class Symbol:
    """An atomic symbol: a deterministic pseudo-random HV identified by a 32-bit seed."""
    __slots__ = ("name", "seed", "index", "family")

    def __init__(self, name, seed, index=None, family=None):
        self.name, self.seed, self.index, self.family = name, seed, index, family

    def __repr__(self):
        return "Symbol(%s, seed=0x%08x)" % (self.name, self.seed)

    def value(self, D):
        return H.hgen(self.seed, D)


class SymbolFamily:
    """n symbols with consecutive seeds base .. base+n-1 so that the device can
    generate member i as HGEN(s_i + base) inside loops."""

    def __init__(self, name, base, n):
        self.name, self.base, self.n = name, base, n
        self.members = [Symbol("%s[%d]" % (name, i), (base + i) & M32, i, self) for i in range(n)]

    def __getitem__(self, i):
        return self.members[i]

    def __len__(self):
        return self.n

    def __iter__(self):
        return iter(self.members)

    def table_bytes(self, D):
        return b"".join(H.to_bytes(m.value(D), D) for m in self.members)


class Buffer:
    """A named region of the SPU data segment (offset resolved at link time)."""

    def __init__(self, name, size, kind="internal", init=None, align=64):
        if init is not None and len(init) > size:
            raise CompileError("buffer %s: init larger than size" % name)
        self.name, self.size, self.kind, self.init, self.align = name, size, kind, init, align
        self.offset = None

    def __repr__(self):
        return "Buffer(%s, size=%d, kind=%s, off=%s)" % (self.name, self.size, self.kind, self.offset)


class Label:
    def __init__(self, name):
        self.name, self.pc = name, None


class _RegPool:
    def __init__(self, regs, what):
        self.free_regs, self.what = list(regs), what
        self.peak = 0
        self.total = len(self.free_regs)

    def alloc(self):
        if not self.free_regs:
            raise CompileError("out of %s registers" % self.what)
        r = self.free_regs.pop(0)
        self.peak = max(self.peak, self.total - len(self.free_regs))
        return r

    def free(self, r):
        if r in self.free_regs:
            raise CompileError("double free of %s register %d" % (self.what, r))
        self.free_regs.append(r)
        self.free_regs.sort()


# ---------------------------------------------------------------------------
# compiled output
# ---------------------------------------------------------------------------
class Binary:
    def __init__(self, cfg, words, buffers, symbols, stats):
        self.cfg, self.words, self.buffers, self.symbols, self.stats = cfg, words, buffers, symbols, stats

    def image(self):
        """List of (offset, bytes) for every buffer with initial content."""
        return [(b.offset, b.init) for b in self.buffers.values() if b.init]

    def image_end(self):
        return max((b.offset + b.size) for b in self.buffers.values()) if self.buffers else 0

    def disassembly(self):
        return disassemble(self.words)

    def manifest(self):
        return {"D": self.cfg.D, "n_instr": len(self.words),
                "buffers": {n: {"offset": b.offset, "size": b.size, "kind": b.kind} for n, b in self.buffers.items()},
                "symbols": {n: s.seed for n, s in self.symbols.items()},
                "stats": self.stats}


# ---------------------------------------------------------------------------
# the program builder
# ---------------------------------------------------------------------------
class Program:
    DATA_BASE = 0x10000     # data segment start (SPU offset); low 64 KB left free for the host

    def __init__(self, cfg: Config = DEFAULT_CONFIG, name="prog", data_base=None):
        self.cfg = cfg
        self.name = name
        self.data_base = self.DATA_BASE if data_base is None else data_base
        self.ins = []                 # list of Instr (imm may be a fixup tuple until link)
        self.fixups = []              # (index, kind, target, addend)
        self.buffers = {}
        self.symbols = {}
        self._next_seed = {}
        self._namespaces = {}
        self.spool = _RegPool(range(1, cfg.NSREG), "scalar")
        self.hpool = _RegPool(range(0, cfg.NHREG), "hypervector")
        self._scopes = [[]]
        self._tie = None
        self._label_id = 0
        self._loops = []

    # ------------------------------------------------------------- symbols
    def namespace(self, name, base=None):
        """Reserve a seed namespace (2^24 seeds) for a symbol group."""
        if name not in self._namespaces:
            if base is None:
                base = (0x1000000 * (len(self._namespaces) + 1)) & M32
            self._namespaces[name] = base
            self._next_seed[name] = base
        return self._namespaces[name]

    def symbol(self, name, ns="sym"):
        key = "%s:%s" % (ns, name)
        if key in self.symbols:
            return self.symbols[key]
        self.namespace(ns)
        seed = self._next_seed[ns]
        self._next_seed[ns] = (seed + 1) & M32
        s = Symbol(key, seed, index=seed - self._namespaces[ns])
        self.symbols[key] = s
        return s

    def symbol_family(self, name, n, ns=None):
        ns = ns or ("fam_" + name)
        base = self.namespace(ns)
        fam = SymbolFamily(name, base, n)
        self._next_seed[ns] = (base + n) & M32
        for m in fam.members:
            self.symbols[m.name] = m
        return fam

    def tie_break(self):
        """Fixed random symbol appended to even-sized bundles."""
        if self._tie is None:
            self._tie = self.symbol("__tie_break__", ns="internal")
        return self._tie

    def hash_seed(self, text):
        return zlib.crc32(text.encode()) & M32

    # ------------------------------------------------------------- buffers
    def buffer(self, name, size, kind="internal", init=None):
        if name in self.buffers:
            raise CompileError("duplicate buffer %s" % name)
        b = Buffer(name, size, kind, init)
        self.buffers[name] = b
        return b

    def hv_table(self, name, hvs, kind="table"):
        """Buffer holding a list of HVs (ints) back to back."""
        data = b"".join(H.to_bytes(x, self.cfg.D) for x in hvs)
        return self.buffer(name, len(data), kind, data)

    def symbol_table(self, name, symbols, kind="table"):
        return self.hv_table(name, [s.value(self.cfg.D) for s in symbols], kind)

    # ---------------------------------------------------------- registers
    @contextmanager
    def scope(self):
        self._scopes.append([])
        try:
            yield
        finally:
            for h in reversed(self._scopes.pop()):
                self._release(h)

    def _release(self, h):
        h.alive = False
        (self.spool if isinstance(h, S) else self.hpool).free(h._r)

    def s(self, init=None, name=""):
        """Allocate a scalar register (optionally initialised)."""
        v = S(self, self.spool.alloc(), name)
        self._scopes[-1].append(v)
        if init is not None:
            self.set(v, init)
        return v

    def hv(self, name=""):
        v = HV(self, self.hpool.alloc(), name)
        self._scopes[-1].append(v)
        return v

    def free(self, v):
        """Explicitly free a handle before its scope ends."""
        for sc in reversed(self._scopes):
            if v in sc:
                sc.remove(v)
                self._release(v)
                return
        raise CompileError("free of unknown register %r" % v)

    # ------------------------------------------------------------ emission
    def _emit(self, op, fn=0, rd=0, ra=0, rb=0, rc=0, imm=0):
        idx = len(self.ins)
        if isinstance(imm, tuple):           # fixup: ("buf", Buffer, addend) | ("label", Label)
            self.fixups.append((idx, imm))
            imm = 0
        self.ins.append(Instr(op, fn, rd, ra, rb, rc, imm))
        return idx

    def label(self, name=None):
        self._label_id += 1
        return Label(name or "L%d" % self._label_id)

    def place(self, label):
        if label.pc is not None:
            raise CompileError("label %s placed twice" % label.name)
        label.pc = len(self.ins)

    def _imm(self, v):
        """Accept int or (Buffer, addend) / Buffer as an immediate."""
        if isinstance(v, Buffer):
            return ("buf", v, 0)
        if isinstance(v, tuple) and isinstance(v[0], Buffer):
            return ("buf", v[0], v[1])
        if isinstance(v, bool):
            return int(v)
        if not isinstance(v, int):
            raise CompileError("immediate expected, got %r" % (v,))
        return v

    # -------------------------------------------------------- scalar ops
    def set(self, dst, val):
        """dst = val (int, S, Expr, Buffer address)."""
        if isinstance(val, (int, Buffer, tuple)):
            self._emit(OP["SOPI"], ALU_FN["ADD"], rd=dst.r, ra=0, imm=self._imm(val))
        elif isinstance(val, S):
            if val.r != dst.r:
                self._emit(OP["SOPI"], ALU_FN["ADD"], rd=dst.r, ra=val.r, imm=0)
        elif isinstance(val, Expr):
            self._eval(val, dst)
        else:
            raise CompileError("cannot assign %r" % (val,))
        return dst

    def _eval(self, e, dst):
        """Lower an Expr tree into dst using temporaries."""
        if isinstance(e, S):
            return self.set(dst, e)
        if isinstance(e, (int, Buffer, tuple)):
            return self.set(dst, e)
        a, b = e.a, e.b
        with self.scope():
            if isinstance(a, Expr):
                ta = self.s()
                self._eval(a, ta)
                a = ta
            if isinstance(b, Expr):
                tb = self.s()
                self._eval(b, tb)
                b = tb
            if isinstance(a, S) and isinstance(b, S):
                self._emit(OP["SOP"], ALU_FN[e.op], rd=dst.r, ra=a.r, rb=b.r)
            elif isinstance(a, S):
                self._emit(OP["SOPI"], ALU_FN[e.op], rd=dst.r, ra=a.r, imm=self._imm(b))
            elif isinstance(b, S):        # imm op S
                if e.op in ("ADD", "MUL", "AND", "OR", "XOR"):
                    self._emit(OP["SOPI"], ALU_FN[e.op], rd=dst.r, ra=b.r, imm=self._imm(a))
                elif e.op == "SUB":       # imm - b
                    self._emit(OP["SOPI"], ALU_FN["SUB"], rd=dst.r, ra=b.r, imm=self._imm(a))  # b - imm
                    self._emit(OP["SOP"], ALU_FN["SUB"], rd=dst.r, ra=0, rb=dst.r)              # negate
                else:
                    t = self.s(a)
                    self._emit(OP["SOP"], ALU_FN[e.op], rd=dst.r, ra=t.r, rb=b.r)
            else:
                t = self.s(a)
                self._emit(OP["SOPI"], ALU_FN[e.op], rd=dst.r, ra=t.r, imm=self._imm(b))
        return dst

    def eval(self, e, name=""):
        """Evaluate an expression into a fresh scalar register."""
        d = self.s(name=name)
        return self._eval(e, d)

    def lw(self, dst, base, off=0):
        self._emit(OP["LW"], rd=dst.r, ra=self._base(base), imm=self._off(base, off))
        return dst

    def lb(self, dst, base, off=0):
        self._emit(OP["LB"], rd=dst.r, ra=self._base(base), imm=self._off(base, off))
        return dst

    def sw(self, src, base, off=0):
        self._emit(OP["SW"], ra=self._base(base), rb=src.r, imm=self._off(base, off))

    def sb(self, src, base, off=0):
        self._emit(OP["SB"], ra=self._base(base), rb=src.r, imm=self._off(base, off))

    def csrr(self, dst, name):
        self._emit(OP["CSRR"], rd=dst.r, imm=CSR[name])
        return dst

    def csrw(self, name, src):
        self._emit(OP["CSRW"], ra=src.r, imm=CSR[name])

    def _base(self, base):
        return base.r if isinstance(base, S) else 0

    def _off(self, base, off):
        """base may be an S (register) or a Buffer (absolute); off an int or Buffer-relative."""
        if isinstance(base, Buffer):
            return ("buf", base, off)
        return self._imm(off)

    def addr(self, buf, index=None, stride=None, offset=0, name=""):
        """Fresh scalar = buf + index*stride + offset (stride defaults to HV_BYTES)."""
        stride = self.cfg.HV_BYTES if stride is None else stride
        d = self.s(name=name or ("&" + buf.name))
        if index is None:
            return self.set(d, (buf, offset))
        if stride & (stride - 1) == 0:
            self._emit(OP["SOPI"], ALU_FN["SHL"], rd=d.r, ra=index.r, imm=stride.bit_length() - 1)
        else:
            self._emit(OP["SOPI"], ALU_FN["MUL"], rd=d.r, ra=index.r, imm=stride)
        self._emit(OP["SOPI"], ALU_FN["ADD"], rd=d.r, ra=d.r, imm=("buf", buf, offset))
        return d

    # ------------------------------------------------------------- HV ops
    def _h(self, x):
        if isinstance(x, HV):
            return x
        raise CompileError("hypervector register expected, got %r" % (x,))

    def _dst(self, dst, name=""):
        return dst if dst is not None else self.hv(name)

    def gen(self, seed, dst=None, name=""):
        """dst = HGEN(seed).  seed: Symbol | int | S | (S, addend) | Expr."""
        dst = self._dst(dst, name)
        if isinstance(seed, Symbol):
            self._emit(OP["HGEN"], rd=dst.r, rc=0, imm=seed.seed - (1 << 32) if seed.seed & 0x80000000 else seed.seed)
        elif isinstance(seed, int):
            self._emit(OP["HGEN"], rd=dst.r, rc=0, imm=seed - (1 << 32) if seed & 0x80000000 else seed & M32)
        elif isinstance(seed, S):
            self._emit(OP["HGEN"], rd=dst.r, rc=seed.r, imm=0)
        elif isinstance(seed, tuple):
            base, add = seed
            self._emit(OP["HGEN"], rd=dst.r, rc=base.r, imm=add - (1 << 32) if add & 0x80000000 else add & M32)
        elif isinstance(seed, Expr):
            with self.scope():
                t = self.eval(seed)
                self._emit(OP["HGEN"], rd=dst.r, rc=t.r, imm=0)
        else:
            raise CompileError("bad seed %r" % (seed,))
        return dst

    def bind(self, a, b, dst=None, name=""):
        dst = self._dst(dst, name)
        self._emit(OP["HXOR"], rd=dst.r, ra=self._h(a).r, rb=self._h(b).r)
        return dst
    xor = bind

    def bind_all(self, hvs, dst=None, name=""):
        hvs = list(hvs)
        if not hvs:
            raise CompileError("bind_all of nothing")
        dst = self._dst(dst, name)
        if len(hvs) == 1:
            return self.mov(hvs[0], dst)
        self.bind(hvs[0], hvs[1], dst)
        for x in hvs[2:]:
            self.bind(dst, x, dst)
        return dst

    def and_(self, a, b, dst=None):
        dst = self._dst(dst)
        self._emit(OP["HAND"], rd=dst.r, ra=a.r, rb=b.r)
        return dst

    def or_(self, a, b, dst=None):
        dst = self._dst(dst)
        self._emit(OP["HOR"], rd=dst.r, ra=a.r, rb=b.r)
        return dst

    def not_(self, a, dst=None):
        dst = self._dst(dst)
        self._emit(OP["HNOT"], rd=dst.r, ra=a.r)
        return dst

    def mov(self, a, dst=None):
        dst = self._dst(dst)
        if dst.r != a.r:
            self._emit(OP["HOR"], rd=dst.r, ra=a.r, rb=a.r)
        return dst

    def permute(self, a, k, dst=None, name=""):
        """dst = rotl(a, k); k int | S | (S, addend)."""
        dst = self._dst(dst, name)
        rc, imm = self._val(k)
        self._emit(OP["HROT"], rd=dst.r, ra=a.r, rc=rc, imm=imm)
        return dst

    def mask(self, n, dst=None, name=""):
        dst = self._dst(dst, name)
        rc, imm = self._val(n)
        self._emit(OP["HMASK"], rd=dst.r, rc=rc, imm=imm)
        return dst

    def _val(self, k):
        if isinstance(k, int):
            return 0, k
        if isinstance(k, S):
            return k.r, 0
        if isinstance(k, tuple):
            return k[0].r, k[1]
        if isinstance(k, Expr):
            t = self.eval(k)     # lives until scope end
            return t.r, 0
        raise CompileError("bad value operand %r" % (k,))

    def load(self, base, off=0, dst=None, name=""):
        """dst = HV at (base + off); base: S | Buffer."""
        dst = self._dst(dst, name)
        self._emit(OP["HLD"], rd=dst.r, ra=self._base(base), imm=self._off(base, off))
        return dst

    def store(self, src, base, off=0):
        self._emit(OP["HST"], rd=src.r, ra=self._base(base), imm=self._off(base, off))

    def load_idx(self, buf, index, dst=None, stride=None, name=""):
        dst = self._dst(dst, name)          # allocate in the caller's scope, not the temporary one
        with self.scope():
            a = self.addr(buf, index, stride)
            return self.load(a, 0, dst)

    def store_idx(self, src, buf, index, stride=None):
        with self.scope():
            a = self.addr(buf, index, stride)
            self.store(src, a, 0)

    def dist(self, a, b, dst=None, name=""):
        dst = dst if dst is not None else self.s(name=name)
        self._emit(OP["HDIST"], rd=dst.r, ra=a.r, rb=b.r)
        return dst

    # ------------------------------------------------------- accumulators
    def acc_clr(self, k):
        self._emit(OP["HACC"], ACC_FN["CLR"], rb=k)

    def acc_add(self, k, a, w=1):
        rc, imm = self._val(w)
        self._emit(OP["HACC"], ACC_FN["ADD"], ra=a.r, rb=k, rc=rc, imm=imm)

    def acc_sub(self, k, a, w=1):
        rc, imm = self._val(w)
        self._emit(OP["HACC"], ACC_FN["SUB"], ra=a.r, rb=k, rc=rc, imm=imm)

    def acc_thr(self, k, t=0, dst=None, name=""):
        dst = self._dst(dst, name)
        rc, imm = self._val(t)
        self._emit(OP["HACC"], ACC_FN["THR"], rd=dst.r, rb=k, rc=rc, imm=imm)
        return dst

    def acc_ld(self, k, base, off=0):
        self._emit(OP["HACC"], ACC_FN["LD"], ra=self._base(base), rb=k, imm=self._off(base, off))

    def acc_st(self, k, base, off=0):
        self._emit(OP["HACC"], ACC_FN["ST"], ra=self._base(base), rb=k, imm=self._off(base, off))

    def bundle(self, hvs, dst=None, acc=None, name="", tie=True):
        """Majority bundle of a list of HV registers via an accumulator.
        Even counts get the program's tie-break symbol appended (tie=True)."""
        hvs = list(hvs)
        k = self.cfg.NACC - 1 if acc is None else acc
        dst = self._dst(dst, name)
        self.acc_clr(k)
        for x in hvs:
            self.acc_add(k, x, 1)
        if tie and len(hvs) % 2 == 0:
            with self.scope():
                t = self.gen(self.tie_break())
                self.acc_add(k, t, 1)
        self.acc_thr(k, 0, dst)
        return dst

    # ------------------------------------------------------------- search
    def search(self, query, base, count, dst=None, wrdist=None, name=""):
        """dst = argmin_i hamming(query, table[i]).  base: Buffer | S, count: int | S.
        wrdist: Buffer | S -> every distance written as u32 to that address.
        Returns the scalar holding the best index; SR_DIST etc. via csrr()."""
        dst = dst if dst is not None else self.s(name=name or "idx")
        with self.scope():
            b = base if isinstance(base, S) else self.addr(base)
            n = count if isinstance(count, S) else self.s(count)
            fn = 0
            if wrdist is not None:
                w = wrdist if isinstance(wrdist, S) else self.addr(wrdist)
                self.csrw("DOUT", w)
                fn = SEARCH_WRDIST
            self._emit(OP["HSEARCH"], fn, rd=dst.r, ra=query.r, rb=b.r, rc=n.r)
        return dst

    def cleanup(self, query, table, count, dst=None, name=""):
        return self.search(query, table, count, dst, None, name or "clean")

    # -------------------------------------------------------- control flow
    def _branch(self, cond, a, b, label):
        if not isinstance(a, S):
            a = self.s(a)
        if not isinstance(b, S):
            b = self.s(b)
        self._emit(OP["BR"], BR_FN[cond], ra=a.r, rb=b.r, imm=("label", label))

    _NEG = {"BEQ": "BNE", "BNE": "BEQ", "BLT": "BGE", "BGE": "BLT", "BLTU": "BGEU", "BGEU": "BLTU"}
    _CMP = {"==": "BEQ", "!=": "BNE", "<": "BLT", ">=": "BGE", "<u": "BLTU", ">=u": "BGEU"}

    def jump(self, label):
        self._emit(OP["JAL"], rd=0, imm=("label", label))

    @contextmanager
    def loop(self, count, start=0, step=1, name="i"):
        """for i in range(start, count, step): ...  (count: int | S; yields the index S).
        Inside the body `break_()` / `continue_()` jump out / to the increment."""
        with self.scope():
            i = self.s(start, name=name)
            n = count if isinstance(count, S) else self.s(count)
            top, nxt, end = self.label("loop"), self.label("next"), self.label("endloop")
            self.place(top)
            with self.scope():
                self._branch("BGE", i, n, end)
            self._loops.append((nxt, end))
            with self.scope():
                yield i
            self._loops.pop()
            self.place(nxt)
            self.set(i, i + step)
            self.jump(top)
            self.place(end)

    @contextmanager
    def while_(self, a, cmp, b):
        """while a <cmp> b: ...  (operands re-evaluated every iteration; break_/continue_ allowed)."""
        if cmp in (">", "<=", ">u", "<=u"):
            a, b, cmp = b, a, {">": "<", "<=": ">=", ">u": "<u", "<=u": ">=u"}[cmp]
        top, end = self.label("while"), self.label("endwhile")
        self.place(top)
        with self.scope():
            self._branch(self._NEG[self._CMP[cmp]], a, b, end)
        self._loops.append((top, end))
        with self.scope():
            yield
        self._loops.pop()
        self.jump(top)
        self.place(end)

    def break_(self):
        if not self._loops:
            raise CompileError("break outside loop")
        self.jump(self._loops[-1][1])

    def continue_(self):
        if not self._loops:
            raise CompileError("continue outside loop")
        self.jump(self._loops[-1][0])

    @contextmanager
    def if_(self, a, cmp, b):
        """if a <cmp> b: ...   cmp in ==, !=, <, >=, <u, >=u  (signed unless u)."""
        if cmp in (">", "<=", ">u", "<=u"):        # swap operands
            a, b, cmp = b, a, {">": "<", "<=": ">=", ">u": "<u", "<=u": ">=u"}[cmp]
        end = self.label("endif")
        with self.scope():
            self._branch(self._NEG[self._CMP[cmp]], a, b, end)
        with self.scope():
            yield
        self.place(end)

    @contextmanager
    def if_else(self, a, cmp, b):
        """with p.if_else(a, "<", b) as (then, els): with then(): ... ; with els(): ..."""
        if cmp in (">", "<=", ">u", "<=u"):
            a, b, cmp = b, a, {">": "<", "<=": ">=", ">u": "<u", "<=u": ">=u"}[cmp]
        els_l, end = self.label("else"), self.label("endif")
        with self.scope():
            self._branch(self._NEG[self._CMP[cmp]], a, b, els_l)

        @contextmanager
        def then():
            with self.scope():
                yield
            self.jump(end)
            self.place(els_l)

        @contextmanager
        def els():
            with self.scope():
                yield
            self.place(end)
        yield then, els

    def halt(self):
        self._emit(OP["HALT"])

    def nop(self):
        self._emit(OP["NOP"])

    # ------------------------------------------------------------ linking
    def compile(self):
        """Lay out buffers, resolve fixups, return a Binary."""
        if not self.ins or self.ins[-1].op != OP["HALT"]:
            self.halt()
        # data layout
        off = self.data_base
        for b in self.buffers.values():
            off = (off + b.align - 1) // b.align * b.align
            b.offset = off
            off += b.size
        words = []
        for idx, (fidx, fx) in enumerate(self.fixups):
            ins = self.ins[fidx]
            if fx[0] == "buf":
                v = (fx[1].offset + fx[2]) & M32
            elif fx[0] == "label":
                if fx[1].pc is None:
                    raise CompileError("label %s never placed" % fx[1].name)
                v = fx[1].pc - fidx
            else:
                raise CompileError("bad fixup")
            ins.imm = v - (1 << 32) if v & 0x80000000 and v >= 0 else v
        if len(self.ins) > self.cfg.PROG_DEPTH:
            raise CompileError("program too large: %d instructions" % len(self.ins))
        for ins in self.ins:
            words.append(ins.encode())
        stats = {"n_instr": len(words), "data_bytes": off - self.data_base,
                 "peak_sregs": self.spool.peak, "peak_hregs": self.hpool.peak}
        return Binary(self.cfg, words, dict(self.buffers), dict(self.symbols), stats)
