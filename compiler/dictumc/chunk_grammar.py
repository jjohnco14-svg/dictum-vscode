#!/usr/bin/env python3
"""
chunk_grammar.py -- Context-aware, per-chunk GBNF generation.

WHY THIS EXISTS
----------------
dictum_safe.gbnf / dictum_unsafe.gbnf (the structural rewrite) fixed the
HANG problem: a single, fixed grammar for the whole language, loaded once
per Build call and reused for every chunk. That grammar is still a
superset of what any ONE chunk actually needs -- a TYPE chunk's grammar
still contains if/while/repeat/print/call branches it will never use, and
every chunk's `identifier` terminal is the fully open
`[a-zA-Z][a-zA-Z0-9_]*`, so the model is grammar-legally free to invent
names that were never in the plan. That's the LOOPHOLE problem: outputs
that are syntactically valid but don't match the plan, wasting a retry
instead of a hang.

This module builds a fresh, narrower grammar for ONE chunk, derived from
that chunk's own `[PLAN: ...]` items, instead of loading one of the two
static files. It does not replace dictum_safe.gbnf/dictum_unsafe.gbnf --
those remain correct, general-purpose fallbacks (see chunkGrammar.js),
and this module's rule BODIES are copied from them verbatim; only which
rules/branches are *included* changes per chunk.

IMPORTANT, VERIFIED CONSTRAINT (parser.py:parse_top_level): the only
legal top-level forms in real Dictum are `program`, `module`, `shape`,
`action`, `use`, `bind`, `import`, `extern`, `define`. A bare top-level
`keep` (global constant) is NOT legal -- `parse_top_level` raises
"Unknown top-level 'keep'" for it. This matters here because it means a
TYPE-tier chunk whose plan items are literal `keep X as ... with value
...` lines (as opposed to `shape X holds ...` lines) cannot be satisfied
by ANY grammar that only allows top-level declarations -- that's a
Plan-prompt-level mismatch, not something a tighter GBNF can route around.
This module's TYPE-tier handling assumes shape-decl items (the form
SOURCE_OF_TRUTH.md's chunking description actually documents: "shape's
field can be another shape") and falls back to also allowing action-decl
(wrapping any bare `keep` items inside a small init action body) if no
`shape ... holds` pattern is found in the chunk -- a safety net, not a
fix for the underlying prompt issue.

SECOND VERIFIED CONSTRAINT: chunking.js's real per-item granularity for
OPERATION is coarser than a naive reading of "one plan item per
statement" suggests. `extractOperationAction` matches OPERATION item
descriptions against `/^action (\\w+)/i` (used to attach a matching
INVARIANT's "inside action X," item to the SAME chunk) -- meaning one
real OPERATION plan item is expected to describe an entire action's body
in prose, not one bare statement per item. Because of that, this module
does NOT attempt fine-grained per-statement-kind whitelisting for
OPERATION/MODIFY chunks (a single prose description can legitimately
imply `if`, `while`, or any combination without using those literal
words) -- it restricts identifiers/dtypes/top-decl-kind there and leaves
the statement-kind and operator branches at full width. Fine-grained
statement-kind whitelisting IS applied for TYPE and MEMORY/SAFETY tiers,
where real items genuinely are one-line, one-construct-per-item (a single
`keep`/`RAW_MALLOC`/`RAW_FREE` line each).

USAGE
-----
Reads one JSON object from stdin:
    {"tierName": "OPERATION", "items": [{"category": "...", "id": "...",
     "desc": "..."}], "unsafe": false}
Writes the generated GBNF grammar text to stdout. On any failure (empty
items, unrecognized tier, internal error) exits non-zero and writes
nothing to stdout -- chunkGrammar.js treats that as "fall back to the
static file", never as a hard error, so a bug in this module can only
ever make chunk generation LESS tight, not broken.

CLI equivalent for manual/Kaggle testing:
    python3 chunk_grammar.py --self-test
runs a handful of representative chunks through the generator and prints
each one's rule count next to the static file's, for a quick sanity
check without needing the VS Code extension in the loop at all.
"""
import json
import re
import sys

# FIX (semantic-failure sweep, Cell 8 results): DTYPE_PHRASES used to be an
# 8-entry hand-copied list, independent of type_registry.py -- the module
# that exists specifically so a type only has to be added ONCE. Because of
# that drift, `nothing` (var_valid=False -- "only legal as a return type",
# literally the registry's own docstring example) was never offered to the
# GBNF at all. With no legal way to emit `produces nothing`, the model's
# only path for an untyped/no-return action was the `identifier` fallback,
# which grabbed whatever name was nearby (a param name, its own action
# name) -- the direct cause of Tier4/5/6/7's bad return types. Deriving
# DTYPE_PHRASES from the registry both fixes that gap and means this list
# can never independently drift from parser/validator/emit_c again.
try:
    from . import type_registry as _tr
except ImportError:  # pragma: no cover -- allows `python3 chunk_grammar.py` standalone
    import type_registry as _tr


def _phrase_to_gbnf(words):
    # BUGFIX (Cell 9 results, Tier2): this used to join word-literals with
    # a plain Python space -- `" ".join(...)` -- which only affects the
    # *readability* of the .gbnf source text, not the generated output.
    # GBNF ignores whitespace between grammar tokens; the only way to make
    # the sampler actually emit a space CHARACTER is an explicit `" "`
    # string literal between the word tokens, exactly the convention
    # CMP_PHRASES/ARITH_PHRASES already use by hand a few lines below
    # (e.g. '"not" " " "equal" " " "to"'). Because this one function
    # skipped that convention, every multi-word DTYPE_PHRASES entry (the
    # one exercised in Cell 9: "whole number") concatenated with no
    # separator at all -- `"whole" "number"` as GBNF emits "wholenumber",
    # not "whole number". Verified live: cell9_results.json's Tier2 shows
    # exactly `A as wholenumber` with the space missing. Single-word
    # phrases (the common case) are unaffected either way, since there's
    # nothing to join.
    return ' " " '.join(f'"{w}"' for w in words)

# ---------------------------------------------------------------------
# Reserved vocabulary (must NEVER be offered back as a candidate
# identifier -- copied from dictum_safe.gbnf/dictum_unsafe.gbnf's own
# keyword literals, kept as a flat set rather than importing grammar.py's
# KEYWORDS to avoid this module silently drifting if grammar.py's set
# ever changes shape -- SOURCE_OF_TRUTH.md section 2 already documents
# grammar.py's KEYWORDS as hand-maintained/not auto-synced, so treating
# THIS list as its own small, static copy is consistent with how the
# rest of the project already handles that same tradeoff for the two
# .gbnf files themselves.)
# ---------------------------------------------------------------------
RESERVED_WORDS = {
    "program", "shape", "action", "end", "holds", "takes", "produces", "and",
    "keep", "set", "print", "call", "release", "if", "then", "otherwise",
    "while", "repeat", "times", "using", "with", "value", "giving", "as",
    "to", "is", "equal", "not", "greater", "less", "than", "or", "the",
    "text", "plus", "minus", "times", "modulo", "divided", "by", "true",
    "false", "nothing", "unsafe", "whole", "number", "decimal", "fractional",
    "truth", "count", "byte", "bool", "raw", "pointer", "list", "of",
    # Common prose-description connector words (plan items are sometimes
    # written descriptively -- "program X that prints Y" -- rather than
    # tersely; these aren't Dictum keywords, but they aren't real
    # identifiers either, and were observed leaking into the identifier
    # whitelist during testing on exactly that phrasing).
    "that", "prints", "contains", "block", "before", "after", "which",
}

