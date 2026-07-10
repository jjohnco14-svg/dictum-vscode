# DICTUM BUILD SKILL — CONCURRENT ADDENDUM
# Layered on top of the base SKILL_BUILD.md system prompt when the Plan
# model's [SKILL: concurrent] directive is present for this task.
#
# Distinct from SKILL_BUILD_unsafe.md: that addendum covers the general case
# of any task needing unsafe constructs (manual memory, FFI, raw pointers).
# This addendum is specifically for tasks where MULTIPLE independent
# execution contexts (threads, callbacks, signal handlers, interrupt
# contexts) touch the SAME state, and the central risk is a composition
# hazard between operations rather than any single operation being unsafe
# in isolation.

---

## THIS TASK WAS FLAGGED [SKILL: concurrent] BY THE PLAN

That means at least one plan item involves shared state that more than one
execution context can touch. The risk here is rarely "I used the wrong
primitive" — it's "I used the right primitives in an order or combination
that has a window where another context can observe an inconsistent state."

## THE THREE SHAPES OF CONCURRENT WORK IN DICTUM

**Simple shared counters/flags** — use the plain ATOMIC_* tokens
(ATOMIC_LOAD, ATOMIC_STORE, ATOMIC_FAA, ATOMIC_CAS_*) directly. No hazard
pointers or RCU needed if the shared state is a single scalar.

**Shared structures with safe deferred reclamation** — use the HP_PROTECT /
HP_CLEAR / HP_RETIRE sequence (hazard pointers) per the base skill's RULE 2.
Reach for this when a reader might be mid-traversal of a structure that a
writer wants to free — HP_PROTECT before the read, HP_CLEAR or HP_RETIRE
once the reader is done, with NO operation that could free the protected
node happening in between.

**Read-mostly shared structures** — use the RCU tokens when reads vastly
outnumber writes and a brief window of readers seeing a slightly-stale
version is acceptable. Prefer this over hazard pointers only when that
staleness tradeoff is actually acceptable for the plan item described —
if the plan item implies readers need the absolute latest value, hazard
pointers or a CAS-based approach is the correct choice instead.

## COMPOSITION HAZARDS TO ACTIVELY CHECK FOR (beyond base skill's L3 rules)

- **Lost update**: two CAS_LOOP sequences on different fields of the same
  structure, where the structure's invariant actually depends on those two
  fields changing together. A CAS_LOOP only guarantees atomicity for the
  single field it targets — if the plan item's invariant spans more than
  one field, either restructure to a single CAS'able value (e.g. pack both
  into one word) or use a different synchronization shape entirely; emit a
  comment flagging the tradeoff rather than silently picking one.
- **ABA across hazard-protected reads**: a node freed and a structurally
  identical new node allocated at a different address between an
  HP_PROTECT and the subsequent read — only a real concern if RAW_FREE /
  reallocation of same-sized nodes happens elsewhere in the same program;
  if so, prefer HP_RETIRE with a grace period over immediate RAW_FREE.
- **Barrier placement relative to the actual data write, not just the
  flag write**: RULE 1 (CAS → BARRIER) in the base skill covers the
  token-adjacency requirement, but double check that the data the flag
  guards is actually written before the CAS that publishes it, not after.

## DO NOT

- Default to ATOMIC_CAS_PTR loops for problems a simple ATOMIC_FAA solves.
  Concurrent does not mean "use the heaviest tool available."
- Mix hazard pointers and RCU for the same structure within one action
  without an explicit reason stated in a comment — picking one consistently
  per structure is almost always correct.
- Treat a `[SKILL: concurrent]` task as license to write concurrent-style
  (unsafe/atomic) code for the surrounding non-concurrent plan items in the
  same task. Only the specific shared-state operations need the concurrent
  treatment above; everything else in the same program follows ordinary
  safe Dictum rules.
