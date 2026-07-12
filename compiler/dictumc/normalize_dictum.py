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
    missing at the end."""
    lines = text.split("\n")
    stack = []
    for line in lines:
        stripped = line.strip()
        if re.match(r'^end\b', stripped):
            if stack:
                stack.pop()
            continue
        for pat, (kind, _) in _OPENERS.items():
            if re.match(pat, line, re.I):
                stack.append(kind)
                break
    closers = []
    while stack:
        kind = stack.pop()
        closers.append(f"end {kind}" if kind != "if" else "end if")
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
    that looks clean but isn't. Order matters: repetition collapse
    first (so field/closer logic isn't confused by duplicate blocks),
    then field filtering, then return-type, then closers last (closers
    depend on the final, cleaned-up body)."""
    text = raw
    text = collapse_repetition(text)
    text = normalize_shape_fields(text, plan_items)
    text = normalize_produces_type(text, plan_items)
    text = inject_closers(text)

    unrecoverable = _detect_unrecoverable(text, plan_items)
    if unrecoverable:
        raise NormalizationIncomplete(unrecoverable, raw)

    return text
