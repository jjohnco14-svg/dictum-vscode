"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.DictumDiagnostics = void 0;
// diagnostics.ts — VS Code diagnostic provider for .dict files
const vscode = require("vscode");
const transpiler_1 = require("./transpiler");
const settings_1 = require("./settings");
class DictumDiagnostics {
    constructor(extensionPath) {
        this.pending = new Map();
        this._flushing = false;
        this._flushAgainRequested = false;
        this._extensionPath = extensionPath;
        this.collection = vscode.languages.createDiagnosticCollection('dictum');
    }
    subscribe(context) {
        context.subscriptions.push(this.collection);
        vscode.workspace.onDidChangeTextDocument(e => {
            if (e.document.languageId !== 'dictum')
                return;
            if (!(0, settings_1.getConfig)().enableDiagnostics)
                return;
            this.pending.set(e.document.uri.toString(), e.document.getText());
            clearTimeout(this.debounceTimer);
            this.debounceTimer = setTimeout(() => this._flushPending(), 600);
        }, null, context.subscriptions);
        vscode.workspace.onDidSaveTextDocument(doc => {
            if (doc.languageId !== 'dictum')
                return;
            if (!(0, settings_1.getConfig)().enableDiagnostics)
                return;
            this.pending.delete(doc.uri.toString());
            this._validateDoc(doc.uri, doc.getText());
        }, null, context.subscriptions);
        vscode.workspace.onDidCloseTextDocument(doc => { this.collection.delete(doc.uri); }, null, context.subscriptions);
        for (const doc of vscode.workspace.textDocuments) {
            if (doc.languageId === 'dictum')
                this._validateDoc(doc.uri, doc.getText());
        }
    }
    async _flushPending() {
        if (this._flushing) {
            // A flush is already running — mark that another pass is needed
            // once it finishes, instead of starting an overlapping run.
            this._flushAgainRequested = true;
            return;
        }
        this._flushing = true;
        try {
            do {
                this._flushAgainRequested = false;
                const entries = Array.from(this.pending.entries());
                this.pending.clear();
                for (const [uriStr, text] of entries)
                    await this._validateDoc(vscode.Uri.parse(uriStr), text);
            } while (this._flushAgainRequested && this.pending.size > 0 || this._flushAgainRequested);
        }
        finally {
            this._flushing = false;
        }
    }
    async _validateDoc(uri, text) {
        const cfg = (0, settings_1.getConfig)();
        if (!cfg.enableDiagnostics) {
            this.collection.delete(uri);
            return;
        }
        try {
            // Use injected extensionPath — no broken extension lookup
            const compilerScript = (0, settings_1.getCompilerPath)(this._extensionPath);
            const diagErrors = await (0, transpiler_1.validate)(text, cfg.pythonPath, compilerScript);
            this.collection.set(uri, diagErrors.map(d => toDiagnostic(d)));
        }
        catch { /* silent */ }
    }
    clear(uri) { this.collection.delete(uri); }
}
exports.DictumDiagnostics = DictumDiagnostics;
function toDiagnostic(d) {
    const range = new vscode.Range(d.line, 0, d.line, 999);
    const severity = d.severity === 'error' ? vscode.DiagnosticSeverity.Error : vscode.DiagnosticSeverity.Warning;
    const diag = new vscode.Diagnostic(range, d.message, severity);
    diag.source = 'dictumc';
    return diag;
}
//# sourceMappingURL=diagnostics.js.map