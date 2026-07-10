"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.DictumStatusBar = void 0;
// statusBar.ts — VS Code status bar item for Dictum compiler state
const vscode = require("vscode");
class DictumStatusBar {
    constructor(_context) {
        this._item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 10);
        this._item.command = 'dictum.openSettings';
        this._item.show();
        this.setReady('c');
    }
    setReady(backend) {
        this._item.text = `$(check) Dictum [${backend}]`;
        this._item.tooltip = 'Dictum compiler ready';
        this._item.backgroundColor = undefined;
    }
    setGenerating() {
        this._item.text = '$(sync~spin) Dictum';
        this._item.tooltip = 'Dictum: generating…';
        this._item.backgroundColor = undefined;
    }
    setChecking() {
        this._item.text = '$(loading~spin) Dictum';
        this._item.tooltip = 'Dictum: checking provider…';
        this._item.backgroundColor = undefined;
    }
    setOllamaDown() {
        this._item.text = '$(warning) Dictum: provider offline';
        this._item.tooltip = 'Dictum: LLM provider not reachable — check settings';
        this._item.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
    }
    setError(msg) {
        this._item.text = `$(error) Dictum: ${msg}`;
        this._item.tooltip = msg;
        this._item.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
    }
    dispose() {
        this._item.dispose();
    }
}
exports.DictumStatusBar = DictumStatusBar;
//# sourceMappingURL=statusBar.js.map