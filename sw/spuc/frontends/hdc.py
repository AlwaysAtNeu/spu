"""HDC / VSA classification frontend.

A classifier is a set of class prototypes (hypervectors).  Samples are encoded
into hypervectors either with the record based encoding

    h(x) = maj_f ( ID_f  (x)  L(x_f) )          (feature id bound to a level HV)

or, for symbol sequences, with the n-gram encoding

    h(x) = maj_t ( rho^{n-1}(S[x_t]) (x) ... (x) rho^0(S[x_{t+n-1}]) )

Training = bundle the encodings of every sample of a class into an integer
accumulator, binarise -> prototype.  Optional retraining epochs apply the usual
perceptron-style update (add to the true class, subtract from the mispredicted
class) whenever the nearest prototype is wrong.  Inference = nearest prototype
by Hamming distance.

Everything runs on the SPU (encoding loops, accumulators, searches, control
flow); the host only quantises the data and reads the results.  `*_ref` methods
are bit-exact host reimplementations used for verification.
"""
import numpy as np
from ..isa import Config, DEFAULT_CONFIG
from ..dsl import Program, CompileError
from .. import hv as H


def quantize(X, n_levels, lo=None, hi=None):
    """Map real features to integer levels 0..n_levels-1 (per-feature min/max)."""
    X = np.asarray(X, dtype=np.float64)
    lo = X.min(axis=0) if lo is None else lo
    hi = X.max(axis=0) if hi is None else hi
    span = np.where(hi > lo, hi - lo, 1.0)
    q = np.floor((X - lo) / span * n_levels)
    return np.clip(q, 0, n_levels - 1).astype(np.uint8), lo, hi


