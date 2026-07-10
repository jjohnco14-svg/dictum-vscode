"use strict";
// chunking.ts — splits an approved Dictum plan into dependency-safe,
// token-budgeted generation chunks instead of building the whole plan in
// one monolithic prompt+generation.
//
// WHY THIS EXISTS:
//
// The previous Build flow sent the ENTIRE approved plan as one prompt and
// asked for the entire program back in a single generation. Two problems
// fall directly out of that, independent of each other and independent of
// whether GBNF grammar is constraining the output correctly:
//
//   1. CPU-only inference (KoboldCpp on a CPU-only host) has a hard
//      wall-clock budget per request (see ollama.js's timeoutMs tiers).
//      A large plan's generation can simply exceed that budget before
//      every plan item is reached, regardless of timeout size — the fix
//      is not "wait longer" but "ask for less per request."
//   2. Structured/grammar-constrained output for adjacent, structurally
//      similar plan items (e.g. two TYPE shapes declared back-to-back)
//      produces legitimate, grammar-forced keyword repetition. Cramming
//      many such items into one continuous generation maximizes the
//      surface area for any repetition-based stuck-loop heuristic to
//      false-positive on output that is actually fine.
//
// Chunking addresses both: each chunk asks for only a small, dependency-
// coherent slice of the plan, with a small, roughly-constant prompt size
// regardless of total plan size. Cross-chunk continuity comes from
// graph.js's buildPromptContext() (the existing "100x small-model boost"
// symbol-table mechanism) — each chunk's prompt includes a compact summary
// of what prior chunks already defined, NOT the full accumulated source
// text, so per-chunk prompt size does not grow linearly with plan size.
//
// CHUNK BOUNDARY POLICY (dependency tiers, in this fixed order):
//   1. ARCHITECTURE — always its own chunk first (establishes the module/
//      program shell every later chunk's symbols live inside).
//   2. TYPE — all TYPE items grouped into one chunk. Types are typically
//      small and mutually referential (one shape's field can be another
//      shape), so splitting them further tends to cost more in cross-chunk
//      coordination than it saves in per-chunk size.
//   3. INVARIANT — grouped into one chunk, generated after every TYPE chunk
//      so invariants can reference real, already-defined shape fields
//      instead of being generated blind.
//   4. OPERATION — ONE chunk per operation. Operations are typically the
//      largest, most logic-heavy individual plan items and the most likely
//      to need isolation; bundling several together reintroduces the same
//      cross-item repetition/length problem chunking exists to avoid.
//   5. MEMORY, SAFETY, and any other/unknown category — grouped into one
//      final chunk (lifecycle/allocation concerns that reference the
//      already-fully-defined symbol table).
//
// ADAPTIVE SIZING:
//   Within each tier, if the combined estimated token cost of that tier's
//   items would exceed the caller-supplied per-chunk token budget, the tier
//   is greedily split into multiple same-category chunks rather than
//   exceeding the budget. This keeps simple/small plans at the minimum
//   chunk count (fast — fewer round-trips) while still protecting large
//   plans from oversized single chunks.
Object.defineProperty(exports, "__esModule", { value: true });
exports.CHUNK_TIER_ORDER = void 0;
exports.estimateTokens = estimateTokens;
exports.planItemTier = planItemTier;
exports.buildChunks = buildChunks;
exports.chunkBudgetFromCalibration = chunkBudgetFromCalibration;
/**
 * Fixed dependency-tier order. Lower index = generated first. Items whose
 * category isn't recognized fall into the same tier as MEMORY/SAFETY (last,
 * after every type/invariant/operation chunk) — this is the safest default
 * since it's the tier with no forward-reference assumptions baked in.
 */
exports.CHUNK_TIER_ORDER = ['ARCHITECTURE', 'TYPE', 'INVARIANT', 'OPERATION', 'MODIFY', 'MEMORY', 'SAFETY'];
function planItemTier(category) {
    const idx = exports.CHUNK_TIER_ORDER.indexOf((category || '').toUpperCase());
    return idx === -1 ? exports.CHUNK_TIER_ORDER.length - 1 : idx;
}
/**
 * Rough token estimate from character count. Deliberately conservative
 * (slightly over-estimates) since under-estimating risks building a chunk
 * that blows the timeout/context budget it was sized to respect, while
 * over-estimating only costs a few extra, still-correct chunk splits.
 * ~3.5 chars/token is a reasonable conservative average for English/code
 * mixed text without pulling in a real tokenizer dependency.
 */
function estimateTokens(text) {
    if (!text)
        return 0;
    return Math.ceil(text.length / 3.5);
}
function planItemText(item) {
    return `[PLAN: ${item.category} : ${item.id} : ${item.desc}]`;
}
/**
 * Greedily packs a single tier's plan items into one or more chunks, never
 * exceeding maxTokensPerChunk per chunk (unless a single item alone exceeds
 * the budget, in which case it gets its own oversized chunk rather than
 * being silently dropped or truncated — a budget violation that's visible
 * and attributable beats one that's silent).
 */
