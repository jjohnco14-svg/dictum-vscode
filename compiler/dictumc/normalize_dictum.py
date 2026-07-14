#!/usr/bin/env python3
"""
normalize_dictum.py -- post-generation cleanup for one chunk's raw output,
run BEFORE it's handed to patchEngine/L2 checks.

WHAT THIS CAN AND CANNOT DO (read this before adding a new rule)
------------------------------------------------------------------
Every failure in the Cell 6 run splits into exactly two classes:

  FIXABLE (this module handles these): the model said something twice, or
  padded past where the plan-satisfying content ended, or the model's
  output is missing a closer a bracket-count can supply. The CORRECT
  content is present in the raw text somewhere -- normalization is
  selecting/deduplicating/closing, not inventing.

    Tier2 (Calculator):  "A/B/Sum as whole number" then repeats
                          "Sum/Calculator as whole number" 3x -- the
                          correct 3 fields ARE there, plus a field
                          ("Calculator") the plan never asked for, plus
                          duplicates. Fixable: filter to the plan's own
                          field list, in the plan's own order, first
                          occurrence only.
    Tier5 (pointer_demo): "print the text *P" x5 in a row -- correct
                          line, repeated. Fixable: collapse a repeating
                          line/cycle to one instance.
    Tier8 (SqrtDemo):     "set SqrtDemo to 1.0" x5 -- same as Tier5.
    Tier3 (countdown):    a CORRECT while-loop, closed with a bare "end",
                          followed by three more statements that repeat/
                          contradict what the loop already did. Fixable
                          up to a point: collapse the repeated closing
                          "print...Countdown complete" lines and the
                          redundant trailing "set countdown to 0" (dead
                          code -- the loop already left countdown at 0);
                          the malformed "print the text "..." is greater
                          than 0" line (a string literal directly
                          followed by a comparison operator, which
                          shouldn't be possible under the print-stmt
                          grammar at all -- worth a separate look at
                          whether print-stmt's `expr` really needs full
                          comparison capability) gets dropped as
                          unparseable trailing debris, not "fixed".

  NOT FIXABLE (this module explicitly does NOT touch these, and neither
  should any regex): the model's output has no recoverable correct
  content to select from -- the ground truth just isn't in the text.

    Tier4 (Person):     "greet as age", "main as age" -- the model put
                         its own two ACTION names (from other plan items
                         in the same chunk) into the SHAPE's field list,
                         with a made-up type. There is no substring here
                         that becomes "correct" by moving/deleting text.
    Tier6 (unsafe_demo): "takes unsafe_demo as raw pointer to decimal
                         number and unsafe_demo as raw pointer" -- the
                         model used the ACTION's own name as both of two
                         parameter names, with a type (decimal number)
                         that contradicts the plan (RAW_MALLOC operates
                         on bytes, not decimals). No recoverable ground
                         truth.
    Tier7 (increment):  "takes increment as increment and increment as
                         increment and..." -- total collapse; the token
                         "increment" (the action's own name) is standing
                         in for every field name AND every type name.

  Trying to regex-patch the second class doesn't fix anything -- it
  manufactures plausible-looking, still-wrong code, which is strictly
  worse than an honest failure the existing L2Fields check + retry loop
  (_runBuildChunk's correction-context mechanism, already in
  extension.js) can catch and regenerate against. This module raises
  NormalizationIncomplete for that class instead of guessing, so the
  caller falls through to the real retry path rather than shipping
  silently-wrong code.
"""
import re


class NormalizationIncomplete(Exception):
    """Raised when the raw output doesn't contain enough recoverable
    structure to normalize safely (Tier4/6/7-class failures). Callers
    should treat this exactly like an L2 check failure -- feed it back
    into the existing retry loop, not paper over it here."""
    def __init__(self, reason, raw):
        self.reason = reason
        self.raw = raw
        super().__init__(reason)