class HDCModel:
    def __init__(self, n_classes, n_features=None, n_levels=16, encoding="record", ngram=3,
                 seq_len=None, cfg: Config = DEFAULT_CONFIG):
        self.cfg = cfg
        self.D = cfg.D
        self.n_classes = n_classes
        self.encoding = encoding
        self.n_levels = n_levels
        self.ngram = ngram
        if encoding == "record":
            assert n_features is not None
            self.n_features = n_features
            self.n_elems = n_features                     # elements bundled per sample
        elif encoding == "ngram":
            assert seq_len is not None and seq_len >= ngram
            self.seq_len = seq_len
            self.n_features = seq_len                     # bytes per sample
            self.n_elems = seq_len - ngram + 1
        else:
            raise ValueError("encoding must be 'record' or 'ngram'")
        if n_classes > cfg.NACC - 1:
            raise CompileError("n_classes must be <= NACC-1 = %d (accumulator %d is the encoder)" % (cfg.NACC - 1, cfg.NACC - 1))
        self.level_step = self.D // (2 * max(1, n_levels - 1))
        # symbol seeds are fixed by a throw-away Program so host and device agree
        self._p = self._symbols(Program(cfg, "hdc_symbols"))

    # ------------------------------------------------------------ symbols
    def _symbols(self, p):
        p.namespace("sym")
        p.namespace("internal")
        p.tie_break()
        if self.encoding == "record":
            self.feat = p.symbol_family("feat", self.n_features)
            self.level0 = p.symbol("level0")
        else:
            self.byte = p.symbol_family("byte", 256)
        return p

    # ---------------------------------------------------- host reference
    def encode_ref(self, x):
        D = self.D
        x = np.asarray(x, dtype=np.int64)
        if self.encoding == "record":
            L0 = self.level0.value(D)
            elems = [self.feat[f].value(D) ^ L0 ^ H.hmask(int(x[f]) * self.level_step, D) for f in range(self.n_features)]
        else:
            syms = [self.byte[int(b)].value(D) for b in x]
            elems = []
            for t in range(self.n_elems):
                g = 0
                for j in range(self.ngram):
                    g ^= H.rotl(syms[t + j], self.ngram - 1 - j, D)
                elems.append(g)
        return H.bundle(elems, D, tie_break=self._p.tie_break().value(D))

    def train_ref(self, X, y, epochs=1):
        """Bit-exact model of the device training program.  Returns (prototypes, accumulators)."""
        D = self.D
        X, y = np.asarray(X), np.asarray(y, dtype=np.int64)
        enc = [self.encode_ref(x) for x in X]
        acc = np.zeros((self.n_classes, D), dtype=np.int16)
        for h, c in zip(enc, y):
            acc[c] = H.sat16(acc[c].astype(np.int32) + H.to_pm1(h, D))
        protos = [H.from_bits(acc[c] > 0) for c in range(self.n_classes)]
        for _ in range(epochs - 1):
            for h, c in zip(enc, y):
                pred = int(np.argmin([H.hamming(h, pr) for pr in protos]))
                if pred != c:
                    pm = H.to_pm1(h, D)
                    acc[c] = H.sat16(acc[c].astype(np.int32) + pm)
                    acc[pred] = H.sat16(acc[pred].astype(np.int32) - pm)
            protos = [H.from_bits(acc[c] > 0) for c in range(self.n_classes)]
        return protos, acc

    def predict_ref(self, X, protos):
        out = []
        for x in np.asarray(X):
            h = self.encode_ref(x)
            out.append(int(np.argmin([H.hamming(h, pr) for pr in protos])))
        return np.array(out)

    # ------------------------------------------------------ device code
    def _emit_encode(self, p, X, i, dst):
        """Encode sample i (row of the byte buffer X) into HV register dst."""
        cfg, D = self.cfg, self.D
        k_enc = cfg.NACC - 1
        p.acc_clr(k_enc)
        with p.scope():
            row = p.addr(X, i, stride=self.n_features, name="row")
            if self.encoding == "record":
                l0 = p.gen(self.level0, name="L0")
                with p.loop(self.n_features, name="f") as f:
                    v = p.lb(p.s(name="v"), p.eval(row + f))
                    idf = p.gen((f, self.feat.base), name="ID_f")
                    m = p.mask(v * self.level_step, name="mask")
                    e = p.bind(idf, l0)
                    p.bind(e, m, e)
                    p.acc_add(k_enc, e, 1)
            else:
                n = self.ngram
                with p.loop(self.n_elems, name="t") as t:
                    pos = p.eval(row + t, name="pos")
                    g = p.hv("gram")
                    for j in range(n):
                        b = p.lb(p.s(name="b"), pos, j)
                        s = p.gen((b, self.byte.base), name="S")
                        r = p.permute(s, n - 1 - j) if n - 1 - j else s
                        if j == 0:
                            p.mov(r, g)
                        else:
                            p.bind(g, r, g)
                        p.free(s)
                        if r is not s:
                            p.free(r)
                        p.free(b)
                    p.acc_add(k_enc, g, 1)
            if self.n_elems % 2 == 0:
                t = p.gen(p.tie_break(), name="tie")
                p.acc_add(k_enc, t, 1)
        p.acc_thr(k_enc, 0, dst)
        return dst

    def _emit_switch_acc(self, p, sel, fn):
        """for k in classes: if sel == k: acc op k   (accumulator index is an instruction field)."""
        for k in range(self.n_classes):
            with p.if_(sel, "==", k):
                fn(k)

    def _emit_binarize(self, p, proto):
        for k in range(self.n_classes):
            with p.scope():
                h = p.acc_thr(k, 0, name="proto")
                p.store(h, proto, k * self.cfg.HV_BYTES)

    def compile_train(self, n_samples, epochs=1, name="hdc_train"):
        """Inputs: X (n_samples * n_features bytes), Y (n_samples u32).
        Outputs: PROTO (n_classes HVs), ACC (raw int16 accumulators, n_classes * D * 2 bytes)."""
        cfg = self.cfg
        p = self._symbols(Program(cfg, name))
        X = p.buffer("X", n_samples * self.n_features, kind="input")
        Y = p.buffer("Y", 4 * n_samples, kind="input")
        proto = p.buffer("PROTO", self.n_classes * cfg.HV_BYTES, kind="output")
        accb = p.buffer("ACC", self.n_classes * self.D * 2, kind="output")
        with p.scope():
            for k in range(self.n_classes):
                p.acc_clr(k)
            with p.loop(n_samples, name="i") as i:
                h = p.hv("h")
                self._emit_encode(p, X, i, h)
                y = p.lw(p.s(name="y"), p.addr(Y, i, stride=4))
                self._emit_switch_acc(p, y, lambda k: p.acc_add(k, h, 1))
            self._emit_binarize(p, proto)
            for _ in range(epochs - 1):
                with p.loop(n_samples, name="i") as i:
                    h = p.hv("h")
                    self._emit_encode(p, X, i, h)
                    pred = p.search(h, proto, self.n_classes, name="pred")
                    y = p.lw(p.s(name="y"), p.addr(Y, i, stride=4))
                    with p.if_(pred, "!=", y):
                        self._emit_switch_acc(p, y, lambda k: p.acc_add(k, h, 1))
                        self._emit_switch_acc(p, pred, lambda k: p.acc_sub(k, h, 1))
                self._emit_binarize(p, proto)
            for k in range(self.n_classes):
                p.acc_st(k, accb, k * self.D * 2)
        p.halt()
        return p.compile()

    def compile_infer(self, n_samples, name="hdc_infer", with_dist=True):
        """Inputs: X, PROTO.  Outputs: PRED (u32 per sample), DIST (u32 best distance per sample)."""
        cfg = self.cfg
        p = self._symbols(Program(cfg, name))
        X = p.buffer("X", n_samples * self.n_features, kind="input")
        proto = p.buffer("PROTO", self.n_classes * cfg.HV_BYTES, kind="input")
        pred_b = p.buffer("PRED", 4 * n_samples, kind="output")
        dist_b = p.buffer("DIST", 4 * n_samples, kind="output") if with_dist else None
        with p.scope():
            with p.loop(n_samples, name="i") as i:
                h = p.hv("h")
                self._emit_encode(p, X, i, h)
                pred = p.search(h, proto, self.n_classes, name="pred")
                p.sw(pred, p.addr(pred_b, i, stride=4))
                if with_dist:
                    d = p.csrr(p.s(name="d"), "SR_DIST")
                    p.sw(d, p.addr(dist_b, i, stride=4))
        p.halt()
        return p.compile()

    # --------------------------------------------------------- helpers
    def pack_X(self, X):
        X = np.asarray(X, dtype=np.uint8)
        assert X.ndim == 2 and X.shape[1] == self.n_features
        return X.tobytes()

    @staticmethod
    def pack_y(y):
        return np.asarray(y, dtype="<u4").tobytes()

    @staticmethod
    def unpack_protos(data, n_classes, D):
        hb = D // 8
        return [H.from_bytes(data[k * hb:(k + 1) * hb]) for k in range(n_classes)]


