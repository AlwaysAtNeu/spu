"""Keep the example scripts runnable (simulator only)."""
import os, subprocess, sys
HERE = os.path.dirname(__file__)
EX = os.path.join(HERE, "..", "examples")


def _run(name, *args):
    r = subprocess.run([sys.executable, os.path.join(EX, name)] + list(args), capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_analogy():
    out = _run("01_analogy.py")
    assert "dollar of Mexico  -> PESO" in out and "capital of USA    -> WASHINGTON" in out


def test_hdc_example():
    out = _run("02_hdc_classify.py", "sim", "ngram")
    assert "bit-exact" in out


def test_kb_example():
    out = _run("03_kb_rules.py", "sim")
    assert "matches host Datalog evaluator: True" in out


def test_benchmark():
    out = _run("04_benchmark.py", "sim", "1")
    assert "cyc/unit" in out


def test_resonator():
    out = _run("05_resonator.py", "sim", "12", "8")
    assert "(correct)" in out