UNSAFE_NAMES = ("RAW_MALLOC", "RAW_FREE", "ATOMIC_FAA")

CMP_PHRASES = {
    "not equal to": '"not" " " "equal" " " "to"',
    "equal to": '"equal" " " "to"',
    "greater than or equal to": '"greater" " " "than" " " "or" " " "equal" " " "to"',
    "greater than": '"greater" " " "than"',
    "less than or equal to": '"less" " " "than" " " "or" " " "equal" " " "to"',
    "less than": '"less" " " "than"',
}

ARITH_PHRASES = {
    "plus": '" " "plus" " "',
    "minus": '" " "minus" " "',
    "times": '" " "times" " "',
    "modulo": '" " "modulo" " "',
    "divided by": '" " "divided" " " "by" " "',
}

# Value-position primitives (legal for `keep`/field/param -- i.e.
# var_valid=True) and return-only primitives (var_valid=False, currently
# just `nothing`), both derived from type_registry.PRIMITIVES instead of
# hand-copied. Order matches the registry so detect_dtypes' phrase-based
# matching still prefers longer/more-specific phrases first where it
# matters (e.g. "decimal number" before "decimal").
DTYPE_PHRASES = [
    (p.name, _phrase_to_gbnf(p.words)) for p in _tr.PRIMITIVES if p.var_valid
]
RETURN_ONLY_DTYPE_PHRASES = [
    (p.name, _phrase_to_gbnf(p.words)) for p in _tr.PRIMITIVES if not p.var_valid
]

STMT_TRIGGERS = {
    "keep": r"\bkeeps?\b",
    "set": r"\bsets?\b",
    "print": r"\bprints?\b",
    "call": r"\bcalls?\b",
    "release": r"\breleases?\b",
}

# BUGFIX (found via kaggle/cell6_context_aware_gbnf_test.py's Phase 1
# vocabulary-containment check on Tier1_HelloWorld, a print-only chunk):
# control-flow (if/while/repeat) triggers were never detected at all --
# stmt1_alts in generate() unconditionally included if-stmt/while-stmt/
# repeat-stmt for every chunk regardless of whether the plan item said
# anything about control flow, which is exactly the loophole class this
# whole module exists to close. simple-stmt kinds (keep/set/print/...)
# WERE being narrowed correctly; the three control-flow statement forms
# were the one thing generate() forgot to gate the same way.
CONTROL_TRIGGERS = {
    "if": r"\bif\b",
    "while": r"\bwhile\b",
    "repeat": r"\brepeat\b.*\btimes\b|\btimes\b.*\busing\b",
}

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# ---------------------------------------------------------------------
# HANG FIX (Kaggle cell7, Tier2_VariablesArithmetic): every previous
# per-chunk body/field rule used raw GBNF `+`, which is UNBOUNDED. The
# grammar constrains *which* tokens are legal at each position but gives
# the sampler zero pressure to ever choose the "stop repeating" branch
# over "emit one more copy" -- at low temperature the model can (and, in
# a live 30s-timeout run, did) sample the same handful of whitelisted
# identifiers in a cycle indefinitely, never reaching `end`, eating the
# whole token/time budget as a hang rather than a wrong-but-terminating
# output. `_bounded_repeat` replaces every open-ended `X+` with an
# explicit min..max chain built from nested optional groups -- portable
# GBNF (`(...)`  `?`), no dependency on llama.cpp supporting `{m,n}`
# quantifiers. Every call site below must pass a max_n derived from the
# chunk's own plan text, never left unbounded again.
# ---------------------------------------------------------------------

def _bounded_repeat(unit, max_n, min_n=1):
    """GBNF for 'unit repeated min_n..max_n times', with no unbounded +/*.
    unit is a GBNF symbol sequence (e.g. 'indent1 field-decl "\\n"')."""
    max_n = max(min_n, max_n, 1)
    extra = max_n - min_n
    tail = ""
    for _ in range(extra):
        tail = f'({unit} {tail})?' if tail else f'({unit})?'
    mandatory = " ".join([f'({unit})'] * min_n) if min_n > 0 else ""
    return f"{mandatory} {tail}".strip() if tail else mandatory


def _count_shape_fields(text):
    """Exact field count from a 'shape X holds A as T, B as T, ...'
    description, so shape-body can be unrolled to an EXACT count instead
    of an open '+' -- TYPE-tier items are one-line/fully-parseable (see
    module docstring), so exact is safe here, unlike the prose-based
    OPERATION case below. Falls back to a small bounded default only
    when the 'holds' clause itself can't be found/parsed."""
    m = re.search(r"\bholds\b\s*:?\s*(.+?)(?:$|\.\s|\.\Z)", text, re.I)
    if not m:
        return 3
    segments = re.split(r",|\band\b", m.group(1), flags=re.I)
    fields = [s for s in segments if re.search(r"\bas\b", s, re.I)]
    return max(1, min(len(fields) if fields else 3, 12))


def _estimate_stmt_count(text, stmt_kinds, control_kinds, allow_all, buffer=1):
    """Upper bound on a SINGLE body's statement count, used to bound its
    repetition instead of an open '+'. Sums trigger-word occurrences for
    every kind actually in play for this chunk, adds a small buffer for
    prose slack, and clamps to a sane range so a pathological
    description can't blow up grammar size or generation time.

    BUGFIX (Cell 8 results, Tier3/Tier7): this used to be called with the
    WHOLE chunk's text for BOTH body1 (action top level) and body2 (the
    nested while/if/unsafe body), so a trigger word that only belongs
    inside the nested block (e.g. Tier3's `print`/`set`, which only
    occur inside the while loop) got counted into body1's budget too --
    giving body1 spare slots to hallucinate a trailing statement after
    the loop closes. Callers now pass body-specific text (see
    _split_nested_text) so each body's budget reflects only the
    statements actually described for THAT nesting level. The buffer
    default also dropped from a flat +3 (generous enough that Tier7's
    single described unsafe block could repeat 3x) to +1, with callers
    opting into a slightly larger buffer only for the body that
    genuinely holds the nested "real work" (body2)."""
    total = 0
    for k, pat in STMT_TRIGGERS.items():
        if allow_all or k in stmt_kinds:
            total += len(re.findall(pat, text, re.I))
    for k, pat in CONTROL_TRIGGERS.items():
        if allow_all or k in control_kinds:
            total += len(re.findall(pat, text, re.I))
    return max(1, min(total + buffer, 14))


