"use strict";
// chunkGrammar.js -- Node-side bridge to compiler/dictumc/chunk_grammar.py.
//
// Same pattern bridge.js already uses to query type_registry.py directly
// (see SOURCE_OF_TRUTH.md section 1) rather than re-implementing grammar
// logic in JS: spawn python3, feed it JSON on stdin, read GBNF text back
// on stdout. Kept as its own small module (not folded into chunking.js)
// because chunking.js's job is deciding WHICH plan items go in which
// chunk -- a pure, dependency-free function tested by simulate_vibecoding.js
// today. This module's job is turning one already-decided chunk into a
// grammar string, which needs a python3 subprocess; keeping that concern
// separate means chunking.js keeps its existing zero-external-dependency
// property.
Object.defineProperty(exports, "__esModule", { value: true });
exports.generateChunkGrammar = generateChunkGrammar;

const { spawn } = require("child_process");
const path = require("path");

/**
 * Returns the generated GBNF text for one chunk, or null if generation
 * failed for any reason (python3 missing, chunk_grammar.py error, bad
 * JSON, timeout). Callers MUST treat null as "fall back to the static
 * dictum_safe.gbnf/dictum_unsafe.gbnf file" -- never as a hard failure.
 * That fallback contract is what makes this module strictly additive:
 * a bug here can only make a chunk's grammar less tight, never break a
 * build that would otherwise have succeeded.
 *
 * chunk: { tierName, items: [{category, id, desc}, ...] }
 * unsafe: whether this chunk may need unsafe-block/unsafe-token support
 *         (mirrors the needsUnsafe check _runBuild already does today).
 */
function generateChunkGrammar(ext, pythonPath, chunk, unsafe, timeoutMs = 5000) {
    return new Promise((resolve) => {
        let script;
        try {
            script = path.join(ext, "compiler", "dictumc", "chunk_grammar.py");
        }
        catch {
            resolve(null);
            return;
        }
        const payload = JSON.stringify({
            tierName: chunk.tierName,
            items: (chunk.items || []).map((it) => ({ category: it.category, id: it.id, desc: it.desc })),
            unsafe: !!unsafe,
        });
        let proc;
        try {
            proc = spawn(pythonPath || "python3", [script], { stdio: ["pipe", "pipe", "pipe"] });
        }
        catch {
            resolve(null);
            return;
        }
        let out = "";
        let errored = false;
        const timer = setTimeout(() => {
            errored = true;
            try {
                proc.kill();
            }
            catch { /* ignore */ }
            resolve(null);
        }, timeoutMs);
        proc.stdout.on("data", (d) => { out += d.toString("utf8"); });
        proc.stderr.on("data", () => { /* surfaced via non-zero exit below; not fatal by itself */ });
        proc.on("error", () => {
            if (errored)
                return;
            errored = true;
            clearTimeout(timer);
            resolve(null);
        });
        proc.on("close", (code) => {
            if (errored)
                return;
            clearTimeout(timer);
            if (code !== 0 || !out.trim()) {
                resolve(null);
                return;
            }
            resolve(out);
        });
        proc.stdin.write(payload);
        proc.stdin.end();
    });
}
