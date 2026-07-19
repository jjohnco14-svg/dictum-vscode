#!/usr/bin/env python3
"""
CELL 15: Plan -> Build -> Review regression suite (supersedes Cell 14)
============================================================================
Cell 14 fixed the real wiring gaps in the TEST HARNESS (prepare_c_imports,
kind-tagged reserved_names, real bracket-token syntax, IMPORT_C suite
separation). Running it against the CURRENT codebase (this file was
written after auditing that state) turns up exactly ONE remaining
failure, and it is again a harness/content bug, not a codebase bug:

  BUG FOUND: IC2_TwoCooperatingImports's mock plan text used
  "the quotient of the sum of A and B by 2". parser.py's quotient
  grammar (parse_prefix_expression, `_two('/')`) requires the literal
  separator "and" between operands -- exactly like sum/difference/
  product -- never "by" ("by" is only valid after "divided", a
  different construct, in parse_multiplicative). "by" after "the
  quotient of X" is therefore a hard SyntaxError ("Expected ('and',),
  got 'by'"), confirmed directly against dictumc_cli.py in isolation,
  with zero chunking/grammar/model involvement. FIXED below by using
  "and" (verified: parses, compiles, and produces the correct AST
  `(A + B) / 2`).

  Everything else Cell 14 flagged as a prior gap (IMPORT_C forward-
  declaration ordering for module/library-only files, ImportC's
  double-typed extern+wrapper signature) is ALREADY FIXED in emit_c.py
  as of this audit -- see get_output()'s "BUGFIX (IMPORT_C forward-
  declaration ordering...)" comment, which puts the deterministic
  extern+wrapper lines (self.output) BETWEEN the prelude and the
  buffered action bodies (self._action_buffer). Verified directly:
  a hand-written multi-import library file (no `program` wrapper, one
  action calling three chained C imports) compiles with zero warnings
  under `-std=c11 -Wall -Wextra -fsyntax-only`. GUARD_RAIL_IMPORT_ORDER
  below pins this down permanently and independently of the chunking/
  mock-Build harness, so if this ordering ever regresses again, this
  suite catches it even if nothing about the Build pipeline itself
  changed.

WHAT THIS SUITE ADDS ON TOP OF CELL 14
---------------------------------------
  1. IC2's mock text bug fixed (see above).
  2. N4_WhileLoop: a `while <cond> repeat ... end while` loop case,
     verified end-to-end (parse -> emit -> compile -> link -> RUN,
     output checked) -- loops were previously untested by either
     Cell 13 or Cell 14's 15 cases, and are a basic, common construct
     any real "easy-to-complex" program needs.
  3. GUARD_RAIL_IMPORT_ORDER: a standalone (non-chunked, no mock Build
     step) regression test that pins the IMPORT_C forward-declaration-
     ordering fix directly, so a regression is caught even if someone
     changes the chunking/Build harness independently of emit_c.py.
  4. Everything is still runnable with MOCK_MODE=True (fast, free,
     CI-suitable -- no GPU/model calls, ~1-2s total) or MOCK_MODE=False
     (real Qwen build against a loaded GGUF, for a periodic real-model
     check). Dictum-the-language is untouched: no new syntax was added
     anywhere to make any case pass; every fix here is either a mock-
     content correction or a verification that existing syntax already
     works.

HOW TO RUN
----------
  Fast CI gate (no model, seconds):        python3 regression_suite.py
  Real model check (Kaggle, GGUF present): set MOCK_MODE=False below
                                            and MODEL_PATH to the GGUF,
                                            then run as a Kaggle cell.
============================================================================
"""

import os, sys, json, subprocess, glob, shutil, re, math
from datetime import datetime, timezone
from collections import defaultdict, Counter

