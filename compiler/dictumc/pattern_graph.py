"""
pattern_graph.py -- storage + lookup + binding for the codegraph pattern
database (see codegraph/PATTERN_SCHEMA.md for the full field contract).

WHY THIS EXISTS: Cell 9/10/11 established that GBNF grammar constraints
restrict *shape* but can't teach a model Dictum's *content* -- a model
that has never seen Dictum in training fills an open slot with whatever
Python/C/JS habit is nearest (lowercasing a variable name, inventing
f-string interpolation, swapping unsafe-token param order, ...). This
module's job is retrieval: given a `pattern_ref` (and optionally params
to bind), return a ready-to-inject block of few-shot context -- a
correct, concrete Dictum example plus its stated preconditions and known
failure modes -- for the Build prompt to include. It does NOT touch
chunk_grammar.py's GBNF generation; wiring a pattern's shape into the
grammar itself is a separate, later integration (see PATTERN_SCHEMA.md's
"Deliberately NOT done yet" section).

STORAGE: one JSON file per pattern in codegraph/patterns/<pattern_ref>.json
-- the same filesystem-as-database convention already used by
skills/library/<name>/*.dict and graphify-out/cache/ast/*.json elsewhere
in this repo, not a new storage dependency.

CLI CONTRACT (mirrors normalize_dictum.py --bridge exactly, so
out/patternGraph.js can use the identical spawn/stdin/stdout bridge shape
as out/normalizeDictum.js):
    python3 pattern_graph.py --bridge < payload.json
        payload: {"pattern_ref": str, "params": {...} | omitted}
        stdout:  {"ok": true, "rendered": str, "bound": str|null}
              or {"ok": false, "detail": str}   -- pattern not found, or
                                                     binding failed (missing/
                                                     invalid params) -- a
                                                     confident refusal, not
                                                     a bridge failure
              or {"ok": null, "error": str}      -- unexpected bug
    python3 pattern_graph.py --list
        stdout:  {"ok": true, "patterns": [{"pattern_ref":..., "category":...,
                   "description":...}, ...]}
"""

import json
import os
import re

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PATTERNS_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", "codegraph", "patterns"))

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class PatternNotFound(KeyError):
    def __str__(self):
        # KeyError's default __str__ reprs its args (extra quoting) --
        # override so the --bridge "detail" field reads as plain text.
        return f"no such pattern: {self.args[0]!r} (looked in {PATTERNS_DIR})"


class PatternSchemaError(ValueError):
    """The pattern FILE itself is malformed -- a repo/data bug, not a
    caller-input bug. Kept distinct from PatternBindingError so a
    broken pattern file fails loudly and specifically rather than
    looking like a normal "missing param" refusal."""
    pass


class PatternBindingError(ValueError):
    """The caller's params don't satisfy a well-formed pattern (missing
    required param, invalid enum value, ...). This is the equivalent of
    normalize_dictum.py's NormalizationIncomplete -- a confident,
    expected refusal, not a crash."""
    pass


_REQUIRED_TOP_LEVEL_FIELDS = ("pattern_ref", "category", "description", "params", "template", "example")
_VALID_PARAM_TYPES = ("identifier", "number", "integer", "enum", "text", "raw", "field_list")


def _render_field_list(value):
    """Renders a list of [name, dictum_type] pairs into indented Dictum
    shape-field lines, one per line, joined with '\\n'. This is the one
    param type that isn't a plain string substitution -- a shape's field
    list is genuinely structured data (see codegraph/patterns/shape-
    declaration.json), not a single scalar slot."""
    if not isinstance(value, (list, tuple)):
        raise PatternBindingError(f"field_list value must be a list of [name, type] pairs, got {value!r}")
    lines = []
    for entry in value:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            raise PatternBindingError(f"field_list entry must be a [name, type] pair, got {entry!r}")
        name, ftype = entry
        lines.append(f"    {name} as {ftype}")
    return "\n".join(lines)


