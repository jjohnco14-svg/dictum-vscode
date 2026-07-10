"use strict";
// patchEngine.js -- Part 1 (continuing Plan/Build/Review sessions).
//
// _runBuild's existing accumulation model is pure append: each chunk's
// generated text is concatenated onto what came before. That's correct
// for brand-new content (a new shape, a new action) but wrong for a
// continuation request that means "change this existing action" --
// appending a second `action update_enemy` definition either shadows the
// first (in C, a duplicate top-level function name is a hard redefinition
// error) or leaves stale dead code sitting next to the real one.
//
// This module finds the exact span of an existing named action/shape/
// module block in the current source and replaces it, so a MODIFY-tier
// chunk's output lands in the right place instead of just being appended.
Object.defineProperty(exports, "__esModule", { value: true });
exports.findBlock = findBlock;
exports.applyPatch = applyPatch;
exports.applyChunk = applyChunk;

const BLOCK_OPENERS = [
    { kind: 'action', re: /^[ \t]*action\s+(\w+)\b.*:\s*$/m, endRe: /^[ \t]*end\s+action\s*$/m },
    { kind: 'shape', re: /^[ \t]*shape\s+(\w+)\s+holds\s*:\s*$/m, endRe: /^[ \t]*end\s+shape\s*$/m },
    { kind: 'module', re: /^[ \t]*module\s+(\w+)\s*:\s*$/m, endRe: /^[ \t]*end\s+module\s*$/m },
];

/**
 * Finds the [start, end) character span of a named action/shape/module in
 * `source`, searching top-level occurrences first, then inside module
 * bodies (a sibling action inside `module X:` is still addressable by its
 * bare name -- matches how Dictum call resolution already treats it).
 * Returns null if no block with that name exists (a genuinely new chunk,
 * not a modification -- caller should append instead).
 */
function findBlock(source, name, kind = null) {
    const openers = kind ? BLOCK_OPENERS.filter(b => b.kind === kind) : BLOCK_OPENERS;
    for (const { kind: k, re, endRe } of openers) {
        // Build a name-specific opener regex from the generic one so we
        // don't just find the FIRST action, we find the one named `name`.
        const namedRe = new RegExp(re.source.replace('(\\w+)', `(${name})`), 'm');
        const m = namedRe.exec(source);
        if (!m) continue;
        const startLineStart = source.lastIndexOf('\n', m.index) + 1;
        const searchFrom = m.index + m[0].length;
        const endMatch = endRe.exec(source.slice(searchFrom));
        if (!endMatch) continue; // malformed source -- caller should fall back to append
        const blockEnd = searchFrom + endMatch.index + endMatch[0].length;
        return { kind: k, name, start: startLineStart, end: blockEnd };
    }
    return null;
}

/**
 * Replaces the named block's exact span with `newBody` (expected to be a
 * complete, self-contained `action ... end action` / `shape ... end
 * shape` / `module ... end module` block, i.e. exactly what Build already
 * produces for a fresh chunk of that kind -- this does not need Build to
 * emit a different shape of output for a MODIFY chunk vs an OPERATION
 * chunk, only that its target name already exists in `source`).
 *
 * Returns { patched: true, source: <new source> } on success, or
 * { patched: false, reason } if the target block wasn't found -- callers
 * should treat `patched: false` as "append this instead", not an error,
 * since a MODIFY-tier plan item CAN legitimately target something that
 * doesn't exist yet (e.g. Plan mis-tiered a genuinely new action).
 */
