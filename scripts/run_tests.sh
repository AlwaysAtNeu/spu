#!/bin/sh
# Run every software test (golden model, compiler, frontends) and, if verilator
# is installed, the RTL co-simulation.
set -e
cd "$(dirname "$0")/../sw"
if python3 -c "import pytest" 2>/dev/null; then
  python3 -m pytest -q tests "$@"
else
  for t in tests/test_*.py; do echo "== $t"; python3 "$t"; done
fi
