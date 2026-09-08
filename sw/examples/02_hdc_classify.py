"""Train and run an HDC classifier entirely on the SPU (training loop, retraining
epochs, prototype binarisation and nearest-prototype inference are all SPU code).

usage: python3 02_hdc_classify.py [sim|rtl] [record|ngram]
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from spuc.runtime import get_device, Session
from spuc.frontends.hdc import HDCModel, synthetic_dataset, synthetic_sequences

dev_name = sys.argv[1] if len(sys.argv) > 1 else "sim"
enc = sys.argv[2] if len(sys.argv) > 2 else "record"
if enc == "record":
    n_classes, n_features, n_levels = 6, 24, 16
    Xtr, ytr, Xte, yte = synthetic_dataset(240, 120, n_classes, n_features, n_levels, noise=0.9, seed=3)
    model = HDCModel(n_classes=n_classes, n_features=n_features, n_levels=n_levels)
else:
    n_classes, seq_len = 4, 48
    Xtr, ytr, Xte, yte = synthetic_sequences(160, 80, n_classes, seq_len, seed=4)
    model = HDCModel(n_classes=n_classes, encoding="ngram", ngram=3, seq_len=seq_len)

epochs = 3
btrain = model.compile_train(len(Xtr), epochs=epochs)
print("train program: %d instructions, data %d bytes" % (btrain.stats["n_instr"], btrain.stats["data_bytes"]))
s = Session(get_device(dev_name), btrain)
s.set_input("X", model.pack_X(Xtr))
s.set_input("Y", model.pack_y(ytr))
s.run()
rep = s.report()
print("train:", rep)
protos_dev = s.get_output("PROTO")

binfer = model.compile_infer(len(Xte))
si = Session(get_device(dev_name), binfer)
si.set_input("X", model.pack_X(Xte))
si.set_input("PROTO", protos_dev)
si.run()
pred = np.array(si.get_u32s("PRED"))
print("infer:", si.report())
print("test accuracy: %.3f  (%d samples, %d classes, %s encoding, %d epochs)" % ((pred == yte).mean(), len(yte), n_classes, enc, epochs))

# cross-check against the bit-exact host reference
protos_ref, _ = model.train_ref(Xtr, ytr, epochs)
assert model.unpack_protos(protos_dev, n_classes, model.D) == protos_ref
assert np.array_equal(pred, model.predict_ref(Xte, protos_ref))
print("device results are bit-exact with the host reference")
