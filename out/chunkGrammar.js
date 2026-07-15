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
exports.extractReservedNames = extractReservedNames;

const { spawn } = require("child_process");
const path = require("path");

/**
 * Pulls every shape/action/field name already committed to the
 * accumulated source (i.e. every EARLIER chunk's output) so the NEXT
 * chunk's grammar can be told "don't re-declare these" -- see
 * generateChunkGrammar's reservedNames param. Regex-based on purpose,
 * same tradeoff graph.js's own lightweight symbol tracking already
 * makes for this file: exact AST-level extraction would need a full
 * parse of source that might currently be mid-chunk/invalid, and this
 * only needs to be a safe, additive OVER-approximation (reserving a
 * name that wasn't really a declaration just makes one specific decl
 * pick unavailable in a rare case -- generate() always has an open-
 * identifier-class fallback if that empties the candidate list; it
 * never breaks a build outright).
 */
function extractReservedNames(accumulatedSource) {
    const src = accumulatedSource || "";
    const names = new Set();
    for (const m of src.matchAll(/^\s*shape\s+(\w+)/gm)) names.add(m[1]);
    for (const m of src.matchAll(/^\s*action\s+(\w+)/gm)) names.add(m[1]);
    // field names: lines inside a `shape ... holds ... end shape` block,
    // of the form `<name> as <type>` at one level of indent.
    for (const m of src.matchAll(/^\s+(\w+)\s+as\s+/gm)) names.add(m[1]);
    return Array.from(names);
}

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
 * reservedNames: shape/action/field names already emitted into the
 *         accumulated source by EARLIER chunks in this build (see
 *         extractReservedNames below). Optional -- omitting it is
 *         identical to the previous behavior; it only ever narrows the
 *         grammar's decl-position candidates further, never widens it.
 *         Fixes the cross-chunk collision class ("redefinition of
 *         'Score'" -- a shape name reused as a later chunk's local
 *         variable) that a per-chunk-only grammar call can't see on
 *         its own, since chunk_grammar.py is spawned fresh per chunk
 *         with no memory of prior chunks.
 */
function generateChunkGrammar(ext, pythonPath, chunk, unsafe, reservedNames = [], timeoutMs = 5000) {
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
            reserved_names: Array.isArray(reservedNames) ? reservedNames : [],
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
