# Dictum — Source of Truth

Canonical reference for where each fact about the Dictum language actually
lives, and what's been verified true about the compiler as of v0.1.38.
Written because the project's core recurring failure mode has been the
same fact about the language living in more than one hand-maintained
place with no sync mechanism — this document is meant to prevent a new
instance of that pattern by saying, explicitly, "this is the one place."

## 1. Type vocabulary

**Single source of truth: `compiler/dictumc/type_registry.py`.**

Every primitive type (name, word tokens, C type, C++ type, numeric?,
usable as a single bare word?, valid as a variable's type?) is one entry
in `PRIMITIVES`. Nothing else should hand-declare a type name, a C/C++
type mapping, or a type-related word list.

Consumers (all derive from the registry, none hand-maintain their own copy):
- `parser.py` — `_TERMINAL_TYPES`, `_PRIMITIVE_SUFFIXES`
- `validator.py` — `PRIMITIVE_TYPES`, `NUMERIC_TYPES`
- `emit_c.py` / `emit_cpp.py` — `self.types` (Dictum name → C/C++ type)
- `grammar.py` — `TYPE_WORDS`
- `out/bridge.js` — queries `type_registry.py` directly via a `python3`
  subprocess (`getRealTypeWords`, `getRealPrimitiveTypeNames`) — does NOT
  regex-scrape grammar.py's source text (that approach broke the moment
  `TYPE_WORDS` became a computed value instead of a literal `{...}`; see
  changelog 0.1.37).
- Static `.gbnf` files (`grammar/dictum_safe.gbnf`, `dictum_unsafe.gbnf`)
  — CANNOT `import` Python. Kept in sync via
  `sync_gbnf_typewords.py`, which must be re-run after any
  `type_registry.py` change. This is the one remaining manual step;
  everything else is automatic.

**Recursive/compound type forms** (`list of <T>`, `unique/shared/weak/raw
handle/pointer to <T>`, `const ref <T>`, `handle to bytes`) are NOT in the
registry — they're hand-coded recursive control flow in `parser.py`'s
`parse_type()`, which genuinely needs to recurse into an inner type. The
registry exports the *building-block words* those forms consume
(`WRAPPER_WORDS`) so `grammar.py`/the `.gbnf` files still know about them,
without needing parser.py's control flow to change.

## 2. Keyword vocabulary

**Single source of truth: `parser.py`'s actual parsing code — nothing
else should be treated as authoritative.**

Unlike types, keywords don't have a registry yet; `grammar.py`'s
`KEYWORDS` set is still a hand-maintained mirror of what the parser
accepts (fixed to match as of 0.1.38, but not automatically kept in sync
going forward). `architecture_test.py`'s Test 2 checks this by extracting
every word literal the parser actually compares against
(`match_word('x')`, `expect_word('x')`, etc.) and diffing against
`grammar.py`'s declared set — **run this after adding any new keyword to
the parser**, since nothing else will catch a gap automatically the way
the type registry does.

## 3. The GBNF files' actual purpose (don't over-trust their strictness)

`grammar/dictum_safe.gbnf` / `dictum_unsafe.gbnf` are what real
koboldcpp Build-tier generation is constrained by (loaded directly in
`out/commands.js` / `out/extension.js`). Their `top-level-item` rule is
**deliberately loose**: `identifier` matches any bare alphabetic word, so
most keyword-vs-vocabulary gaps are invisible in practice (the word is
still producible via the identifier fallback). What is NOT covered by
that fallback, and therefore genuinely blocks generation if missing:
**bare punctuation** — `:` (mandatory on every `program`/`module`/
`shape`/`action` opener), `.` (field access), `-` (negative numbers), `#`
(comments). These were entirely missing until 0.1.35, meaning the grammar
could not produce a single valid Dictum program of any kind. Fixed, and
verified via `gbnf_check.py` (a real GBNF parser + matcher, not a
approximation) against the project's full real `.dict` corpus.

**If you add a new symbol/punctuation character to the language, it must
be added to the `punctuation` rule in both `.gbnf` files by hand** — this
is not derived from anything, and the `identifier` fallback will NOT save
you (it only covers `[a-zA-Z_][a-zA-Z0-9_]*`).

