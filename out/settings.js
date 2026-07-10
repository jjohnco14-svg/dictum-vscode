"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.getConfig = getConfig;
exports.getCompilerPath = getCompilerPath;
// settings.ts — centralised config accessors for the Dictum extension
const vscode = require("vscode");
const path = require("path");
function getConfig() {
    const cfg = vscode.workspace.getConfiguration('dictum');
    const baseUrl = cfg.get('baseUrl', '') || cfg.get('ollamaUrl', 'http://localhost:11434');
    // FIX: Build previously shared Plan/Review's provider/baseUrl/apiKey —
    // there was no separate config surface for it at all. The moment a
    // person configured a cloud provider (e.g. NVIDIA NIM) for good Plan/
    // Review quality, Build silently inherited it too, and
    // ollama.autoGrammarMode() (a hard `provider === 'koboldcpp' ? 'gbnf' :
    // 'tools'` switch) then dropped GBNF entirely for Build's generation —
    // not because GBNF was broken, but because it became unreachable.
    //
    // FIX 2: this default used to be 'ollama' with a buildBaseUrl default
    // of 'http://localhost:11434' (Ollama's port) — but koboldcpp is the
    // actual GBNF-capable Build provider (confirmed: Ollama's real API has
    // no grammar field at all), and package.json's own configuration
    // schema default for dictum.buildProvider is 'koboldcpp', not
    // 'ollama'. These two defaults had drifted out of sync with each
    // other and with the real architecture. KoboldCpp's default port is
    // 5001, not Ollama's 11434.
    const buildBaseUrl = cfg.get('buildBaseUrl', '') || 'http://localhost:5001';
    return {
        provider: cfg.get('provider', 'ollama'),
        baseUrl,
        ollamaUrl: baseUrl,
        apiKey: cfg.get('apiKey', ''),
        buildProvider: cfg.get('buildProvider', 'koboldcpp'),
        buildBaseUrl,
        buildApiKey: cfg.get('buildApiKey', ''),
        topModel: cfg.get('topModel', 'llama3.1:8b'),
        buildModel: cfg.get('buildModel', 'llama3.1:8b'),
        backend: cfg.get('backend', 'c'),
        cppStandard: cfg.get('cppStandard', 17),
        pythonPath: cfg.get('pythonPath', 'python'),
        grammarMode: cfg.get('grammarMode', 'gbnf'),
        graphPersistence: cfg.get('graphPersistence', 'session'),
        activeSkill: cfg.get('activeSkill', 'general'),
        rpmLimit: cfg.get('rpmLimit', 0),
        buildRpmLimit: cfg.get('buildRpmLimit', 0),
        actMode: cfg.get('actMode', false),
        autoApprove: cfg.get('autoApprove', false),
        strictPlanMode: cfg.get('strictPlanMode', false),
        autoCompact: cfg.get('autoCompact', true),
        focusChain: cfg.get('focusChain', true),
        enableDiagnostics: cfg.get('enableDiagnostics', true),
        temperature: cfg.get('temperature', 0.2),
        maxTokens: cfg.get('maxTokens', 4096),
        contextWindow: cfg.get('contextWindow', 8192),
    };
}
/** Absolute path to dictumc_cli.py inside the installed extension. */
function getCompilerPath(extensionPath) {
    return path.join(extensionPath, 'compiler', 'dictumc_cli.py');
}
//# sourceMappingURL=settings.js.map