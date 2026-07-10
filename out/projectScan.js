"use strict";
// projectScan.js -- Part 2 (project-wide codegraph).
//
// graph.js already tracks the whole workspace's currently-OPEN documents
// (indexSource is called from onDidOpenTextDocument/onDidChangeTextDocument
// in extension.js's activate()), and can persist that graph across reloads
// via initStorage(..., 'project'). What's missing is proactive discovery:
// a .dict file the user hasn't opened in an editor tab yet is invisible to
// the graph, even though it's a real part of the project.
//
// This module closes that gap by walking the project folder once (matching
// dictum.project.json's format from project_builder.py exactly, so a
// project already built with `dictum.buildProject` is automatically
// recognized here too) and feeding every file it finds to graph.js's
// EXISTING indexSource -- this module does no symbol extraction of its
// own, only discovery, so there is exactly one place symbol extraction
// logic lives for the purposes of the AI pipeline (graph.js). (Python's
// project_builder.py has its own independent extraction, parse_deps, for
// the separate non-AI compile/link/Makefile pipeline -- a pre-existing,
// separate concern this module doesn't touch or duplicate further.)
Object.defineProperty(exports, "__esModule", { value: true });
exports.loadOrDiscoverManifest = loadOrDiscoverManifest;
exports.findProjectFiles = findProjectFiles;
exports.scanProject = scanProject;
exports.resolveTargetFile = resolveTargetFile;

const fs = require("fs");
const path = require("path");

const MANIFEST_NAME = 'dictum.project.json';
const DEFAULT_EXCLUDE = ['build/', '.git/', 'node_modules/'];

/**
 * Mirrors project_builder.py's load_or_create_manifest exactly (same
 * defaults, same auto-discover fallback) so a project manifest means the
 * same thing whether it's read by the AI pipeline or by `dictum.buildProject`.
 * Does NOT write a manifest to disk if one doesn't exist (unlike the
 * Python version, which is invoked explicitly by a build command) --
 * scanning shouldn't have the side effect of creating project files.
 */
function loadOrDiscoverManifest(workspaceRoot) {
    const manifestPath = path.join(workspaceRoot, MANIFEST_NAME);
    if (fs.existsSync(manifestPath)) {
        try {
            return { manifest: JSON.parse(fs.readFileSync(manifestPath, 'utf8')), manifestPath, found: true };
        } catch (e) {
            return { manifest: null, manifestPath, found: true, error: `malformed ${MANIFEST_NAME}: ${e.message}` };
        }
    }
    return {
        manifest: {
            name: path.basename(workspaceRoot),
            version: '0.1.0',
            backend: 'c',
            cpp_standard: 17,
            entry: null,
            exclude: DEFAULT_EXCLUDE,
        },
        manifestPath, found: false,
    };
}

function _isExcluded(relPath, excludePatterns) {
    const normalized = relPath.split(path.sep).join('/');
    return excludePatterns.some(pat => {
        if (pat.endsWith('/')) return normalized.startsWith(pat) || normalized.includes('/' + pat);
        return normalized === pat || normalized.endsWith('/' + pat);
    });
}

/**
 * Walks workspaceRoot for every .dict file, respecting the manifest's
 * exclude patterns. Returns absolute paths.
 */
function findProjectFiles(workspaceRoot, manifest) {
    const exclude = manifest?.exclude || DEFAULT_EXCLUDE;
    const found = [];
    function walk(dir) {
        let entries;
        try {
            entries = fs.readdirSync(dir, { withFileTypes: true });
        } catch {
            return;
        }
        for (const entry of entries) {
            const abs = path.join(dir, entry.name);
            const rel = path.relative(workspaceRoot, abs);
            if (_isExcluded(rel, exclude)) continue;
            if (entry.isDirectory()) {
                walk(abs);
            } else if (entry.isFile() && entry.name.endsWith('.dict')) {
                found.push(abs);
            }
        }
    }
    walk(workspaceRoot);
    return found.sort();
}

/**
 * Full project scan: discovers every .dict file and indexes each one via
 * the given graph module's indexSource (passed in rather than required
 * directly, so this module has no hard dependency on graph.js's specific
 * location -- keeps it testable standalone too).
 *
 * Returns a summary, not the graph itself (callers already have
 * graph.getNodes()/getEdges() available after this runs).
 */
function scanProject(workspaceRoot, graphModule) {
    const { manifest, found: manifestFound, error: manifestError } = loadOrDiscoverManifest(workspaceRoot);
    if (manifestError) {
        return { ok: false, error: manifestError, filesIndexed: [] };
    }
    const files = findProjectFiles(workspaceRoot, manifest);
    const filesIndexed = [];
    for (const file of files) {
        try {
            const content = fs.readFileSync(file, 'utf8');
            graphModule.indexSource(file, content);
            filesIndexed.push(file);
        } catch (e) {
            // A single unreadable file shouldn't abort the whole scan.
            continue;
        }
    }
    return { ok: true, manifest, manifestFound, filesIndexed };
}

/**
 * Resolves which file a new/modified chunk's output should be written to.
 * Priority: explicit [FILE: name] directive from Plan > manifest's entry
 * file > the single existing project file if there's exactly one > null
 * (caller must ask, or default to a fresh file -- genuinely ambiguous).
 */
function resolveTargetFile(workspaceRoot, manifest, requestedFile, existingFiles) {
    if (requestedFile) {
        const resolved = path.isAbsolute(requestedFile) ? requestedFile : path.join(workspaceRoot, requestedFile);
        return { file: resolved, isNew: !fs.existsSync(resolved), reason: 'explicit [FILE:] directive' };
    }
    if (manifest?.entry) {
        const resolved = path.isAbsolute(manifest.entry) ? manifest.entry : path.join(workspaceRoot, manifest.entry);
        return { file: resolved, isNew: !fs.existsSync(resolved), reason: 'manifest entry' };
    }
    if (existingFiles && existingFiles.length === 1) {
        return { file: existingFiles[0], isNew: false, reason: 'single existing project file' };
    }
    return { file: null, isNew: null, reason: existingFiles && existingFiles.length > 1
        ? 'ambiguous: multiple project files exist and no [FILE:]/entry given'
        : 'no project files exist yet' };
}
