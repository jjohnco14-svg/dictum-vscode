"use strict";
// graph.ts — in-memory code graph for the full workspace
// Tracks shapes, actions, modules, and programs across ALL .dict files.
// This is the mechanism behind the model quality boost: the build model
// receives the complete symbol table of the workspace as prompt context.
//
// PERSISTENCE MODES:
//   - "session" (default, unchanged from before): pure in-memory Maps.
//     Cleared the moment the extension host process ends — window reload,
//     VS Code restart, or extension deactivation all wipe it. Nothing to
//     configure; this is what graph.js has always done.
//   - "project": same Maps, but every mutation (indexSource/clearFile) also
//     writes a snapshot to context.workspaceState, and that snapshot is
//     loaded back in on activation. workspaceState is already scoped per
//     workspace folder by VS Code itself, so this naturally gives "the
//     graph persists across reloads, but a different project's workspace
//     starts with its own separate graph" — no manual project-ID/path
//     bookkeeping needed.
// Mode is set via initStorage(), called once from activate(); if it's never
// called, behavior is identical to the pre-existing session-only code.
Object.defineProperty(exports, "__esModule", { value: true });
exports.indexSource = indexSource;
exports.clearFile = clearFile;
exports.getNodes = getNodes;
exports.getEdges = getEdges;
exports.getGraphData = getGraphData;
exports.buildPromptContext = buildPromptContext;
exports.initStorage = initStorage;
exports.getPersistenceMode = getPersistenceMode;
const nodesByFile = new Map();
const edgesByFile = new Map();
const WORKSPACE_STATE_KEY = 'dictum.graph.v1';
let _workspaceState = null;
let _persistenceMode = 'session';
/**
 * Wire up project-scoped persistence. Call once from activate() with
 * context.workspaceState and the desired mode ('session' or 'project').
 * Safe to call with mode: 'session' explicitly too — it just skips load/save.
 */