function packTier(items, maxTokensPerChunk) {
    if (items.length === 0)
        return [];
    const chunks = [];
    let current = [];
    let currentTokens = 0;
    for (const item of items) {
        const itemTokens = estimateTokens(planItemText(item));
        if (current.length > 0 && currentTokens + itemTokens > maxTokensPerChunk) {
            chunks.push(current);
            current = [];
            currentTokens = 0;
        }
        current.push(item);
        currentTokens += itemTokens;
    }
    if (current.length > 0)
        chunks.push(current);
    return chunks;
}
/**
 * Splits an approved plan (array of {category, id, desc}) into ordered
 * chunks respecting the dependency-tier policy above.
 *
 * maxTokensPerChunk: adaptive budget, typically derived from a measured
 * tokens/sec calibration (see ollama.js calibrate()) combined with the
 * timeout tier the chunk would fall under — see chunkBudgetFromCalibration
 * below. Defaults to a conservative fixed value if no calibration is
 * available yet (e.g. first run, before Setup has measured anything).
 *
 * OPERATION items always get their own chunk regardless of how small they
 * are relative to the budget — see policy note above.
 */
/**
 * Fallback token budget when no calibration measurement exists yet (e.g.
 * first-ever build before Setup has measured this host). Deliberately
 * conservative — small enough to keep even a slow CPU-only host's
 * generation well within the 'small payload' koboldcpp timeout tier
 * (60s, see ollama.js) rather than assuming favorable hardware.
 */
const DEFAULT_CHUNK_TOKEN_BUDGET = 600;
/**
 * Converts a measured tokens/sec figure into a safe max-tokens-per-chunk
 * budget, sized against the SHORT koboldcpp timeout tier (60s for
 * "small" payloads — see ollama.js's isLargePayload logic). Chunk prompts
 * are deliberately kept small (plan-item text + compact symbol-table
 * context, not full accumulated source — see graph.js buildPromptContext),
 * so they should almost always land in the short tier; sizing against that
 * tier rather than the long one is what keeps chunk generation itself
 * fast, which is the whole point of chunking.
 *
 * marginFraction reserves headroom for:
 *   - prompt processing time, which is a separate (and on CPU-only
 *     hardware, often substantial) cost from generation time and is NOT
 *     included in the tokens/sec measurement calibrate() produces (that
 *     measurement is generation speed only, timed from request start,
 *     which does include prompt processing for a tiny calibration prompt
 *     — but a real chunk's prompt, carrying plan text + symbol context, is
 *     larger, so its processing overhead is larger too).
 *   - normal variance between the calibration sample and any specific
 *     chunk's actual content (grammar-constrained decoding, in particular,
 *     can be slower than unconstrained calibration decoding).
 *
 * Returns DEFAULT_CHUNK_TOKEN_BUDGET if tokensPerSecond is 0/unmeasured.
 */
function chunkBudgetFromCalibration(tokensPerSecond, opts = {}) {
    const { timeoutMs = 60000, marginFraction = 0.5 } = opts;
    if (!tokensPerSecond || tokensPerSecond <= 0)
        return DEFAULT_CHUNK_TOKEN_BUDGET;
    const safeSeconds = (timeoutMs / 1000) * marginFraction;
    const budget = Math.floor(tokensPerSecond * safeSeconds);
    // Never go below a sane floor (a chunk needs enough budget to hold at
    // least one real plan item's worth of generated Dictum code) or above
    // a sane ceiling (extremely fast measured hardware shouldn't produce
    // chunks so large they defeat the purpose of chunking in the first
    // place — cross-chunk symbol-table context and stagnation detection
    // both work better with more, smaller chunks than fewer, huge ones).
    return Math.max(200, Math.min(budget, 2000));
}
/**
 * FIX (invariant/operation chunk-boundary mismatch): the original tier order
 * always generated the INVARIANT tier before ANY OPERATION chunk. This meant
 * an invariant that was supposed to be enforced "inside action move" was
 * always built and transpiled before `move` existed anywhere in the
 * generated source — there was no action body for the check to go into,
 * regardless of how precisely the plan item described where it belonged.
 * That is a chunk-boundary bug, not a prompt-wording bug: no amount of plan
 * detail fixes an invariant whose host action doesn't exist yet at the time
 * it's generated.
 *
 * Fix: an INVARIANT item whose description begins with the exact phrase
 * "inside action <name>, " (see SKILL_PLAN.md rule 5.5) is routed into that
 * action's own OPERATION chunk instead of the standalone INVARIANT tier —
 * so the check and the action body it lives inside are generated together,
 * in one call, with the action already partially present in-context as the
 * model writes it.
 *
 * One-operation-per-chunk is preserved exactly as before (still the
 * documented policy — operations are the largest/most logic-heavy items,
 * bundling several defeats the point of chunking). This only ever adds a
 * *matched* action's *own* invariant(s) into its *own* chunk — never merges
 * two different operations together.
 *
 * Any invariant with no "inside action X," prefix, or whose named action
 * doesn't match any OPERATION item in this plan (typo, or a genuinely
 * standalone/structural invariant), falls back to the original standalone
 * INVARIANT tier — visible and attributable, never silently dropped. This
 * preserves prior behavior exactly for every plan that doesn't use the new
 * phrasing, so it's a strict addition, not a behavior change for old plans.
 */