def _split_nested_text(text):
    """Best-effort split of a chunk's description into (outer_text,
    inner_text): inner_text is what's described as happening INSIDE a
    while/if/unsafe block; outer_text is everything else (what belongs
    in body1). Falls back to returning the same text for both when no
    nested block phrasing is recognized -- i.e. never LESS informed than
    the old single-estimate behavior, only more precise when a block is
    clearly present."""
    m = re.search(r"\b(?:while|if)\b.*?\b(?:repeat|then)\s*:\s*(.+)", text, re.I | re.S)
    if m:
        return text[:m.start(1)], m.group(1)
    m = re.search(r"\bunsafe\b.*?\bcontains\b\s*(.+)", text, re.I | re.S)
    if m:
        return text[:m.start(1)], m.group(1)
    return text, text


def _estimate_unsafe_token_count(text):
    """Exact-ish count of unsafe tokens described (`RAW_MALLOC ... then
    RAW_FREE ...` = 2), used instead of the generic simple-stmt trigger
    count for unsafe bodies -- STMT_TRIGGERS has no entry for
    RAW_MALLOC/RAW_FREE/ATOMIC_FAA at all, so the generic estimator
    always fell through to just the flat buffer regardless of how many
    tokens the plan actually named."""
    m = re.search(r"\bunsafe\b.*?\bcontains\b\s*(.+)", text, re.I | re.S)
    if not m:
        return 2
    parts = [p for p in re.split(r"\bthen\b|,", m.group(1), flags=re.I) if p.strip()]
    return max(1, min(len(parts), 6))