function initStorage(workspaceState, mode = 'session') {
    _workspaceState = workspaceState;
    _persistenceMode = mode;
    if (mode === 'project' && _workspaceState) {
        const saved = _workspaceState.get(WORKSPACE_STATE_KEY);
        if (saved && saved.nodesByFile && saved.edgesByFile) {
            for (const [file, nodes] of Object.entries(saved.nodesByFile))
                nodesByFile.set(file, nodes);
            for (const [file, edges] of Object.entries(saved.edgesByFile))
                edgesByFile.set(file, edges);
        }
    }
}
function getPersistenceMode() {
    return _persistenceMode;
}
function _persistIfProject() {
    if (_persistenceMode !== 'project' || !_workspaceState)
        return;
    _workspaceState.update(WORKSPACE_STATE_KEY, {
        nodesByFile: Object.fromEntries(nodesByFile),
        edgesByFile: Object.fromEntries(edgesByFile),
    });
}
function indexSource(filePath, source) {
    const nodes = [];
    const edges = [];
    const lines = source.split('\n');
    let currentScope = '';
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const progMatch = line.match(/^\s*program\s+(\w+)\s*:/);
        if (progMatch) {
            nodes.push({ kind: 'program', name: progMatch[1], file: filePath });
            currentScope = progMatch[1];
        }
        const modMatch = line.match(/^\s*module\s+(\w+)\s*:/);
        if (modMatch) {
            nodes.push({ kind: 'module', name: modMatch[1], file: filePath });
            currentScope = modMatch[1];
        }
        const shapeMatch = line.match(/^\s*shape\s+(\w+)\s+holds\s*:/);
        if (shapeMatch) {
            const fields = [];
            for (let j = i + 1; j < lines.length; j++) {
                if (/^\s*end\s+shape/.test(lines[j]))
                    break;
                const f = lines[j].match(/^\s+(\w+)\s+as\s+(.+)/);
                if (f)
                    fields.push(`${f[1]}: ${f[2].trim()}`);
            }
            nodes.push({ kind: 'shape', name: shapeMatch[1], file: filePath, fields });
        }
        // action Name takes/produces — handles zero-arg actions too
        const actionMatch = line.match(/^\s*action\s+(\w+)\s+(?:takes|produces)/);
        if (actionMatch) {
            const paramMatch = line.match(/takes\s+(.*?)\s+produces/);
            const params = paramMatch ? paramMatch[1].split(' and ').map(p => p.trim()) : [];
            nodes.push({ kind: 'action', name: actionMatch[1], file: filePath, params });
            if (currentScope)
                edges.push({ from: currentScope, to: actionMatch[1], type: 'defines' });
        }
        // call actionName with ...  OR  call actionName (no args)
        const callMatch = line.match(/^\s*call\s+(\w+)(?:\s+with|\s*$)/);
        if (callMatch && currentScope)
            edges.push({ from: currentScope, to: callMatch[1], type: 'calls' });
        const useMatch = line.match(/^\s*use\s+(\w+)/);
        if (useMatch && currentScope)
            edges.push({ from: currentScope, to: useMatch[1], type: 'uses' });
    }
    nodesByFile.set(filePath, nodes);
    edgesByFile.set(filePath, edges);
    _persistIfProject();
}
function clearFile(filePath) {
    nodesByFile.delete(filePath);
    edgesByFile.delete(filePath);
    _persistIfProject();
}
function getNodes() {
    const all = [];
    for (const nodes of nodesByFile.values())
        all.push(...nodes);
    return all;
}
function getEdges() {
    const all = [];
    for (const edges of edgesByFile.values())
        all.push(...edges);
    return all;
}
function getGraphData(nodes, edges) {
    return { nodes, edges };
}
/**
 * Serialise the project graph into a compact, token-efficient prompt string.
 *
 * WHY THIS EXISTS — the "100x small-model boost":
 *
 * Without this, the build model generates code blind. It has no idea what
 * shapes, actions, or modules already exist in the project. It hallucates
 * types, invents action signatures that conflict with existing APIs, and
 * re-defines shapes that are already defined in other files. Every error
 * requires human intervention.
 *
 * With this, the model receives the complete symbol table of the workspace
 * before it generates a single token. It knows:
 *   - every shape and its fields (so it uses the right field names)
 *   - every action and its parameters (so it calls them correctly)
 *   - every module and what it exports (so use statements are accurate)
 *   - every program and what it depends on (so it doesn't create conflicts)
 *
 * Cost: a 20-file project with 80 symbols costs ~400 tokens of context.
 * This is negligible for a local model and trivial for a cloud model.
 * The quality gain — especially for 7B parameter models — is substantial.
 *
 * Format: one line per symbol, kind prefix, fields/params inline,
 * filename in brackets. No Dictum syntax — pure structural information.
 */
function buildPromptContext(nodes, edges) {
    if (!nodes.length)
        return '';
    const lines = ['## Project symbols (existing — do not redefine)'];
    const shapes = nodes.filter(n => n.kind === 'shape');
    const actions = nodes.filter(n => n.kind === 'action');
    const modules = nodes.filter(n => n.kind === 'module');
    const programs = nodes.filter(n => n.kind === 'program');
    if (shapes.length) {
        lines.push('\n### Shapes');
        for (const s of shapes) {
            const fieldStr = s.fields?.length ? s.fields.join(', ') : '(empty)';
            lines.push(`shape ${s.name} { ${fieldStr} }  [${s.file.split('/').pop()}]`);
        }
    }
    if (actions.length) {
        lines.push('\n### Actions');
        for (const a of actions) {
            const paramStr = a.params?.length ? a.params.join(', ') : '()';
            lines.push(`action ${a.name}(${paramStr})  [${a.file.split('/').pop()}]`);
        }
    }
    if (modules.length) {
        lines.push('\n### Modules');
        for (const m of modules) {
            const exports = edges.filter(e => e.from === m.name && e.type === 'defines').map(e => e.to);
            lines.push(`module ${m.name} (exports: ${exports.join(', ') || 'none'})  [${m.file.split('/').pop()}]`);
        }
    }
    if (programs.length) {
        lines.push('\n### Programs');
        for (const p of programs) {
            const uses = edges.filter(e => e.from === p.name && e.type === 'uses').map(e => e.to);
            lines.push(`program ${p.name} (${uses.length ? 'uses: ' + uses.join(', ') : 'standalone'})  [${p.file.split('/').pop()}]`);
        }
    }
    return lines.join('\n');
}
//# sourceMappingURL=graph.js.map