def list_patterns():
    """Returns sorted pattern_refs available on disk (cheap directory
    listing, no JSON parsing -- use load_pattern for the full record)."""
    if not os.path.isdir(PATTERNS_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(PATTERNS_DIR) if f.endswith(".json"))


def load_pattern(pattern_ref):
    """Loads and schema-validates one pattern by ref. Raises
    PatternNotFound if no such file exists, PatternSchemaError if the
    file exists but is malformed (missing fields, pattern_ref mismatch,
    a template placeholder with no matching params entry, or a
    required param that's declared but never used in the template --
    each almost certainly a copy/paste mistake worth failing loudly on,
    per this repo's established "fail loudly on data bugs" convention
    -- see chunk_grammar.py's own _validate_rule_names for the same
    philosophy applied to generated GBNF instead of pattern files)."""
    path = os.path.join(PATTERNS_DIR, f"{pattern_ref}.json")
    if not os.path.isfile(path):
        raise PatternNotFound(pattern_ref)
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise PatternSchemaError(f"{pattern_ref}.json is not valid JSON: {e}")

    missing = [k for k in _REQUIRED_TOP_LEVEL_FIELDS if k not in data]
    if missing:
        raise PatternSchemaError(f"{pattern_ref}.json missing required field(s): {missing}")

    if data["pattern_ref"] != pattern_ref:
        raise PatternSchemaError(
            f"{pattern_ref}.json's own pattern_ref field is {data['pattern_ref']!r} "
            f"-- must match the filename exactly (copy/rename mistake?)"
        )

    params = data["params"]
    if not isinstance(params, dict):
        raise PatternSchemaError(f"{pattern_ref}.json's params must be an object")
    for name, spec in params.items():
        if not isinstance(spec, dict) or "type" not in spec or "required" not in spec:
            raise PatternSchemaError(f"{pattern_ref}.json param {name!r} needs at least 'type' and 'required'")
        if spec["type"] not in _VALID_PARAM_TYPES:
            raise PatternSchemaError(f"{pattern_ref}.json param {name!r} has unknown type {spec['type']!r}")
        if spec["type"] == "enum" and not spec.get("options"):
            raise PatternSchemaError(f"{pattern_ref}.json param {name!r} is type=enum but has no options")

    template_placeholders = set(_PLACEHOLDER_RE.findall(data["template"]))
    declared_params = set(params.keys())
    unknown_placeholders = template_placeholders - declared_params
    if unknown_placeholders:
        raise PatternSchemaError(
            f"{pattern_ref}.json template references undeclared param(s): {sorted(unknown_placeholders)}"
        )
    unused_required = {n for n, spec in params.items() if spec["required"]} - template_placeholders
    if unused_required:
        raise PatternSchemaError(
            f"{pattern_ref}.json declares required param(s) never used in the template "
            f"(likely a copy/paste mistake): {sorted(unused_required)}"
        )

    return data


def bind_pattern(pattern_ref, params=None):
    """Fills a pattern's template with concrete param values. Returns
    the bound Dictum snippet as a string. Raises PatternBindingError if
    a required param is missing or an enum value isn't in its declared
    options list. Unknown params (not declared in the pattern's schema)
    are ignored rather than erroring -- a caller passing extra context
    fields through isn't a binding failure."""
    pattern = load_pattern(pattern_ref)
    params = dict(params or {})

    missing = []
    invalid = []
    resolved = {}
    for name, spec in pattern["params"].items():
        if name in params:
            value = params[name]
        elif "default" in spec:
            value = spec["default"]
        elif spec["required"]:
            missing.append(name)
            continue
        else:
            value = ""  # optional, no default, not supplied -- template must tolerate empty
        if spec["type"] == "enum" and value and value not in spec["options"]:
            invalid.append(f"{name}={value!r} not in {spec['options']}")
        resolved[name] = value

    if missing or invalid:
        parts = []
        if missing:
            parts.append(f"missing required param(s): {missing}")
        if invalid:
            parts.append(f"invalid param value(s): {invalid}")
        raise PatternBindingError(f"{pattern_ref}: " + "; ".join(parts))

    bound = pattern["template"]
    for name, value in resolved.items():
        spec = pattern["params"][name]
        rendered_value = _render_field_list(value) if spec["type"] == "field_list" else str(value)
        bound = bound.replace("{" + name + "}", rendered_value)
    return bound


