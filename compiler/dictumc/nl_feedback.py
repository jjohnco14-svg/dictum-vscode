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


def explain(stage, detail, code=None):
    """stage: 'normalize' | 'parse' | 'review' | 'compile' -- used only
    to phrase the opening clause naturally if a caller wants to prefix
    it; NEVER used to pick which pattern to check. A failure signature
    is recognized by its own shape, not by which stage happened to
    report it, since the same normalize_dictum.py reason string or the
    same parser.py SyntaxError text can in principle surface from more
    than one call site in the pipeline.
    code: the chunk's generated Dictum text, if available -- used only
    to quote the offending line back for context, never to re-derive
    the diagnosis (the diagnosis always comes from `detail` alone)."""
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
    json.dump({"explanation": explain(stage, detail, code)}, sys.stdout)


def _self_test():
    cases = [
        ("normalize", "parameter/type collapse: 'unsafe_demo' used as both name and type", None),
        ("normalize", "duplicate parameter name 'x' within one takes-clause", None),
        ("parse", "Unknown top-level 'while' at line 3",
         "program X\nwhile Y is greater than 0 repeat\nend program\n"),
        ("parse", "Expected 'as', got 'is' at line 2",
         "action foo produces nothing\n    keep X is count\nend action\n"),
        ("parse", "Unexpected 'end' at line 4", "action foo produces nothing\nend action\nend action\n"),
        ("parse", "", None),
    ]
    for stage, detail, code in cases:
        out = explain(stage, detail, code)
        assert out and isinstance(out, str) and len(out) > 0
        print(f"[{stage or '(none)'}] {detail or '(empty)'}\n  -> {out}\n")
    print("nl_feedback self-test OK")


if __name__ == "__main__":
    if "--bridge" in sys.argv:
        _bridge_main()
    elif "--self-test" in sys.argv:
        _self_test()
    else:
        sys.stderr.write("usage: nl_feedback.py --bridge < payload.json  |  nl_feedback.py --self-test\n")
        sys.exit(1)