# ---------------------------------------------------------------------
# Step A: collapse immediate repetition (single lines or short cycles)
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# PHASE 3 -- synonym canonicalization, companion to chunk_grammar.py's
# KEYWORD_SYNONYMS/CONNECTOR_SYNONYMS. Build's GBNF now offers several
# fluent alternates at five statement-trigger positions ("keep" |
# "declare" | "make", etc.) plus two connector positions ("as" | "of
# type", "with value" | "initialized to" | "starting at"), so the model
# doesn't have to fight its own natural phrasing to stay grammar-legal.
# Every OTHER pass in this file, and parser.py/emit_c.py downstream,
# still only understand the ONE canonical spelling -- this function
# reverses the substitution before anything else runs, so nothing past
# this point needs to know synonyms exist at all.
#
# This is safe to do unconditionally (not a "best guess"): because
# Build's decoding is grammar-CONSTRAINED, the raw text handed to this
# module can only contain, at each of these positions, one of the exact
# literal alternates chunk_grammar.py actually offered there. Reversing
# a closed, known substitution set back to its one canonical spelling
# is a deterministic rewrite, not a heuristic -- the same category of
# operation as inject_closers() below, not a guess about model intent.
#
# SCOPE: matches chunk_grammar.py's KEYWORD_SYNONYMS/CONNECTOR_SYNONYMS
# exactly -- five statement keywords (at statement-start position only,
# where the grammar actually put them) and the two connector phrases
# ("of type", "initialized to", "starting at"). Nothing wider than what
# the grammar can actually produce is matched here, on purpose.
# ---------------------------------------------------------------------
_STMT_KW_CANON = {
    "keep": "keep", "declare": "keep", "make": "keep",
    "set": "set", "update": "set", "change": "set",
    "print": "print", "display": "print", "show": "print",
    "call": "call", "invoke": "call", "run": "call",
    "release": "release", "free": "release", "deallocate": "release",
}
# Longest-first alternation so e.g. a hypothetical future "call" vs.
# "calling" collision would prefer the more specific match -- not
# currently ambiguous with today's word list, but cheap to keep correct.
_STMT_KW_RE = re.compile(
    r'^(\s*)(' + "|".join(sorted(_STMT_KW_CANON, key=len, reverse=True)) + r')\b',
    re.M,
)
_OF_TYPE_RE = re.compile(r'\bof\s+type\b')
_INIT_TO_RE = re.compile(r'\binitialized\s+to\b')
_START_AT_RE = re.compile(r'\bstarting\s+at\b')


def canonicalize_synonyms(text):
    """Rewrites every chunk_grammar.py synonym alternate back to its one
    canonical Dictum spelling. Idempotent -- running it twice is a
    no-op, since the canonical spellings are themselves members of
    their own synonym sets and map to themselves."""
    text = _STMT_KW_RE.sub(lambda m: f"{m.group(1)}{_STMT_KW_CANON[m.group(2)]}", text)
    text = _OF_TYPE_RE.sub("as", text)
    text = _INIT_TO_RE.sub("with value", text)
    text = _START_AT_RE.sub("with value", text)
    return text


def collapse_repetition(text, max_cycle=3):
    """Collapses a line, or a short (2-3 line) repeating cycle, that
    repeats 3+ times in a row down to ONE instance. Deliberately
    conservative: only touches EXACT immediate repeats (after trimming
    trailing whitespace), never near-matches -- guessing at "close
    enough" duplicates is exactly the kind of judgment call that belongs
    in the L2 retry loop, not a normalizer."""
    lines = text.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        collapsed = False
        for cycle_len in range(1, max_cycle + 1):
            if i + cycle_len * 3 > n:
                continue
            cycle = lines[i:i + cycle_len]
            if not any(l.strip() for l in cycle):
                continue
            reps = 1
            j = i + cycle_len
            while j + cycle_len <= n and lines[j:j + cycle_len] == cycle:
                reps += 1
                j += cycle_len
            if reps >= 3:
                out.extend(cycle)
                i = j
                collapsed = True
                break
        if not collapsed:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------
# Step B: shape field-list normalization against the plan's own text
# ---------------------------------------------------------------------
_FIELD_RE = re.compile(r'^(\s*)([A-Za-z_]\w*)\s+as\s+(.+?)\s*$')
_PLAN_FIELD_RE = re.compile(
    r'([A-Za-z_]\w*)\s+as\s+((?:whole|decimal|fractional|truth|raw|list)?\s*'
    r'(?:number|value|text|count|byte|bool|pointer\s+to\s+\w+(?:\s+\w+)?|of\s+\w+(?:\s+\w+)?))',
    re.I,
)


def _plan_field_names(plan_items):
    """Field names the plan itself named, in the order it named them --
    extracted from a `shape X holds A as T1, B as T2, ...` style item."""
    names = []
    seen = set()
    for it in plan_items:
        desc = it.get("desc", "")
        if not re.search(r"\bholds\b", desc, re.I):
            continue
        for m in _PLAN_FIELD_RE.finditer(desc):
            name = m.group(1)
            if name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
    return names