# ---------------------------------------------------------------------
# Deterministic expansion: for the small set of patterns whose template
# is FULLY mechanical given just a couple of plan-text values (no
# synthesis, no judgment call), skip the model entirely instead of
# hoping a few-shot example is enough. This is narrower than
# render_context's few-shot path on purpose -- see the module docstring
# above render_context for the general (model-still-writes-it) case.
# Motivating data (Cell 12c, Tier7): even WITH the correct pattern
# example in context, a 2B model collapsed `{target}Ptr`/`{target}Result`
# into `Counter` itself (`keep Counter as raw pointer ... value
# &Counter`) or substituted an entirely different primitive (`release
# Counter`) -- deriving two new distinct identifiers by concatenation
# and threading them through a 3-statement + 3-arg-call structure is
# multi-step synthesis this class of model doesn't reliably do, no
# matter how the prompt is worded. But the expansion itself needs zero
# judgment: given `target` and `delta`, `{target}Ptr`/`{target}Result`
# and the full 5-line body are 100% determined -- exactly the kind of
# thing this codebase already does at Build time elsewhere (see
# chunk_grammar.py's _detect_forced_return_dtype).
# ---------------------------------------------------------------------
_DETERMINISTIC_REFS = frozenset({"atomic-increment", "unsafe-malloc"})

_ACTION_NAME_RE = re.compile(r"\baction\s+([A-Za-z_]\w*)", re.I)
# Tolerant of whatever separator the plan prose uses between the token
# name and its values ("ATOMIC_FAA Counter 1", "ATOMIC_FAA: Counter, 1",
# ...) -- \D+? (non-digit, non-greedy) between pieces rather than a
# literal space, since plan text isn't guaranteed to be single-spaced.
_ATOMIC_FAA_RE = re.compile(r"\bATOMIC_FAA\b\D*?([A-Za-z_]\w*)\D+?(-?\d+)", re.I)
_RAW_MALLOC_RE = re.compile(r"\bRAW_MALLOC\b\D*?(-?\d+)\D+?([A-Za-z_]\w*)", re.I)


def extract_deterministic_params(pattern_ref, text):
    """Best-effort extraction of the params a mechanical pattern needs,
    straight out of a chunk's own combined plan-item text -- no model
    call, no LLM output involved yet. Returns a dict on success, None
    if the text doesn't contain everything needed (caller falls back to
    the normal few-shot+LLM path in that case -- this is always an
    optional fast path, never a required one, so a miss here can never
    make anything worse than before this function existed)."""
    if pattern_ref not in _DETERMINISTIC_REFS:
        return None
    action_m = _ACTION_NAME_RE.search(text)
    if not action_m:
        return None
    action_name = action_m.group(1)

    if pattern_ref == "atomic-increment":
        m = _ATOMIC_FAA_RE.search(text)
        if not m:
            return None
        return {"action_name": action_name, "target": m.group(1), "delta": m.group(2)}

    if pattern_ref == "unsafe-malloc":
        m = _RAW_MALLOC_RE.search(text)
        if not m:
            return None
        return {"action_name": action_name, "size": m.group(1), "buffer": m.group(2)}

    return None  # unreachable given the _DETERMINISTIC_REFS check above


def try_deterministic_expand(pattern_ref, text):
    """Returns a ready-to-use, already-correct Dictum snippet for this
    chunk if (a) pattern_ref is one of the fully-mechanical constructs
    and (b) the chunk's own plan text contains everything needed to
    bind it -- else None, meaning "fall back to the model as normal".
    Never raises: a malformed/missing pattern file or a binding
    failure both just mean this fast path doesn't apply this time."""
    params = extract_deterministic_params(pattern_ref, text)
    if params is None:
        return None
    try:
        return bind_pattern(pattern_ref, params)
    except (PatternNotFound, PatternBindingError):
        return None


