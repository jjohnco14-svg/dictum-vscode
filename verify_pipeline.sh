#!/usr/bin/env bash
# verify_pipeline.sh — the ONLY thing allowed to declare a bug "fixed".
# Run from the root of your real dev repo (the one with src/, foundation_test.py, etc),
# NOT from an unpacked .vsix. Exits non-zero and prints WHAT failed on any failure.
# Roo Code's dictum-qa-loop mode is instructed to paste this script's tail output
# as proof before claiming anything is done — no proof, no "fixed" claim accepted.

set -uo pipefail
FAIL=0
step() { echo; echo "== $1 =="; }
run() { "$@"; if [ $? -ne 0 ]; then echo "FAILED: $*"; FAIL=1; fi; }

step "1/8 TypeScript compile"
run npm run compile

step "2/8 Package (sanity — does it even build a vsix)"
run npx vsce package --out /tmp/dictum-gate-check.vsix

step "3/8 Python compiler self-test"
run python3 compiler/run_selftest.py

step "4/8 Foundation test"
run python3 foundation_test.py

step "5/8 Architecture invariants (type_registry sync, emit_c/emit_cpp parity)"
run python3 architecture_test.py

step "6/8 GBNF grammar check"
run python3 gbnf_check.py

step "7/8 Extensions test"
run python3 extensions_test.py

step "8/8 E2E + vibecoding simulation"
run node e2e_test.js
run node simulate_vibecoding.js

echo
if [ $FAIL -eq 0 ]; then
  echo "ALL GREEN — every gate passed. Safe to claim fixed."
  exit 0
else
  echo "GATE FAILED — do NOT claim this is fixed. See failures above."
  exit 1
fi
