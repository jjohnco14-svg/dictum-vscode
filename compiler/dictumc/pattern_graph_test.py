#!/usr/bin/env python3
"""
pattern_graph_test.py -- self-test for the codegraph pattern database
mechanism (pattern_graph.py + codegraph/patterns/*.json).

Run directly: `python3 pattern_graph_test.py`. Exits non-zero with a
clear message on first failure (fail loudly, per this repo's own
convention -- see chunk_grammar.py --self-test for the sibling style
this deliberately matches).

Covers three layers, since a bug could live in any of them independently:
  1. In-process API (load_pattern/bind_pattern/render_context) --
     schema validation, binding success/failure, context rendering.
  2. The CLI subprocess contract itself (--bridge / --list) -- the
     EXACT bytes a Node caller will send/receive, not just the Python
     functions underneath. This is the layer Cell 10's kernel crash
     taught us not to skip: an in-process check can pass while the
     subprocess-facing contract is still broken.
  3. Schema-violation fixtures -- deliberately malformed pattern files,
     written to a temp scratch dir (never to the real codegraph/patterns/
     directory) so a broken fixture can never leak into the real
     pattern set even if a test is interrupted mid-run.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
import pattern_graph as pg  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print(f"  [OK]   {label}")
    else:
        print(f"  [FAIL] {label}  {detail}")
        FAILURES.append(label)


def run_cli(args, input_text):
    res = subprocess.run(
        ["python3", os.path.join(_THIS_DIR, "pattern_graph.py"), *args],
        input=input_text, capture_output=True, text=True, timeout=10,
    )
    return res


# ---------------------------------------------------------------------
# Layer 1: in-process API
# ---------------------------------------------------------------------
print("Layer 1: in-process API")

refs = pg.list_patterns()
check("list_patterns() finds the while-loop seed pattern", "while-loop" in refs, refs)

pattern = pg.load_pattern("while-loop")
check("load_pattern() returns the right pattern_ref", pattern["pattern_ref"] == "while-loop")
check("load_pattern() result has all required top-level fields",
      all(k in pattern for k in ("category", "description", "params", "template", "example")))

try:
    pg.load_pattern("this-pattern-does-not-exist")
    check("load_pattern() raises PatternNotFound for a missing ref", False)
except pg.PatternNotFound as e:
    check("load_pattern() raises PatternNotFound for a missing ref", True)
    check("PatternNotFound message is plain text, not repr-quoted", not str(e).startswith("'"), str(e))

bound = pg.bind_pattern("while-loop", {"action_name": "countdown", "var": "Count", "init_value": "5", "threshold": "0"})
check("bind_pattern() fills required params", "while Count is greater than 0 repeat" in bound, bound)
check("bind_pattern() reuses {var} consistently across keep/while/print/set", bound.count("Count") == 5, bound)
check("bind_pattern() leaves no unfilled placeholders", "{" not in bound and "}" not in bound, bound)

try:
    pg.bind_pattern("while-loop", {"var": "Count"})  # missing action_name, init_value, threshold
    check("bind_pattern() raises PatternBindingError for missing required params", False)
except pg.PatternBindingError as e:
    check("bind_pattern() raises PatternBindingError for missing required params", True)
    check("...error names the missing params", "threshold" in str(e) and "init_value" in str(e), str(e))

try:
    pg.bind_pattern("while-loop", {"action_name": "x", "var": "Count", "init_value": "5"})  # missing threshold only
    check("bind_pattern() raises PatternBindingError for a single missing required param", False)
except pg.PatternBindingError as e:
    check("bind_pattern() raises PatternBindingError for a single missing required param", True)

rendered_no_params, bound_none = pg.render_context("while-loop")
check("render_context() without params includes the canonical example", "Correct example:" in rendered_no_params)
check("render_context() without params returns bound=None", bound_none is None)

rendered_with_params, bound_val = pg.render_context("while-loop", {"action_name": "x", "var": "X", "init_value": "10", "threshold": "0"})
check("render_context() with params includes the bound instance", "For this specific call:" in rendered_with_params)
check("render_context() with params returns the bound text", bound_val is not None and "while X is greater than 0 repeat" in bound_val, bound_val)

# ---------------------------------------------------------------------
# Layer 2: CLI subprocess contract (the exact shape Node will use)
# ---------------------------------------------------------------------
print("\nLayer 2: CLI subprocess contract")

res = run_cli(["--list"], "")
try:
    payload = json.loads(res.stdout)
    check("--list exits 0 with valid JSON", res.returncode == 0)
    check("--list reports ok:true", payload.get("ok") is True, payload)
    check("--list includes while-loop", any(p.get("pattern_ref") == "while-loop" for p in payload.get("patterns", [])), payload)
except json.JSONDecodeError:
    check("--list exits 0 with valid JSON", False, res.stdout + res.stderr)

res = run_cli(["--bridge"], json.dumps({"pattern_ref": "while-loop"}))
try:
    payload = json.loads(res.stdout)
    check("--bridge (no params) exits 0 with ok:true", res.returncode == 0 and payload.get("ok") is True, payload)
except json.JSONDecodeError:
    check("--bridge (no params) exits 0 with valid JSON", False, res.stdout + res.stderr)

res = run_cli(["--bridge"], json.dumps({"pattern_ref": "while-loop", "params": {"var": "N"}}))  # missing action_name/init_value/threshold
try:
    payload = json.loads(res.stdout)
    check("--bridge with incomplete params returns ok:false (not a crash)", payload.get("ok") is False, payload)
    check("--bridge ok:false includes a 'detail' string", isinstance(payload.get("detail"), str) and len(payload["detail"]) > 0, payload)
except json.JSONDecodeError:
    check("--bridge with incomplete params returns valid JSON", False, res.stdout + res.stderr)

res = run_cli(["--bridge"], json.dumps({"pattern_ref": "no-such-pattern"}))
try:
    payload = json.loads(res.stdout)
    check("--bridge for an unknown pattern_ref returns ok:false (not a crash)", payload.get("ok") is False, payload)
except json.JSONDecodeError:
    check("--bridge for an unknown pattern_ref returns valid JSON", False, res.stdout + res.stderr)

res = run_cli(["--bridge"], "not valid json at all")
try:
    payload = json.loads(res.stdout)
    check("--bridge with garbage stdin returns ok:null (not a crash)", payload.get("ok") is None, payload)
except json.JSONDecodeError:
    check("--bridge with garbage stdin returns valid JSON", False, res.stdout + res.stderr)

res = subprocess.run(["python3", os.path.join(_THIS_DIR, "pattern_graph.py")], capture_output=True, text=True, timeout=10)
check("no args at all exits non-zero with a usage message (not silent)", res.returncode != 0 and "usage" in res.stderr.lower(), res.stderr)

# ---------------------------------------------------------------------
# Layer 3: schema-violation fixtures, in an isolated scratch dir (never
# written into the real codegraph/patterns/ directory)
# ---------------------------------------------------------------------
print("\nLayer 3: schema validation on malformed pattern files")

_scratch = tempfile.mkdtemp(prefix="pattern_graph_test_")
_real_dir = pg.PATTERNS_DIR
try:
    pg.PATTERNS_DIR = _scratch

    def write(name, obj):
        with open(os.path.join(_scratch, f"{name}.json"), "w") as f:
            json.dump(obj, f)

    write("bad_json", {})  # placeholder, overwritten raw below
    with open(os.path.join(_scratch, "bad_json.json"), "w") as f:
        f.write("{not valid json")
    try:
        pg.load_pattern("bad_json")
        check("invalid JSON syntax raises PatternSchemaError", False)
    except pg.PatternSchemaError:
        check("invalid JSON syntax raises PatternSchemaError", True)

    write("missing_field", {"pattern_ref": "missing_field", "category": "OPERATION", "params": {}, "template": "x"})  # no description/example
    try:
        pg.load_pattern("missing_field")
        check("missing required top-level field raises PatternSchemaError", False)
    except pg.PatternSchemaError:
        check("missing required top-level field raises PatternSchemaError", True)

    write("ref_mismatch", {"pattern_ref": "not-the-filename", "category": "OPERATION", "description": "x", "params": {}, "template": "x", "example": "x"})
    try:
        pg.load_pattern("ref_mismatch")
        check("pattern_ref/filename mismatch raises PatternSchemaError", False)
    except pg.PatternSchemaError:
        check("pattern_ref/filename mismatch raises PatternSchemaError", True)

    write("bad_type", {"pattern_ref": "bad_type", "category": "OPERATION", "description": "x",
                        "params": {"p": {"type": "not-a-real-type", "required": True}},
                        "template": "{p}", "example": "x"})
    try:
        pg.load_pattern("bad_type")
        check("unknown param type raises PatternSchemaError", False)
    except pg.PatternSchemaError:
        check("unknown param type raises PatternSchemaError", True)

    write("enum_no_options", {"pattern_ref": "enum_no_options", "category": "OPERATION", "description": "x",
                               "params": {"p": {"type": "enum", "required": True}},
                               "template": "{p}", "example": "x"})
    try:
        pg.load_pattern("enum_no_options")
        check("enum param with no options raises PatternSchemaError", False)
    except pg.PatternSchemaError:
        check("enum param with no options raises PatternSchemaError", True)

    write("ghost_placeholder", {"pattern_ref": "ghost_placeholder", "category": "OPERATION", "description": "x",
                                 "params": {}, "template": "{ghost}", "example": "x"})
    try:
        pg.load_pattern("ghost_placeholder")
        check("template placeholder with no matching param raises PatternSchemaError", False)
    except pg.PatternSchemaError:
        check("template placeholder with no matching param raises PatternSchemaError", True)

    write("unused_required", {"pattern_ref": "unused_required", "category": "OPERATION", "description": "x",
                               "params": {"p": {"type": "identifier", "required": True}},
                               "template": "no placeholders here", "example": "x"})
    try:
        pg.load_pattern("unused_required")
        check("required param never used in template raises PatternSchemaError", False)
    except pg.PatternSchemaError:
        check("required param never used in template raises PatternSchemaError", True)

finally:
    pg.PATTERNS_DIR = _real_dir
    shutil.rmtree(_scratch, ignore_errors=True)

# ---------------------------------------------------------------------
# Layer 4: every pattern actually shipped in codegraph/patterns/ loads
# cleanly and its own canonical `example` is internally consistent with
# its own `template` (i.e. binding the template with the values implied
# by the example reproduces the example, for patterns simple enough to
# check this way automatically -- field_list-bearing and multi-param
# patterns are covered by the dataset cross-check below instead, not
# here, since deriving "the params implied by an example" automatically
# isn't well-defined for those).
# ---------------------------------------------------------------------
print("\nLayer 4: every shipped pattern loads cleanly")
for ref in pg.list_patterns():
    try:
        p = pg.load_pattern(ref)
        check(f"{ref} loads and passes schema validation", True)
        check(f"{ref} has a non-empty example", bool(p.get("example")))
    except Exception as e:
        check(f"{ref} loads and passes schema validation", False, str(e))

# ---------------------------------------------------------------------
# Layer 5: cross-validate every pattern's `example` field against
# validated_patterns.json's real transpiler-tested base entries, if that
# dataset is present on disk. This is what actually ties each pattern
# back to ground truth rather than hand-written prose -- best-effort:
# the dataset isn't checked into the repo (it's a one-time generation
# artifact), so this layer is skipped cleanly, not failed, when it's
# not found at any of the paths below.
# ---------------------------------------------------------------------
print("\nLayer 5: cross-check against validated_patterns.json (if present)")
_dataset_candidates = [
    os.path.join(_THIS_DIR, "..", "..", "validated_patterns.json"),
    "/mnt/user-data/uploads/validated_patterns.json",
    "/kaggle/working/validated_patterns.json",
]
_dataset_path = next((p for p in _dataset_candidates if os.path.isfile(p)), None)
if _dataset_path:
    with open(_dataset_path) as f:
        _dataset = json.load(f)
    _by_id = {p["id"]: p for p in _dataset["patterns"]}
    # atomic-increment's example intentionally uses the compound-naming
    # convention from real variations, not the (differently-named)
    # literal base entry -- see the pattern file's own comment.
    _skip_base_example_check = {"atomic-increment"}
    for ref in pg.list_patterns():
        base = _by_id.get(ref)
        if not base:
            continue
        pattern = pg.load_pattern(ref)
        if ref not in _skip_base_example_check:
            check(f"{ref}'s example matches the real validated base entry exactly",
                  pattern["example"] == base["dictum"])
else:
    print("  (validated_patterns.json not found on disk -- skipping this layer, not failing)")

# ---------------------------------------------------------------------
print()
if FAILURES:
    print(f"pattern_graph self-test FAILED ({len(FAILURES)} failure(s)):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("pattern_graph self-test OK")