def render_context(pattern_ref, params=None):
    """Builds the human/model-facing few-shot context block for one
    pattern -- description, preconditions, common mistakes, the
    canonical example, and (only if params were supplied) the bound
    instance for this specific call site. This is what gets injected
    into a Build prompt. Raises the same exceptions as load_pattern/
    bind_pattern -- callers needing the --bridge three-way contract
    should go through _bridge_main below instead of calling this
    directly."""
    pattern = load_pattern(pattern_ref)
    lines = [
        f"Pattern: {pattern['pattern_ref']} ({pattern['category']})",
        pattern["description"],
    ]
    if pattern.get("requires"):
        lines.append("Requires:")
        lines.extend(f"  - {r}" for r in pattern["requires"])
    if pattern.get("common_mistakes"):
        lines.append("Common mistakes to avoid:")
        lines.extend(f"  - {m}" for m in pattern["common_mistakes"])
    lines.append("Correct example:")
    lines.append(pattern["example"])

    bound = None
    if params is not None:
        bound = bind_pattern(pattern_ref, params)
        lines.append("For this specific call:")
        lines.append(bound)

    return "\n".join(lines), bound


# ---------------------------------------------------------------------
# CLI bridge mode -- same three-way {ok:true|false|null} contract as
# normalize_dictum.py --bridge, on purpose, so out/patternGraph.js can
# reuse out/normalizeDictum.js's exact spawn/stdin/stdout handling
# rather than inventing a fourth contract shape in this codebase.
# ---------------------------------------------------------------------
def _bridge_main():
    import sys as _sys
    try:
        payload = json.load(_sys.stdin)
        pattern_ref = payload.get("pattern_ref", "")
        params = payload.get("params")
        plan_text = payload.get("plan_text")
        # Optional fast path: only taken if the caller supplied plan_text
        # AND this pattern_ref is one of the fully-mechanical constructs
        # AND the text actually contains everything needed. Every other
        # case (plan_text omitted, pattern not mechanical, extraction
        # miss) falls straight through to the existing render_context
        # behavior unchanged -- old callers that never pass plan_text
        # get byte-identical output to before, plus one new additive key.
        if plan_text:
            deterministic = try_deterministic_expand(pattern_ref, plan_text)
            if deterministic is not None:
                json.dump({"ok": True, "deterministic": True, "bound": deterministic, "rendered": None}, _sys.stdout)
                return
        rendered, bound = render_context(pattern_ref, params)
        json.dump({"ok": True, "deterministic": False, "rendered": rendered, "bound": bound}, _sys.stdout)
    except (PatternNotFound, PatternSchemaError, PatternBindingError) as e:
        json.dump({"ok": False, "detail": str(e)}, _sys.stdout)
    except Exception as e:
        json.dump({"ok": None, "error": str(e)}, _sys.stdout)


def _list_main():
    import sys as _sys
    try:
        out = []
        for ref in list_patterns():
            try:
                p = load_pattern(ref)
                out.append({"pattern_ref": p["pattern_ref"], "category": p["category"], "description": p["description"]})
            except (PatternNotFound, PatternSchemaError) as e:
                out.append({"pattern_ref": ref, "error": str(e)})
        json.dump({"ok": True, "patterns": out}, _sys.stdout)
    except Exception as e:
        json.dump({"ok": None, "error": str(e)}, _sys.stdout)


if __name__ == "__main__":
    import sys as _sys
    if "--bridge" in _sys.argv:
        _bridge_main()
    elif "--list" in _sys.argv:
        _list_main()
    else:
        _sys.stderr.write("usage: pattern_graph.py --bridge < payload.json   OR   pattern_graph.py --list\n")
        _sys.exit(1)
