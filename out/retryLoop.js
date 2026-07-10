"use strict";
// retryLoop.ts — unified retry decision for the Build -> Review -> CompileGate
// cycle.
//
// BEFORE this file existed, Review (driven by L2/L3 text checks) and the
// compile gate (driven by real gcc/g++ output) each had their own counter
// (_reviewAttempt, capped at MAX_REVIEW_RETRIES; the recursive `attempt`
// parameter in _runCompileGate, capped at MAX_COMPILE_RETRIES) and neither
// loop knew what the other had already tried. This was provable, not
// theoretical: the debug/pipeline_debug.js scenario
// gap_syntax_only_misses_link_errors showed a build that passed L2 on every
// single retry while failing the compile gate identically every single
// retry, because "did the model emit every VERIFY token it promised" and
// "does the resulting code actually link" are completely orthogonal facts,
// and nothing connected the two loops' knowledge of failure.
//
// This module folds both into ONE retry decision, driven by a single
// question asked after every attempt, regardless of which stage (Review's
// text checks or the real compiler) produced the failure: "is this attempt
// making real progress, or is it stuck?"
//
// "Stuck" is intentionally a conjunction, not either signal alone:
//   - Graph state alone is too blunt. A model can keep the same
//     function/shape names across retries (graph hash never moves) while
//     genuinely changing the body trying different fixes — a legitimate,
//     productive retry pattern a pure graph-hash check would kill
//     prematurely.
//   - The failure text alone is too loose. A model could ping-pong between
//     two different wrong fixes that never produce the literal same error
//     twice in a row, looping forever while a text-diff check sees
//     "different every time" and never trips.
//   - Together: if the workspace symbol table AND the literal failure
//     detail are both unchanged across two consecutive attempts, the model
//     produced the same broken result twice — not just superficially
//     different code, but a failure to make any actual progress. That's a
//     much lower false-positive signal than either check alone, though it
//     is still a heuristic, not a proof — hence the separate backstop
//     below as a true last resort.
Object.defineProperty(exports, "__esModule", { value: true });
exports.STAGNATION_THRESHOLD = exports.RETRY_BACKSTOP_CEILING = void 0;
exports.freshRetryState = freshRetryState;
exports.computeAttemptSignature = computeAttemptSignature;
exports.decideRetry = decideRetry;
exports.describeStop = describeStop;
function freshRetryState() {
    return { attempt: 0, lastSignature: null, stoppedReason: null };
}
/**
 * Generous last-resort ceiling, deliberately set well above the old
 * per-stage caps (previously 2 each) so it is not the primary mechanism —
 * the stagnation check above is. This only fires if stagnation itself
 * somehow fails to catch a real loop (e.g. a model that produces a
 * genuinely-but-uselessly different signature every single attempt). This
 * number is a judgment call, not a measured constant; revisit if real
 * usage shows it's too tight or too loose.
 */
exports.RETRY_BACKSTOP_CEILING = 8;
/** Consecutive identical signatures required before declaring stagnation.
 *  2 means: this exact attempt failed the same way as the one immediately
 *  before it. Kept as a named constant rather than inlined so it can be
 *  tuned without hunting through call sites. */
exports.STAGNATION_THRESHOLD = 2;
/**
 * Builds the combined graph-state + failure-detail signature for one
 * attempt. Graph state is reduced to a sorted, deduplicated list of
 * "kind:name:file" strings (field/param contents intentionally excluded —
 * two attempts that define the same symbols with different internal
 * field lists are still "the same shape of mistake" for stagnation
 * purposes; the literal failure detail text is what actually captures
 * whether the *content* changed in a way that matters).
 */
function computeAttemptSignature(nodes, edges, failure) {
    const nodeSig = nodes
        .map(n => `${n.kind}:${n.name}:${n.file}`)
        .sort()
        .join('|');
    const edgeSig = edges
        .map(e => `${e.type}:${e.from}->${e.to}`)
        .sort()
        .join('|');
    // Normalize whitespace/casing differences in failure detail that don't
    // reflect a real change (e.g. a temp file path embedded in a gcc error
    // would otherwise make every attempt's signature unique even when the
    // actual error is identical).
    const normalizedDetail = failure.detail
        .replace(/\/tmp\/[^\s:"]+/g, '<tmp>')
        .replace(/\s+/g, ' ')
        .trim();
    return `nodes[${nodeSig}]|edges[${edgeSig}]|detail[${normalizedDetail}]`;
}
/**
 * The single decision point both Review and the compile gate call into
 * after a failure, instead of each maintaining and checking its own
 * independent counter.
 */
function decideRetry(state, nodes, edges, failure) {
    const signature = computeAttemptSignature(nodes, edges, failure);
    const isStagnant = state.lastSignature !== null && signature === state.lastSignature;
    if (isStagnant) {
        return {
            shouldStop: true,
            reason: 'stagnant',
            nextState: { ...state, stoppedReason: 'stagnant' },
        };
    }
    const nextAttempt = state.attempt + 1;
    if (nextAttempt >= exports.RETRY_BACKSTOP_CEILING) {
        return {
            shouldStop: true,
            reason: 'backstop',
            nextState: { attempt: nextAttempt, lastSignature: signature, stoppedReason: 'backstop' },
        };
    }
    return {
        shouldStop: false,
        reason: 'continue',
        nextState: { attempt: nextAttempt, lastSignature: signature, stoppedReason: null },
    };
}
/** Human-readable explanation for the panel/status bar when retries stop. */
function describeStop(decision, failure) {
    if (decision.reason === 'stagnant') {
        return `Stuck: the same ${failure.source === 'review' ? 'review check failures' : 'compile error'} ` +
            `repeated with no change to the generated code's symbols after attempt ${decision.nextState.attempt}. ` +
            `Stopping rather than retrying a fix that isn't taking effect. Manual fix needed.`;
    }
    if (decision.reason === 'backstop') {
        return `Stopped after ${exports.RETRY_BACKSTOP_CEILING} attempts (last-resort limit — each attempt ` +
            `produced a different result, but none succeeded). Manual fix needed.`;
    }
    return '';
}
//# sourceMappingURL=retryLoop.js.map