def synthetic_dataset(n_train, n_test, n_classes, n_features, n_levels, noise=0.6, seed=0):
    """Gaussian blobs, quantised to levels."""
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(n_classes, n_features))
    def make(n):
        y = rng.integers(0, n_classes, size=n)
        X = centers[y] + noise * rng.normal(size=(n, n_features))
        return X, y
    Xtr, ytr = make(n_train)
    Xte, yte = make(n_test)
    Xq, lo, hi = quantize(Xtr, n_levels)
    Xqt, _, _ = quantize(Xte, n_levels, lo, hi)
    return Xq, ytr, Xqt, yte


def synthetic_sequences(n_train, n_test, n_classes, seq_len, alphabet=8, seed=0):
    """Each class is a random first-order Markov chain over `alphabet` symbols."""
    rng = np.random.default_rng(seed)
    T = rng.dirichlet(np.full(alphabet, 0.3), size=(n_classes, alphabet))
    def make(n):
        y = rng.integers(0, n_classes, size=n)
        X = np.zeros((n, seq_len), dtype=np.uint8)
        for i in range(n):
            s = rng.integers(alphabet)
            for t in range(seq_len):
                X[i, t] = s
                s = rng.choice(alphabet, p=T[y[i], s])
        return X, y
    Xtr, ytr = make(n_train)
    Xte, yte = make(n_test)
    return Xtr, ytr, Xte, yte
