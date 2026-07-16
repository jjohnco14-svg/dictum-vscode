#!/usr/bin/env python3
"""
nl_feedback.py -- Phase 4 (NL-appreciation direction, part 3 of 4):
turn a normalize/parse failure into a plain-English explanation of what
went wrong, instead of handing the retry loop a raw grammar diff or an
"Expected X, got Y at line N" parser message the model has to
reverse-engineer before it can act on it.

WHY THIS EXISTS
----------------
Phase 1 (role-scoped identifiers) and Phase 3 (synonym-tolerant
grammar) both make CORRECT generation cheaper. This module is the
retry-side counterpart: when generation still fails, it changes what
the model is handed back on the next attempt. The old path re-sends
the same grammar plus a bare error string; the model has to translate
"Expected 'as', got 'is' at line 3" into "oh, I used 'is' where 'as'
belongs" itself, on every single retry. That translation step is
exactly the kind of thing a language model is fluent at doing to OTHER
text but currently has to do blind to its OWN. This module does that
translation once, up front, in the direction that plays to the
model's actual strength (reading comprehension) instead of demanding
it decode a structural diff.

CONTRACT (mirrors normalize_dictum.py's own fixable/not-fixable
split): every branch below either matches a KNOWN failure signature --
one of normalize_dictum.py's two `_detect_unrecoverable` reasons, or
one of parser.py's `SyntaxError` message shapes, both hand-checked
against the patterns here -- and produces a specific sentence naming
the actual symbol/line involved, or it falls through to a generic-but-
still-readable rewrap of the raw detail. This module never invents a
diagnosis it doesn't have evidence for, and it never returns an empty
string.
"""
import json
import re
import sys

# PHASE 3: N1/N2-class ambiguity detector. The root cause behind N1
# ("program AddTwo - calls action add_two and prints the result") and
# N2's ARCHITECTURE items isn't a pipeline bug -- try_sequential_expand
# correctly declines them (out_of_scope_params/call, not this chunk's
# concern), and the model path's grammar is satisfiable in principle.
# The actual problem is that "the result" never gets bound to a real
# identifier anywhere in the plan text: nothing ever writes
# `giving <Name>` before "prints the result" references it, so no
# amount of retrying the SAME wording can produce a parseable capture
# -- the model has to invent a variable name from nothing, differently
# each attempt, and normalize_dictum.py/parser.py have no fixed target
# to check that invention against.
#
# This mirrors _render_seq_clause's own "call" clause shape (see
# pattern_graph.py) -- a call either has `giving <Name>` (capturing the
# return value under a real, referenceable identifier) or it doesn't
# (a bare fire-and-forget call). "Calls X and prints/uses the result"
# without ever naming that captured value is the exact gap: a
# call-then-reference-by-vague-noun pattern with no `giving` clause in
# between.
_UNCAPTURED_CALL_THEN_RESULT_RE = re.compile(
    r"\bcalls?\s+(?:action\s+)?(\w+)\b"          # "calls action add_two" / "calls add_two"
    r"(?![^.]*\bgiving\b)"                        # ... with no 'giving <Name>' anywhere after it in this sentence
    r"[^.]*?\b(?:the|its)\s+(result|value|output|answer)\b",  # ... before a vague back-reference
    re.I,
)


def detect_unbound_reference(plan_text):
    """Scans one chunk's raw plan text (not generated code) for the
    N1/N2 shape: a call to some action, followed by a vague reference
    ("the result"/"the value"/"the output"/"the answer") that was never
    bound to a real name via a `giving <Name>` clause anywhere in that
    same sentence. Returns None if no such pattern is found, else a
    dict: {"callee": <action name>, "vague_term": <the word used>,
    "suggestion": <ready-to-use corrected phrasing>}.

    This is deliberately scoped to PLAN text, not generated code -- the
    ambiguity lives in the plan's wording itself (see module docstring
    and Cell 13's finding that N1/N2 fail identically across all 3
    retries with the SAME plan text), so re-running generation against
    the same unchanged plan sentence can't fix it. The fix has to
    either reword the plan (upstream) or hand the model a concrete
    substitute name to use (this function's `suggestion`), which is
    exactly the "structured retry prompt" the proposed Phase 3 calls
    for instead of just re-showing a parser error."""
    if not plan_text:
        return None
    m = _UNCAPTURED_CALL_THEN_RESULT_RE.search(plan_text)
    if not m:
        return None
    callee, vague_term = m.group(1), m.group(2)
    capture_name = "Result"
    return {
        "callee": callee,
        "vague_term": vague_term,
        "suggestion": (
            f"call {callee} with <its arguments> giving {capture_name}, "
            f"print the number {capture_name}"
        ),
        "explanation": (
            f"This plan item calls '{callee}' and then refers to \"the {vague_term}\" "
            f"without ever naming it. Dictum has no way to reference a call's return "
            f"value unless it's captured with a 'giving <Name>' clause first -- "
            f"rewrite this as something like: call {callee} with <its arguments> "
            f"giving {capture_name}, print the number {capture_name}."
        ),
    }


