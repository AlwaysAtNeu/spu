import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from spuc.isa import DEFAULT_CONFIG as cfg
from spuc.runtime import SimDevice, Session
from spuc.frontends.hdc import HDCModel, synthetic_dataset, synthetic_sequences
from spuc import hv as H

D = cfg.D


def _roundtrip(model, Xtr, ytr, Xte, yte, epochs):
    protos_ref, acc_ref = model.train_ref(Xtr, ytr, epochs)
    b = model.compile_train(len(Xtr), epochs=epochs)
    s = Session(SimDevice(), b)
    s.set_input("X", model.pack_X(Xtr))
    s.set_input("Y", model.pack_y(ytr))
    s.run()
    protos = model.unpack_protos(s.get_output("PROTO"), model.n_classes, D)
    assert protos == protos_ref, "device prototypes differ from the reference"
    acc = np.frombuffer(s.get_output("ACC"), dtype="<i2").reshape(model.n_classes, D)
    assert np.array_equal(acc, acc_ref)
    bi = model.compile_infer(len(Xte))
    si = Session(SimDevice(), bi)
    si.set_input("X", model.pack_X(Xte))
    si.set_input("PROTO", s.get_output("PROTO"))
    si.run()
    pred = np.array(si.get_u32s("PRED"))
    assert np.array_equal(pred, model.predict_ref(Xte, protos))
    acc_te = float((pred == yte).mean())
    return acc_te, b, bi


def test_record_encoding_train_infer():
    Xtr, ytr, Xte, yte = synthetic_dataset(60, 30, n_classes=4, n_features=12, n_levels=8, seed=1)
    m = HDCModel(n_classes=4, n_features=12, n_levels=8)
    acc, b, bi = _roundtrip(m, Xtr, ytr, Xte, yte, epochs=2)
    assert acc > 0.8, acc
    assert b.stats["n_instr"] < 200


def test_ngram_encoding_train_infer():
    Xtr, ytr, Xte, yte = synthetic_sequences(40, 20, n_classes=3, seq_len=24, seed=2)
    m = HDCModel(n_classes=3, encoding="ngram", ngram=3, seq_len=24)
    acc, b, bi = _roundtrip(m, Xtr, ytr, Xte, yte, epochs=1)
    assert acc > 0.7, acc


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