def normalize_shape_fields(text, plan_items):
    """If this chunk is a shape-decl, keep only field lines whose name
    the plan actually listed, first occurrence only, in the plan's
    order. Anything else (an invented field name, or a repeat) is
    dropped -- not "corrected", since there's nothing to correct it TO
    beyond what the plan already said."""
    if not re.search(r"^\s*shape\s+\w+\s+holds\b", text, re.M | re.I):
        return text
    planned = {n.lower(): n for n in _plan_field_names(plan_items)}
    if not planned:
        return text  # can't safely filter without a ground-truth list
    lines = text.split("\n")
    out = []
    seen = set()
    for line in lines:
        m = _FIELD_RE.match(line)
        if not m:
            out.append(line)
            continue
        indent, name, dtype = m.groups()
        key = name.lower()
        if key in planned and key not in seen:
            seen.add(key)
            out.append(line)
        # else: silently dropped -- unplanned field or repeat
    return "\n".join(out)


# ---------------------------------------------------------------------
# Step C: force the declared return type to match what the plan said,
# when the plan item states it explicitly ("produces <type>")
# ---------------------------------------------------------------------
_PLAN_PRODUCES_RE = re.compile(r'\bproduces\s+([a-zA-Z][\w\s]*?)(?:[-,.:]|$)', re.I)
_GEN_PRODUCES_RE = re.compile(r'("action"\s+"\s"\s*[A-Za-z_]\w*.*?"produces"\s+" "\s*)([a-zA-Z][\w\s]*?)(\s*(?:":"\?)?\s*"\\n")')


def normalize_produces_type(text, plan_items):
    """Only fires when a plan item explicitly names the return type
    (SKILL_PLAN.md rule 2 requires OPERATION items to state this) --
    otherwise there's no ground truth to force it to, and this is a
    no-op rather than a guess."""
    plan_type = None
    for it in plan_items:
        m = _PLAN_PRODUCES_RE.search(it.get("desc", ""))
        if m:
            plan_type = m.group(1).strip().rstrip(".")
            break
    if not plan_type:
        return text
    return re.sub(
        r'(action\s+\w+(?:\s+takes\s+.*?)?\s+produces\s+)([a-zA-Z][\w\s]*?)(\s*:?\s*\n)',
        lambda m: m.group(1) + plan_type + m.group(3),
        text,
        count=1,
        flags=re.I,
    )


# ---------------------------------------------------------------------
# Step D: inject missing closers based on a simple open/close count
# ---------------------------------------------------------------------
_OPENERS = {
    r"^\s*program\s+\w+": ("program", None),
    r"^\s*shape\s+\w+\s+holds\b": ("shape", None),
    r"^\s*action\s+\w+": ("action", None),
    r"^\s*while\b": ("while", "repeat"),
    r"^\s*repeat\s+\d+\s+times\b": ("repeat", None),
    r"^\s*if\b": ("if", None),
    r"^\s*unsafe\b": ("unsafe", None),
}


def inject_closers(text):
    """Walks the lines tracking a simple open-block stack (program/
    shape/action/while/repeat/if/unsafe) and appends whatever `end`s are
    missing at EOF. Does not try to fix a closer in the wrong PLACE
    (that's a structural error the retry loop should catch), only ones
    missing at the end.

    BUGFIX (Cell 12c, Tier3): closers used to be appended as bare
    `end {kind}` with no indentation at all, regardless of where the
    opener sat. That's fine for top-level `action`/`shape`/`program`
    (indent ""), but a `while`/`if`/`unsafe` nested inside an action
    opens at e.g. 4 spaces, and the parser expects its `end while` at
    that SAME indentation -- confirmed live: cell12c_results.json's
    Tier3 hint=True case had the model burn its whole token budget on a
    runaway number before reaching `end while`, this function correctly
    detected the missing closer, but emitted it at column 0, and the
    parser then rejected the synthetic `end while` as a stray
    top-level statement ("Unknown top-level 'while' at line 5"). Each
    stack entry now carries the opener's own indentation string, and
    the closer is emitted at that same indentation instead of always
    at zero."""
    lines = text.split("\n")
    stack = []  # list of (kind, indent_str)
    for line in lines:
        stripped = line.strip()
        if re.match(r'^end\b', stripped):
            if stack:
                stack.pop()
            continue
        for pat, (kind, _) in _OPENERS.items():
            if re.match(pat, line, re.I):
                indent_str = line[:len(line) - len(line.lstrip(" "))]
                stack.append((kind, indent_str))
                break
    closers = []
    while stack:
        kind, indent_str = stack.pop()
        label = "end if" if kind == "if" else f"end {kind}"
        closers.append(f"{indent_str}{label}")
    if closers:
        text = text.rstrip("\n") + "\n" + "\n".join(closers) + "\n"
    return text