## 4. C vs C++ backend: NOT source-compatible for everything

`emit_c.py` and `emit_cpp.py` are two largely-parallel, independently
hand-written transpiler implementations. This is a structural risk in
itself: a bug fixed in one has repeatedly needed the identical fix
hand-applied to the other (module preamble ordering, sibling/`use`d-module
call resolution, `RAW_MALLOC`-family redeclaration, the `Use`-in-body
whitelist gap — all found and fixed twice, once per backend, this
session). **Whenever you fix a codegen bug in one backend, check the
other for the same bug before considering it done.**
`architecture_test.py`'s Test 3 (backend parity) automates the "did you
forget the other backend" check across the full `foundation_test.py`
corpus — treat any new gcc/g++ divergence it reports as a real bug unless
it's added to `KNOWN_BACKEND_DIVERGENCES` with a documented reason.

**Currently known, deliberate, documented divergence** (not a bug, a real
architectural limitation — see `SKILL_BUILD.md`'s Atomics section):
`ATOMIC_*`/`CAS_*` unsafe tokens are not source-compatible between
backends. C expands to `__atomic_fetch_add(ptr, ...)` (needs a plain
`T*`); C++ expands to `ptr->fetch_add(...)` (needs `std::atomic<T>*`).
Picking a backend for atomics-using code is a real upfront decision.

## 5. The compile gate is two-phase on purpose — don't test with only Phase 1

`transpiler.py`'s `compileCheck()` (used by the real `dictum.run` command
via `out/extension.js`) runs `gcc/g++ -fsyntax-only` (Phase 1) THEN a full
compile-and-link (Phase 2), unconditionally. This is deliberate and
important: C treats calling an undeclared function as a WARNING
(`-Wimplicit-function-declaration`), not an error — a hallucinated or
typo'd callee name (a highly plausible weak-model mistake, e.g.
`calc_distance` instead of `distance`) passes Phase 1 silently and is only
caught by Phase 2's link failure (`undefined reference to ...`). **If you
write a test harness or tooling that checks compilation, always run both
phases** — `-fsyntax-only` alone will produce false "PASS" results for
this exact failure mode. (Confirmed via `simulate_vibecoding.js`; this was
initially mis-diagnosed as a gap in the real pipeline during this
session's own investigation, before re-checking against the actual
two-phase design and finding it already handled correctly. The mistake
was in the test, not the compiler — worth remembering when investigating
future "why did this pass when it shouldn't have" reports too.)

## 6. Known, real, currently-unfixed limitations (not bugs — documented decisions)

- **Atomics are backend-incompatible.** See §4.
- **Module-scope tracking for `use` is file-global, not scope-local**
  (0.1.38). A same-named plain top-level action defined elsewhere in a
  file that also `use`s a module exporting an action with that name could
  resolve to the wrong one. Low real-world risk; full fix needs
  scope-aware tracking, judged not worth the complexity yet.
- **`bytes` is a raw `uint8_t*`, distinct from `handle to bytes` (opaque
  `void*`).** Both exist; pick deliberately.

## 7. Permanent test suite (run all of these after any compiler change)

- `run_selftest.py` — behavioral + historical-regression tests (existed
  before this session).
- `foundation_test.py` — every LANGUAGE_REFERENCE.md example + a
  registry-driven feature matrix, through validate → both backends →
  GBNF reachability.
- `gbnf_check.py` — standalone real GBNF parser/matcher; use directly to
  check "can this exact string be produced under grammar constraint."
- `architecture_test.py` — hunts specifically for vocabulary/logic
  duplication drift (the pattern behind most bugs found this session),
  not "does this program compile."
- `sync_gbnf_typewords.py` — run after any `type_registry.py` change.
- `simulate_vibecoding.js` — exercises the real, shipped `chunking.js`/
  `graph.js`/`retryLoop.js` against a synthetic Plan, including a
  deliberately-injected bug, to check the real Build/Review/retry
  orchestration logic (not just the Python compiler) end to end.

## 8. Continuing sessions, project-wide codegraph, and skills (v0.1.39)

Three related additions, all additive (no core compiler files touched,
verified by identical `run_selftest.py`/`foundation_test.py`/
`architecture_test.py` results before and after).