function applyPatch(source, name, newBody, kind = null) {
    const block = findBlock(source, name, kind);
    if (!block) {
        return { patched: false, reason: `no existing ${kind || 'action/shape/module'} named '${name}' found` };
    }
    const before = source.slice(0, block.start);
    const after = source.slice(block.end);
    // Preserve the original block's indentation level (e.g. an action
    // nested inside a `module X:` is indented one level) rather than
    // dropping the replacement to column 0.
    const originalFirstLine = source.slice(block.start, source.indexOf('\n', block.start));
    const indentMatch = originalFirstLine.match(/^[ \t]*/);
    const indent = indentMatch ? indentMatch[0] : '';
    const rawLines = newBody.replace(/^\n+/, '').replace(/\n+$/, '').split('\n');
    const nonBlank = rawLines.filter(l => l.trim());
    const commonIndent = nonBlank.length
        ? nonBlank.reduce((min, line) => {
            const m = line.match(/^[ \t]*/)[0];
            return m.length < min.length ? m : min;
        }, nonBlank[0].match(/^[ \t]*/)[0])
        : '';
    const reindented = rawLines.map(line => {
        if (!line.trim()) return '';
        const dedented = line.startsWith(commonIndent) ? line.slice(commonIndent.length) : line.trimStart();
        return indent + dedented;
    }).join('\n');
    const patchedSource = `${before}${reindented}\n${after}`.replace(/\n{3,}/g, '\n\n');
    return { patched: true, source: patchedSource, replacedSpan: block };
}

const ACTION_NAME_RE = /^[ \t]*action\s+(\w+)\b/m;
const SHAPE_NAME_RE = /^[ \t]*shape\s+(\w+)\s+holds/m;

// BUGFIX (tool-mode chunk concatenation): jsonChunkToDictum() (toolSchema.js)
// returns text with NO trailing newline -- by design it's a pure assembly
// function, not responsible for inter-chunk spacing. Every append call
// site here used to be a bare `accumulated + generatedText`, so two
// consecutive tool-mode chunks (an entirely ordinary sequence -- e.g. a
// TYPE chunk followed by an OPERATION chunk) glued together with zero
// separator: "end shapeaction deposit ..." -- tokens merged, not just an
// ugly whitespace nit. Plain-mode/GBNF model output happens to include its
// own blank lines, which is exactly why this was invisible until two real
// tool-mode chunks ran back-to-back through this exact function. Fixed at
// this single choke point rather than requiring every caller/generator to
// remember to pad its own output.
function _joinChunk(accumulated, generatedText) {
    if (accumulated.length === 0 || /\s$/.test(accumulated) || /^\s/.test(generatedText)) {
        return accumulated + generatedText;
    }
    return accumulated + "\n\n" + generatedText;
}

/**
 * Convenience entry point used by the Build accumulation loop: given the
 * current accumulated source and one chunk's generated code, decides
 * whether this chunk is a MODIFY (patch in place) or anything else
 * (append, the existing/original behavior) based on the chunk's plan
 * item category and target name extracted from its own generated text.
 * Falls back to append whenever a patch can't be confidently applied, so
 * this can never make a continuation build WORSE than plain append would
 * have -- only better when it can find the target.
 */
function applyChunk(accumulated, chunk, generatedText) {
    const isModify = (chunk.tierName === 'MODIFY') ||
        (chunk.items && chunk.items.some(i => (i.category || '').toUpperCase() === 'MODIFY'));
    if (!isModify) {
        return { source: _joinChunk(accumulated, generatedText), mode: 'append' };
    }
    const actionMatch = ACTION_NAME_RE.exec(generatedText);
    const shapeMatch = SHAPE_NAME_RE.exec(generatedText);
    const name = actionMatch?.[1] || shapeMatch?.[1];
    const kind = actionMatch ? 'action' : (shapeMatch ? 'shape' : null);
    if (!name) {
        return { source: _joinChunk(accumulated, generatedText), mode: 'append', note: 'MODIFY chunk but no action/shape name found in generated text' };
    }
    const result = applyPatch(accumulated, name, generatedText, kind);
    if (!result.patched) {
        return { source: _joinChunk(accumulated, generatedText), mode: 'append', note: result.reason };
    }
    return { source: result.source, mode: 'patch', target: name, kind };
}
