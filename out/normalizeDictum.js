"use strict";
// normalizeDictum.js -- Node-side bridge to compiler/dictumc/normalize_dictum.py.
//
// Same spawn/stdin/stdout contract as chunkGrammar.js. Sits between raw
// generation and patchEngine.applyChunk in _runBuildChunk: either returns
// fixed text (fixable-class mistakes -- repetition, unplanned/duplicate
// fields, missing closers) or a refusal reason (unrecoverable-class
// mistakes -- see normalize_dictum.py's module docstring for exactly
// which is which and why). A refusal is NOT a bridge failure -- it's
// the module correctly declining to guess, and the caller is expected
// to route it into the SAME retry-with-correction-context path an L2
// check failure already uses, not treat it as separate error handling.
Object.defineProperty(exports, "__esModule", { value: true });
exports.normalizeDictum = normalizeDictum;

const { spawn } = require("child_process");
const path = require("path");

/**
 * Returns one of:
 *   { ok: true, text: <normalized dictum text> }
 *   { ok: false, reason: <string> }              -- refused, needs retry
 *   { ok: null }                                  -- bridge itself failed
 *      (python3 missing, script error, timeout) -- caller should treat
 *      this like chunkGrammar.js's null contract: fall back to using
 *      the RAW text unnormalized rather than blocking the build on a
 *      normalizer bug. Distinct from `ok:false`, which is a confident
 *      "this needs a retry", not "something went wrong here".
 */
function normalizeDictum(ext, pythonPath, rawText, planItems, timeoutMs = 5000) {
    return new Promise((resolve) => {
        let script;
        try {
            script = path.join(ext, "compiler", "dictumc", "normalize_dictum.py");
        }
        catch {
            resolve({ ok: null });
            return;
        }
        const payload = JSON.stringify({
            raw: rawText,
            items: (planItems || []).map((it) => ({ category: it.category, id: it.id, desc: it.desc })),
        });
        let proc;
        try {
            proc = spawn(pythonPath || "python3", [script, "--bridge"], { stdio: ["pipe", "pipe", "pipe"] });
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
                const parsed = JSON.parse(out);
                resolve(parsed);
            }
            catch {
                resolve({ ok: null });
            }
        });
        proc.stdin.write(payload);
        proc.stdin.end();
    });
}