**Continuing Plan/Build/Review (Part 1).** `graph.js` already tracked
every open document workspace-wide before this was built — the actual gap
was that `_runGenerate` (Plan) never consumed it, and there was no way to
patch an existing action instead of duplicating it. Now: Plan gets an
"EXISTING CODE CONTEXT" section built from `graph.buildPromptContext`
whenever the graph is non-empty; a new `MODIFY` plan-item category
(parallel to `OPERATION`) signals a change rather than an addition; and
`out/patchEngine.js` finds and replaces the named block in place, applied
before validation (not after) in `_runBuildChunk`. Falls back to append
whenever a target can't be confidently found. `[MODE: fresh|continue]`
and `[FILE: name.dict]` directives, parsed in `validator.js`.

**Project-wide codegraph (Part 2).** `project_builder.py` already had a
`dictum.project.json` manifest and dependency parsing — for a completely
separate, non-AI compile/link pipeline (`dictum.buildProject`), unwired
from the Plan/Build/Review commands entirely. `out/projectScan.js` closes
the actual gap (proactive discovery of files nobody's opened in a tab
yet), reusing `graph.js`'s existing `indexSource` rather than writing a
third independent symbol extractor (there are already two: `graph.js` and
`project_builder.py`'s `parse_deps` — for different consumers, not
duplicated further). **Known scope limit, not a hidden gap**: `[FILE:]`
directive parsing and target resolution (`resolveTargetFile`) are done;
actually wiring `_runBuild` to write to more than one physical file live
in a single session is the next step, not this one.

**Skills as curated library bundles (Part 3A).** Confirms the "Option A"
design (see earlier discussion): zero compiler changes needed. A skill is
`skills/library/<name>/*.dict` (real shapes + `import_c` bindings) plus
optional `SKILL_PLAN_<name>.md`/`SKILL_BUILD_<name>.md` addenda, loaded by
`out/skills.js`. `dictum.activeSkill` is orthogonal to the existing
`[SKILL: general/unsafe/concurrent]` plan directive (domain vs.
safety-tier — both layer independently). First real skill: `gamedev`
(the raylib subset from this session's 3D game work). `'general'`
(default) is a verified true no-op.

**If you add a new skill**: don't add new primitive types for it (see §1
and the Option A/B discussion) unless a concrete need proves shapes +
`import_c` genuinely insufficient — that should be rare. Drop a `.dict`
bindings file in `skills/library/<name>/`, optionally add the two
addendum markdown files, and it's picked up automatically — no code
change needed in `skills.js` itself.

## 9. Test suite (updated)

- `run_selftest.py`, `foundation_test.py`, `gbnf_check.py`,
  `architecture_test.py`, `sync_gbnf_typewords.py` — unchanged, see §7.
- **`extensions_test.py`** (new) — Parts 1/2/3A specifically: patch
  correctness (including a real compile check, not just string
  inspection), project discovery/exclude/manifest-parity, skill loading
  and no-op verification for the default case.
- **`e2e_test.js`** (new) — all three parts together in one realistic
  session, including a full graph clear + re-scan from disk between the
  initial build and the continuation, specifically to prove a genuinely
  new session (not just in-process state) can continue correctly.

Version at time of writing: **0.1.39**.

## 10. Codegraph pattern database -- storage/lookup mechanism (v0.1.46)

