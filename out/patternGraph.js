"use strict";
// patternGraph.js -- Node-side bridge to compiler/dictumc/pattern_graph.py.
//
// Same spawn/stdin/stdout contract as normalizeDictum.js and
// chunkGrammar.js: spawn python3, feed JSON on stdin, read JSON back on
// stdout. Kept as its own module for the same reason chunkGrammar.js
// and normalizeDictum.js are separate from chunking.js/patchEngine.js --
// this concern (turning a pattern_ref into few-shot Build context) is
// independent of chunk-splitting and patch-application logic, and
// needs a python3 subprocess while those don't.
//
// See codegraph/PATTERN_SCHEMA.md for what a pattern is and why this
// exists (short version: GBNF constrains shape, not content -- a model
// that's never seen Dictum fills an open slot with a Python/C/JS habit;
// this gives the Build prompt a real, correct Dictum example to show it
// first).
Object.defineProperty(exports, "__esModule", { value: true });
exports.renderPatternContext = renderPatternContext;
exports.tryDeterministicExpand = tryDeterministicExpand;
exports.listPatterns = listPatterns;

const { spawn } = require("child_process");
const path = require("path");

function _runBridge(ext, pythonPath, script, args, payload, timeoutMs) {
    return new Promise((resolve) => {
        let scriptPath;
        try {
            scriptPath = path.join(ext, "compiler", "dictumc", script);
        }
        catch {
            resolve({ ok: null });
            return;
        }
        let proc;
        try {
            proc = spawn(pythonPath || "python3", [scriptPath, ...args], { stdio: ["pipe", "pipe", "pipe"] });
        }
        catch {
            resolve({ ok: null });
            return;
        }
        let out = "";
        let errored = false;
        const timer = setTimeout(() => {
            errored = true;
            try { proc.kill(); } catch { /* ignore */ }
            resolve({ ok: null });
        }, timeoutMs);
        proc.stdout.on("data", (d) => { out += d.toString("utf8"); });
        proc.stderr.on("data", () => { /* surfaced via non-zero exit / bad JSON below */ });
        proc.on("error", () => {
            if (errored) return;
            errored = true;
            clearTimeout(timer);
            resolve({ ok: null });
        });
        proc.on("close", (code) => {
            if (errored) return;
            clearTimeout(timer);
            if (code !== 0 || !out.trim()) {
                resolve({ ok: null });
                return;
            }
            try {
                resolve(JSON.parse(out));
            }
            catch {
                resolve({ ok: null });
            }
        });
        if (payload !== null) {
            proc.stdin.write(JSON.stringify(payload));
        }
        proc.stdin.end();
    });
}

/**
 * Returns one of:
 *   { ok: true, rendered: <few-shot context string>, bound: <string|null> }
 *   { ok: false, detail: <string> }   -- pattern not found, or binding
 *      failed (missing/invalid params). A confident, expected refusal --
 *      NOT a bridge failure. Callers should treat this the same way
 *      normalizeDictum's ok:false is treated: not fatal, just "this
 *      specific request didn't resolve", and fall back to building the
 *      prompt without this pattern's context rather than blocking the
 *      whole Build step on it.
 *   { ok: null }   -- bridge itself failed (python3 missing, script
 *      error, timeout, bad JSON). Same fallback contract as
 *      chunkGrammar.js's null: never block a build on a bug here, just
 *      proceed without the extra context.
 *
 * params may be omitted/undefined to render just the pattern's
 * description + canonical example (no specific bound instance) --
 * useful for a first pass before the caller has concrete values yet.
 */
function renderPatternContext(ext, pythonPath, patternRef, params, timeoutMs = 5000) {
    const payload = { pattern_ref: patternRef };
    if (params !== undefined && params !== null) {
        payload.params = params;
    }
    return _runBridge(ext, pythonPath, "pattern_graph.py", ["--bridge"], payload, timeoutMs);
}

/**
 * Fast path for the small set of patterns whose Dictum expansion is
 * fully mechanical given just the plan text (currently atomic-increment
 * and unsafe-malloc -- see pattern_graph.py's _DETERMINISTIC_REFS).
 * Returns one of:
 *   { ok: true, deterministic: true, bound: <ready-to-use Dictum text> }
 *     -- this chunk's plan text contained everything needed; the
 *        caller can skip the model call entirely for this chunk.
 *   { ok: true, deterministic: false, rendered: <few-shot text>, bound: null }
 *     -- not a mechanical pattern, or the plan text didn't have enough
 *        (e.g. no explicit delta) -- same as calling renderPatternContext
 *        directly; caller should fall back to the normal LLM path.
 *   { ok: false, detail } | { ok: null }
 *     -- same fallback contract as renderPatternContext: never block a
 *        build on this, just proceed as if it returned deterministic:false.
 *
 * planText should be the chunk's own combined plan-item text (same
 * string patternMatch.js's matchPatternRef was given), NOT the whole
 * plan -- extraction is scoped to one chunk's action/target/delta.
 */
function tryDeterministicExpand(ext, pythonPath, patternRef, planText, timeoutMs = 5000) {
    const payload = { pattern_ref: patternRef, plan_text: planText };
    return _runBridge(ext, pythonPath, "pattern_graph.py", ["--bridge"], payload, timeoutMs);
}

/**
 * Returns { ok: true, patterns: [{pattern_ref, category, description}, ...] }
 * or { ok: null } on bridge failure. Useful for a future Plan-phase
 * step that wants to know what pattern_refs exist before referencing
 * one -- not currently called from anywhere in the Build path itself.
 */
function listPatterns(ext, pythonPath, timeoutMs = 5000) {
    return _runBridge(ext, pythonPath, "pattern_graph.py", ["--list"], null, timeoutMs);
}