# ---------------------------------------------------------------------
# Step E: detect the NOT-fixable class and refuse to guess
# ---------------------------------------------------------------------
_TAKES_CLAUSE_RE = re.compile(r'\btakes\s+(.+?)(?:\s+produces\b|\s*$)', re.I | re.M)
_PARAM_NAME_RE = re.compile(r'([A-Za-z_]\w*)\s+as\s+')


def _detect_unrecoverable(text, plan_items):
    """Two signatures of total collapse, neither of which has a
    recoverable substring to select from:

      1. A field/param name IDENTICAL to the enclosing action's own
         name, used as its own type ("X as X") -- the Tier7 pattern.
      2. ANY parameter name repeated within the same `takes` clause
         (found via testing: Tier6's failure was "unsafe_demo as raw
         pointer to decimal number and unsafe_demo as raw pointer" --
         two DIFFERENT types, so the narrow "name as name" check in (1)
         missed it entirely, and it silently passed through as if
         normalized, closer and all. Two params can never legally share
         a name regardless of what type each one claims, so this check
         doesn't need the exact "X as X" shape -- just a repeated name
         anywhere in one takes-clause.)
    """
    action_names = set(re.findall(r'\baction\s+(\w+)', text, re.I))
    for name in action_names:
        if re.search(rf'\b{re.escape(name)}\s+as\s+{re.escape(name)}\b', text, re.I):
            return f"parameter/type collapse: '{name}' used as both name and type"

    for m in _TAKES_CLAUSE_RE.finditer(text):
        clause = m.group(1)
        names = [n.lower() for n in _PARAM_NAME_RE.findall(clause)]
        seen = set()
        for n in names:
            if n in seen:
                return f"duplicate parameter name '{n}' within one takes-clause"
            seen.add(n)
    return None


def normalize_dictum(raw, plan_items):
    """Runs the fixable-class passes in order, then checks for the
    unrecoverable signature and raises rather than returning something
    that looks clean but isn't. Order matters: synonym canonicalization
    FIRST (Phase 3 -- every regex below is written against the one
    canonical spelling, so synonym rewriting has to happen before any
    of them see the text), then repetition collapse (so field/closer
    logic isn't confused by duplicate blocks), then field filtering,
    then return-type, then closers last (closers depend on the final,
    cleaned-up body)."""
    text = raw
    text = canonicalize_synonyms(text)
    text = collapse_repetition(text)
    text = normalize_shape_fields(text, plan_items)
    text = normalize_produces_type(text, plan_items)
    text = inject_closers(text)

    unrecoverable = _detect_unrecoverable(text, plan_items)
    if unrecoverable:
        raise NormalizationIncomplete(unrecoverable, raw)

    return text


# ---------------------------------------------------------------------
# CLI bridge mode -- this was entirely missing, which is why every
# Kaggle cell7 run logged "Invalid JSON from normalizer" on every tier:
# `python3 normalize_dictum.py --bridge` had no `if __name__` block at
# all, so it printed nothing and exited 0, and the caller's
# `json.loads("")` failed. Reads {"code": str, "plan_items": [...]}
# from stdin, writes {"ok": true, "code": ...} on success,
# {"ok": false, "detail": ...} when NormalizationIncomplete is raised
# (the caller's existing retry-loop path), or {"ok": null, "error": ...}
# for anything else -- matching the three-way contract cell7's
# normalize_dictum() already expects.
# ---------------------------------------------------------------------
def _bridge_main():
    import json
    import sys as _sys
    try:
        payload = json.load(_sys.stdin)
        code = payload.get("code", "")
        plan_items = payload.get("plan_items") or []
        result = normalize_dictum(code, plan_items)
        json.dump({"ok": True, "code": result}, _sys.stdout)
    except NormalizationIncomplete as e:
        json.dump({"ok": False, "detail": e.reason, "code": e.raw}, _sys.stdout)
    except Exception as e:
        json.dump({"ok": None, "error": str(e)}, _sys.stdout)


if __name__ == "__main__":
    import sys as _sys
    if "--bridge" in _sys.argv:
        _bridge_main()
    else:
        _sys.stderr.write("usage: normalize_dictum.py --bridge < payload.json\n")
        _sys.exit(1)