def _detect_forced_return_dtype(text):
    """When the plan text is explicit (or explicitly silent) about an
    action's return type, lock the grammar to that ONE literal instead
    of leaving return-dtype's full alternation open for the model to
    guess from. This is the direct fix for Tier4/5/6/7's wrong return
    types:
      - Tier4 ("action greet produces nothing") -> explicit match.
      - Tier5/6/7 never say "produces" at all -> no signal for the model
        to work from, so the old grammar's open return-dtype rule was
        pure guesswork every time. Since Dictum actions with unstated
        return type overwhelmingly mean "no return value" in these plan
        chunks, default to "nothing" rather than leave it open -- this
        eliminates the exact failure class seen in the results (P,
        Buffer, Counter, and the action's own name all hallucinated
        into produces position) rather than just narrowing it.
    A "produces <phrase>" that doesn't match a known registry phrase
    (a real user-defined shape return type) is left alone -- forcing
    would be wrong there, not just imprecise."""
    if re.search(r"\bproduces\s+nothing\b", text, re.I):
        return '"nothing"'
    for phrase, lit in DTYPE_PHRASES + RETURN_ONLY_DTYPE_PHRASES:
        if re.search(r"\bproduces\s+" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b", text, re.I):
            return lit
    if not re.search(r"\bproduces\b", text, re.I):
        return '"nothing"'
    return None


def _print_needs_arith(text):
    """Whether print-arg specifically needs the full additive expression
    chain -- scoped to text actually following a 'print' trigger, rather
    than inheriting the whole chunk's arithmetic capability.

    BUGFIX (Cell 8 results, Tier3): the old code gave EVERY print-arg in
    the chunk the full additive chain whenever ANY part of the chunk
    (e.g. an unrelated `set X to X minus 1`) used arithmetic anywhere.
    That's exactly how Tier3 generated the illegal
    `print the text "Countdown complete" minus 1` -- the print statement
    itself never mentioned arithmetic, but a `set` sixty characters
    later did, and the flag was chunk-global. This looks only at a
    window of text after each 'print' occurrence."""
    for m in re.finditer(r"\bprints?\b", text, re.I):
        window = text[m.end(): m.end() + 120]
        if detect_arith_ops(window) or re.search(r"[&*]\s*[A-Za-z_]", window):
            return True
    return False


def _all_desc(items):
    return " ".join((it.get("desc") or "") for it in items)


def extract_identifiers(items):
    """Every non-reserved word mentioned across the chunk's plan items.
    This is deliberately name-based, not syntax-based (we don't try to
    tell 'this word is a variable name' from 'this word is an action
    name' -- both are legal `identifier` uses in Dictum, and the grammar
    doesn't distinguish them positionally either)."""
    text = _all_desc(items)
    found = []
    seen = set()
    for m in IDENT_RE.finditer(text):
        w = m.group(0)
        lw = w.lower()
        if lw in RESERVED_WORDS or lw in seen:
            continue
        if lw in ("raw_malloc", "raw_free", "atomic_faa"):
            continue
        seen.add(lw)
        found.append(w)
    return found


def _phrase_re(phrase):
    # \b-bounded, whitespace-flexible match. BUGFIX (found via
    # --self-test on a realistic chunk): a naive `phrase in text.lower()`
    # substring check matched the dtype word "count" INSIDE the
    # identifier "request_count", collapsing the whole dtype rule down
    # to just "count" for a chunk that actually needed "whole number" --
    # not a rare edge case, it fires on any identifier that happens to
    # contain a dtype/op word as a substring (count, byte, is, to, ...).
    # \b prevents matching inside a larger identifier/word.
    return re.compile(r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b", re.I)


def detect_cmp_ops(text):
    return [lit for phrase, lit in CMP_PHRASES.items() if _phrase_re(phrase).search(text)]


def detect_arith_ops(text):
    return [lit for phrase, lit in ARITH_PHRASES.items() if _phrase_re(phrase).search(text)]


def detect_dtypes(text):
    return [lit for phrase, lit in DTYPE_PHRASES if _phrase_re(phrase).search(text)]


def detect_unsafe_names(text):
    found = [n for n in UNSAFE_NAMES if n in text]
    return found


def detect_stmt_kinds(text):
    tl = text.lower()
    found = {k for k, pat in STMT_TRIGGERS.items() if re.search(pat, tl)}
    return found


def detect_control_kinds(text):
    tl = text.lower()
    found = {k for k, pat in CONTROL_TRIGGERS.items() if re.search(pat, tl)}
    return found


def _identifier_literal_alt(candidates):
    return " | ".join(f'"{c}"' for c in candidates)


def _extract_own_name(text):
    """The name being DECLARED by this chunk (after program/shape/action).
    Used to keep a declaration from reusing its own name in a role where
    that's never correct -- e.g. Tier6's `takes ... unsafe_demo as raw
    pointer to count`, which reused the action's own name as a second
    parameter name."""
    m = re.search(r"\b(?:program|shape|action)\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.I)
    return m.group(1) if m else None


def _extract_param_names(text):
    """Best-effort extraction of names in `takes X as T and Y as T` /
    `keep X as T` position -- i.e. names that are PARAMETER or LOCAL
    VARIABLE identifiers, not type names. Used to exclude them from the
    dtype-identifier fallback (see _dtype_rule) -- Tier6 generated
    `produces Buffer` where Buffer was a parameter name from an earlier
    `takes` clause, which is only possible because the old code offered
    the exact same unfiltered `identifier` rule for both roles."""
    names = set()
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+as\s+", text, re.I):
        names.add(m.group(1))
    return names


def _action_names(text):
    return set(re.findall(r"\baction\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.I))


def _program_names(text):
    return set(re.findall(r"\bprogram\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.I))


def _shape_names(text):
    return set(re.findall(r"\bshape\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.I))


def _container_names(text):
    """Every name this chunk uses to introduce a top-level declaration
    (program/shape/action). GENERALIZABLE FIX (replaces per-tier hint
    routing): these names are never legal reused as a field name,
    parameter name, or variable target/reference -- doing so is the
    mechanism behind the Tier4 failure documented in
    normalize_dictum.py ("greet as age", "main as age" -- the model's
    own action names leaking into an unrelated shape's field list).
    That happened because every identifier-producing grammar slot drew
    from the SAME chunk-global `idents` pool regardless of which role
    it was filling. _dtype_identifier_candidates below already excludes
    action/program names from the TYPE slot; this is the same exclusion
    made available to every OTHER slot (see _member_name_candidates),
    so the fix isn't specific to shapes, fields, or any one tier --
    it applies to every position where a chunk introduces or references
    a variable-role name, for any future pattern that mixes container
    names with variable names the same way Tier4 did."""
    return _action_names(text) | _program_names(text) | _shape_names(text)


def _dtype_identifier_candidates(idents, text):
    """Role-scoped candidate list for dtype's user-defined-shape-type
    fallback: every extracted identifier EXCEPT names that are never
    legal as a type --
      - every action/program name in the chunk (actions aren't types;
        self-test on Tier4's 3-item chunk caught this: excluding only
        the FIRST declared name left 'greet'/'main' -- both action
        names -- offered as candidate FIELD types for an unrelated
        shape, since a naive single-name exclusion doesn't cover a
        chunk with more than one declaration).
      - any name used in a `<name> as <type>` (parameter/keep) position
        elsewhere in the chunk -- Tier5's `produces P` and Tier6's
        `produces Buffer` were both a parameter name leaking into
        return-type position via this exact path.
    Shape names ARE kept as candidates (a legitimate user-defined-type
    use, e.g. `keep Bob as Person` in Tier4)."""
    exclude = _action_names(text) | _program_names(text) | _extract_param_names(text)
    return [c for c in idents if c not in exclude]


def _member_name_candidates(idents, text):
    """Role-scoped pool for every identifier-producing slot EXCEPT a
    top-level declaration's own name (program-decl/shape-decl/
    action-decl, which legitimately draw from the full unfiltered
    `identifier` pool since that slot IS the container name being
    introduced). Used for: field-decl, param, keep/set/release targets,
    repeat's loop variable, unsafe-param, and unary/primary variable
    references -- i.e. every slot where a container name (action/
    program/shape) showing up would be a bug, not a legitimate name,
    the same class of bug _dtype_identifier_candidates already fixed
    for the dtype slot specifically. This is what makes the fix
    tier-agnostic: it's keyed on grammar ROLE (declaring a container vs.
    naming/referencing a member), not on which of the five known tier
    names produced the chunk."""
    exclude = _container_names(text)
    return [c for c in idents if c not in exclude]


def _call_target_candidates(text):
    """call-stmt's callee names an ACTION, never a variable -- routing
    it through the general/member-name identifier pool is exactly how a
    variable name became grammar-legal in call position. Scoped to
    action names actually declared in this chunk's own text; falls back
    to the open identifier class (via _identifier_rule's own
    empty-candidates branch) when this chunk doesn't declare the action
    being called (e.g. calling an action introduced in an earlier
    chunk) -- same permissive-fallback contract every other role-scoped
    rule here already uses, so this can only ever tighten generation,
    never make a legitimate chunk unsatisfiable."""
    called = set(re.findall(r"\bcall\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.I))
    return sorted(_action_names(text) | called)


def _identifier_rule(candidates, rule_name="identifier"):
    """Tightest-safe identifier rule: if we found real candidate names,
    whitelist exactly those (Option A from the design doc). If we found
    none (a chunk whose desc text happens to name nothing we can
    recognize -- e.g. it only uses numbers/literals), fall back to the
    open identifier class rather than emit a rule with zero
    alternatives, which would make the whole grammar unsatisfiable.
    rule_name lets callers emit distinctly-scoped nonterminals
    (identifier / member-name / call-target) instead of one shared
    production reused across every grammar position."""
    head = f"{rule_name:<13} ::="
    if not candidates:
        return f'{head} [a-zA-Z_] [a-zA-Z0-9_]*'
    lits = " | ".join(f'"{c}"' for c in candidates)
    return f'{head} {lits}'


def _cmp_op_rule(found):
    if not found:
        # Full set -- see module docstring: under-detection must never
        # narrow past what a legitimate description could mean.
        # SINGLE PHYSICAL LINE -- see root-cause note above.
        return ('cmp-op        ::= "equal" " " "to" | "not" " " "equal" " " "to" | '
                '"greater" " " "than" (" " "or" " " "equal" " " "to")? | '
                '"less" " " "than" (" " "or" " " "equal" " " "to")?')
    return "cmp-op        ::= " + " | ".join(found)


_CHAIN_CAP = 4  # max chained binary ops / "and"-joined args a single
                # statement may generate -- same hang class as the +/*
                # fixes above, just lower-risk (shorter loop), still
                # unbounded and still worth capping.


def _arith_rule(found):
    if not found:
        add_tail = _bounded_repeat('(" " "plus" " " | " " "minus" " ") multiplicative', _CHAIN_CAP, 0)
        mul_tail = _bounded_repeat('(" " "times" " " | " " "modulo" " " | " " "divided" " " "by" " ") unary', _CHAIN_CAP, 0)
        return (
            f'additive      ::= multiplicative {add_tail}\n'
            f'multiplicative::= unary {mul_tail}'
        )
    # Split found literals back into additive-tier (+/-) vs
    # multiplicative-tier (*, %, /) so the two precedence rules stay
    # correct instead of collapsing both tiers into one flat alternation.
    add_ops = [f for f in found if f in (ARITH_PHRASES["plus"], ARITH_PHRASES["minus"])]
    mul_ops = [f for f in found if f in (ARITH_PHRASES["times"], ARITH_PHRASES["modulo"], ARITH_PHRASES["divided by"])]
    add_alt = " | ".join(add_ops) if add_ops else '" " "plus" " "'
    mul_alt = " | ".join(mul_ops) if mul_ops else None
    add_tail = _bounded_repeat(f'({add_alt}) multiplicative', _CHAIN_CAP, 0)
    lines = [f'additive      ::= multiplicative {add_tail}']
    if mul_alt:
        mul_tail = _bounded_repeat(f'({mul_alt}) unary', _CHAIN_CAP, 0)
        lines.append(f'multiplicative::= unary {mul_tail}')
    else:
        lines.append('multiplicative::= unary')
    return "\n".join(lines)


def _dtype_rule(found, dtype_ident_candidates):
    """Value-position dtype rule (legal for keep/field/param). `found` is
    the list of var_valid primitive literals actually detected in this
    chunk's text; falls back to the full var_valid set from the registry
    when nothing was detected (never narrows past what a legitimate
    description could mean -- same policy as _cmp_op_rule).

    dtype_ident_candidates is the ROLE-SCOPED identifier whitelist for
    the user-defined-shape-type fallback branch (`identifier` alone used
    to be offered here unfiltered -- see _dtype_identifier_candidates for
    why that let a parameter name or the chunk's own action name get
    hallucinated back as a return/field type, e.g. Tier6's
    `produces Buffer` where Buffer was actually a parameter name)."""
    if not found:
        prim = " | ".join(lit for _, lit in DTYPE_PHRASES)
    else:
        prim = " | ".join(found)
    dtype_ident = (_identifier_literal_alt(dtype_ident_candidates)
                   if dtype_ident_candidates else None)
    dtype_alts = "prim-type | ptr-type | list-type"
    if dtype_ident:
        dtype_alts += " | dtype-identifier"
    lines = [
        f'dtype         ::= {dtype_alts}',
        f'prim-type     ::= {prim}',
        'ptr-type      ::= "raw" " " "pointer" " " "to" " " prim-type',
        'list-type     ::= "list" " " "of" " " prim-type',
    ]
    if dtype_ident:
        lines.append(f'dtype-identifier ::= {dtype_ident}')
    return "\n".join(lines)


def _return_dtype_rule():
    """Return-position dtype: everything `dtype` allows, PLUS the
    return-only primitives (currently just `nothing`) that value
    positions must never accept (type_registry.py's var_valid=False --
    `void` isn't a value a variable can hold). Kept as a thin wrapper
    around `dtype` rather than a duplicated prim list, so the two rules
    can't drift apart the way DTYPE_PHRASES and type_registry.py did."""
    return_only = " | ".join(lit for _, lit in RETURN_ONLY_DTYPE_PHRASES)
    return f'return-dtype  ::= dtype | {return_only}' if return_only else 'return-dtype  ::= dtype'


# BUGFIX (Cell 9 results, Tier7): the old unsafe-token rule was one
# generic `"[" unsafe-name (":" unsafe-param){1,4} "]"` shape shared by
# all three unsafe ops, with a param-count RANGE (1..4) instead of an
# exact count -- so the moment the model emitted the minimum legal one
# param, the grammar was already satisfied and happily closed with `]`,
# giving `[ATOMIC_FAA: Counter ]` instead of the required
# `[ATOMIC_FAA: Counter : 1]`. Unlike the generic simple-stmt count
# (genuinely prose-dependent, see _estimate_stmt_count), each unsafe
# op's arity is a FIXED fact about the operation itself, not something
# that needs estimating from text at all: RAW_MALLOC always takes
# (size, name-to-bind), RAW_FREE always takes (name), ATOMIC_FAA always
# takes (name, delta). Splitting unsafe-token into one exact-arity
# sub-rule per op name (instead of one shared shape with a param-count
# range) closes the loophole structurally rather than by guessing a
# tighter range.
# BUGFIX (validated_patterns.json ground truth, 2026-07-13): ATOMIC_FAA's
# real arity is THREE params, not two -- confirmed by 21/21 real
# transpiler-tested examples, every one shaped
# `[ATOMIC_FAA: <pointer> : <delta> : <result-variable>]`. The Cell 9-11
# fix that introduced UNSAFE_ARITY guessed 2 (pointer, delta) from the
# test suite's own plan text ("ATOMIC_FAA Counter 1"), which was itself
# wrong -- it never mentioned the result variable at all, and used the
# bare variable instead of a pointer to it. Both the grammar's arity
# AND the Cell 9-11 test payload's plan text were wrong in the same
# direction; this fixes the grammar side (see codegraph/patterns/
# atomic-increment.json for the corrected canonical example + the
# preconditions this construct actually needs: target variable, a
# pointer to it, and a result variable, all declared via `keep` first).
UNSAFE_ARITY = {"RAW_MALLOC": 2, "RAW_FREE": 1, "ATOMIC_FAA": 3}


def _unsafe_token_rule(found):
    names = found if found else list(UNSAFE_NAMES)
    lines = []
    alt_names = []
    for n in names:
        arity = UNSAFE_ARITY.get(n, 2)
        param_seq = " ".join(['" "? ":" " "? unsafe-param'] * arity)
        # BUGFIX (Kaggle Cell 10 kernel crash): rule NAMES in this GBNF
        # parser must not contain "_" -- confirmed live by the exact
        # crash this produced: `unsafe-tok-raw_malloc` parsed only as
        # far as `unsafe-tok-raw`, then choked on the literal `_malloc`
        # with "expecting newline or end at _malloc". This is NOT the
        # same rule as quoted terminal strings, where "RAW_MALLOC" (with
        # its underscore) is and remains completely fine -- only bare
        # nonterminal names are affected. n.lower() on "RAW_MALLOC"
        # produces "raw_malloc", carrying the underscore straight into
        # rule-name position; replace it with "-" instead.
        rule_name = f"unsafe-tok-{n.lower().replace('_', '-')}"
        alt_names.append(rule_name)
        lines.append(f'{rule_name:<13} ::= "[" "{n}" {param_seq} " "? "]"')
    lines.insert(0, f"unsafe-token  ::= {' | '.join(alt_names)}")
    return "\n".join(lines)


_NUMBER_DIGITS = _bounded_repeat('[0-9]', 12, 1)
_TERMINALS_BASE = (
    # BUGFIX (Cell 12c, Tier3): this was `[0-9]+`, the one terminal that
    # never got folded into the _bounded_repeat pass everything else in
    # this file went through. Under grammar constraint the sampler ran
    # away and emitted ~170 digits into a print statement, burning the
    # entire token budget before the model ever reached `end while`/`end
    # action` -- confirmed live in cell12c_results.json's Tier3 hint=True
    # raw output. Bounding at 12 digits (comfortably covers 32/64-bit
    # integer literals) closes this off the same way every other rule
    # here already is.
    f'number        ::= {_NUMBER_DIGITS} ("." {_NUMBER_DIGITS})?\n'
    # BUGFIX (Cell 9 results, Tier4): the old string terminal --
    # [^"\n]* -- allowed absolutely any character between the quotes,
    # which is exactly what let the model reach for a Python-f-string
    # habit it knows well but Dictum doesn't have: `"...[Person.name]..."`.
    # Dictum has no string-interpolation syntax at all (confirmed: no
    # bracket-substitution form anywhere in parser.py/grammar.py), so a
    # literal `[` or `]` inside a string is never legitimate Dictum
    # output -- excluding them from the string terminal closes off that
    # specific hallucination path without touching any real string
    # content, since no valid Dictum program needs brackets in a string.
    # FOLLOW-UP (Cell 10 results, Tier4): banning only `[`/`]` didn't
    # stop the interpolation habit, it just relocated it -- the model's
    # very next attempt used `<name>`/`<age>` instead of `[Person.name]`.
    # Same underlying pattern (a bracket-delimited placeholder Dictum
    # doesn't support), different bracket character. Excluding `<`/`>`
    # too closes that specific escape route as well. This is a blunt,
    # blacklist-style fix and is explicitly NOT a claim that it closes
    # the whole class -- see the module docstring's Tier4 entry under
    # "known remaining limitations": a model that wants to hallucinate
    # a placeholder syntax can likely find another delimiter (`{name}`,
    # `%name%`, ...) that isn't banned yet. Real strings needing a
    # literal `<`/`>` character are not a realistic Dictum use case, so
    # this trades a small amount of expressiveness for closing off two
    # more confirmed, live failure modes.
    'string        ::= "\\"" [^"\\n\\[\\]<>]* "\\""\n'
    'indent1       ::= "    "\n'
    'indent2       ::= "        "'
)
_INTEGER_TERMINAL = f'integer       ::= {_NUMBER_DIGITS}'

_UNARY = (
    'unary         ::= "&" member-name | "*" member-name | "-" unary | primary\n'
    'primary       ::= number | string | "true" | "false" | "nothing" | member-name'
)


def _stmt_rule(kinds, allow_all):
    """simple-stmt alternation, narrowed to only the kinds actually
    detected -- ONLY used for tiers where per-item granularity is fine
    enough to trust (TYPE, MEMORY, SAFETY). See module docstring for why
    OPERATION/MODIFY always pass allow_all=True instead."""
    print_and_tail = _bounded_repeat('" " "and" " " print-arg', _CHAIN_CAP, 0)
    call_and_tail = _bounded_repeat('" " "and" " " expr', _CHAIN_CAP, 0)
    all_kinds = {
        "keep": 'keep-stmt     ::= "keep" " " member-name " " "as" " " dtype (" " "with" " " "value" " " expr)?',
        "set": 'set-stmt      ::= "set" " " member-name " " "to" " " expr',
        "print": f'print-stmt    ::= "print" " " "the" " " "text" " " print-arg {print_and_tail}',
        "call": f'call-stmt     ::= "call" " " call-target (" " "with" " " expr {call_and_tail})? (" " "giving" " " member-name)?',
        "release": 'release-stmt  ::= "release" " " member-name',
    }
    use = set(all_kinds) if (allow_all or not kinds) else kinds
    alt_names = " | ".join(f"{k}-stmt" for k in all_kinds if k in use)
    lines = [f"simple-stmt   ::= {alt_names}"]
    for k in all_kinds:
        if k in use:
            lines.append(all_kinds[k])
    return "\n".join(lines)


_RULE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")


def _validate_rule_names(gbnf_text):
    """Defense-in-depth for the Cell 10 kernel-crash class of bug: a
    generated rule NAME containing '_' (or any other char outside
    [a-zA-Z0-9-]) parses fine as far as this module and Python are
    concerned, but this GBNF parser's nonterminal-name grammar doesn't
    include '_' -- confirmed live by `unsafe-tok-raw_malloc` producing
    "expecting newline or end at _malloc" and taking the whole host
    process down with it (a native-level parse failure, not a Python
    exception -- try/except around LlamaGrammar.from_string does NOT
    reliably catch this). Quoted terminal strings ("RAW_MALLOC" etc.)
    are completely unaffected and don't need this -- this only checks
    bare rule-name definitions (the `name ::=` position). Raising here
    means any future bug of this shape fails as an ordinary ValueError
    -- an ordinary non-zero exit, per this module's own documented
    failure contract -- instead of ever reaching the grammar loader."""
    bad = []
    for line in gbnf_text.splitlines():
        head = line.split("::=", 1)
        if len(head) != 2:
            continue
        name = head[0].strip()
        if name and not _RULE_NAME_RE.match(name):
            bad.append(name)
    if bad:
        raise ValueError(f"generated rule name(s) invalid for this GBNF parser (must be [a-zA-Z][a-zA-Z0-9-]*, no underscore): {bad}")


def generate(chunk):
    """chunk: {"tierName": str, "items": [{"category","id","desc"}, ...],
    "unsafe": bool}. Returns GBNF text, or raises ValueError if the chunk
    has no items (nothing to scope a grammar to)."""
    items = chunk.get("items") or []
    if not items:
        raise ValueError("chunk has no items")
    tier = (chunk.get("tierName") or "OTHER").upper()
    unsafe = bool(chunk.get("unsafe"))
    text = _all_desc(items)

    idents = extract_identifiers(items)
    cmp_found = detect_cmp_ops(text)
    arith_found = detect_arith_ops(text)
    dtype_found = detect_dtypes(text)
    stmt_kinds = detect_stmt_kinds(text)
    control_kinds = detect_control_kinds(text)

    has_shape_decl = bool(re.search(r"\bshape\s+\w+\s+holds\b", text, re.I))
    has_program_decl = bool(re.search(r"\bprogram\s+\w+\b", text, re.I))
    has_action_decl = bool(re.search(r"\baction\s+\w+\b", text, re.I))

    # --- decide which top-level declaration forms this chunk may emit ---
    if tier == "ARCHITECTURE":
        allowed_top = [k for k, v in (("program", has_program_decl), ("shape", has_shape_decl), ("action", has_action_decl)) if v]
        if not allowed_top:
            allowed_top = ["program", "shape", "action"]  # couldn't tell -- stay permissive
    elif tier == "TYPE":
        allowed_top = ["shape"]
        if not has_shape_decl:
            # See module docstring's first verified constraint: bare
            # top-level `keep` isn't legal Dictum, so if this chunk's
            # items look like bare keeps rather than shape decls, the
            # only legal way to satisfy them at all is wrapped in an
            # action body -- allow that instead of emitting a grammar
            # the plan items can never actually satisfy.
            allowed_top = ["shape", "action"]
    elif tier in ("OPERATION", "MODIFY", "INVARIANT"):
        allowed_top = ["action"]
    elif tier in ("MEMORY", "SAFETY"):
        allowed_top = ["action"]
        unsafe = True
    else:
        allowed_top = ["program", "shape", "action"]

    body_allow_all = tier in ("OPERATION", "MODIFY", "INVARIANT")

    parts = []
    parts.append(f"# chunk_grammar.py auto-generated -- tier={tier}, items={[it.get('id') for it in items]}")
    parts.append(f"# Do not hand-edit; regenerate from the plan chunk instead.")
    parts.append("")
    if len(items) > 1:
        extra_decls = _bounded_repeat('"\\n" top-decl', len(items) - 1, len(items) - 1)
        parts.append(f'root          ::= top-decl {extra_decls} "\\n"?')
    else:
        parts.append('root          ::= top-decl "\\n"?')
    top_alt = " | ".join(f"{k}-decl" for k in allowed_top)
    parts.append(f"top-decl      ::= {top_alt}")
    parts.append("")

    # Only include a control-flow form when either (a) this tier's
    # per-item signal isn't trustworthy enough to gate on (OPERATION/
    # MODIFY/INVARIANT -- see module docstring) or (b) the literal
    # trigger word was actually found in this chunk's own item text.
    # This is the fix for the bug the vocabulary-containment self-test
    # caught: previously all three were unconditional.
    # BUGFIX (Cell 9 results, Tier6): _stmt_rule's zero-kinds fallback
    # ("never narrow past what a legitimate description could mean")
    # offers ALL FIVE simple-stmt kinds -- including call-stmt -- the
    # moment no keep/set/print/call/release trigger word is found
    # anywhere in the chunk's text. That fallback is correct for
    # ARCHITECTURE/OPERATION prose (a description can genuinely imply an
    # unstated print/call), but it's actively wrong for a MEMORY/SAFETY
    # chunk whose ENTIRE described content is an unsafe block
    # (RAW_MALLOC/RAW_FREE/ATOMIC_FAA, no accompanying simple statement
    # at all): offering simple-stmt there just hands the model a lower-
    # perplexity escape hatch ("call unsafe_demo") as a grammar-legal
    # alternative to the unsafe-token it's actually supposed to emit --
    # exactly what Tier6's real run produced, twice, inside its own
    # unsafe: block. Narrow this ONLY for that one unambiguous case:
    # unsafe=True, zero simple-stmt kinds detected, and this tier already
    # trusts fine-grained per-item detection rather than staying wide
    # open (not body_allow_all -- see module docstring).
    omit_simple_stmt = unsafe and not stmt_kinds and not body_allow_all
    stmt1_alts = [] if omit_simple_stmt else ["simple-stmt"]
    include_if = body_allow_all or "if" in control_kinds
    include_while = body_allow_all or "while" in control_kinds
    include_repeat = body_allow_all or "repeat" in control_kinds
    if include_if:
        stmt1_alts.append("if-stmt")
    if include_while:
        stmt1_alts.append("while-stmt")
    if include_repeat:
        stmt1_alts.append("repeat-stmt")
    if unsafe:
        stmt1_alts.append("unsafe-block")

    if "program" in allowed_top:
        parts.append('program-decl  ::= "program" " " identifier ":"? "\\n" body1 "end" " " "program"')
    if "shape" in allowed_top:
        n_fields = _count_shape_fields(text)
        shape_body_gbnf = _bounded_repeat('indent1 field-decl "\\n"', n_fields, n_fields)
        parts.append('shape-decl    ::= "shape" " " identifier " " "holds" ":"? "\\n" shape-body "end" " " "shape"')
        parts.append(f'shape-body    ::= {shape_body_gbnf}')
        parts.append('field-decl    ::= member-name " " "as" " " dtype')
    forced_return = None
    if "action" in allowed_top:
        # BUGFIX (Tier6/7): the "takes" clause used to always be offered
        # as optional, even when the plan text never mentions parameters
        # at all -- giving the model room to invent a takes-clause out of
        # thin air (and, since the same unrestricted `identifier` rule
        # was used for the new param name, reuse the action's OWN name
        # as that param, as seen in Tier6's
        # `takes ... and unsafe_demo as raw pointer to count`). Only
        # offer the clause at all when the plan text has a "takes" (or an
        # explicit "as"-typed parameter list) to justify it.
        has_takes_clause = bool(re.search(r"\btakes\b", text, re.I))
        forced_return = _detect_forced_return_dtype(text)
        return_ref = forced_return if forced_return else "return-dtype"
        if has_takes_clause:
            parts.append(f'action-decl   ::= "action" " " identifier " " "takes" " " params " " "produces" " " {return_ref} ":"? "\\n" body1 "end" " " "action"')
            n_params = max(1, min(len(re.findall(r'\bas\b', text, re.I)) + 1, 6))
            params_gbnf = _bounded_repeat('" " "and" " " param', n_params - 1, 0)
            parts.append(f'params        ::= param {params_gbnf}'.rstrip())
            parts.append('param         ::= member-name " " "as" " " dtype')
        else:
            parts.append(f'action-decl   ::= "action" " " identifier " " "produces" " " {return_ref} ":"? "\\n" body1 "end" " " "action"')
    parts.append("")

    used_stmt_kinds = set()
    outer_text, inner_text = _split_nested_text(text)
    if "program" in allowed_top or "action" in allowed_top:
        # BUGFIX (Cell 9 results, Tier3): a flat buffer=1 here double-counts
        # when outer_text's ENTIRE content is a single control-flow
        # statement (e.g. "...while Count is greater than 0 repeat: ").
        # _estimate_stmt_count already counts the "while" trigger word as
        # 1 (correctly -- that's the while-statement itself occupying one
        # body1 slot), so adding +1 more "for prose slack" on top gives
        # body1 room for a SECOND statement that was never described --
        # exactly how Tier3 got a trailing print(...) minus 1 hallucinated
        # after the loop's `end repeat`. The slack buffer is still needed
        # for chunks whose outer text names real simple statements (keep/
        # set/print/call/release) that the trigger-word count might
        # undercount from prose phrasing -- so only suppress it when
        # outer_text has NO simple-stmt trigger word at all, i.e. the
        # control-flow statement genuinely is the whole of body1's content.
        # NOTE: deliberately NOT gated on body_allow_all -- Tier3 (the
        # chunk this fix targets) IS an OPERATION-tier chunk, so an
        # `or body_allow_all` override here would silently defeat the
        # whole fix for exactly the case it exists to cover. The
        # outer/inner split above already isolates body1's real content
        # with high confidence (it only fires on a recognized "while/if
        # ... repeat/then:" boundary), independent of whether this
        # tier's simple-stmt KIND detection is trusted -- that's a
        # separate axis (see body_allow_all's use in _stmt_rule) from
        # whether outer_text has any simple-stmt word in it at all.
        outer_has_simple_stmt = any(re.search(pat, outer_text, re.I) for pat in STMT_TRIGGERS.values())
        body1_buffer = 1 if outer_has_simple_stmt else 0
        n_stmts1 = _estimate_stmt_count(outer_text, stmt_kinds, control_kinds, body_allow_all, buffer=body1_buffer)
        body1_gbnf = _bounded_repeat('indent1 stmt1 "\\n"', n_stmts1, 1)
        parts.append(f"body1         ::= {body1_gbnf}")
        parts.append(f"stmt1         ::= {' | '.join(stmt1_alts)}")
        parts.append("")
        # Same "allow all vs. only detected" decision _stmt_rule makes
        # internally -- recomputed here (not returned from _stmt_rule)
        # so the dtype section below can check whether keep-stmt is
        # actually in play without re-parsing the emitted rule text.
        _ALL_SIMPLE_KINDS = {"keep", "set", "print", "call", "release"}
        if omit_simple_stmt:
            # simple-stmt is referenced by neither stmt1 (above) nor
            # unsafe-stmt (below) in this case -- don't emit it at all,
            # both to avoid an unreferenced rule and so there's no
            # lingering path back to it.
            used_stmt_kinds = set()
        else:
            used_stmt_kinds = _ALL_SIMPLE_KINDS if (body_allow_all or not stmt_kinds) else stmt_kinds
            parts.append(_stmt_rule(stmt_kinds, body_allow_all))
            if "call" in used_stmt_kinds:
                parts.append(_identifier_rule(_call_target_candidates(text), rule_name="call-target"))
            parts.append("")
        needs_body2 = include_if or include_while or include_repeat or unsafe
        if needs_body2:
            body2_stmt = "unsafe-stmt" if unsafe else "simple-stmt"
            if unsafe:
                # STMT_TRIGGERS has no entry for RAW_MALLOC/RAW_FREE/
                # ATOMIC_FAA, so the generic estimator always fell
                # through to just the flat buffer here regardless of
                # how many tokens the plan named -- e.g. Tier7 named
                # exactly ONE token but got a buffer-derived budget of
                # several, which is why its single described unsafe
                # block came out repeated 3x.
                n_stmts2 = _estimate_unsafe_token_count(text)
            else:
                n_stmts2 = _estimate_stmt_count(inner_text, stmt_kinds, control_kinds, body_allow_all, buffer=2)
            body2_gbnf = _bounded_repeat(f'indent2 {body2_stmt} "\\n"', n_stmts2, 1)
            parts.append(f'body2         ::= {body2_gbnf}')
            if unsafe:
                # See omit_simple_stmt above: an unsafe-only chunk (no
                # keep/set/print/call/release described) must not be able
                # to satisfy its unsafe block with a simple-stmt at all --
                # that's the grammar-legal "call unsafe_demo" loophole
                # Tier6 hit, and it lived here, not just in stmt1.
                parts.append("unsafe-stmt   ::= unsafe-token" if omit_simple_stmt
                              else "unsafe-stmt   ::= simple-stmt | unsafe-token")
        # Each control-flow rule body is only emitted when it was actually
        # included in stmt1_alts above -- an unreferenced rule left in the
        # file wouldn't make output invalid, but it WOULD reopen exactly
        # the loophole this fix exists to close (the vocabulary-
        # containment check in cell6 greps for the rule name, not just
        # reachability from root, precisely so a stray unused rule can't
        # hide here unnoticed).
        if include_if:
            parts.append('if-stmt       ::= "if" " " expr " " "then" "\\n" body2 (indent1 "otherwise" "\\n" body2)? indent1 "end" " " "if"')
        if include_while:
            parts.append('while-stmt    ::= "while" " " expr " " "repeat" "\\n" body2 indent1 "end" " " ("while" | "repeat")')
        if include_repeat:
            parts.append('repeat-stmt   ::= "repeat" " " integer " " "times" " " "using" " " member-name "\\n" body2 indent1 "end" " " "repeat"')
        if unsafe:
            parts.append('unsafe-block  ::= "unsafe" ":"? "\\n" body2 indent1 "end" " " "unsafe"')
            unsafe_found = detect_unsafe_names(text)
            parts.append(_unsafe_token_rule(unsafe_found))
            parts.append('unsafe-param  ::= member-name | integer')
        parts.append("")

    has_ptr_ops = bool(re.search(r"[&*]\s*[A-Za-z_]", text))
    needs_full_expr_chain = (
        body_allow_all or include_if or include_while
        or cmp_found or arith_found or has_ptr_ops
    )
    needs_expr = (
        body_allow_all or include_if or include_while
        or bool(used_stmt_kinds & {"keep", "set", "print", "call"})
    )
    print_needs_arith = ("print" in used_stmt_kinds) and _print_needs_arith(text)
    if needs_expr:
        parts.append("expr          ::= comparison" if needs_full_expr_chain else
                     'expr          ::= number | string | "true" | "false" | "nothing" | identifier')
        if needs_full_expr_chain or print_needs_arith:
            if needs_full_expr_chain:
                parts.append('comparison    ::= additive (" " "is" " " cmp-op " " additive)?')
                parts.append(_cmp_op_rule(cmp_found))
            parts.append(_arith_rule(arith_found))
            parts.append(_UNARY)
        if "print" in used_stmt_kinds:
            # print's argument skips the comparison layer entirely --
            # `expr` allows a trailing `is <cmp-op> <additive>` suffix
            # (legitimate for keep/set/call, which may assign or pass a
            # truth-value result), but a print argument being a
            # comparison is never sensible ("Countdown complete" is
            # greater than 0). This was found live: it's grammatically
            # legal today, which is exactly how Tier3's real Kaggle run
            # produced `print the text "Countdown complete" is greater
            # than 0`. In the collapsed (no-comparison) case `expr` is
            # already comparison-free, so print-arg is just an alias.
            parts.append('print-arg     ::= additive' if print_needs_arith else 'print-arg     ::= expr')
        parts.append("")
    # dtype is only referenced by keep-stmt, field-decl (shape), and
    # param/produces (action) -- omit the whole dtype/prim-type/ptr-type/
    # list-type block entirely when none of those are in play (e.g. a
    # print-only chunk has no use for it at all).
    needs_dtype = ("shape" in allowed_top) or ("action" in allowed_top) or ("keep" in used_stmt_kinds)
    if needs_dtype:
        dtype_ident_candidates = _dtype_identifier_candidates(idents, text)
        parts.append(_dtype_rule(dtype_found, dtype_ident_candidates))
        # return-dtype is only referenced when action-decl's produces slot
        # wasn't already forced to an exact literal (see
        # _detect_forced_return_dtype) -- avoids emitting an unreferenced
        # rule in the common case where the return type is fully pinned.
        if "action" in allowed_top and forced_return is None:
            parts.append(_return_dtype_rule())
        parts.append("")
    parts.append(_identifier_rule(idents))
    parts.append(_identifier_rule(_member_name_candidates(idents, text), rule_name="member-name"))
    parts.append(_TERMINALS_BASE)
    if include_repeat or unsafe:
        parts.append(_INTEGER_TERMINAL)

    result = "\n".join(parts) + "\n"
    _validate_rule_names(result)
    return result


def _self_test():
    cases = [
        {"tierName": "TYPE", "items": [
            {"category": "TYPE", "id": "2", "desc": "shape Request holds method as text, path as text, body as text"},
            {"category": "TYPE", "id": "3", "desc": "shape Response holds status as whole number, body as text"},
        ]},
        {"tierName": "MEMORY", "items": [
            {"category": "MEMORY", "id": "15", "desc": "unsafe block contains RAW_MALLOC 4096 buffer"},
        ], "unsafe": True},
        {"tierName": "SAFETY", "items": [
            {"category": "SAFETY", "id": "16", "desc": "RAW_FREE buffer before end program"},
        ], "unsafe": True},
        {"tierName": "OPERATION", "items": [
            {"category": "OPERATION", "id": "10", "desc": "action main - while request_count is less than MAX_REQUESTS repeat: call handle_request with request_ptr giving response, call send_response with response, set request_count to request_count plus 1"},
        ]},
        {"tierName": "ARCHITECTURE", "items": [
            {"category": "ARCHITECTURE", "id": "1", "desc": "program Server"},
        ]},
    ]
    for c in cases:
        g = generate(c)
        rule_count = len(re.findall(r"^[a-zA-Z_][a-zA-Z0-9_-]*\s*::=", g, re.MULTILINE))
        idents_line = next((l for l in g.splitlines() if l.startswith("identifier")), "")
        print(f"[{c['tierName']:<12}] rules={rule_count:<3} {idents_line[:90]}")
    print("\nself-test OK")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        _self_test()
        return
    try:
        chunk = json.load(sys.stdin)
        grammar = generate(chunk)
        sys.stdout.write(grammar)
    except Exception as e:
        sys.stderr.write(f"chunk_grammar.py error: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