Built in response to Cell 9/10/11's Kaggle findings: GBNF grammar
constraints (chunk_grammar.py) restrict a generated chunk's *shape*, but
several real, live failures showed they can't restrict its *content* --
a small model that's never seen Dictum in training fills an open slot
with the nearest Python/C/JS habit instead (`countdown` instead of the
plan's `Count`, invented `<name>`/`[Person.name]` string interpolation,
swapped unsafe-token param order, ...). Tightening the grammar further
only relocates the hallucination to a different escape hatch each time
(see Cell 11's own "known remaining limitations" notes).

**What this is:** a small file-based pattern store --
`codegraph/patterns/<pattern_ref>.json`, one file per construct, same
filesystem-as-database convention as `skills/library/<name>/*.dict` and
`graphify-out/cache/ast/*.json` (no new storage dependency) -- plus a
loader/binder/renderer (`compiler/dictumc/pattern_graph.py`) and a
Node-side bridge (`out/patternGraph.js`, same spawn/stdin/stdout
contract as `normalizeDictum.js`) that turns a `pattern_ref` (+ optional
params) into a ready-to-inject few-shot context block: description,
stated preconditions, known common mistakes, and a concrete correct
Dictum example -- so the Build prompt can show the model real Dictum for
the specific construct it's about to generate, not just constrain the
token shape it emits.

**What this is not, yet:** not wired into `_runBuild`/the Build prompt
itself (that's the next step once there's a real pattern set to wire
in); not wired into `chunk_grammar.py`'s GBNF generation (a pattern
could later carry an exact-shape grammar fragment, but that's a
separate, larger integration -- see `codegraph/PATTERN_SCHEMA.md`'s
"Deliberately NOT done yet" section); not a Plan-phase `pattern_ref`
directive yet (parallel to how `[SKILL: name]`/`[FILE: name]` already
work) -- that requires updating the Plan prompt/`validator.js`, also
deferred. This commit is the storage + lookup + binding mechanism and
its tests only, seeded with one real pattern (`while-loop` -- not
coincidentally the exact construct behind Tier3's undeclared-variable
failure) so the plumbing has something real to run against; the rest of
the pattern set (print-interpolation, pointer-ops, unsafe-RAW_MALLOC/
RAW_FREE/ATOMIC_FAA, ...) is intentionally a separate follow-up.

**Files added:**
- `codegraph/PATTERN_SCHEMA.md` -- full field contract + rationale.
- `codegraph/patterns/*.json` -- 10 patterns (hello-world, import-c,
  shape-declaration, shape-actions, while-loop, pointer-ops, unsafe-
  malloc, atomic-increment, importc-math, importc-raylib), all
  cross-validated against `validated_patterns.json` (200 real
  transpiler-run examples supplied separately, 100% pass rate) -- see
  `pattern_graph_test.py`'s Layer 5 and each pattern's own
  `requires`/`common_mistakes` for what's exactly reproduced vs. a
  documented coverage limit (structural body variation not captured by
  recorded params, for `while-loop`/`shape-actions`/`importc-raylib`
  specifically).
- `compiler/dictumc/pattern_graph.py` -- load/validate/bind/render +
  `--bridge`/`--list` CLI (same `{ok: true|false|null}` three-way
  contract as `normalize_dictum.py --bridge`). Adds a `field_list` param
  type (structured, not scalar -- a list of `[name, type]` pairs
  rendered as indented shape-field lines) for `shape-declaration.json`/
  `shape-actions.json`.
- `compiler/dictumc/pattern_graph_test.py` -- self-test, five layers
  (in-process API, the actual CLI subprocess contract, schema-violation
  fixtures in an isolated scratch dir, every shipped pattern loads
  cleanly, and a cross-check of every pattern's `example` field against
  `validated_patterns.json`'s real base entries when that dataset file
  is present on disk). Run directly:
  `python3 compiler/dictumc/pattern_graph_test.py`.
- `out/patternGraph.js` -- Node bridge (`renderPatternContext`,
  `listPatterns`), not yet called from any Build-path file.

**Bugfix found by this data, applied to `chunk_grammar.py`:**
`UNSAFE_ARITY["ATOMIC_FAA"]` was hardcoded to 2 (pointer, delta) when
the unsafe-token grammar work landed -- based on the Cell 9-11 test
suite's own plan text ("ATOMIC_FAA Counter 1"), which never mentioned a
result variable and used a bare variable instead of a pointer to it.
`validated_patterns.json`'s 21/21 real transpiler-run ATOMIC_FAA
examples all show a **three**-parameter form --
`[ATOMIC_FAA: <pointer> : <delta> : <result-variable>]` -- so
`UNSAFE_ARITY["ATOMIC_FAA"]` is now 3. Both the grammar's arity and the
test suite's plan text were wrong in the same direction; this fixes the
grammar side. See `codegraph/patterns/atomic-increment.json` for the
corrected canonical example and the preconditions this construct
actually needs (target variable, a pointer to it, and a result
variable, all declared via `keep` first).

Version at time of writing: **0.1.46**.