_PARAM_TYPE_COLLAPSE_RE = re.compile(
    r"parameter/type collapse: '(\w+)' used as both name and type"
)
_DUP_PARAM_RE = re.compile(
    r"duplicate parameter name '(\w+)' within one takes-clause"
)
_UNKNOWN_TOPLEVEL_RE = re.compile(r"Unknown top-level '([^']+)' at line (\d+)")
_EXPECTED_GOT_STR_RE = re.compile(r"Expected (.+?), got '([^']*)' at line (\d+)")
_EXPECTED_GOT_TYPE_RE = re.compile(r"Expected (.+?), got (\w+) at line (\d+)")
_UNEXPECTED_END_RE = re.compile(r"Unexpected 'end' at line (\d+)")
_UNKNOWN_WITH_RE = re.compile(r"Unknown 'with' clause at line (\d+)")
_UNKNOWN_ATTR_RE = re.compile(r"Unknown attribute #(\w+) at line (\d+)")
_TUPLE_ITEM_RE = re.compile(r"'([^']*)'")


def _clean_expected(expected):
    """parser.py's expect_word(*words) renders its `words` tuple with a
    bare f-string -- real output looks like "Expected ('as',), got 'is'
    at line 2", not "Expected as". Rewrite that Python tuple-repr into
    a plain word or an 'X or Y' list before it goes in front of a
    person (or back to the model)."""
    expected = expected.strip()
    if expected.startswith("(") and expected.endswith(")"):
        items = _TUPLE_ITEM_RE.findall(expected)
        if items:
            quoted = [f"'{i}'" for i in items]
            return quoted[0] if len(quoted) == 1 else " or ".join(quoted[:-1]) + f" or {quoted[-1]}"
    return expected

# Order matters: _EXPECTED_GOT_STR_RE must be tried before
# _EXPECTED_GOT_TYPE_RE, since both can match the same "Expected X, got
# Y at line N" shape and the quoted-string variant is more specific
# (carries the model's actual bad token, not just its token kind).
_ORDERED_PATTERNS = [
    _PARAM_TYPE_COLLAPSE_RE,
    _DUP_PARAM_RE,
    _UNKNOWN_TOPLEVEL_RE,
    _EXPECTED_GOT_STR_RE,
    _EXPECTED_GOT_TYPE_RE,
    _UNEXPECTED_END_RE,
    _UNKNOWN_WITH_RE,
    _UNKNOWN_ATTR_RE,
]


def _line_text(code, line_no):
    if not code:
        return None
    lines = code.split("\n")
    if 1 <= line_no <= len(lines):
        stripped = lines[line_no - 1].strip()
        return stripped or None
    return None


def _quote_line(code, line_no):
    ctx = _line_text(code, line_no)
    return f' Line {line_no} reads: "{ctx}".' if ctx else f" (line {line_no})"