REPO_DIR = os.environ.get("DICTUM_REPO_DIR", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUT_PATH = os.environ.get("DICTUM_REGRESSION_OUT", "/tmp/regression_suite_results.json")

MODEL_PATH = os.environ.get("DICTUM_MODEL_PATH", "/kaggle/working/Qwen3.5-9B-Q4_K_M.gguf")
MOCK_MODE = os.environ.get("DICTUM_MOCK_MODE", "1") != "0"

MAX_ATTEMPTS_PER_CHUNK = 2
GEN_MAX_TOKENS = 200
N_CTX = 1024

# =====================================================================
# THINKING SUPPORT
# ---------------------------------------------------------------------
# Goal (per request): keep thinking ON for better output quality (Qwen3.5
# has a large context, so the token cost is cheap), but guarantee the
# code that reaches normalize/parse is clean -- no <think> text leaking
# into the compiler.
#
# The naive approach (generate freely, regex-strip "<think>...</think>"
# afterward) has a fatal ordering problem: grammar-constrained sampling
# (LlamaGrammar) constrains EVERY token from the start of the completion.
# If the grammar is the raw Dictum grammar, the model literally cannot
# emit a <think> block at all -- there's no "thinking happened but got
# stripped" state, thinking never happens in the first place. And if you
# drop the grammar to let it think, you lose the guarantee that what
# comes after is valid Dictum.
#
# Fix: wrap the grammar itself so <think>...</think> is PART of the
# grammar, as an optional prefix before the real (unmodified) Dictum
# rule. The model can write freely inside the think-block (any char
# except '<', matching this codebase's existing string-literal idiom
# `[^"\n<>]*"` in chunk_grammar.py), then MUST close it with the literal
# "</think>" before the grammar allows anything else -- at which point
# it drops into the exact same narrowed per-chunk grammar as before.
# One model call, real thinking, and the code half is still guaranteed
# grammar-valid by construction (not by hoping the strip regex works).
ENABLE_THINKING = os.environ.get("DICTUM_ENABLE_THINKING", "1") != "0"
THINK_MAX_TOKENS = int(os.environ.get("DICTUM_THINK_MAX_TOKENS", "400"))

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def strip_think_blocks(text):
    """Removes any *closed* <think>...</think> block. (With the grammar
    wrapper below, an unclosed block can't happen -- the grammar itself
    won't allow dictum-root content to start until </think> is emitted --
    so if generation is truncated mid-think by max_tokens, the result is
    an empty/incomplete string, which fails cleanly at the empty-output
    check below rather than silently feeding <think> text to the parser.)
    """
    return THINK_BLOCK_RE.sub("", text).strip()


def wrap_grammar_with_thinking(gbnf_text):
    """Renames the chunk grammar's `root` rule to `dictum-root` and
    installs a new `root` that optionally allows a <think> block first.
    No-op if `enable_thinking` is off or the grammar doesn't look like
    the expected shape (defensive: never silently corrupt a grammar we
    don't recognize)."""
    if not ENABLE_THINKING:
        return gbnf_text
    if not re.search(r"^root\s*::=", gbnf_text, re.M):
        return gbnf_text
    renamed = re.sub(r"^root(\s*::=)", r"dictum-root\1", gbnf_text, count=1, flags=re.M)
    preamble = (
        'root          ::= think-block? dictum-root\n'
        'think-block   ::= "<think>" think-char* "</think>" "\\n"*\n'
        'think-char    ::= [^<]\n\n'
    )
    return preamble + renamed


def make_prompt(text: str, system: str) -> str:
    """Qwen chat template for raw llm() completion with a grammar. Every
    turn is properly closed with <|im_end|> (the previous version of
    this had that token corrupted into a stray literal " oes", which
    left every turn boundary unterminated)."""
    return (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\n{text}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )


THINKING_SYSTEM_PROMPT = (
    "You are DictumBuild. Think through the plan items step by step inside "
    "<think>...</think> tags, then close the tag and output ONLY valid Dictum "
    "syntax after it -- no explanation outside the think block."
)
NO_THINKING_SYSTEM_PROMPT = (
    "You are DictumBuild. You translate structured plan items into Dictum "
    "code. Output only valid Dictum syntax."
)



def run_cmd(cmd, timeout=20, input_text=None, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            shell=isinstance(cmd, str), input=input_text, cwd=cwd)
        return {"returncode": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "TIMEOUT"}
    except Exception as e:
        return {"returncode": -2, "stdout": "", "stderr": str(e)}


def find_file(name, root=REPO_DIR):
    m = glob.glob(os.path.join(root, "**", name), recursive=True)
    return m[0] if m else None


# =====================================================================
# SECTION 1 -- chunking / pattern-match / retry-decision logic
# (unchanged from Cell 14)
# =====================================================================

CHUNK_TIER_ORDER = ["ARCHITECTURE", "TYPE", "INVARIANT", "OPERATION", "MODIFY", "MEMORY", "SAFETY"]
DEFAULT_CHUNK_TOKEN_BUDGET = 600
HOST_ACTION_RE = re.compile(r"^inside action (\w+),", re.I)
OPERATION_ACTION_RE = re.compile(r"^action (\w+)", re.I)


def plan_item_tier(category):
    cat = (category or "").upper()
    try:
        return CHUNK_TIER_ORDER.index(cat)
    except ValueError:
        return len(CHUNK_TIER_ORDER) - 1


def estimate_tokens(text):
    return math.ceil(len(text) / 3.5) if text else 0


def plan_item_text(item):
    return f"[PLAN: {item['category']} : {item['id']} : {item['desc']}]"


def pack_tier(items, max_tokens_per_chunk):
    if not items:
        return []
    chunks, current, current_tokens = [], [], 0
    for item in items:
        item_tokens = estimate_tokens(plan_item_text(item))
        if current and current_tokens + item_tokens > max_tokens_per_chunk:
            chunks.append(current)
            current, current_tokens = [], 0
        current.append(item)
        current_tokens += item_tokens
    if current:
        chunks.append(current)
    return chunks


def extract_host_action(invariant_desc):
    m = HOST_ACTION_RE.match(invariant_desc or "")
    return m.group(1) if m else None


def extract_operation_action(operation_desc):
    m = OPERATION_ACTION_RE.match(operation_desc or "")
    return m.group(1) if m else None


def build_chunks(plan, max_tokens_per_chunk=DEFAULT_CHUNK_TOKEN_BUDGET):
    if not plan:
        return []
    by_tier = defaultdict(list)
    invariant_items = []
    inv_tier_idx = CHUNK_TIER_ORDER.index("INVARIANT")
    for item in plan:
        tier = plan_item_tier(item["category"])
        if tier == inv_tier_idx:
            invariant_items.append(item)
            continue
        by_tier[tier].append(item)

    op_tier_idx = CHUNK_TIER_ORDER.index("OPERATION")
    modify_tier_idx = CHUNK_TIER_ORDER.index("MODIFY")
    operation_items = by_tier.get(op_tier_idx, []) + by_tier.get(modify_tier_idx, [])

    attached_to = defaultdict(list)
    unrouted_invariants = []
    for inv in invariant_items:
        host_action = extract_host_action(inv["desc"])
        match = None
        if host_action:
            for op in operation_items:
                if extract_operation_action(op["desc"]) == host_action:
                    match = op
                    break
        if match is not None:
            attached_to[id(match)].append(inv)
        else:
            unrouted_invariants.append(inv)
    if unrouted_invariants:
        by_tier[inv_tier_idx].extend(unrouted_invariants)

    chunks = []
    for tier in sorted(by_tier.keys()):
        items = by_tier[tier]
        tier_name = CHUNK_TIER_ORDER[tier] if tier < len(CHUNK_TIER_ORDER) else "OTHER"
        if tier_name in ("OPERATION", "MODIFY"):
            for item in items:
                attached = attached_to.get(id(item), [])
                chunks.append({"tierName": tier_name, "items": [item] + attached})
        else:
            for group in pack_tier(items, max_tokens_per_chunk):
                chunks.append({"tierName": tier_name, "items": group})
    return chunks


PATTERN_MATCH_RULES = [
    ("importc-raylib", re.compile(r"\braylib\b|initwindow|begindrawing|drawcircle|drawpixel|drawline", re.I), None),
    ("importc-math", re.compile(r"import from c", re.I), re.compile(r"\b(sqrt|cos|sin|floor|ceil|fabs|exp|log)\b", re.I)),
    ("atomic-increment", re.compile(r"atomic_faa|\batomic\b", re.I), None),
    ("unsafe-malloc", re.compile(r"raw_malloc|raw_free", re.I), None),
    ("pointer-ops", re.compile(r"raw pointer", re.I), None),
    ("while-loop", re.compile(r"\bwhile\b.*\brepeat\b", re.I), None),
    ("shape-actions", re.compile(r"\bshape\b", re.I), re.compile(r"\baction\b", re.I)),
    ("shape-declaration", re.compile(r"\bholds\b", re.I), None),
    ("hello-world", re.compile(r"\bprogram\b", re.I), None),
]


def match_pattern_ref(items):
    text = " ".join((it.get("desc") or "") for it in (items or []))
    if not text.strip():
        return None
    for ref, test, also in PATTERN_MATCH_RULES:
        if test.search(text) and (also is None or also.search(text)):
            return ref
    return None


def chunk_is_unsafe(chunk):
    text = " ".join((it.get("desc") or "") for it in chunk["items"])
    return bool(re.search(r"\bunsafe\b|RAW_MALLOC|RAW_FREE|ATOMIC_|CAS_|BARRIER|HAZARD|HP_", text, re.I))


def normalize_detail(detail):
    detail = re.sub(r"/tmp/[^\s:\"]+", "<tmp>", detail or "")
    detail = re.sub(r"\s+", " ", detail).strip()
    return detail


def attempt_signature(failed_at, detail):
    return f"{failed_at}|{normalize_detail(detail)}"


def decide_retry(last_signature, attempt_num, failed_at, detail):
    sig = attempt_signature(failed_at, detail)
    if last_signature is not None and sig == last_signature:
        return True, "stagnant", sig
    if attempt_num >= MAX_ATTEMPTS_PER_CHUNK:
        return True, "backstop", sig
    return False, "continue", sig


def extract_reserved_names(accumulated):
    by_name = {}
    for m in re.finditer(r"^\s*shape\s+(\w+)", accumulated, re.M):
        by_name.setdefault(m.group(1), "shape")
    for m in re.finditer(r"^\s*action\s+(\w+)", accumulated, re.M):
        by_name.setdefault(m.group(1), "action")
    for m in re.finditer(r"^\s+(\w+)\s+as\s+", accumulated, re.M):
        by_name.setdefault(m.group(1), "field")
    return [{"name": n, "kind": k} for n, k in by_name.items()]


def prepare_c_imports(chunk_grammar_py, chunk, accumulated):
    res = run_cmd(["python3", chunk_grammar_py, "--prepare-c-imports"], timeout=10,
                   input_text=json.dumps({"chunk": chunk, "accumulated": accumulated}))
    if res["returncode"] != 0:
        return [], []
    try:
        parsed = json.loads(res["stdout"])
        return parsed.get("import_lines", []), parsed.get("alias_names", [])
    except Exception:
        return [], []


def P(category, id_, desc):
    return {"category": category, "id": str(id_), "desc": desc}


# =====================================================================
# SECTION 2 -- suites
# =====================================================================

GAP_SUITE = [
    {"name": "E1_HelloWorld", "difficulty": "easy", "tags": ["print"],
     "plan": [P("ARCHITECTURE", 1, "program HelloWorld - print the text Hello and newline")]},

    {"name": "E2_ShapeDeclOnly", "difficulty": "easy", "tags": ["shape"],
     "plan": [P("TYPE", 1, "shape Point holds x as whole number, y as whole number")]},

    {"name": "N1_ArithmeticSum", "difficulty": "normal", "tags": ["arithmetic"],
     "plan": [
         P("ARCHITECTURE", 1, "program AddTwo - keeps a whole number Result with value 0, "
                               "calls action add_two giving Result, and prints Result"),
         P("OPERATION", 2, "action add_two takes A as whole number and B as whole number produces whole number - "
                            "produce success with the sum of A and B"),
     ]},

    {"name": "N2_IfElse", "difficulty": "normal", "tags": ["conditional"],
     "plan": [
         P("ARCHITECTURE", 1, "program CheckPositive - calls action classify with a whole number and prints the result"),
         P("OPERATION", 2, "action classify takes N as whole number produces nothing - "
                            "if N is greater than 0 then: print the text \"positive\" and newline "
                            "otherwise: print the text \"non-positive\" and newline end if"),
     ]},

    {"name": "N3_AtomicIncrement", "difficulty": "normal", "tags": ["unsafe", "atomic", "deterministic_candidate"],
     "plan": [P("SAFETY", 1, "action increment - unsafe block contains [ATOMIC_FAA: Counter : 1 : NewValue]")]},

    # NEW: loops were untested by Cell 13/14's 15 cases. Verified directly
    # against dictumc_cli.py (parse -> emit -> compile -> link -> RUN,
    # output checked to be "01234") before being added here.
    {"name": "N4_WhileLoop", "difficulty": "normal", "tags": ["loop", "while"],
     "plan": [P("ARCHITECTURE", 1,
                "program CountToFive - keep N as whole number with value 0, "
                "while N is less than 5 repeat: print the text N and set N to the sum of N and 1 end while")],
     "expected_stdout": "01234"},

    {"name": "C1_InvariantFieldAccess", "difficulty": "complex",
     "tags": ["invariant_routing", "field_access", "shape", "operation"],
     "plan": [
         P("TYPE", 1, "shape World holds width as whole number, height as whole number"),
         P("TYPE", 2, "shape Player holds pos_x as whole number, pos_y as whole number"),
         P("INVARIANT", 3, "inside action move, before \"set P.pos_x to new_x\": "
                            "reject if new_x is less than 0 or new_x is at least W.width"),
         P("OPERATION", 4, "action move takes P as Player and W as World and new_x as whole number "
                            "produces nothing - if new_x is less than 0 then: "
                            "produce failure with text \"out of bounds\" end if, "
                            "if new_x is at least W.width then: "
                            "produce failure with text \"out of bounds\" end if, "
                            "set P.pos_x to new_x"),
     ]},

    {"name": "C2_CompareAndSwap", "difficulty": "complex", "tags": ["cas", "bracket_token", "safety"],
     "plan": [
         P("TYPE", 1, "shape Counter holds value as whole number"),
         P("OPERATION", 2, "action bump takes Expected as whole number and Desired as whole number produces nothing - "
                            "unsafe block contains [CAS_LOOP_32: value_ptr : Expected : Desired : Success]"),
     ]},

    {"name": "C3_CallTargetAmbiguity", "difficulty": "complex",
     "tags": ["call_target", "shape_name_collision"],
     "plan": [
         P("TYPE", 1, "shape Account holds balance as whole number, owner as text"),
         P("OPERATION", 2, "action log_balance takes A as Account produces nothing - "
                            "print the text \"logged\""),
         P("OPERATION", 3, "action main takes Acc as Account produces nothing - "
                            "assert Acc.balance is greater than 0, call log_balance with Acc"),
     ]},

    {"name": "C4_DualInvariantSameShape", "difficulty": "complex",
     "tags": ["invariant_routing", "multi_action", "field_access"],
     "plan": [
         P("TYPE", 1, "shape Account holds balance as whole number"),
         P("INVARIANT", 2, "inside action withdraw, before \"set Account.balance to new_balance\": "
                            "reject if amount is greater than Account.balance"),
         P("INVARIANT", 3, "inside action deposit, before \"set Account.balance to new_balance\": "
                            "reject if amount is less than 0"),
         P("OPERATION", 4, "action withdraw takes amount as whole number produces nothing - "
                            "if amount is greater than 100 then: produce failure with text \"insufficient\" end if"),
         P("OPERATION", 5, "action deposit takes amount as whole number produces nothing - "
                            "if amount is less than 0 then: produce failure with text \"invalid\" end if"),
     ]},

    {"name": "V1_HazardPointer", "difficulty": "novel", "tags": ["hazard", "bracket_token", "safety"],
     "plan": [
         P("TYPE", 1, "shape Node holds data as whole number"),
         P("OPERATION", 2, "action read_node takes nothing produces nothing - "
                            "unsafe block contains [HP_PROTECT: record : NodePtr], "
                            "[HP_READ: record : NodePtr : Data], [HP_CLEAR: record]"),
     ]},

    {"name": "V2_HeterogeneousOperationChunk", "difficulty": "novel",
     "tags": ["role_scoped_grammar", "heterogeneous_signatures"],
     "plan": [
         P("ARCHITECTURE", 1, "shape Item holds label as text, count as whole number"),
         P("ARCHITECTURE", 2, "action greet takes nothing produces nothing"),
         P("ARCHITECTURE", 3, "action scale takes Factor as whole number produces whole number - "
                               "produce success with the product of Factor and 2"),
     ]},

    {"name": "V3_UnrecognizedCategoryFallback", "difficulty": "novel",
     "tags": ["unrecognized_category", "tier_fallback_stress"],
     "plan": [
         P("ARCHITECTURE", 1, "program Combined - keep Status as text with value ready, print the text Status"),
         P("UTILITY", 2, "action helper takes nothing produces nothing"),
         P("SAFETY", 3, "action helper - unsafe block contains [RAW_MALLOC: 64 : Scratch], [RAW_FREE: Scratch]"),
     ]},
]

IMPORTC_SUITE = [
    {"name": "IC1_SingleImport", "difficulty": "easy", "tags": ["import_c"],
     "plan": [P("OPERATION", 1, "action root takes X as whole number produces whole number - "
                                 "import from c the math function sqrt, produce success with the sqrt of X")]},

    # BUG FIX (this file): was "the floor of the quotient of the sum of
    # A and B by 2" -- parser.py's quotient grammar requires "and", not
    # "by" ("by" only follows "divided"). Confirmed via dictumc_cli.py in
    # isolation: "Expected ('and',), got 'by'". Fixed to "and", verified
    # to parse to `(A + B) / 2` and compile cleanly.
    {"name": "IC2_TwoCooperatingImports", "difficulty": "normal", "tags": ["import_c"],
     "plan": [
         P("OPERATION", 1, "action safe_sqrt_sum takes A as whole number and B as whole number produces whole number - "
                            "import from c the math function sqrt, produce success with the sqrt of the sum of A and B"),
         P("OPERATION", 2, "action rounded_average takes A as whole number and B as whole number produces whole number - "
                            "import from c the math function floor, produce success with the floor of the quotient of the sum of A and B and 2"),
     ]},

    {"name": "IC3_ThreeComposedImports", "difficulty": "complex", "tags": ["import_c"],
     "plan": [P("OPERATION", 1, "action geometry_metric takes X as whole number produces whole number - "
                                 "import from c the math functions sqrt, fabs, and floor, "
                                 "produce success with the floor of the sqrt of the fabs of X")]},
]

ALL_SUITES = {"gap": GAP_SUITE, "importc": IMPORTC_SUITE}


# =====================================================================
# SECTION 3 -- mock Build (only used when MOCK_MODE=True)
# =====================================================================

def symbol_context(accumulated_source):
    shapes = re.findall(r"^\s*shape\s+(\w+)", accumulated_source, re.M)
    actions = re.findall(r"^\s*action\s+(\w+)", accumulated_source, re.M)
    if not shapes and not actions:
        return ""
    parts = []
    if shapes:
        parts.append("Already-defined shapes: " + ", ".join(shapes))
    if actions:
        parts.append("Already-defined actions: " + ", ".join(actions))
    return "\n".join(parts)


def try_expansions(chunk, pattern_graph_py):
    res = run_cmd(["python3", pattern_graph_py, "--bridge"], timeout=10,
                   input_text=json.dumps({"chunk": chunk}))
    try:
        parsed = json.loads(res["stdout"])
        if parsed.get("ok") and parsed.get("expansion") == "sequential":
            return "sequential", parsed.get("bound")
    except Exception:
        pass
    pattern_ref = match_pattern_ref(chunk["items"])
    if pattern_ref:
        combined_text = " ".join(it.get("desc", "") for it in chunk["items"])
        res = run_cmd(["python3", pattern_graph_py, "--bridge"], timeout=10,
                       input_text=json.dumps({"pattern_ref": pattern_ref, "plan_text": combined_text}))
        try:
            parsed = json.loads(res["stdout"])
            if parsed.get("ok") and parsed.get("deterministic"):
                return "pattern", parsed.get("bound")
        except Exception:
            pass
    return None, None


def _mock_generate(chunk):
    parts = []
    for it in chunk["items"]:
        text = it["desc"]

        if "program HelloWorld" in text:
            parts.append("program HelloWorld:\n    print the text \"Hello\" and newline\nend program")
        elif "shape Point holds" in text:
            parts.append("shape Point holds\n    x as whole number\n    y as whole number\nend shape")
        elif "program AddTwo" in text:
            parts.append("program AddTwo:\n    keep Result as whole number with value 0\n"
                          "    call add_two with 2 and 3 giving Result\n    print the text Result\nend program")
        elif "action add_two takes" in text:
            parts.append("action add_two takes A as whole number and B as whole number produces whole number:\n"
                          "    produce success with the sum of A and B\nend action")
        elif "program CheckPositive" in text:
            parts.append("program CheckPositive:\n    call classify with 5\nend program")
        elif "action classify takes" in text:
            parts.append("action classify takes N as whole number produces nothing:\n"
                          "    if N is greater than 0 then:\n        print the text \"positive\" and newline\n"
                          "    otherwise:\n        print the text \"non-positive\" and newline\n    end if\nend action")
        elif "ATOMIC_FAA" in text:
            parts.append("action increment:\n    keep Counter as whole number with value 0\n"
                          "    unsafe:\n        [ATOMIC_FAA: Counter : 1 : NewValue]\n    end unsafe\nend action")
        elif "program CountToFive" in text:
            parts.append("program CountToFive:\n    keep N as whole number with value 0\n"
                          "    while N is less than 5 repeat\n        print the text N\n"
                          "        set N to the sum of N and 1\n    end while\nend program")
        elif "shape World holds" in text:
            parts.append("shape World holds\n    width as whole number\n    height as whole number\nend shape")
        elif "shape Player holds" in text:
            parts.append("shape Player holds\n    pos_x as whole number\n    pos_y as whole number\nend shape")
        elif "action move takes" in text:
            parts.append("action move takes P as Player and W as World and new_x as whole number produces nothing:\n"
                          "    if new_x is less than 0 then:\n        produce failure with text \"out of bounds\"\n    end if\n"
                          "    if new_x is at least W.width then:\n        produce failure with text \"out of bounds\"\n    end if\n"
                          "    set P.pos_x to new_x\nend action")
        elif "shape Counter holds" in text:
            parts.append("shape Counter holds\n    value as whole number\nend shape")
        elif "action bump takes" in text:
            parts.append("action bump takes Expected as whole number and Desired as whole number produces nothing:\n"
                          "    keep value_ptr as raw pointer to whole number with value &Expected\n"
                          "    keep Success as truth value with value false\n"
                          "    unsafe:\n        [CAS_LOOP_32: value_ptr : Expected : Desired : Success]\n"
                          "        [BARRIER_SEQ_CST]\n    end unsafe\nend action")
        elif "shape Account holds" in text:
            parts.append("shape Account holds\n    balance as whole number\n    owner as text\nend shape")
        elif "action log_balance takes" in text:
            parts.append("action log_balance takes A as Account produces nothing:\n    print the text \"logged\"\nend action")
        elif "action main takes Acc" in text:
            parts.append("action main takes Acc as Account produces nothing:\n"
                          "    assert Acc.balance is greater than 0\n    call log_balance with Acc\nend action")
        elif "action withdraw takes" in text:
            parts.append("action withdraw takes amount as whole number produces nothing:\n"
                          "    if amount is greater than 100 then:\n        produce failure with text \"insufficient\"\n    end if\nend action")
        elif "action deposit takes" in text:
            parts.append("action deposit takes amount as whole number produces nothing:\n"
                          "    if amount is less than 0 then:\n        produce failure with text \"invalid\"\n    end if\nend action")
        elif "shape Node holds" in text:
            parts.append("shape Node holds\n    data as whole number\nend shape")
        elif "action read_node takes" in text:
            parts.append("action read_node takes nothing produces nothing:\n"
                          "    keep Slot as whole number with value 0\n"
                          "    keep record as raw pointer to whole number with value &Slot\n"
                          "    keep Target as whole number with value 0\n"
                          "    keep NodePtr as raw pointer to whole number with value &Target\n"
                          "    keep Data as whole number with value 0\n"
                          "    unsafe:\n        [HP_PROTECT: record : NodePtr]\n"
                          "        [HP_READ: record : NodePtr : Data]\n"
                          "        [HP_CLEAR: record : NodePtr]\n    end unsafe\nend action")
        elif "shape Item holds" in text:
            parts.append("shape Item holds\n    label as text\n    count as whole number\nend shape")
        elif "action greet takes" in text:
            parts.append("action greet takes nothing produces nothing:\n    print the text \"hi\" and newline\nend action")
        elif "action scale takes" in text:
            parts.append("action scale takes Factor as whole number produces whole number:\n"
                          "    produce success with the product of Factor and 2\nend action")
        elif "program Combined" in text:
            parts.append("program Combined:\n    keep Status as text with value \"ready\"\n    print the text Status\nend program")
        elif "action helper takes" in text:
            parts.append("action helper takes nothing produces nothing:\n    print the text \"helping\" and newline\nend action")
        elif "RAW_MALLOC" in text:
            parts.append("action helper:\n    unsafe:\n        [RAW_MALLOC: 64 : Scratch]\n"
                          "        [RAW_FREE: Scratch]\n    end unsafe\nend action")
        elif "action root takes" in text:
            parts.append("action root takes X as whole number produces whole number:\n"
                          "    call c_sqrt with X giving Result\n    produce success with Result\nend action")
        elif "safe_sqrt_sum takes" in text:
            parts.append("action safe_sqrt_sum takes A as whole number and B as whole number produces whole number:\n"
                          "    keep Sum as whole number with value the sum of A and B\n"
                          "    call c_sqrt with Sum giving Result\n    produce success with Result\nend action")
        elif "rounded_average takes" in text:
            parts.append("action rounded_average takes A as whole number and B as whole number produces whole number:\n"
                          "    keep Half as whole number with value the quotient of the sum of A and B and 2\n"
                          "    call c_floor with Half giving Result\n    produce success with Result\nend action")
        elif "geometry_metric takes" in text:
            parts.append("action geometry_metric takes X as whole number produces whole number:\n"
                          "    call c_fabs with X giving Step1\n    call c_sqrt with Step1 giving Step2\n"
                          "    call c_floor with Step2 giving Result\n    produce success with Result\nend action")

    return "\n" + "\n".join(parts) + "\n" if parts else ""


def _join_chunk(accumulated, generated_text):
    if not accumulated:
        return generated_text
    if re.search(r"\s$", accumulated) or re.match(r"^\s", generated_text):
        return accumulated + generated_text
    return accumulated + "\n\n" + generated_text


class Paths:
    def __init__(self):
        self.CHUNK_GRAMMAR_PY = find_file("chunk_grammar.py")
        self.PATTERN_GRAPH_PY = find_file("pattern_graph.py")
        self.NORMALIZE_DICTUM_PY = find_file("normalize_dictum.py")
        self.TRANSPILER_CLI = find_file("dictumc_cli.py")
        self.VALIDATOR_JS = find_file("validator.js")
        guess = find_file("dictum_core.h")
        self.RUNTIME_DIR = os.path.dirname(guess) if guess else None


def build_one_chunk(paths, chunk, accumulated_source, chunk_log, llm=None, grammar_cache=None):
    pattern_ref = match_pattern_ref(chunk["items"])
    unsafe = chunk_is_unsafe(chunk)

    expansion_path, bound = try_expansions(chunk, paths.PATTERN_GRAPH_PY)
    if expansion_path:
        chunk_log["expansion_path"] = expansion_path
        return True, bound, 0, expansion_path, None

    chunk_log["expansion_path"] = "model"

    import_lines, alias_names = prepare_c_imports(paths.CHUNK_GRAMMAR_PY, chunk, accumulated_source)
    working_source = accumulated_source
    if import_lines:
        # BUG FIX (this file, found while auditing Cell 14): this used to
        # be `working_source + "\n".join(import_lines) + "\n\n"` -- a bare
        # concatenation with NO separator check against accumulated_source.
        # _mock_generate()'s output is `.strip()`-ed by the caller before
        # being joined into accumulated (see the `raw = ...strip()` line
        # below and in run_suite), so accumulated_source frequently ends
        # with no trailing newline at all (e.g. "...end action"). Bare
        # concatenation then produced literal glued tokens like
        # "end actionimport from C the action floor...", which the lexer
        # reads as a single malformed identifier -- a real, reproducible
        # parse error, confirmed directly against dictumc_cli.py, and
        # entirely a harness bug (missing separator), not a compiler bug.
        # This was the actual cause of IC2's "attempt 2 == attempt 1"
        # stagnation in Cell 14: retrying the SAME correct model output
        # against a BROKEN join can never succeed. Reuse _join_chunk's
        # safe-separator logic instead of ad hoc concatenation.
        working_source = _join_chunk(working_source, "\n".join(import_lines) + "\n\n")
        chunk_log["c_imports"] = alias_names

    reserved_names = extract_reserved_names(working_source)
    chunk_log["reserved_names"] = reserved_names

    grammar_payload = dict(chunk)
    grammar_payload["reserved_names"] = reserved_names
    grammar_payload["extra_identifiers"] = alias_names
    grammar_payload["unsafe"] = unsafe

    gres = run_cmd(["python3", paths.CHUNK_GRAMMAR_PY], input_text=json.dumps(grammar_payload), timeout=10)
    if gres["returncode"] != 0:
        return False, None, 0, "model", "grammar_gen"
    gbnf = gres["stdout"]

    grammar = None
    if not MOCK_MODE:
        try:
            from llama_cpp.llama import LlamaGrammar
            grammar = LlamaGrammar.from_string(wrap_grammar_with_thinking(gbnf))
        except Exception:
            return False, None, 0, "model", "grammar_load"

    last_signature, attempt, feedback = None, 0, ""
    while True:
        attempt += 1
        if MOCK_MODE:
            raw = _mock_generate(chunk).strip()
        else:
            plan_text = "\n".join(plan_item_text(it) for it in chunk["items"])
            ctx = symbol_context(working_source)
            base_prompt = f"Implement this part of the plan in Dictum:\n\n{plan_text}\n\nWrite only the code."
            if ctx:
                base_prompt = f"{ctx}\n\n{base_prompt}"
            prompt = base_prompt + (f"\n\nYour previous attempt was wrong: {feedback}" if feedback else "")
            system = THINKING_SYSTEM_PROMPT if ENABLE_THINKING else NO_THINKING_SYSTEM_PROMPT
            formatted = make_prompt(prompt, system=system)
            # BUG FIX (found auditing a hand-edited copy of this cell): the
            # old stop list included "\n\n". Chain-of-thought reasoning
            # routinely contains blank lines between points, so that stop
            # sequence would cut generation off mid-<think>, before any
            # code was ever produced -- every single thinking-enabled call
            # would truncate early. Stop ONLY on the real turn-end token;
            # the grammar wrapper is what bounds the output shape now, not
            # a guessed stop string. Budget covers thinking + code -- cheap
            # on Qwen3.5's large context.
            gen_max_tokens = GEN_MAX_TOKENS + (THINK_MAX_TOKENS if ENABLE_THINKING else 0)
            try:
                output = llm(formatted, grammar=grammar, max_tokens=gen_max_tokens, temperature=0.05,
                             stop=["<|im_end|>"])
                raw = output["choices"][0]["text"]
            except Exception as e:
                should_stop, reason, last_signature = decide_retry(last_signature, attempt, "generation", str(e)[:200])
                if should_stop:
                    chunk_log["retries_used"] = attempt - 1
                    return False, None, attempt - 1, "model", "generation"
                continue
            # Strip the (grammar-guaranteed-closed) <think> block before
            # anything downstream ever sees it. If generation ran out of
            # budget mid-think, dictum-root never started -- raw strips
            # down to "" and fails cleanly at the empty-output check below,
            # rather than feeding <think> text to normalize/parse.
            raw = strip_think_blocks(raw)
            if not raw:
                should_stop, reason, last_signature = decide_retry(
                    last_signature, attempt, "generation", "empty output after stripping <think> block")
                if should_stop:
                    chunk_log["retries_used"] = attempt - 1
                    return False, None, attempt - 1, "model", "generation"
                feedback = "you ran out of budget while still thinking -- keep the reasoning shorter"
                continue

        chunk_log.setdefault("raw_outputs", []).append({"attempt": attempt, "raw": raw})

        norm_res = run_cmd(["python3", paths.NORMALIZE_DICTUM_PY, "--bridge"], timeout=10,
                            input_text=json.dumps({"code": raw, "plan_items": chunk["items"]}))
        code = raw
        try:
            norm = json.loads(norm_res["stdout"])
            if norm.get("ok"):
                code = norm["code"]
        except Exception:
            pass

        trial_source = _join_chunk(working_source, code)
        tmp_path = "/tmp/_chunk_trial.dictum"
        with open(tmp_path, "w") as f:
            f.write(trial_source)
        parse_res = run_cmd(["python3", paths.TRANSPILER_CLI, tmp_path, "--emit-ast"], timeout=10)
        parsed_ok = parse_res["returncode"] == 0 and "error" not in parse_res["stderr"].lower()
        if parsed_ok:
            chunk_log["retries_used"] = attempt - 1
            return True, (working_source, code), attempt - 1, "model", None

        failed_at, detail = "parse", parse_res["stderr"][:300]
        should_stop, reason, last_signature = decide_retry(last_signature, attempt, failed_at, detail)
        if should_stop:
            chunk_log["retries_used"] = attempt - 1
            chunk_log["stop_reason"] = reason
            return False, None, attempt - 1, "model", failed_at
        feedback = detail


def run_l2_l3(node_ok, validator_js, source, plan):
    if not node_ok:
        return {"ok": False, "error": "node not available"}
    bridge_js = "/tmp/l2l3_bridge.js"
    with open(bridge_js, "w") as f:
        f.write(
            "const VALIDATOR_PATH = process.argv[2];\n"
            "let checkL2Structural, checkL3;\n"
            "try { ({ checkL2Structural, checkL3 } = require(VALIDATOR_PATH)); }\n"
            "catch (e) { process.stdout.write(JSON.stringify({ ok:false, error:'require failed: '+String(e&&e.stack||e) })); process.exit(0); }\n"
            "let raw=''; process.stdin.on('data', d => raw+=d);\n"
            "process.stdin.on('end', () => {\n"
            "  try {\n"
            "    const payload = JSON.parse(raw);\n"
            "    const l2 = checkL2Structural(payload.source||'', payload.plan||[]);\n"
            "    const l3 = checkL3(payload.source||'');\n"
            "    process.stdout.write(JSON.stringify({ ok:true, l2, l3 }));\n"
            "  } catch (e) { process.stdout.write(JSON.stringify({ ok:false, error:String(e&&e.stack||e) })); }\n"
            "});\n"
        )
    res = run_cmd(["node", bridge_js, validator_js], timeout=10,
                   input_text=json.dumps({"source": source, "plan": plan}))
    try:
        parsed = json.loads(res["stdout"])
        if not parsed.get("ok"):
            parsed.setdefault("error", res["stderr"][:300] or "bridge ok:false, no message")
        return parsed
    except Exception as e:
        return {"ok": False, "error": f"{e}: stdout={res['stdout'][:200]!r}"}


def review_program(paths, node_ok, c_flags, c_link_flags, source, plan, tag, expected_stdout=None):
    review = {"l2": None, "l3": None, "parsed": None, "emitted": None, "compiled": None,
              "linked": None, "ran": None, "compile_error": None,
              "l2_l3_error": None, "l2_l3_status": "not_run", "stdout_match": None}

    l2l3 = run_l2_l3(node_ok, paths.VALIDATOR_JS, source, plan)
    if l2l3.get("ok"):
        review["l2"] = l2l3["l2"]
        review["l3"] = l2l3["l3"]
        review["l2_l3_status"] = "ran"
    else:
        review["l2_l3_error"] = l2l3.get("error")
        review["l2_l3_status"] = "unavailable"

    dpath = f"/tmp/prog_{tag}.dictum"
    with open(dpath, "w") as f:
        f.write(source)

    parse_res = run_cmd(["python3", paths.TRANSPILER_CLI, dpath, "--emit-ast"], timeout=10)
    review["parsed"] = parse_res["returncode"] == 0 and "error" not in parse_res["stderr"].lower()
    if not review["parsed"]:
        review["compile_error"] = parse_res["stderr"][:300]
        return review

    cpath = f"/tmp/prog_{tag}.c"
    emit_res = run_cmd(["python3", paths.TRANSPILER_CLI, dpath, "--backend", "c", "--output", cpath], timeout=10)
    review["emitted"] = emit_res["returncode"] == 0 and os.path.exists(cpath)
    if not review["emitted"]:
        review["compile_error"] = emit_res["stderr"][:300]
        return review

    syn_res = run_cmd(["cc"] + c_flags + [cpath], timeout=15)
    review["compiled"] = syn_res["returncode"] == 0
    if not review["compiled"]:
        review["compile_error"] = syn_res["stderr"][:400]
        return review

    has_program = re.search(r"^\s*program\s+\w+", source, re.M) is not None
    review["shell"] = "program" if has_program else "library"
    if has_program:
        exe = f"/tmp/prog_{tag}"
        link_res = run_cmd(["cc"] + c_link_flags + [cpath, "-o", exe], timeout=15)
        review["linked"] = link_res["returncode"] == 0 and os.path.exists(exe)
        review["link_error"] = link_res["stderr"][:300] if not review["linked"] else None
        if review["linked"]:
            run_res = run_cmd([exe], timeout=5)
            review["ran"] = run_res["returncode"] == 0
            review["run_error"] = run_res["stderr"][:300] if not review["ran"] else None
            if expected_stdout is not None:
                review["stdout_match"] = run_res["stdout"] == expected_stdout
                review["actual_stdout"] = run_res["stdout"]
    else:
        review["linked"] = review["ran"] = None

    return review


def run_suite(paths, node_ok, c_flags, c_link_flags, suite_name, cases, verbose=True):
    if verbose:
        print("\n" + "=" * 70)
        print(f"RUNNING SUITE: {suite_name} ({len(cases)} cases)")
        print("=" * 70)
    results = []
    for tc in cases:
        if verbose:
            print(f"\n--- {tc['name']} ({tc['difficulty']}) ---")
        chunks = build_chunks(tc["plan"])
        accumulated = ""
        chunk_details = []
        build_failed = False

        for chunk in chunks:
            chunk_log = {"tierName": chunk["tierName"], "item_ids": [it["id"] for it in chunk["items"]],
                         "expansion_path": None, "retries_used": 0, "stop_reason": None}
            pre_source = accumulated
            ok, generated, retries, path, failed_at = build_one_chunk(paths, chunk, accumulated, chunk_log)
            chunk_log["ok"] = ok
            chunk_log["failed_at"] = failed_at
            if ok:
                if isinstance(generated, tuple):
                    working_source, code = generated
                    accumulated = _join_chunk(working_source, code)
                else:
                    accumulated = _join_chunk(accumulated, generated)
                status = f"OK [{path}]" + (f" ({retries} retries)" if retries else "")
            else:
                accumulated = pre_source
                build_failed = True
                status = f"FAIL@{failed_at} [{path}] after {retries} retries"
            if verbose:
                print(f"  chunk[{chunk['tierName']:<12}]: {status}")
            chunk_details.append(chunk_log)

        review = review_program(paths, node_ok, c_flags, c_link_flags, accumulated, tc["plan"], tc["name"],
                                 expected_stdout=tc.get("expected_stdout"))
        l2 = review.get("l2")
        l3 = review.get("l3")
        review_ran = review.get("l2_l3_status") == "ran"
        l2_all_ok = review_ran and len((l2 or {}).get("failed", [])) == 0
        l3_clean = review_ran and len(l3 or []) == 0
        gate_ok = review["parsed"] and review["emitted"] and review["compiled"] \
            and review["linked"] is not False and review["ran"] is not False \
            and review.get("stdout_match") is not False

        if not review_ran:
            overall = (not build_failed) and gate_ok
            overall_note = "review_unavailable (compile gate only)"
        else:
            overall = (not build_failed) and l2_all_ok and l3_clean and gate_ok
            overall_note = "full"

        total_retries = sum(c["retries_used"] for c in chunk_details)
        if verbose:
            print(f"  Review: L2 status={review.get('l2_l3_status')} "
                  f"failed={len((l2 or {}).get('failed', []))} L3={len(l3 or [])} gate_ok={gate_ok}")
            if review.get("compile_error"):
                print(f"  Compile error: {review['compile_error'][:200]}")
            print(f"  -> {'PASS' if overall else 'FAIL'} ({overall_note}) (retries: {total_retries})")

        results.append({
            "suite": suite_name, "name": tc["name"], "difficulty": tc["difficulty"], "tags": tc["tags"],
            "chunks": chunk_details, "build_failed": build_failed,
            "review": review, "total_retries": total_retries, "overall": overall,
            "overall_note": overall_note, "final_source": accumulated,
        })
    return results


# =====================================================================
# SECTION 4 -- GUARD_RAIL_IMPORT_ORDER: standalone regression pin for
# the IMPORT_C forward-declaration-ordering bug, independent of
# chunking/mock-Build. If emit_c.py's get_output() ever regresses on
# this, this test fails even if the Build/chunking harness is untouched.
# =====================================================================

GUARD_RAIL_SOURCE = """import from C the action sqrt takes decimal number produces decimal number as c_sqrt
import from C the action fabs takes decimal number produces decimal number as c_fabs
import from C the action floor takes decimal number produces decimal number as c_floor

action geometry_metric takes X as whole number produces whole number:
    call c_fabs with X giving Step1
    call c_sqrt with Step1 giving Step2
    call c_floor with Step2 giving Result
    produce success with Result
end action
"""


GUARD_RAIL_VAR_SCOPE_SOURCE = """action first_calc takes X as whole number produces whole number:
    keep Temp as whole number with value the sum of X and 1
    keep Result as whole number with value the product of Temp and 2
    produce success with Result
end action

action second_calc takes Y as whole number produces whole number:
    keep Result as whole number with value the sum of Y and 100
    produce success with Result
end action
"""


def run_guard_rail_action_var_scope(paths, c_flags, verbose=True):
    """Pins the cross-action declared_vars leakage bug found while
    debugging IC2_TwoCooperatingImports: two sibling actions in the same
    file that each independently declare a local of the SAME name
    (`Result` here) must each get their own `int32_t Result = ...;`
    declaration -- CEmitter.declared_vars must not leak state from one
    action's C scope into the next action's. Before the fix in
    emit_node's Action branch (snapshot/restore the WHOLE declared_vars
    dict, not just param names), the second action's `Result` compiled
    to a bare `Result = ...;` with no type, since declared_vars still
    had `Result` from the first action -- a real, confirmed "'Result'
    undeclared" compile error. Standalone and non-chunked, independent of
    the mock Build harness."""
    if verbose:
        print("\n" + "=" * 70)
        print("GUARD_RAIL_ACTION_VAR_SCOPE (standalone, non-chunked)")
        print("=" * 70)
    dpath = "/tmp/guard_rail_var_scope.dictum"
    cpath = "/tmp/guard_rail_var_scope.c"
    with open(dpath, "w") as f:
        f.write(GUARD_RAIL_VAR_SCOPE_SOURCE)
    emit_res = run_cmd(["python3", paths.TRANSPILER_CLI, dpath, "--backend", "c", "--output", cpath], timeout=10)
    emitted = emit_res["returncode"] == 0 and os.path.exists(cpath)
    if not emitted:
        if verbose:
            print(f"  -> FAIL (emit): {emit_res['stderr'][:300]}")
        return {"name": "GUARD_RAIL_ACTION_VAR_SCOPE", "overall": False, "stage": "emit",
                "error": emit_res["stderr"][:300]}
    syn_res = run_cmd(["cc"] + c_flags + [cpath], timeout=15)
    ok = syn_res["returncode"] == 0
    if verbose:
        print(f"  -> {'PASS' if ok else 'FAIL'} (compile, -Wall -Wextra -fsyntax-only)")
        if not ok:
            print(f"     {syn_res['stderr'][:400]}")
    return {"name": "GUARD_RAIL_ACTION_VAR_SCOPE", "overall": ok, "stage": "compile",
            "error": None if ok else syn_res["stderr"][:400]}


def run_guard_rail_import_order(paths, c_flags, verbose=True):
    if verbose:
        print("\n" + "=" * 70)
        print("GUARD_RAIL_IMPORT_ORDER (standalone, non-chunked)")
        print("=" * 70)
    dpath = "/tmp/guard_rail_import_order.dictum"
    cpath = "/tmp/guard_rail_import_order.c"
    with open(dpath, "w") as f:
        f.write(GUARD_RAIL_SOURCE)
    emit_res = run_cmd(["python3", paths.TRANSPILER_CLI, dpath, "--backend", "c", "--output", cpath], timeout=10)
    emitted = emit_res["returncode"] == 0 and os.path.exists(cpath)
    if not emitted:
        if verbose:
            print(f"  -> FAIL (emit): {emit_res['stderr'][:300]}")
        return {"name": "GUARD_RAIL_IMPORT_ORDER", "overall": False, "stage": "emit",
                "error": emit_res["stderr"][:300]}
    syn_res = run_cmd(["cc"] + c_flags + [cpath], timeout=15)
    ok = syn_res["returncode"] == 0
    if verbose:
        print(f"  -> {'PASS' if ok else 'FAIL'} (compile, -Wall -Wextra -fsyntax-only)")
        if not ok:
            print(f"     {syn_res['stderr'][:400]}")
    return {"name": "GUARD_RAIL_IMPORT_ORDER", "overall": ok, "stage": "compile",
            "error": None if ok else syn_res["stderr"][:400]}


# =====================================================================
# SECTION 5 -- main
# =====================================================================

def main():
    print("=" * 70)
    print("CELL 15: Regression suite (Cell 14 + IC2 content fix + loop case + guard rail)")
    print(f"MOCK_MODE={MOCK_MODE}  REPO_DIR={REPO_DIR}")
    print(f"cases={len(GAP_SUITE)} (gap) + {len(IMPORTC_SUITE)} (import_c) + 2 (guard rails)")
    print("=" * 70)

    paths = Paths()
    for label, p in [("chunk_grammar.py", paths.CHUNK_GRAMMAR_PY), ("pattern_graph.py", paths.PATTERN_GRAPH_PY),
                     ("normalize_dictum.py", paths.NORMALIZE_DICTUM_PY), ("dictumc_cli.py", paths.TRANSPILER_CLI),
                     ("validator.js", paths.VALIDATOR_JS)]:
        print(f"{label:<20} {'OK' if p else 'MISSING'}")
        if not p:
            print("FATAL: a required repo script is missing, aborting.")
            sys.exit(1)

    node_ok = shutil.which("node") is not None
    print(f"node (for L2/L3 review): {'OK' if node_ok else 'MISSING'}")

    c_flags = ["-std=c11", "-Wall", "-Wextra", "-fsyntax-only"] + ([f"-I{paths.RUNTIME_DIR}"] if paths.RUNTIME_DIR else [])
    c_link_flags = ["-std=c11", "-Wall", "-Wextra"] + ([f"-I{paths.RUNTIME_DIR}"] if paths.RUNTIME_DIR else []) + ["-lm"]

    if not MOCK_MODE and not os.path.exists(MODEL_PATH):
        print(f"ERROR: Model not found at {MODEL_PATH} (MOCK_MODE=False)")
        sys.exit(1)

    all_results = []
    for suite_name, cases in ALL_SUITES.items():
        all_results.extend(run_suite(paths, node_ok, c_flags, c_link_flags, suite_name, cases))

    guard_rail_results = [
        run_guard_rail_import_order(paths, c_flags),
        run_guard_rail_action_var_scope(paths, c_flags),
    ]

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    by_suite = defaultdict(list)
    for r in all_results:
        by_suite[r["suite"]].append(r)
    for suite_name, group in by_suite.items():
        passed = sum(1 for r in group if r["overall"])
        print(f"\n{suite_name.upper()}: {passed}/{len(group)} passed")
        for r in group:
            status = "PASS" if r["overall"] else "FAIL"
            print(f"  [{status}] {r['name']:<38} retries={r['total_retries']:<3} ({r['overall_note']})")
    for gr in guard_rail_results:
        print(f"\n[{'PASS' if gr['overall'] else 'FAIL'}] {gr['name']}")

    total_passed = sum(1 for r in all_results if r["overall"]) + sum(1 for gr in guard_rail_results if gr["overall"])
    total_cases = len(all_results) + len(guard_rail_results)
    print(f"\n{'='*70}")
    print(f"OVERALL: {total_passed}/{total_cases} passed ({total_passed/total_cases*100:.1f}%)")
    print(f"{'='*70}")

    with open(OUT_PATH, "w") as f:
        json.dump({
            "metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mock_mode": MOCK_MODE,
                "total_cases": total_cases,
                "total_passed": total_passed,
            },
            "results": all_results,
            "guard_rails": guard_rail_results,
        }, f, indent=2)
    print(f"\nResults saved to {OUT_PATH}")

    return 0 if total_passed == total_cases else 1


if __name__ == "__main__":
    sys.exit(main())
