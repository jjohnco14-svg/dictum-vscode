# DICTUM BUILD SKILL — UNSAFE ADDENDUM
# Layered on top of the base SKILL_BUILD.md system prompt when the Plan
# model's [SKILL: unsafe] directive is present for this task.
#
# This file does NOT redefine the unsafe token vocabulary, L3 composition
# rules, or syntax — all of that already lives in SKILL_BUILD.md and applies
# regardless of this addendum. This file exists to change the model's
# DEFAULT POSTURE for a task that has been explicitly flagged as needing
# unsafe constructs, so the model reaches for the right tool deliberately
# instead of defaulting to a "safe-looking" approximation that either fails
# the L3 compiler checks or produces correct-looking code that is actually
# unsound (e.g. a hand-rolled spinlock using plain reads/writes instead of
# the real ATOMIC_* tokens).

---

## THIS TASK WAS FLAGGED [SKILL: unsafe] BY THE PLAN

That means the Plan model has already judged that ordinary safe Dictum
constructs (`keep`, `set`, `put ... into`) are insufficient for at least one
plan item — usually because it involves: shared mutable state accessed from
more than one execution context, a data structure that must remain
consistent under concurrent access, manual memory layout control, raw
pointer arithmetic, or a hand-off with non-Dictum code via FFI.

Default to the unsafe token vocabulary for any plan item that matches one of
those shapes, rather than trying to express it in safe Dictum first and only
reaching for unsafe tokens if that fails. A safe-looking implementation of an
inherently unsafe operation (e.g. a counter incremented from a callback
without ATOMIC_FAA) is not a correct fallback — it is a race condition that
the L2/L3 checks may not catch if the plan item's wording doesn't make the
hazard obvious from the description alone.

## CHECKLIST BEFORE EMITTING AN UNSAFE BLOCK

1. Identify which L3 composition rule(s) in the base skill apply to this
   specific operation (CAS→BARRIER, HP_PROTECT→HP_CLEAR, RAW_MALLOC→RAW_FREE,
   FFI_LOAD→FFI_CLOSE, IS_ALIGNED→SIMD_LOAD). Name the rule in a comment
   immediately before the relevant token sequence.
2. Emit the full required token sequence for that rule — never a partial
   sequence "to be completed later." An unsafe block with an unmatched
   RAW_MALLOC or HP_PROTECT will fail L3 review even if it would otherwise
   run correctly once.
3. Prefer the narrowest unsafe primitive that satisfies the plan item.
   ATOMIC_FAA for a counter, not a full CAS_LOOP. A CAS_LOOP only when the
   update genuinely depends on the previous value in a way a fetch-and-op
   primitive can't express.
4. State the memory ordering explicitly (acquire/release/seq_cst/relaxed)
   rather than defaulting to seq_cst out of caution — seq_cst is correct but
   the plan or surrounding code may call for a specific, weaker ordering,
   and an unnecessarily strong ordering can mask a genuine race elsewhere
   that would otherwise have been caught by a correctly-scoped relaxed/
   acquire/release pairing.

## DO NOT

- Wrap an entire action body in `unsafe:` because one operation needs it.
  Keep unsafe blocks as narrow as the specific hazardous operation requires.
- Invent a new unsafe token. The complete vocabulary is in the base skill's
  "UNSAFE TOKENS" section — use only what's listed there.
- Silently fall back to a safe-Dictum approximation if an unsafe token seems
  hard to apply correctly. If genuinely unsure which token fits, emit the
  closest match and let L3 review catch a composition error — that is a
  recoverable, visible failure. A silent safe-looking substitute that is
  actually a race condition is not recoverable by the review pipeline,
  because it has no [CHECK: FAIL] signal to trigger on.
