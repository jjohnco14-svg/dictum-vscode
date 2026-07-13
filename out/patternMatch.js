"use strict";
// patternMatch.js -- maps a Build chunk to at most one codegraph
// pattern_ref, by cheap keyword matching over the chunk's own plan text.
//
// WHY A HEURISTIC AND NOT AN EMBEDDING/SIMILARITY LOOKUP: PATTERN_SCHEMA.md
// deliberately scopes pattern_graph.py's own lookup to exact pattern_ref
// keys, no fuzzy matching -- the intended long-term mechanism is a Plan-
// phase [PATTERN: pattern_ref] directive (parallel to the existing
// [SKILL: name]/[FILE: name] directives), not free-text retrieval. That
// directive doesn't exist yet. This module is the interim bridge: until
// Plan emits pattern_ref directly, Build guesses ONE relevant pattern from
// the chunk's own text using the same kind of cheap, explicit, auditable
// regex rules chunking.js already uses for its own routing decisions
// (see chunking.js's HOST_ACTION_RE/OPERATION_ACTION_RE) -- not a step
// backward in rigor, just a smaller, more literal version of the same
// approach, with an explicit expiry condition (delete this file once Plan
// emits pattern_ref directly).
//
// WHY AT MOST ONE MATCH: chunking.js's whole design is built around
// keeping each chunk's prompt small and roughly constant-size, because
// prompt PROCESSING time (not just generation time) is a real, separate,
// often-substantial cost on CPU-only hardware (see chunking.js's own
// chunkBudgetFromCalibration docstring). Injecting more than one pattern's
// few-shot context per chunk would grow prompt size per chunk in a way
// nothing in the existing budget accounting expects. One well-chosen
// example per chunk is the bounded version of "show the model what
// correct Dictum for this construct looks like."
//
// Pure function, no I/O, no subprocess -- same testability property
// chunking.js's buildChunks has (see simulate_vibecoding.js).
Object.defineProperty(exports, "__esModule", { value: true });
exports.matchPatternRef = matchPatternRef;

/**
 * Ordered rules, most-specific/rarest construct first. Order matters:
 * e.g. importc-math's own canonical example contains a `while I is less
 * than 3 repeat` loop, so the math/raylib rules MUST be checked before
 * the generic while-loop rule or every importc-math chunk would falsely
 * match while-loop instead.
 */
const RULES = [
    { ref: "importc-raylib", test: /\braylib\b|initwindow|begindrawing|drawcircle|drawpixel|drawline/i },
    { ref: "importc-math", test: /import from c/i, also: /\b(sqrt|cos|sin|floor|ceil|fabs|exp|log)\b/i },
    { ref: "atomic-increment", test: /atomic_faa|\batomic\b/i },
    { ref: "unsafe-malloc", test: /raw_malloc|raw_free/i },
    { ref: "pointer-ops", test: /raw pointer/i },
    { ref: "while-loop", test: /\bwhile\b.*\brepeat\b/i },
    { ref: "shape-actions", test: /\bshape\b/i, also: /\baction\b/i },
    { ref: "shape-declaration", test: /\bholds\b/i },
    { ref: "hello-world", test: /\bprogram\b/i },
];

/**
 * Returns the matched pattern_ref (a string) or null if nothing matched.
 * tierName/items mirror the shape chunking.js's buildChunks() produces:
 * items is an array of {category, id, desc}.
 */
function matchPatternRef(tierName, items) {
    const text = (items || []).map((it) => it && it.desc ? it.desc : "").join(" ");
    if (!text.trim())
        return null;
    for (const rule of RULES) {
        if (rule.test.test(text) && (!rule.also || rule.also.test(text))) {
            return rule.ref;
        }
    }
    return null;
}
