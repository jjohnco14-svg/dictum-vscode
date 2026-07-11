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

DTYPE_PHRASES = [
    ("whole number", '"whole" " " "number"'),
    ("decimal number", '"decimal" " " "number"'),
    ("fractional number", '"fractional" " " "number"'),
    ("truth value", '"truth" " " "value"'),
    ("text", '"text"'),
    ("count", '"count"'),
    ("byte", '"byte"'),
    ("bool", '"bool"'),
]

STMT_TRIGGERS = {
    "keep": r"\bkeep\b",
    "set": r"\bset\b",
    "print": r"\bprint\b",
    "call": r"\bcall\b",
    "release": r"\brelease\b",
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


def _identifier_rule(candidates):
    """Tightest-safe identifier rule: if we found real candidate names,
    whitelist exactly those (Option A from the design doc). If we found
    none (a chunk whose desc text happens to name nothing we can
    recognize -- e.g. it only uses numbers/literals), fall back to the
    open identifier class rather than emit a rule with zero
    alternatives, which would make the whole grammar unsatisfiable."""
    if not candidates:
        return 'identifier    ::= [a-zA-Z_] [a-zA-Z0-9_]*'
    lits = " | ".join(f'"{c}"' for c in candidates)
    return f'identifier    ::= {lits}'


def _cmp_op_rule(found):
    if not found:
        # Full set -- see module docstring: under-detection must never
        # narrow past what a legitimate description could mean.
        return (
            'cmp-op        ::= "equal" " " "to"\n'
            '                | "not" " " "equal" " " "to"\n'
            '                | "greater" " " "than" (" " "or" " " "equal" " " "to")?\n'
            '                | "less" " " "than" (" " "or" " " "equal" " " "to")?'
        )
    return "cmp-op        ::= " + "\n                | ".join(found)


def _arith_rule(found):
    if not found:
        return (
            'additive      ::= multiplicative ((" " "plus" " " | " " "minus" " ") multiplicative)*\n'
            'multiplicative::= unary ((" " "times" " " | " " "modulo" " " | " " "divided" " " "by" " ") unary)*'
        )
    # Split found literals back into additive-tier (+/-) vs
    # multiplicative-tier (*, %, /) so the two precedence rules stay
    # correct instead of collapsing both tiers into one flat alternation.
    add_ops = [f for f in found if f in (ARITH_PHRASES["plus"], ARITH_PHRASES["minus"])]
    mul_ops = [f for f in found if f in (ARITH_PHRASES["times"], ARITH_PHRASES["modulo"], ARITH_PHRASES["divided by"])]
    add_alt = " | ".join(add_ops) if add_ops else '" " "plus" " "'
    mul_alt = " | ".join(mul_ops) if mul_ops else None
    lines = [f'additive      ::= multiplicative (({add_alt}) multiplicative)*']
    if mul_alt:
        lines.append(f'multiplicative::= unary (({mul_alt}) unary)*')
    else:
        lines.append('multiplicative::= unary')
    return "\n".join(lines)


def _dtype_rule(found):
    if not found:
        prim = (
            '"whole" " " "number"\n'
            '                | "decimal" " " "number"\n'
            '                | "fractional" " " "number"\n'
            '                | "truth" " " "value"\n'
            '                | "text" | "count" | "byte" | "bool"'
        )
    else:
        prim = "\n                | ".join(found)
    return (
        f'dtype         ::= prim-type | ptr-type | list-type | identifier\n'
        f'prim-type     ::= {prim}\n'
        f'ptr-type      ::= "raw" " " "pointer" " " "to" " " prim-type\n'
        f'list-type     ::= "list" " " "of" " " prim-type'
    )


def _unsafe_name_rule(found):
    names = found if found else list(UNSAFE_NAMES)
    lits = " | ".join(f'"{n}"' for n in names)
    return f'unsafe-name   ::= {lits}'


_TERMINALS = (
    'number        ::= [0-9]+ ("." [0-9]+)?\n'
    'integer       ::= [0-9]+\n'
    'string        ::= "\\"" [^"\\n]* "\\""\n'
    'indent1       ::= "    "\n'
    'indent2       ::= "        "'
)

_UNARY = (
    'unary         ::= "&" identifier\n'
    '                | "*" identifier\n'
    '                | "-" unary\n'
    '                | primary\n'
    'primary       ::= number | string | "true" | "false" | "nothing" | identifier'
)


def _stmt_rule(kinds, allow_all):
    """simple-stmt alternation, narrowed to only the kinds actually
    detected -- ONLY used for tiers where per-item granularity is fine
    enough to trust (TYPE, MEMORY, SAFETY). See module docstring for why
    OPERATION/MODIFY always pass allow_all=True instead."""
    all_kinds = {
        "keep": 'keep-stmt     ::= "keep" " " identifier " " "as" " " dtype (" " "with" " " "value" " " expr)?',
        "set": 'set-stmt      ::= "set" " " identifier " " "to" " " expr',
        "print": 'print-stmt    ::= "print" " " "the" " " "text" " " expr (" " "and" " " expr)*',
        "call": 'call-stmt     ::= "call" " " identifier (" " "with" " " expr (" " "and" " " expr)*)? (" " "giving" " " identifier)?',
        "release": 'release-stmt  ::= "release" " " identifier',
    }
    use = set(all_kinds) if (allow_all or not kinds) else kinds
    alt_names = " | ".join(f"{k}-stmt" for k in all_kinds if k in use)
    lines = [f"simple-stmt   ::= {alt_names}"]
    for k in all_kinds:
        if k in use:
            lines.append(all_kinds[k])
    return "\n".join(lines)


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
    parts.append("root          ::= top-decl (\"\\n\" top-decl)* \"\\n\"?")
    top_alt = " | ".join(f"{k}-decl" for k in allowed_top)
    parts.append(f"top-decl      ::= {top_alt}")
    parts.append("")

    # Only include a control-flow form when either (a) this tier's
    # per-item signal isn't trustworthy enough to gate on (OPERATION/
    # MODIFY/INVARIANT -- see module docstring) or (b) the literal
    # trigger word was actually found in this chunk's own item text.
    # This is the fix for the bug the vocabulary-containment self-test
    # caught: previously all three were unconditional.
    stmt1_alts = ["simple-stmt"]
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
        parts.append('program-decl  ::= "program" " " identifier ":"? "\\n" body1 "end" (" " "program")?')
    if "shape" in allowed_top:
        parts.append('shape-decl    ::= "shape" " " identifier " " "holds" ":"? "\\n" shape-body "end" (" " "shape")?')
        parts.append('shape-body    ::= (indent1 field-decl "\\n")+')
        parts.append('field-decl    ::= identifier " " "as" " " dtype')
    if "action" in allowed_top:
        parts.append('action-decl   ::= "action" " " identifier (" " "takes" " " params)? " " "produces" " " dtype ":"? "\\n" body1 "end" (" " "action")?')
        parts.append('params        ::= param (" " "and" " " param)*')
        parts.append('param         ::= identifier " " "as" " " dtype')
    parts.append("")

    if "program" in allowed_top or "action" in allowed_top:
        parts.append(f"body1         ::= (indent1 stmt1 \"\\n\")+")
        parts.append(f"stmt1         ::= {' | '.join(stmt1_alts)}")
        parts.append("")
        parts.append(_stmt_rule(stmt_kinds, body_allow_all))
        parts.append("")
        needs_body2 = include_if or include_while or include_repeat or unsafe
        if needs_body2:
            body2_stmt = "unsafe-stmt" if unsafe else "simple-stmt"
            parts.append(f'body2         ::= (indent2 {body2_stmt} "\\n")+')
            if unsafe:
                parts.append("unsafe-stmt   ::= simple-stmt | unsafe-token")
        # Each control-flow rule body is only emitted when it was actually
        # included in stmt1_alts above -- an unreferenced rule left in the
        # file wouldn't make output invalid, but it WOULD reopen exactly
        # the loophole this fix exists to close (the vocabulary-
        # containment check in cell6 greps for the rule name, not just
        # reachability from root, precisely so a stray unused rule can't
        # hide here unnoticed).
        if include_if:
            parts.append('if-stmt       ::= "if" " " expr " " "then" "\\n" body2 (indent1 "otherwise" "\\n" body2)? indent1 "end" (" " "if")?')
        if include_while:
            parts.append('while-stmt    ::= "while" " " expr " " "repeat" "\\n" body2 indent1 "end" (" " ("while" | "repeat"))?')
        if include_repeat:
            parts.append('repeat-stmt   ::= "repeat" " " integer " " "times" " " "using" " " identifier "\\n" body2 indent1 "end" (" " "repeat")?')
        if unsafe:
            parts.append('unsafe-block  ::= "unsafe" ":"? "\\n" body2 indent1 "end" (" " "unsafe")?')
            unsafe_found = detect_unsafe_names(text)
            parts.append('unsafe-token  ::= "[" unsafe-name (" "? ":" " "? unsafe-param)+ " "? "]"')
            parts.append(_unsafe_name_rule(unsafe_found))
            parts.append('unsafe-param  ::= identifier | integer')
        parts.append("")

    parts.append("expr          ::= comparison")
    parts.append('comparison    ::= additive (" " "is" " " cmp-op " " additive)?')
    parts.append(_cmp_op_rule(cmp_found))
    parts.append(_arith_rule(arith_found))
    parts.append(_UNARY)
    parts.append("")
    parts.append(_dtype_rule(dtype_found))
    parts.append("")
    parts.append(_identifier_rule(idents))
    parts.append(_TERMINALS)

    return "\n".join(parts) + "\n"


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