def explain(stage, detail, code=None, plan_text=None):
    """stage: 'normalize' | 'parse' | 'review' | 'compile' -- used only
    to phrase the opening clause naturally if a caller wants to prefix
    it; NEVER used to pick which pattern to check. A failure signature
    is recognized by its own shape, not by which stage happened to
    report it, since the same normalize_dictum.py reason string or the
    same parser.py SyntaxError text can in principle surface from more
    than one call site in the pipeline.
    code: the chunk's generated Dictum text, if available -- used only
    to quote the offending line back for context, never to re-derive
    the diagnosis (the diagnosis always comes from `detail` alone).
    plan_text: PHASE 3 (optional, additive): the chunk's raw plan text,
    if available. Checked first, ahead of every parser-error pattern
    below -- a call-then-vague-reference ambiguity (see
    detect_unbound_reference) is a plan-wording problem, not a
    generation mistake, so retrying generation against the same
    unchanged plan text can't fix it no matter how many attempts are
    spent. Omitting plan_text reproduces the exact previous behavior;
    this is purely additive."""
    if plan_text:
        unbound = detect_unbound_reference(plan_text)
        if unbound:
            return unbound["explanation"]

    detail = (detail or "").strip()
    if not detail:
        return (
            "Something went wrong, but no failure detail was reported for this "
            "chunk. Try regenerating it from the plan."
        )

    m = _PARAM_TYPE_COLLAPSE_RE.search(detail)
    if m:
        name = m.group(1)
        return (
            f"You used the name '{name}' twice in the same declaration -- once as "
            f"the thing being declared, once as its own type (\"{name} as {name}\"). "
            f"A name can't be its own type. Give it a real type from the plan (a "
            f"shape name, or a primitive like 'whole number' or 'text'), and keep "
            f"'{name}' only as the name."
        )

    m = _DUP_PARAM_RE.search(detail)
    if m:
        name = m.group(1)
        return (
            f"Two parameters in the same 'takes' clause are both named '{name}'. "
            f"Every parameter needs its own distinct name -- rename one of them to "
            f"match what the plan actually calls it."
        )

    m = _UNKNOWN_TOPLEVEL_RE.search(detail)
    if m:
        word, line = m.group(1), int(m.group(2))
        return (
            f"Line {line} starts with '{word}', but only 'program', 'shape', "
            f"'action', 'use', 'bind', 'import', 'extern', or 'define' are legal "
            f"at the top level -- a bare statement can't sit outside one of those "
            f"blocks.{_quote_line(code, line)} Wrap it in the action or program "
            f"body it belongs to."
        )

    m = _EXPECTED_GOT_STR_RE.search(detail)
    if m:
        expected, got, line = _clean_expected(m.group(1)), m.group(2), int(m.group(3))
        return (
            f"On line {line}, the plan calls for {expected}, but the generated "
            f"code has '{got}' there instead.{_quote_line(code, line)} Fix just "
            f"that one spot to match what the plan asked for -- the rest of the "
            f"chunk doesn't need to change."
        )

    m = _EXPECTED_GOT_TYPE_RE.search(detail)
    if m:
        expected, got_kind, line = _clean_expected(m.group(1)), m.group(2), int(m.group(3))
        return (
            f"On line {line}, {expected} was expected, but what's there instead "
            f"is a {got_kind.lower()} token, which doesn't fit that "
            f"spot.{_quote_line(code, line)} Rewrite that line so its shape "
            f"matches what the plan describes."
        )

    m = _UNEXPECTED_END_RE.search(detail)
    if m:
        line = int(m.group(1))
        return (
            f"Line {line} closes a block with 'end' that was never opened -- "
            f"there's one 'end' too many, or it's closing the wrong "
            f"block.{_quote_line(code, line)} Remove the extra 'end' or move it "
            f"to close the block it actually belongs to."
        )

    m = _UNKNOWN_WITH_RE.search(detail)
    if m:
        line = int(m.group(1))
        return (
            f"Line {line} uses a 'with' clause that doesn't match any form "
            f"Dictum understands there.{_quote_line(code, line)} Check the plan "
            f"for what that 'with' is supposed to attach to."
        )

    m = _UNKNOWN_ATTR_RE.search(detail)
    if m:
        attr, line = m.group(1), int(m.group(2))
        return (
            f"Line {line} uses an attribute (#{attr}) that Dictum doesn't "
            f"recognize.{_quote_line(code, line)} Remove it or replace it with "
            f"one the plan actually calls for."
        )

    # Generic fallback: still a readable sentence, not a bare diff dump,
    # but doesn't invent a diagnosis this module has no pattern for --
    # consistent with normalize_dictum.py's own "don't guess" stance.
    return (
        f"The generated code didn't match what the plan describes: {detail}. "
        f"Re-read the plan for this chunk and fix that specific mismatch."
    )


def _bridge_main():
    payload = json.load(sys.stdin)
    stage = payload.get("stage", "")
    detail = payload.get("detail", "")
    code = payload.get("code")
    plan_text = payload.get("plan_text")
    json.dump({"explanation": explain(stage, detail, code, plan_text)}, sys.stdout)


def _self_test():
    cases = [
        ("normalize", "parameter/type collapse: 'unsafe_demo' used as both name and type", None, None),
        ("normalize", "duplicate parameter name 'x' within one takes-clause", None, None),
        ("parse", "Unknown top-level 'while' at line 3",
         "program X\nwhile Y is greater than 0 repeat\nend program\n", None),
        ("parse", "Expected 'as', got 'is' at line 2",
         "action foo produces nothing\n    keep X is count\nend action\n", None),
        ("parse", "Unexpected 'end' at line 4", "action foo produces nothing\nend action\nend action\n", None),
        ("parse", "", None, None),
        # PHASE 3: N1/N2-shaped plan ambiguity, detected from plan_text
        # alone (detail/code don't even matter here -- this is checked
        # before any of the parser-error patterns).
        ("parse", "some parser error", None,
         "program AddTwo - calls action add_two and prints the result"),
        ("parse", "some parser error", None,
         "program CheckPositive - calls action classify with a whole number and prints the result"),
    ]
    for stage, detail, code, plan_text in cases:
        out = explain(stage, detail, code, plan_text)
        assert out and isinstance(out, str) and len(out) > 0
        print(f"[{stage or '(none)'}] {detail or '(empty)'} | plan_text={plan_text!r}\n  -> {out}\n")

    # detect_unbound_reference direct cases
    assert detect_unbound_reference("program AddTwo - calls action add_two and prints the result") is not None
    assert detect_unbound_reference(
        "action print_square takes N as whole number produces nothing - "
        "call square with N giving Result, print the number Result"
    ) is None, "a real 'giving' capture must NOT be flagged as unbound"
    assert detect_unbound_reference("") is None
    assert detect_unbound_reference(None) is None

    print("nl_feedback self-test OK")


if __name__ == "__main__":
    if "--bridge" in sys.argv:
        _bridge_main()
    elif "--self-test" in sys.argv:
        _self_test()
    else:
        sys.stderr.write("usage: nl_feedback.py --bridge < payload.json  |  nl_feedback.py --self-test\n")
        sys.exit(1)
