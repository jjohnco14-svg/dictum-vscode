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

const { spawn } = require("child_process");
const path = require("path");
exports.generateChunkGrammar = generateChunkGrammar;
exports.extractReservedNames = extractReservedNames;
exports.prepareCImports = prepareCImports;

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
    // PHASE 2: returns {name, kind} pairs instead of flat strings, so
    // chunk_grammar.py's grammar generation can eventually tell "this
    // reserved name is a shape" apart from "this one is an action" or
    // "field" (finer per-kind rules, e.g. "field names may shadow shape
    // names but not action names", become possible later). Kind-tagging
    // isn't required for the current fix (C1's decl-name exclusion is
    // flat -- any reserved name, regardless of kind, can't be reused as
    // a new declaration's name), but chunk_grammar.py's
    // _reserved_name_strs() accepts BOTH this form and the old flat-
    // string form, so this is a safe, non-breaking upgrade of the
    // payload shape -- any caller still passing flat strings keeps
    // working unchanged.
    const src = accumulatedSource || "";
    const byName = new Map(); // name -> kind (first kind wins if seen more than once)
    const add = (name, kind) => { if (!byName.has(name)) byName.set(name, kind); };
    for (const m of src.matchAll(/^\s*shape\s+(\w+)/gm)) add(m[1], "shape");
    for (const m of src.matchAll(/^\s*action\s+(\w+)/gm)) add(m[1], "action");
    // field names: lines inside a `shape ... holds ... end shape` block,
    // of the form `<name> as <type>` at one level of indent.
    for (const m of src.matchAll(/^\s+(\w+)\s+as\s+/gm)) add(m[1], "field");
    return Array.from(byName, ([name, kind]) => ({ name, kind }));
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
function generateChunkGrammar(ext, pythonPath, chunk, unsafe, reservedNames = [], extraIdentifiers = [], timeoutMs = 5000) {
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
            // DISPOSABLE-RESERVE IMPORT_C: aliases (e.g. "c_sqrt") this
            // chunk needs to be able to call even though they don't
            // appear literally in its own plan text -- see
            // prepareCImports() below, and chunk_grammar.py's generate()
            // docstring for how these get folded into both `identifier`
            // (callable) and the decl-position exclusion sets (not
            // re-declarable). Optional/additive: an empty array here
            // reproduces the exact previous payload shape.
            extra_identifiers: Array.isArray(extraIdentifiers) ? extraIdentifiers : [],
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

/**
 * DISPOSABLE-RESERVE IMPORT_C bridge. Given one chunk and the build's
 * accumulated source so far, asks chunk_grammar.py's prepare_c_imports()
 * for:
 *   - importLines: ready-to-prepend, deterministic `import from C ...`
 *     top-level statements for any KNOWN_C_IMPORTS name this chunk's
 *     own plan text asks for that no earlier chunk already imported.
 *   - aliasNames: the matching Dictum-visible aliases (e.g. ["c_sqrt"])
 *     for every name this chunk needs, whether it's importing it right
 *     now or reusing an earlier chunk's import -- pass this straight
 *     into generateChunkGrammar's extraIdentifiers param so the SAME
 *     chunk's grammar can legally call it.
 *
 * Same fail-quiet contract as generateChunkGrammar: any failure at all
 * (spawn error, timeout, non-zero exit, unparseable stdout, ok:false)
 * resolves to {importLines: [], aliasNames: []} -- never throws, never
 * blocks the build. A chunk with nothing to import behaves exactly as
 * if this function didn't exist.
 */
function prepareCImports(ext, pythonPath, chunk, accumulated, timeoutMs = 5000) {
    return new Promise((resolve) => {
        const EMPTY = { importLines: [], aliasNames: [] };
        let script;
        try {
            script = path.join(ext, "compiler", "dictumc", "chunk_grammar.py");
        }
        catch {
            resolve(EMPTY);
            return;
        }
        const payload = JSON.stringify({
            chunk: {
                tierName: chunk.tierName,
                items: (chunk.items || []).map((it) => ({ category: it.category, id: it.id, desc: it.desc })),
            },
            accumulated: accumulated || "",
        });
        let proc;
        try {
            proc = spawn(pythonPath || "python3", [script, "--prepare-c-imports"], { stdio: ["pipe", "pipe", "pipe"] });
        }
        catch {
            resolve(EMPTY);
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
            resolve(EMPTY);
        }, timeoutMs);
        proc.stdout.on("data", (d) => { out += d.toString("utf8"); });
        proc.stderr.on("data", () => { /* surfaced via non-zero exit below; not fatal by itself */ });
        proc.on("error", () => {
            if (errored)
                return;
            errored = true;
            clearTimeout(timer);
            resolve(EMPTY);
        });
        proc.on("close", (code) => {
            if (errored)
                return;
            clearTimeout(timer);
            if (code !== 0 || !out.trim()) {
                resolve(EMPTY);
                return;
            }
            try {
                const parsed = JSON.parse(out);
                if (!parsed || parsed.ok !== true) {
                    resolve(EMPTY);
                    return;
                }
                resolve({
                    importLines: Array.isArray(parsed.import_lines) ? parsed.import_lines : [],
                    aliasNames: Array.isArray(parsed.alias_names) ? parsed.alias_names : [],
                });
            }
            catch {
                resolve(EMPTY);
            }
        });
        proc.stdin.write(payload);
        proc.stdin.end();
    });
}
exports.prepareCImports = prepareCImports;