const HOST_ACTION_RE = /^inside action (\w+),/i;
const OPERATION_ACTION_RE = /^action (\w+)/i;
function extractHostAction(invariantDesc) {
    const m = (invariantDesc || '').match(HOST_ACTION_RE);
    return m ? m[1] : null;
}
function extractOperationAction(operationDesc) {
    const m = (operationDesc || '').match(OPERATION_ACTION_RE);
    return m ? m[1] : null;
}
function buildChunks(plan, maxTokensPerChunk = DEFAULT_CHUNK_TOKEN_BUDGET) {
    if (!plan || plan.length === 0)
        return [];
    const byTier = new Map();
    const invariantItems = [];
    const invTierIdx = exports.CHUNK_TIER_ORDER.indexOf('INVARIANT');
    for (const item of plan) {
        const tier = planItemTier(item.category);
        if (tier === invTierIdx) {
            // Held back for routing below instead of going straight into
            // byTier — may end up attached to an OPERATION item instead of
            // occupying the standalone INVARIANT tier.
            invariantItems.push(item);
            continue;
        }
        if (!byTier.has(tier))
            byTier.set(tier, []);
        byTier.get(tier).push(item);
    }
    const opTierIdx = exports.CHUNK_TIER_ORDER.indexOf('OPERATION');
    const modifyTierIdx = exports.CHUNK_TIER_ORDER.indexOf('MODIFY');
    const operationItems = [...(byTier.get(opTierIdx) || []), ...(byTier.get(modifyTierIdx) || [])];
    const attachedTo = new Map(); // operation item -> [invariant item, ...]
    const unroutedInvariants = [];
    for (const inv of invariantItems) {
        const hostAction = extractHostAction(inv.desc);
        const match = hostAction
            ? operationItems.find((op) => extractOperationAction(op.desc) === hostAction)
            : null;
        if (match) {
            if (!attachedTo.has(match))
                attachedTo.set(match, []);
            attachedTo.get(match).push(inv);
        }
        else {
            unroutedInvariants.push(inv);
        }
    }
    if (unroutedInvariants.length > 0) {
        if (!byTier.has(invTierIdx))
            byTier.set(invTierIdx, []);
        byTier.get(invTierIdx).push(...unroutedInvariants);
    }
    const chunks = [];
    const sortedTiers = [...byTier.keys()].sort((a, b) => a - b);
    for (const tier of sortedTiers) {
        const items = byTier.get(tier);
        const tierName = exports.CHUNK_TIER_ORDER[tier] ?? 'OTHER';
        if (tierName === 'OPERATION' || tierName === 'MODIFY') {
            // Each operation is its own chunk, full stop — never packed with
            // OTHER operations, even if several would technically fit the
            // token budget. Its own matched invariant(s), if any, ride
            // along in the same chunk (see routing above) since they
            // describe the same action body, not a separate concern.
            for (const item of items) {
                const attached = attachedTo.get(item) || [];
                chunks.push({ tierName, items: [item, ...attached] });
            }
        }
        else {
            const packed = packTier(items, maxTokensPerChunk);
            for (const group of packed) {
                chunks.push({ tierName, items: group });
            }
        }
    }
    return chunks.map((c, i) => ({
        index: i,
        total: chunks.length,
        tierName: c.tierName,
        items: c.items,
        label: c.items.length === 1
            ? `${c.tierName.toLowerCase()}: ${c.items[0].desc.slice(0, 60)}${c.items[0].desc.length > 60 ? '…' : ''}`
            : (c.tierName === 'OPERATION' || c.tierName === 'MODIFY')
                ? `${c.tierName.toLowerCase()}: ${c.items[0].desc.slice(0, 50)}${c.items[0].desc.length > 50 ? '…' : ''}${c.items.length > 1 ? ` (+${c.items.length - 1} invariant)` : ''}`
                : `${c.tierName.toLowerCase()} (${c.items.length} items)`,
    }));
}
//# sourceMappingURL=chunking.js.map
