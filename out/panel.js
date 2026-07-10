"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.DictumPanel = void 0;
// panel.ts — Dictum sidebar webview panel
const vscode = require("vscode");
const SECRET_KEY = 'dictum.apiKey';
// FIX: Build previously had no API key of its own — Plan/Review's
// SECRET_KEY was the only one ever stored, so Build's API key field in the
// settings UI was always blank/shared. BUILD_SECRET_KEY mirrors the same
// SecretStorage pattern, independent of SECRET_KEY.
const BUILD_SECRET_KEY = 'dictum.buildApiKey';
class DictumPanel {
    constructor(_extensionUri, secrets) {
        this._extensionUri = _extensionUri;
        this._secrets = secrets;
    }
    /** Read API key — SecretStorage first, fallback to plain-text setting for migration. */
    async _getApiKey(secretKey = SECRET_KEY, configKey = 'apiKey') {
        if (this._secrets) {
            const secret = await this._secrets.get(secretKey);
            if (secret)
                return secret;
        }
        // Migration: if old plain-text key exists, move it to SecretStorage and clear it
        const cfg = vscode.workspace.getConfiguration('dictum');
        const legacy = cfg.get(configKey, '');
        if (legacy && this._secrets) {
            await this._secrets.store(secretKey, legacy);
            await cfg.update(configKey, '', true);
            return legacy;
        }
        return legacy;
    }
    /** Store API key securely. */
    async _setApiKey(key, secretKey = SECRET_KEY, configKey = 'apiKey') {
        if (this._secrets) {
            if (key) {
                await this._secrets.store(secretKey, key);
            }
            else {
                await this._secrets.delete(secretKey);
            }
            // Ensure plain-text setting is cleared
            await vscode.workspace.getConfiguration('dictum').update(configKey, '', true);
        }
        else {
            // Fallback if SecretStorage unavailable (shouldn't happen in VS Code 1.85+)
            await vscode.workspace.getConfiguration('dictum').update(configKey, key, true);
        }
    }
    /** Read Build's own API key — separate from Plan/Review's. */
    async _getBuildApiKey() {
        return this._getApiKey(BUILD_SECRET_KEY, 'buildApiKey');
    }
    /** Store Build's own API key — separate from Plan/Review's. */
    async _setBuildApiKey(key) {
        return this._setApiKey(key, BUILD_SECRET_KEY, 'buildApiKey');
    }
    resolveWebviewView(webviewView, _context, _token) {
        this._view = webviewView;
        webviewView.webview.options = { enableScripts: true, localResourceRoots: [this._extensionUri] };
        webviewView.webview.html = this._getHtml();
        webviewView.webview.onDidReceiveMessage(async (msg) => {
            switch (msg.command) {
                case 'set_provider': {
                    const cfg = vscode.workspace.getConfiguration('dictum');
                    await cfg.update('provider', msg.provider, true);
                    await cfg.update('baseUrl', msg.baseUrl, true);
                    if (msg.apiKey !== undefined) {
                        await this._setApiKey(msg.apiKey);
                    }
                    break;
                }
                case 'set_build_provider': {
                    // FIX: previously there was no separate persistence path for
                    // Build's provider — settings UI only ever wrote provider/
                    // baseUrl/apiKey, which Build silently shared with Plan/
                    // Review. This mirrors set_provider but writes buildProvider/
                    // buildBaseUrl/buildApiKey instead, matching settings.js's
                    // getConfig() (see extension.js fix from the previous round).
                    //
                    // FIX 2: this used to be split across TWO separate postMessage
                    // calls from the frontend (setBuildProvider() sent both
                    // 'set_grammar' and 'set_build_provider'). Each cfg.update()
                    // independently fires onDidChangeConfiguration -> _pushConfig(),
                    // and since these are separate async round-trips with no
                    // ordering guarantee, a _pushConfig() triggered by one update
                    // could read a not-yet-written value from the other and push a
                    // stale grammarMode back down — visually snapping the grammar-
                    // mode dropdown back to its old value right after picking a
                    // new Build provider. Fixed by computing grammarMode here,
                    // server-side, from the same provider value in the same
                    // handler (matching ollama.js's real autoGrammarMode logic:
                    // only 'koboldcpp' gets 'gbnf', everything else gets 'tools'),
                    // writing all four settings via Promise.all (parallel, not
                    // sequential-with-broadcast-in-between), and pushing config
                    // exactly once after all writes have actually landed.
                    const cfg = vscode.workspace.getConfiguration('dictum');
                    const grammarMode = msg.provider === 'koboldcpp' ? 'gbnf' : 'tools';
                    const updates = [
                        cfg.update('buildProvider', msg.provider, true),
                        cfg.update('buildBaseUrl', msg.baseUrl, true),
                        cfg.update('grammarMode', grammarMode, true),
                    ];
                    if (msg.apiKey !== undefined) {
                        updates.push(this._setBuildApiKey(msg.apiKey));
                    }
                    await Promise.all(updates);
                    await this._pushConfig();
                    break;
                }
                case 'set_rpm_limit': {
                    await vscode.workspace.getConfiguration('dictum').update('rpmLimit', msg.limit, true);
                    break;
                }
                case 'set_build_rpm_limit': {
                    await vscode.workspace.getConfiguration('dictum').update('buildRpmLimit', msg.limit, true);
                    break;
                }
                case 'set_graph_persistence': {
                    await vscode.workspace.getConfiguration('dictum').update('graphPersistence', msg.mode, true);
                    break;
                }
                case 'check_system_requirements': {
                    await vscode.commands.executeCommand('dictum.checkSystemRequirements');
                    break;
                }
                case 'open_external': {
                    if (msg.url)
                        vscode.env.openExternal(vscode.Uri.parse(msg.url));
                    break;
                }
                case 'set_grammar': {
                    await vscode.workspace.getConfiguration('dictum').update('grammarMode', msg.mode, true);
                    break;
                }
                case 'toggle_setting': {
                    const cfg = vscode.workspace.getConfiguration('dictum');
                    const cur = cfg.get(msg.key, false);
                    await cfg.update(msg.key, !cur, true);
                    break;
                }
                case 'set_config': {
                    // Generic single-key config setter — used by settings controls
                    // (backend, cppStandard, buildModel, grammarMode) that don't need
                    // their own dedicated message type.
                    if (msg.key) {
                        await vscode.workspace.getConfiguration('dictum').update(msg.key, msg.value, true);
                    }
                    break;
                }
                case 'generate': {
                    vscode.commands.executeCommand('dictum.generate', msg.prompt, msg.provider, msg.baseUrl, msg.apiKey, msg.topModel);
                    break;
                }
                case 'approve_plan': {
                    if (this.onPlanReceived)
                        this.onPlanReceived(msg.planText || '');
                    vscode.commands.executeCommand('dictum.approvePlan', msg.planText || '');
                    break;
                }
                case 'build': {
                    vscode.commands.executeCommand('dictum.build', msg.buildModel, msg.provider, msg.baseUrl, msg.apiKey, msg.grammarMode);
                    break;
                }
                case 'review': {
                    vscode.commands.executeCommand('dictum.review', msg.provider, msg.baseUrl, msg.apiKey, msg.topModel);
                    break;
                }
                case 'fetch_models': {
                    vscode.commands.executeCommand('dictum.fetchModels', msg.provider, msg.baseUrl, msg.apiKey, msg.target);
                    break;
                }
                case 'apply': {
                    vscode.commands.executeCommand('dictum.apply');
                    break;
                }
                case 'run': {
                    vscode.commands.executeCommand('dictum.run');
                    break;
                }
                case 'transpile': {
                    vscode.commands.executeCommand('dictum.transpile');
                    break;
                }
                case 'open_mcp_settings': {
                    vscode.commands.executeCommand('workbench.action.openSettings', 'dictum.mcpServers');
                    break;
                }
            }
        });
        this._pushMcpServers();
        this._pushConfig();
    }
    _pushMcpServers() {
        const mcpCfg = vscode.workspace.getConfiguration('dictum').get('mcpServers', []);
        this.postMcpServers(mcpCfg.map(s => ({ name: s.name || 'Unnamed', status: s.enabled !== false ? 'connected' : 'disabled' })));
    }
    async _pushConfig() {
        const cfg = vscode.workspace.getConfiguration('dictum');
        const baseUrl = cfg.get('baseUrl', '') || cfg.get('ollamaUrl', 'http://localhost:11434');
        const hasApiKey = !!(await this._getApiKey());
        // FIX: Build's provider/baseUrl/key were never pushed to the webview
        // separately — the settings panel had no way to even display them
        // differently from Plan/Review's, since there was nothing to show.
        const buildBaseUrl = cfg.get('buildBaseUrl', '') || 'http://localhost:11434';
        const hasBuildApiKey = !!(await this._getBuildApiKey());
        this.postMessage({
            type: 'config', provider: cfg.get('provider', 'ollama'), baseUrl, hasApiKey,
            buildProvider: cfg.get('buildProvider', 'koboldcpp'), buildBaseUrl, hasBuildApiKey,
            topModel: cfg.get('topModel', 'llama3.1:8b'),
            buildModel: cfg.get('buildModel', 'llama3.1:8b'),
            grammarMode: cfg.get('grammarMode', 'gbnf'),
            graphPersistence: cfg.get('graphPersistence', 'session'),
            rpmLimit: cfg.get('rpmLimit', 0),
            buildRpmLimit: cfg.get('buildRpmLimit', 0),
            backend: cfg.get('backend', 'c'), cppStandard: cfg.get('cppStandard', 17),
            actMode: cfg.get('actMode', false),
            autoApprove: cfg.get('autoApprove', false),
            strictPlanMode: cfg.get('strictPlanMode', false), autoCompact: cfg.get('autoCompact', true),
            focusChain: cfg.get('focusChain', true),
        });
    }
    postPlan(plan) { this._view?.webview.postMessage({ type: 'plan', plan }); }
    postBuildOutput(output) { this._view?.webview.postMessage({ type: 'build_output', output }); }
    postStatus(status) { this._view?.webview.postMessage({ type: 'status', status }); }
    // FIX: transient errors (429/503/connection drop) that exhausted their
    // automatic retries used to just report failure via postStatus with no
    // way to try again short of re-triggering the whole mode from scratch.
    // This posts a distinct message type so the webview can render an
    // inline manual "Retry" action next to the error, the same way Cline
    // offers a retry once its own auto-retry gives up. 'stage' is
    // 'plan' | 'build' | 'review' so the webview knows which action to
    // re-fire on click.
    postRetryableError(stage, message) { this._view?.webview.postMessage({ type: 'retryable_error', stage, message }); }
    postMcpServers(servers) { this._view?.webview.postMessage({ type: 'mcp_servers', servers }); }
    postChecks(checks) { this._view?.webview.postMessage({ type: 'checks', checks }); }
    postVerdict(passed, message) { this._view?.webview.postMessage({ type: 'verdict', passed, message }); }
    postGrammar(mode) { this._view?.webview.postMessage({ type: 'grammar', mode }); }
    postGraph(nodes, edges) { this._view?.webview.postMessage({ type: 'graph', nodes, edges }); }
    postRunState(canRun) { this._view?.webview.postMessage({ type: 'run_state', canRun }); }
    postProgress(percent, verified, total, currentItemDesc) {
        this._view?.webview.postMessage({ type: 'progress', percent, verified, total, currentItemDesc });
    }
    // Generic post — used by fetchModels and other direct-type messages
    postMessage(msg) {
        if (!this._view)
            return;
        this._view?.webview.postMessage(msg);
    }
    postLayer(layer) { this._view?.webview.postMessage({ type: 'layer', layer }); }
    _getHtml() {
        return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  :root {
    --bg: var(--vscode-sideBar-background, var(--vscode-editor-background));
    --bg-elevated: var(--vscode-editorWidget-background, var(--vscode-input-background));
    --border: var(--vscode-widget-border, var(--vscode-panel-border, rgba(128,128,128,0.25)));
    --fg: var(--vscode-foreground);
    --fg-muted: var(--vscode-descriptionForeground, #999);
    --accent: var(--vscode-button-background, var(--vscode-focusBorder));
    --accent-fg: var(--vscode-button-foreground, #fff);
    --hover: var(--vscode-list-hoverBackground, rgba(128,128,128,0.08));
    --input-bg: var(--vscode-input-background);
    --input-border: var(--vscode-input-border, var(--border));
    --input-fg: var(--vscode-input-foreground, var(--fg));
    --code-bg: var(--vscode-textCodeBlock-background, rgba(128,128,128,0.1));
    --success: var(--vscode-charts-green, #3fb950);
    --danger: var(--vscode-charts-red, #f85149);
    --radius: 8px;
    --radius-sm: 5px;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; padding: 0; overflow: hidden;
    font-family: var(--vscode-font-family, sans-serif);
    font-size: 13px; color: var(--fg); background: var(--bg);
  }
  button, select, input, textarea { font-family: inherit; color: inherit; }
  .screen { display: flex; flex-direction: column; height: 100vh; }
  .hidden { display: none !important; }
  .icon { width: 14px; height: 14px; flex: none; }
  .icon-lg { width: 16px; height: 16px; }

  /* ---- Topbar ---- */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 12px; border-bottom: 1px solid var(--border); flex: none;
    gap: 8px;
  }
  .brand { display: flex; align-items: center; gap: 7px; font-size: 14px; font-weight: 600; flex-shrink: 0; }
  .brand-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); flex: none; }
  .topbar-actions { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1 1 auto; justify-content: flex-end; }
  .icon-btn {
    background: transparent; border: none; color: var(--fg-muted); cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    width: 26px; height: 26px; border-radius: var(--radius-sm); padding: 0;
    /* FIX: at narrow VS Code sidebar widths, the status pill (up to 180px)
       plus this button could together exceed the available topbar width.
       With no flex-shrink guarantee here, the flex layout could squeeze
       this button down to near-zero width or push it out of the visible
       row — which is exactly why the settings gear was reported as
       "missing" at a narrow panel width, when it was actually still
       present in the DOM, just not visibly clickable/visible. The status
       pill already truncates its own text gracefully (text-overflow:
       ellipsis on #status-text below), so it should be the one to give up
       space first, not this button. */
    flex-shrink: 0;
  }
  .icon-btn:hover { background: var(--hover); color: var(--fg); }

  .status-pill {
    display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--fg-muted);
    padding: 2px 8px; border-radius: 999px; background: var(--bg-elevated); border: 1px solid var(--border);
    max-width: 180px; overflow: hidden;
    /* FIX: allow this to shrink below its content size in the flex row —
       paired with min-width:0 on the parent .topbar-actions above, this is
       what actually lets the ellipsis truncation in #status-text kick in
       under real width pressure, instead of the pill just refusing to
       shrink and forcing something else (the settings button) out instead. */
    flex-shrink: 1; min-width: 0;
  }
  .status-pill #status-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .status-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--fg-muted); flex: none; }
  .status-pill.busy .status-dot { background: var(--accent); animation: pulse 1.1s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.45; transform: scale(0.7); } }

  /* ---- Conversation ---- */
  .conversation { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
  .empty-state { text-align: center; color: var(--fg-muted); padding: 30px 14px; font-size: 12.5px; line-height: 1.6; }
  .empty-state .icon-lg { width: 26px; height: 26px; margin-bottom: 10px; opacity: 0.6; }

  #messages { display: flex; flex-direction: column; gap: 8px; }
  .msg-user {
    align-self: flex-end; max-width: 88%; background: var(--accent); color: var(--accent-fg);
    font-size: 12.5px; line-height: 1.5; padding: 7px 11px; border-radius: var(--radius);
    white-space: pre-wrap; word-break: break-word;
  }
  .msg-status { font-size: 11.5px; color: var(--fg-muted); padding: 1px 2px; }

  .card {
    background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 10px 12px;
  }
  .card-header {
    display: flex; align-items: center; justify-content: space-between; gap: 6px;
    font-size: 11.5px; font-weight: 600; color: var(--fg-muted); margin-bottom: 8px;
    text-transform: uppercase; letter-spacing: 0.02em;
  }
  .card-header > span:first-child { display: flex; align-items: center; gap: 6px; }
  .plan-text { font-size: 12.5px; line-height: 1.65; white-space: pre-wrap; word-break: break-word; }
  .card-actions { display: flex; gap: 8px; margin-top: 10px; }

  .btn {
    border-radius: var(--radius-sm); padding: 5px 11px; font-size: 12px; cursor: pointer; border: 1px solid transparent;
  }
  .btn-primary { background: var(--accent); color: var(--accent-fg); }
  .btn-primary:hover { background: var(--vscode-button-hoverBackground, var(--accent)); filter: brightness(1.05); }
  /* Deliberately a hardcoded blue rather than --accent: --accent tracks
     whatever the current VS Code theme's button color is (which may not
     read as "blue" in every theme), and this needs to be recognizable at a
     glance as the retry affordance regardless of theme, matching the
     always-blue retry button Cline uses for the same "auto-retry gave up"
     case. */
  .btn-retry { background: #2f7ee6; color: #fff; border: none; }
  .btn-retry:hover { background: #1f6fd8; }
  .btn-retry:disabled { opacity: 0.6; cursor: default; }
  .retry-wrap { margin: 2px 0 8px 0; }
  .btn-ghost { background: transparent; border: 1px solid var(--border); color: var(--fg); }
  .btn-ghost:hover { background: var(--hover); }
  .btn-sm { padding: 3px 8px; font-size: 11px; }

  .pill { display: inline-flex; align-items: center; gap: 4px; font-size: 10.5px; padding: 2px 8px; border-radius: 999px; }
  .pill-muted { background: var(--hover); color: var(--fg-muted); }
  .pill-info { background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent); }
  .pill-success { background: color-mix(in srgb, var(--success) 18%, transparent); color: var(--success); }
  .pill-fail { background: color-mix(in srgb, var(--danger) 18%, transparent); color: var(--danger); }

  .graph-wrap { position: relative; }
  #graph-canvas { width: 100%; height: 140px; display: block; }
  .graph-empty {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font-size: 11.5px; color: var(--fg-muted); text-align: center; padding: 0 20px; pointer-events: none;
  }

  .progress-label { font-size: 11px; color: var(--fg-muted); margin-bottom: 5px; }
  .progress-track { width: 100%; height: 4px; background: var(--hover); border-radius: 2px; overflow: hidden; }
  .progress-fill { width: 0%; height: 100%; background: var(--accent); transition: width 0.2s ease; }

  .code-block {
    font-family: var(--vscode-editor-font-family, monospace); font-size: 12px; background: var(--code-bg);
    border-radius: var(--radius-sm); padding: 8px 10px; max-height: 220px; overflow: auto;
    white-space: pre-wrap; word-break: break-word; margin: 0;
  }

  .checks-list { display: flex; flex-direction: column; gap: 5px; font-size: 12px; }
  .check-item { display: flex; gap: 7px; align-items: flex-start; }
  .check-item .mark { flex: none; font-weight: 700; }
  .check-pass .mark { color: var(--success); }
  .check-fail .mark { color: var(--danger); }

  .mcp-row { display: flex; align-items: center; gap: 8px; font-size: 11.5px; color: var(--fg-muted); flex-wrap: wrap; }
  .mcp-list { display: flex; gap: 6px; flex-wrap: wrap; }
  .mcp-chip { background: var(--bg-elevated); border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; font-size: 11px; }

  /* ---- Composer ---- */
  .composer { border-top: 1px solid var(--border); padding: 10px 12px; flex: none; }
  .input-row { display: flex; align-items: flex-end; gap: 6px; margin-bottom: 8px; }
  #prompt-input {
    flex: 1; resize: none; background: var(--input-bg); color: var(--input-fg);
    border: 1px solid var(--input-border); border-radius: var(--radius-sm); padding: 7px 9px;
    font-size: 12.5px; line-height: 1.4; max-height: 120px; overflow-y: auto;
  }
  #prompt-input:focus { outline: 1px solid var(--accent); outline-offset: -1px; }
  .send-btn {
    flex: none; width: 30px; height: 30px; border-radius: var(--radius-sm); border: none;
    background: var(--accent); color: var(--accent-fg); cursor: pointer;
    display: flex; align-items: center; justify-content: center;
  }
  .send-btn:hover { filter: brightness(1.08); }

  .mode-tabs { display: flex; gap: 6px; }
  .mode-tab {
    flex: 1; text-align: center; font-size: 11.5px; padding: 6px 4px; border-radius: var(--radius-sm);
    background: transparent; border: 1px solid var(--border); color: var(--fg-muted); cursor: pointer;
  }
  .mode-tab:hover { background: var(--hover); }
  .mode-tab.active { background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); border-color: transparent; font-weight: 600; }

  /* ---- Settings screen ---- */
  .settings-body { flex: 1; overflow-y: auto; padding: 12px 14px; }
  .settings-section { margin-bottom: 18px; }
  .section-label {
    font-size: 11px; font-weight: 600; color: var(--fg-muted); text-transform: uppercase;
    letter-spacing: 0.03em; margin-bottom: 7px;
  }
  .field-label { font-size: 11.5px; color: var(--fg-muted); margin: 8px 0 4px; }
  .field-hint { font-size: 10.5px; color: var(--fg-muted); opacity: 0.85; margin: -2px 0 6px; line-height: 1.4; }
  .settings-body select, .settings-body input[type="text"], .settings-body input[type="password"] {
    width: 100%; background: var(--input-bg); color: var(--input-fg); border: 1px solid var(--input-border);
    border-radius: var(--radius-sm); padding: 5px 7px; font-size: 12.5px; margin-bottom: 6px;
  }
  .row-with-button { display: flex; gap: 6px; align-items: center; }
  .row-with-button select { flex: 1; margin-bottom: 0; }
  .row-2 { display: flex; gap: 8px; }
  .row-2 select { flex: 1; }

  .toggle-row {
    display: flex; align-items: center; justify-content: space-between; padding: 6px 0; font-size: 12.5px; cursor: pointer;
  }
  .toggle-switch {
    width: 30px; height: 17px; border-radius: 999px; background: var(--input-border); position: relative;
    flex: none; transition: background 0.15s ease; cursor: pointer;
  }
  .toggle-switch::after {
    content: ''; position: absolute; top: 2px; left: 2px; width: 13px; height: 13px; border-radius: 50%;
    background: var(--bg); transition: transform 0.15s ease;
  }
  .toggle-switch.on { background: var(--accent); }
  .toggle-switch.on::after { transform: translateX(13px); background: var(--accent-fg); }

  .github-link { display: block; text-align: center; font-size: 11.5px; color: var(--vscode-textLink-foreground); margin-top: 6px; text-decoration: none; }
  .github-link:hover { text-decoration: underline; }

  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-thumb { background: var(--hover); border-radius: 4px; }
</style>
<script>
const vscode = acquireVsCodeApi();
const PROVIDER_DEFAULTS = {
  'koboldcpp':    'http://localhost:5001',
  'ollama':       'http://localhost:11434',
  'lmstudio':     'http://localhost:1234',
  'openai-compat':'http://localhost:8000',
  'openai':       'https://api.openai.com',
  'anthropic':    'https://api.anthropic.com',
};
const LOCAL_PROVIDERS = new Set(['koboldcpp', 'ollama', 'lmstudio', 'openai-compat']);
let settings = { autoApprove: false, strictPlanMode: false, autoCompact: true, focusChain: true, actMode: false };
let activeMode = 'plan';
let _fetchTarget = 'top';
// Last prompt sent to Plan — Build/Review need no equivalent since they
// carry no free-text prompt of their own; retrying those just re-reads the
// current settings fields via getApiState(), same as a fresh send.
let _lastPlanPrompt = '';

function cssVar(name, fallback) {
  try {
    const v = getComputedStyle(document.body).getPropertyValue(name).trim();
    return v || fallback;
  } catch (e) { return fallback; }
}

function getApiState() {
  const provider = document.getElementById('provider-select')?.value || 'ollama';
  const baseUrl  = document.getElementById('base-url')?.value || '';
  const apiKey   = document.getElementById('api-key')?.value || '';
  // FIX: previously buildProvider/buildBaseUrl/buildApiKey did not exist —
  // build() in sendMessage() was sending the SAME provider/baseUrl/apiKey
  // as Plan/Review, which is exactly why a freshly-installed extension
  // showed "the same provider" for Build no matter what was picked here.
  const buildProvider = document.getElementById('build-provider-select')?.value || 'ollama';
  const buildBaseUrl  = document.getElementById('build-base-url')?.value || '';
  const buildApiKey   = document.getElementById('build-api-key')?.value || '';
  const topModel = document.getElementById('top-model')?.value || '';
  const buildModel = document.getElementById('build-model')?.value || '';
  const grammarMode = document.getElementById('grammar-mode')?.value || 'auto';
  return { provider, baseUrl, apiKey, buildProvider, buildBaseUrl, buildApiKey, topModel, buildModel, grammarMode };
}

function hideEmptyState() {
  const el = document.getElementById('empty-state');
  if (el) el.classList.add('hidden');
}

// FIX: scrollToBottom() used to unconditionally snap the conversation pane
// to the bottom on every single call site, including appendStatusLine()
// which fires on every "Building…"/"Compile check failed, retrying…" line.
// That meant scrolling up to read an earlier plan item or the graph view
// got yanked back down on the very next status update. isNearBottom() lets
// callers that fire frequently (status lines, build output, review checks)
// only auto-follow when the user hasn't deliberately scrolled away; force=true
// is reserved for the one case where snapping to bottom is unambiguously
// wanted — the user's own just-sent message.
function isNearBottom() {
  const c = document.getElementById('conversation');
  if (!c) return true;
  const NEAR_BOTTOM_PX = 80;
  return c.scrollHeight - c.scrollTop - c.clientHeight < NEAR_BOTTOM_PX;
}

function scrollToBottom(force = false) {
  const c = document.getElementById('conversation');
  if (c && (force || isNearBottom())) c.scrollTop = c.scrollHeight;
}

function appendUserMessage(text) {
  hideEmptyState();
  const wrap = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg-user';
  div.textContent = text;
  wrap.appendChild(div);
  scrollToBottom(true);
}

function appendStatusLine(text) {
  if (!text) return;
  hideEmptyState();
  const wrap = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg-status';
  div.textContent = text;
  wrap.appendChild(div);
  scrollToBottom();
}

// Renders an error line plus an inline blue "Retry" button, for the case
// where automatic retries (see ollama.js's TRANSIENT_RETRY_DELAYS_MS) were
// exhausted on a genuinely transient error (429/503/connection drop) — the
// same pattern Cline uses: don't just dead-end, offer one tap to try again
// once the person (or the upstream provider) has had a moment. 'stage' is
// 'plan' | 'build' | 'review'.
function appendRetryableError(message, stage) {
  hideEmptyState();
  const wrap = document.getElementById('messages');
  const line = document.createElement('div');
  line.className = 'msg-status';
  line.textContent = 'Error: ' + message;
  wrap.appendChild(line);
  const btnWrap = document.createElement('div');
  btnWrap.className = 'retry-wrap';
  const btn = document.createElement('button');
  btn.className = 'btn btn-retry btn-sm';
  btn.textContent = 'Retry';
  btn.onclick = () => {
    btn.disabled = true;
    btnWrap.remove();
    retryStage(stage);
  };
  btnWrap.appendChild(btn);
  wrap.appendChild(btnWrap);
  scrollToBottom();
}

// Re-fires the same command the failed attempt used. Provider/baseUrl/
// apiKey/model fields are re-read live from the settings UI (getApiState())
// rather than snapshotted at send time, so a Retry after tweaking a setting
// (e.g. lowering the RPM limit) picks up the change. Plan is the one
// exception that needs a remembered value (_lastPlanPrompt) since its input
// field is cleared immediately after the original send.
function retryStage(stage) {
  const state = getApiState();
  appendStatusLine('Retrying…');
  if (stage === 'plan') {
    vscode.postMessage({ command: 'generate', prompt: _lastPlanPrompt, provider: state.provider, baseUrl: state.baseUrl, apiKey: state.apiKey, topModel: state.topModel });
  } else if (stage === 'build') {
    vscode.postMessage({ command: 'build', buildModel: state.buildModel, provider: state.buildProvider, baseUrl: state.buildBaseUrl, apiKey: state.buildApiKey, grammarMode: state.grammarMode });
  } else if (stage === 'review') {
    vscode.postMessage({ command: 'review', provider: state.provider, baseUrl: state.baseUrl, apiKey: state.apiKey, topModel: state.topModel });
  }
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function handleInputKey(ev) {
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault();
    sendMessage();
  }
}

function setMode(mode) {
  activeMode = mode;
  ['plan', 'build', 'review'].forEach(m => {
    const el = document.getElementById('tab-' + m);
    if (el) el.classList.toggle('active', m === mode);
  });
  const input = document.getElementById('prompt-input');
  if (input) {
    input.placeholder = mode === 'plan'
      ? 'Describe what you want to build…'
      : mode === 'build'
        ? 'Press send to build the approved plan'
        : 'Press send to review the current build';
  }
}

function sendMessage() {
  const input = document.getElementById('prompt-input');
  const text = (input?.value || '').trim();
  const state = getApiState();
  if (activeMode === 'plan') {
    if (!text) { input?.focus(); return; }
    appendUserMessage(text);
    appendStatusLine('Requesting plan…');
    // Remembered so a later manual Retry (see appendRetryableError/
    // retryStage) can resend the same prompt — the input field itself gets
    // cleared right below, so the DOM alone can't be re-read for this one.
    _lastPlanPrompt = text;
    vscode.postMessage({ command: 'generate', prompt: text, provider: state.provider, baseUrl: state.baseUrl, apiKey: state.apiKey, topModel: state.topModel });
    if (input) { input.value = ''; autoGrow(input); }
  } else if (activeMode === 'build') {
    appendStatusLine('Starting build…');
    // FIX: was sending state.provider/baseUrl/apiKey (Plan/Review's), which
    // is the exact bug behind Build always showing "the same provider".
    vscode.postMessage({ command: 'build', buildModel: state.buildModel, provider: state.buildProvider, baseUrl: state.buildBaseUrl, apiKey: state.buildApiKey, grammarMode: state.grammarMode });
  } else if (activeMode === 'review') {
    appendStatusLine('Starting review…');
    vscode.postMessage({ command: 'review', provider: state.provider, baseUrl: state.baseUrl, apiKey: state.apiKey, topModel: state.topModel });
  }
}

function approvePlanFn() {
  const planText = document.getElementById('plan-text')?.textContent || '';
  if (!planText.trim()) { appendStatusLine('Generate a plan first.'); return; }
  vscode.postMessage({ command: 'approve_plan', planText });
  appendStatusLine('Plan approved — switch to Build to generate code.');
  setMode('build');
}

function reviseFn() {
  setMode('plan');
  const input = document.getElementById('prompt-input');
  if (input) input.focus();
}

function showSettings() {
  document.getElementById('screen-chat')?.classList.add('hidden');
  document.getElementById('screen-settings')?.classList.remove('hidden');
}
function showChat() {
  document.getElementById('screen-settings')?.classList.add('hidden');
  document.getElementById('screen-chat')?.classList.remove('hidden');
}

function updateGrammarBadge() {
  const mode = document.getElementById('grammar-mode')?.value;
  const badge = document.getElementById('grammar-badge');
  if (badge) badge.classList.toggle('hidden', mode !== 'gbnf');
}

function toggleCppStandardVisibility() {
  const backend = document.getElementById('backend-select')?.value;
  const cppSel = document.getElementById('cpp-standard-select');
  if (cppSel) cppSel.classList.toggle('hidden', backend !== 'cpp');
}

function pushProviderConfig() {
  const state = getApiState();
  vscode.postMessage({ command: 'set_provider', provider: state.provider, baseUrl: state.baseUrl, apiKey: state.apiKey });
}

function pushBuildProviderConfig() {
  const state = getApiState();
  vscode.postMessage({ command: 'set_build_provider', provider: state.buildProvider, baseUrl: state.buildBaseUrl, apiKey: state.buildApiKey });
}

function pushRpmLimit() {
  const val = parseInt(document.getElementById('rpm-limit')?.value || '0', 10);
  vscode.postMessage({ command: 'set_rpm_limit', limit: isNaN(val) ? 0 : Math.max(0, val) });
}

function pushBuildRpmLimit() {
  const val = parseInt(document.getElementById('build-rpm-limit')?.value || '0', 10);
  vscode.postMessage({ command: 'set_build_rpm_limit', limit: isNaN(val) ? 0 : Math.max(0, val) });
}

function fetchModels(target) {
  _fetchTarget = target || 'top';
  const state = getApiState();
  // FIX: previously always read provider/baseUrl/apiKey regardless of
  // target, so "Fetch" under Build model was silently querying Plan/
  // Review's provider too.
  const { provider, baseUrl, apiKey } = _fetchTarget === 'build'
    ? { provider: state.buildProvider, baseUrl: state.buildBaseUrl, apiKey: state.buildApiKey }
    : { provider: state.provider, baseUrl: state.baseUrl, apiKey: state.apiKey };
  vscode.postMessage({ command: 'fetch_models', provider, baseUrl, apiKey, target: _fetchTarget });
}

function setProvider(provider) {
  const current = document.getElementById('base-url')?.value || '';
  const urlEl = document.getElementById('base-url');
  const isDefault = Object.values(PROVIDER_DEFAULTS).includes(current) || !current;
  if (isDefault && urlEl) urlEl.value = PROVIDER_DEFAULTS[provider] || '';
  // NOTE: this dropdown no longer touches grammar-mode. GBNF only ever
  // applies to Build's generation (ollama.js's autoGrammarMode() is called
  // exclusively from Build's code path) — Plan/Review's provider choice
  // has no effect on it. See setBuildProvider() below for the dropdown
  // that actually should drive grammar-mode.
  pushProviderConfig();
}

function setBuildProvider(provider) {
  // FIX: this whole function did not exist — there was no separate Build
  // provider dropdown to attach a handler to. Note this is the ONLY
  // dropdown whose choice should drive grammar-mode/GBNF, since GBNF only
  // ever applies to Build's generation, never to Plan/Review (see
  // ollama.js's autoGrammarMode — it's called exclusively from Build's
  // code path).
  const current = document.getElementById('build-base-url')?.value || '';
  const urlEl = document.getElementById('build-base-url');
  const isDefault = Object.values(PROVIDER_DEFAULTS).includes(current) || !current;
  if (isDefault && urlEl) urlEl.value = PROVIDER_DEFAULTS[provider] || '';
  // FIX: this used to be LOCAL_PROVIDERS.has(provider) ? 'gbnf' : 'tools',
  // which wrongly treated every local provider (Ollama, LM Studio,
  // openai-compat) as GBNF-capable just because it's "local" — none of
  // those actually honor a raw grammar field (see ollama.js's
  // autoGrammarMode comment). This mirrors that function's real logic so
  // the UI and the actual request never disagree about grammar mode again.
  //
  // FIX 2: previously sent a separate 'set_grammar' postMessage right
  // before 'set_build_provider' — two independent async config writes that
  // could race (see the backend's set_build_provider handler for the full
  // explanation), visibly snapping the grammar-mode dropdown back to its
  // old value right after picking a provider. The backend now derives and
  // persists grammarMode atomically from the provider value in the SAME
  // handler — this function only needs to update the dropdown's own local
  // visual state (no round-trip needed for that part) and send one message.
  const autoMode = provider === 'koboldcpp' ? 'gbnf' : 'tools';
  const grammarEl = document.getElementById('grammar-mode');
  if (grammarEl) grammarEl.value = autoMode;
  updateGrammarBadge();
  pushBuildProviderConfig();
}

function toggleSetting(key) {
  settings[key] = !settings[key];
  const elementId = 'toggle-' + key.replace(/[A-Z]/g, m => '-' + m.toLowerCase());
  const el = document.getElementById(elementId);
  if (el) el.classList.toggle('on', settings[key]);
  vscode.postMessage({ command: 'toggle_setting', key, value: settings[key], ...settings });
}

// ── Force-directed graph renderer ──────────────────────────────────────────
// Dependency-free (no d3-force, keeps the extension small). Nodes repel each
// other, edges act as springs, light center gravity keeps things on-canvas.
// Simulation state persists across renderGraph() calls so nodes settle into
// position rather than re-randomizing every time new graph data streams in
// during Build — that persistence is what makes it feel like Obsidian's
// graph view rather than a static diagram that resets on every update.

const _graphSim = {
  positions: new Map(),   // name -> {x, y, vx, vy}
  running: false,
  rafHandle: 0,
};

const GRAPH_COLORS = {
  program: '#8b8b8b',
  module:  '#c792ea',
  shape:   '#c792ea',
  action:  '#4fd6be',
};

function _graphNodeKey(n) { return n.kind + ':' + n.name; }

function _graphEnsurePosition(key, canvas) {
  if (!_graphSim.positions.has(key)) {
    _graphSim.positions.set(key, {
      x: canvas.width / 2 + (Math.random() - 0.5) * 60,
      y: canvas.height / 2 + (Math.random() - 0.5) * 60,
      vx: 0, vy: 0,
    });
  }
  return _graphSim.positions.get(key);
}

function _graphStep(nodes, edges, canvas) {
  const W = canvas.width, H = canvas.height;
  const keys = nodes.map(_graphNodeKey);
  const posByKey = {};
  for (const k of keys) posByKey[k] = _graphEnsurePosition(k, canvas);

  const REPEL = 1800;
  for (let i = 0; i < keys.length; i++) {
    for (let j = i + 1; j < keys.length; j++) {
      const a = posByKey[keys[i]], b = posByKey[keys[j]];
      let dx = a.x - b.x, dy = a.y - b.y;
      let dist2 = dx * dx + dy * dy;
      if (dist2 < 1) dist2 = 1;
      const force = REPEL / dist2;
      const dist = Math.sqrt(dist2);
      const fx = (dx / dist) * force, fy = (dy / dist) * force;
      a.vx += fx; a.vy += fy;
      b.vx -= fx; b.vy -= fy;
    }
  }

  const SPRING_K = 0.02, REST_LEN = 55;
  for (const e of edges) {
    const fromKey = keys.find(k => k.endsWith(':' + e.from));
    const toKey = keys.find(k => k.endsWith(':' + e.to));
    if (!fromKey || !toKey) continue;
    const a = posByKey[fromKey], b = posByKey[toKey];
    const dx = b.x - a.x, dy = b.y - a.y;
    const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
    const stretch = dist - REST_LEN;
    const fx = (dx / dist) * stretch * SPRING_K;
    const fy = (dy / dist) * stretch * SPRING_K;
    a.vx += fx; a.vy += fy;
    b.vx -= fx; b.vy -= fy;
  }

  const GRAVITY = 0.01;
  for (const k of keys) {
    const p = posByKey[k];
    p.vx += (W / 2 - p.x) * GRAVITY;
    p.vy += (H / 2 - p.y) * GRAVITY;
  }

  const DAMPING = 0.85;
  for (const k of keys) {
    const p = posByKey[k];
    p.vx *= DAMPING; p.vy *= DAMPING;
    p.x += p.vx; p.y += p.vy;
    p.x = Math.max(14, Math.min(W - 14, p.x));
    p.y = Math.max(14, Math.min(H - 14, p.y));
  }
}

const RECENT_PULSE_MS = 2500;

function _graphDraw(nodes, edges, canvas, recentTimestamps) {
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const keys = nodes.map(_graphNodeKey);
  const posByKey = {};
  for (const k of keys) posByKey[k] = _graphEnsurePosition(k, canvas);

  ctx.strokeStyle = 'rgba(150,150,150,0.35)';
  ctx.lineWidth = 1;
  for (const e of edges) {
    const fromKey = keys.find(k => k.endsWith(':' + e.from));
    const toKey = keys.find(k => k.endsWith(':' + e.to));
    if (!fromKey || !toKey) continue;
    const a = posByKey[fromKey], b = posByKey[toKey];
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }

  const now = Date.now();
  const fg = cssVar('--vscode-foreground', '#cccccc');
  for (const n of nodes) {
    const key = _graphNodeKey(n);
    const p = posByKey[key];
    const baseR = n.kind === 'action' ? 8 : n.kind === 'program' ? 11 : 9;
    const addedAt = recentTimestamps ? recentTimestamps.get(n.name) : undefined;
    const age = addedAt !== undefined ? now - addedAt : Infinity;
    const isRecent = age <= RECENT_PULSE_MS;
    const fade = isRecent ? 1 - (age / RECENT_PULSE_MS) : 0;
    const pulse = isRecent ? 1 + 0.25 * fade * Math.sin(now / 220) : 1;
    const r = baseR * pulse;
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = GRAPH_COLORS[n.kind] || '#888';
    ctx.globalAlpha = isRecent ? (0.55 + 0.45 * fade) : (n.kind !== 'action' ? 0.9 : 0.55);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.fillStyle = fg;
    ctx.font = '9px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(n.name, p.x, p.y + r + 10);
  }
}

let _graphLatest = { nodes: [], edges: [], recentTimestamps: new Map() };

function _graphTotalVelocity() {
  let total = 0;
  for (const p of _graphSim.positions.values()) total += Math.abs(p.vx) + Math.abs(p.vy);
  return total;
}

function _graphLoop() {
  const canvas = document.getElementById('graph-canvas');
  if (!canvas || !_graphSim.running) return;
  _graphStep(_graphLatest.nodes, _graphLatest.edges, canvas);
  _graphDraw(_graphLatest.nodes, _graphLatest.edges, canvas, _graphLatest.recentTimestamps);
  const stillPulsing = _graphLatest.recentTimestamps.size > 0
    && Array.from(_graphLatest.recentTimestamps.values()).some(ts => Date.now() - ts <= RECENT_PULSE_MS);
  if (_graphTotalVelocity() < 0.05 && !stillPulsing) {
    _graphSim.running = false;
    return;
  }
  _graphSim.rafHandle = requestAnimationFrame(_graphLoop);
}

function renderGraph(nodes, edges) {
  const canvas = document.getElementById('graph-canvas');
  if (!canvas) return;
  const newNodes = nodes || [];
  const prevKeys = new Set(_graphLatest.nodes.map(_graphNodeKey));
  const now = Date.now();
  const recentTimestamps = new Map(_graphLatest.recentTimestamps || []);
  for (const [name, ts] of Array.from(recentTimestamps)) {
    if (now - ts > RECENT_PULSE_MS) recentTimestamps.delete(name);
  }
  for (const n of newNodes) {
    if (!prevKeys.has(_graphNodeKey(n))) recentTimestamps.set(n.name, now);
  }
  _graphLatest = { nodes: newNodes, edges: edges || [], recentTimestamps };
  const liveKeys = new Set(newNodes.map(_graphNodeKey));
  for (const k of Array.from(_graphSim.positions.keys())) {
    if (!liveKeys.has(k)) _graphSim.positions.delete(k);
  }

  const emptyEl = document.getElementById('graph-empty');
  const statusEl = document.getElementById('graph-status');
  if (emptyEl) emptyEl.classList.toggle('hidden', newNodes.length > 0);
  if (statusEl) {
    statusEl.textContent = newNodes.length > 0 ? 'building' : 'idle';
    statusEl.className = 'pill ' + (newNodes.length > 0 ? 'pill-info' : 'pill-muted');
  }
  if (newNodes.length > 0) hideEmptyState();

  if (!_graphSim.running) {
    _graphSim.running = true;
    _graphLoop();
  }
}

window.addEventListener('message', ev => {
  const msg = ev.data;
  switch (msg.type) {
    case 'plan': {
      const el = document.getElementById('plan-text');
      if (el) el.textContent = msg.plan;
      document.getElementById('card-plan')?.classList.remove('hidden');
      hideEmptyState();
      scrollToBottom();
      break;
    }
    case 'build_output': {
      const el = document.getElementById('build-output');
      if (el) el.textContent = msg.output;
      document.getElementById('card-build')?.classList.remove('hidden');
      hideEmptyState();
      scrollToBottom();
      break;
    }
    case 'graph': {
      renderGraph(msg.nodes || [], msg.edges || []);
      break;
    }
    case 'progress': {
      const bar = document.getElementById('progress-bar');
      const label = document.getElementById('progress-label');
      if (bar) bar.style.width = (msg.percent || 0) + '%';
      if (label) label.textContent = msg.verified + ' / ' + msg.total + ' plan items'
        + (msg.currentItemDesc ? ' — generating: ' + msg.currentItemDesc : '');
      document.getElementById('card-progress')?.classList.remove('hidden');
      break;
    }
    case 'checks': {
      const el = document.getElementById('checks-list');
      if (el) el.innerHTML = (msg.checks || []).map((c) =>
        '<div class="check-item ' + (c.pass ? 'check-pass' : 'check-fail') + '">' +
          '<span class="mark">' + (c.pass ? '✓' : '✗') + '</span>' +
          '<span><b>[' + c.layer + '] ' + c.id + '</b> — ' + c.detail + '</span>' +
        '</div>'
      ).join('');
      document.getElementById('card-review')?.classList.remove('hidden');
      hideEmptyState();
      scrollToBottom();
      break;
    }
    case 'verdict': {
      const el = document.getElementById('verdict');
      if (el) {
        el.textContent = msg.passed ? 'PASS' : 'FAIL';
        el.className = 'pill ' + (msg.passed ? 'pill-success' : 'pill-fail');
      }
      document.getElementById('card-review')?.classList.remove('hidden');
      break;
    }
    case 'run_state': {
      const btn = document.getElementById('run-btn');
      if (btn) {
        btn.disabled = !msg.canRun;
        btn.title = msg.canRun ? 'Run the compiled binary' : 'Build must pass review and compile check before running';
      }
      break;
    }
    case 'models_list': {
      const selId = _fetchTarget === 'build' ? 'build-model' : 'top-model';
      const sel = document.getElementById(selId);
      if (sel) { sel.innerHTML = ''; (msg.models || []).forEach((m) => { const o = document.createElement('option'); o.value = m; o.textContent = m; sel.appendChild(o); }); }
      break;
    }
    case 'grammar': {
      const el = document.getElementById('grammar-mode');
      if (el) el.value = msg.mode;
      updateGrammarBadge();
      break;
    }
    case 'mcp_servers': {
      const chips = (msg.servers || []).map((s) =>
        '<span class="mcp-chip">' + s.name + (s.status === 'connected' ? ' ●' : ' ○') + '</span>'
      ).join('');
      const list1 = document.getElementById('mcp-list-chat');
      const list2 = document.getElementById('mcp-list-settings');
      if (list1) list1.innerHTML = chips;
      if (list2) list2.innerHTML = chips || '<span style="color:var(--fg-muted); font-size:11.5px;">No MCP servers configured.</span>';
      document.getElementById('mcp-row')?.classList.toggle('hidden', (msg.servers || []).length === 0);
      break;
    }
    case 'config': {
      const sel = document.getElementById('provider-select');
      if (sel) sel.value = msg.provider || 'ollama';
      const urlEl = document.getElementById('base-url');
      if (urlEl) urlEl.value = msg.baseUrl || '';
      const apiKeyEl = document.getElementById('api-key');
      if (apiKeyEl) apiKeyEl.placeholder = msg.hasApiKey ? 'API key saved — leave blank to keep' : 'API key';
      // FIX: these four lines did not exist — the Build provider section is
      // new this round, so its fields need to be populated from config the
      // same way Plan/Review's already were.
      const buildSel = document.getElementById('build-provider-select');
      if (buildSel) buildSel.value = msg.buildProvider || 'ollama';
      const buildUrlEl = document.getElementById('build-base-url');
      if (buildUrlEl) buildUrlEl.value = msg.buildBaseUrl || '';
      const buildApiKeyEl = document.getElementById('build-api-key');
      if (buildApiKeyEl) buildApiKeyEl.placeholder = msg.hasBuildApiKey ? 'API key saved — leave blank to keep' : 'API key';
      const topModelEl = document.getElementById('top-model');
      if (topModelEl && msg.topModel) topModelEl.value = msg.topModel;
      const buildModelEl = document.getElementById('build-model');
      if (buildModelEl && msg.buildModel) buildModelEl.value = msg.buildModel;
      const grammarEl = document.getElementById('grammar-mode');
      if (grammarEl && msg.grammarMode) grammarEl.value = msg.grammarMode;
      updateGrammarBadge();
      const rpmEl = document.getElementById('rpm-limit');
      if (rpmEl) rpmEl.value = msg.rpmLimit || 0;
      const buildRpmEl = document.getElementById('build-rpm-limit');
      if (buildRpmEl) buildRpmEl.value = msg.buildRpmLimit || 0;
      const graphPersistEl = document.getElementById('graph-persistence');
      if (graphPersistEl) graphPersistEl.value = msg.graphPersistence || 'session';
      const backendEl = document.getElementById('backend-select');
      if (backendEl && msg.backend) backendEl.value = msg.backend;
      const cppEl = document.getElementById('cpp-standard-select');
      if (cppEl && msg.cppStandard) cppEl.value = String(msg.cppStandard);
      toggleCppStandardVisibility();

      settings.autoApprove = msg.autoApprove || false;
      settings.strictPlanMode = msg.strictPlanMode || false;
      settings.autoCompact = msg.autoCompact !== false;
      settings.focusChain = msg.focusChain !== false;
      settings.actMode = msg.actMode || false;
      const toggleMap = {
        autoApprove: 'toggle-auto-approve',
        strictPlanMode: 'toggle-strict-plan-mode',
        autoCompact: 'toggle-auto-compact',
        focusChain: 'toggle-focus-chain',
        actMode: 'toggle-act-mode',
      };
      Object.entries(toggleMap).forEach(([k, id]) => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle('on', settings[k]);
      });
      break;
    }
    case 'status': {
      const el = document.getElementById('status-text');
      if (el) el.textContent = msg.status || 'Idle';
      const pill = document.getElementById('status-pill');
      if (pill) pill.classList.toggle('busy', /…$|ing\\b/i.test(msg.status || ''));
      appendStatusLine(msg.status);
      break;
    }
    case 'retryable_error': {
      const el = document.getElementById('status-text');
      if (el) el.textContent = msg.message || 'Error';
      appendRetryableError(msg.message || 'Request failed.', msg.stage);
      break;
    }
  }
});

window.addEventListener('DOMContentLoaded', () => { setMode('plan'); });
</script>
</head><body>

<div id="screen-chat" class="screen">
  <div class="topbar">
    <div class="brand"><span class="brand-dot"></span>Dictum</div>
    <div class="topbar-actions">
      <span id="status-pill" class="status-pill"><span class="status-dot"></span><span id="status-text">Idle</span></span>
      <button class="icon-btn" title="Settings" onclick="showSettings()">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M19.4 13a7.6 7.6 0 0 0 0-2l2-1.6-2-3.4-2.4.6a7.6 7.6 0 0 0-1.7-1l-.4-2.6h-4l-.4 2.6a7.6 7.6 0 0 0-1.7 1l-2.4-.6-2 3.4 2 1.6a7.6 7.6 0 0 0 0 2l-2 1.6 2 3.4 2.4-.6c.5.4 1.1.8 1.7 1l.4 2.6h4l.4-2.6c.6-.2 1.2-.6 1.7-1l2.4.6 2-3.4-2-1.6Z"/></svg>
      </button>
    </div>
  </div>

  <div id="conversation" class="conversation">
    <div id="empty-state" class="empty-state">
      <svg class="icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" style="display:block;margin:0 auto 10px;"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 9h8M8 13h5"/></svg>
      Describe what you want to build. Dictum plans it, builds it through a grammar-constrained model, then reviews the generated code.
    </div>

    <div id="messages"></div>

    <div id="card-plan" class="card hidden">
      <div class="card-header"><span><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 11l2 2 4-4"/><rect x="3" y="3" width="18" height="18" rx="3"/></svg>Plan</span></div>
      <div id="plan-text" class="plan-text"></div>
      <div class="card-actions">
        <button class="btn btn-primary" onclick="approvePlanFn()">Approve plan</button>
        <button class="btn btn-ghost" onclick="reviseFn()">Revise</button>
      </div>
    </div>

    <div id="card-graph" class="card">
      <div class="card-header">
        <span><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="6" cy="7" r="2.4"/><circle cx="18" cy="6" r="2.2"/><circle cx="18" cy="14" r="2.2"/><circle cx="9" cy="17" r="2.2"/><path d="M8 8.4l8-1.6M8 8.4l8 6M11 17l5-2.6"/></svg>Code graph</span>
        <span id="graph-status" class="pill pill-muted">idle</span>
      </div>
      <div class="graph-wrap">
        <canvas id="graph-canvas" width="520" height="140"></canvas>
        <div id="graph-empty" class="graph-empty">Symbols will appear here once Build starts.</div>
      </div>
    </div>

    <div id="card-progress" class="card hidden">
      <div id="progress-label" class="progress-label"></div>
      <div class="progress-track"><div id="progress-bar" class="progress-fill"></div></div>
    </div>

    <div id="card-build" class="card hidden">
      <div class="card-header"><span><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M8 9l-3 3 3 3M16 9l3 3-3 3M13 6l-2 12"/></svg>Build output</span></div>
      <pre id="build-output" class="code-block"></pre>
      <div class="card-actions">
        <button class="btn btn-ghost btn-sm" onclick="vscode.postMessage({command:'apply'})">Apply</button>
        <button class="btn btn-ghost btn-sm" onclick="vscode.postMessage({command:'transpile'})">Transpile</button>
        <button id="run-btn" class="btn btn-ghost btn-sm" disabled title="Build must pass review and compile check before running" onclick="vscode.postMessage({command:'run'})">Run</button>
      </div>
    </div>

    <div id="card-review" class="card hidden">
      <div class="card-header"><span><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3l8 4v5c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V7l8-4Z"/></svg>Review</span><span id="verdict" class="pill"></span></div>
      <div id="checks-list" class="checks-list"></div>
    </div>

    <div id="mcp-row" class="mcp-row hidden">
      <span>MCP:</span>
      <div id="mcp-list-chat" class="mcp-list"></div>
    </div>
  </div>

  <div class="composer">
    <div class="input-row">
      <textarea id="prompt-input" rows="1" placeholder="Describe what you want to build…" oninput="autoGrow(this)" onkeydown="handleInputKey(event)"></textarea>
      <button class="send-btn" onclick="sendMessage()" aria-label="Send">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
      </button>
    </div>
    <div class="mode-tabs">
      <button class="mode-tab" id="tab-plan" onclick="setMode('plan')">Plan</button>
      <button class="mode-tab" id="tab-build" onclick="setMode('build')">Build</button>
      <button class="mode-tab" id="tab-review" onclick="setMode('review')">Review</button>
    </div>
  </div>
</div>

<div id="screen-settings" class="screen hidden">
  <div class="topbar">
    <button class="icon-btn" title="Back" onclick="showChat()">
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 6l-6 6 6 6"/></svg>
    </button>
    <div class="brand">Settings</div>
    <span style="width:26px;"></span>
  </div>

  <div class="settings-body">
    <div class="settings-section">
      <div class="section-label">Plan &amp; Review Provider</div>
      <select id="provider-select" onchange="setProvider(this.value)">
        <option value="ollama">Ollama (local)</option>
        <option value="lmstudio" title="Requires LM Studio's local server. In LM Studio: click the lightning bolt icon → Start Server.">LM Studio (local)</option>
        <option value="openai-compat">OpenAI-compatible</option>
        <option value="openai">OpenAI</option>
        <option value="anthropic">Anthropic</option>
      </select>
      <input id="base-url" type="text" placeholder="Base URL" onblur="pushProviderConfig()" />
      <input id="api-key" type="password" placeholder="API key" onblur="pushProviderConfig()" />
      <div class="field-hint">Used for Plan and Review — usually a stronger cloud model.</div>
      <div class="field-label">Rate limit (requests/min)</div>
      <input id="rpm-limit" type="number" min="0" step="1" placeholder="0 = unlimited" onblur="pushRpmLimit()" />
      <div class="field-hint">Caps how often Plan/Review calls this provider. Set this to match your provider's actual RPM ceiling (e.g. NVIDIA NIM) to avoid 429 errors during busy retry/fallback loops. 0 = no limit.</div>
    </div>

    <div class="settings-section">
      <div class="section-label">Build Provider</div>
      <select id="build-provider-select" onchange="setBuildProvider(this.value)">
        <option value="koboldcpp" title="Single-binary, no-install llama.cpp-based server. The only provider here with real GBNF grammar support — runs fine on CPU-only machines.">KoboldCpp (local, GBNF-constrained)</option>
        <option value="ollama" title="No raw GBNF passthrough support — Build falls back to tool-calling, same as the cloud providers below.">Ollama (local, no GBNF)</option>
        <option value="lmstudio" title="Requires LM Studio's local server. In LM Studio: click the lightning bolt icon → Start Server. No GBNF support.">LM Studio (local)</option>
        <option value="openai-compat">OpenAI-compatible</option>
        <option value="openai">OpenAI</option>
        <option value="anthropic">Anthropic</option>
      </select>
      <input id="build-base-url" type="text" placeholder="Base URL" onblur="pushBuildProviderConfig()" />
      <input id="build-api-key" type="password" placeholder="API key" onblur="pushBuildProviderConfig()" />
      <div class="field-hint">Used for Build only. Keep this set to Ollama (local) to get real GBNF-grammar-constrained generation — every other provider here falls back to tool-calling and Build's output is no longer grammar-constrained.</div>
      <div class="field-label">Rate limit (requests/min)</div>
      <input id="build-rpm-limit" type="number" min="0" step="1" placeholder="0 = unlimited" onblur="pushBuildRpmLimit()" />
      <div class="field-hint">Independent of Plan/Review's rate limit above. 0 = no limit.</div>
      <div class="field-label">Build model</div>
      <div class="row-with-button">
        <select id="build-model"><option value="">Select model</option></select>
        <button class="btn btn-ghost btn-sm" onclick="fetchModels('build')">Fetch</button>
      </div>
      <div class="field-label">Grammar mode</div>
      <select id="grammar-mode" onchange="vscode.postMessage({command:'set_grammar', mode:this.value}); updateGrammarBadge();">
        <option value="auto">Auto</option>
        <option value="gbnf">GBNF</option>
        <option value="tools">Tools</option>
      </select>
      <div id="grammar-badge" class="pill pill-success hidden" style="margin-top:4px;">
        <svg class="icon" style="width:11px;height:11px;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l8 4v5c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V7l8-4Z"/><path d="M9 12l2 2 4-4"/></svg>
        GBNF constrained
      </div>
    </div>

    <div class="settings-section">
      <div class="section-label">Code Graph</div>
      <select id="graph-persistence" onchange="vscode.postMessage({command:'set_graph_persistence', mode:this.value})">
        <option value="session">Session — cleared on reload</option>
        <option value="project">Project — persists across reloads</option>
      </select>
      <div class="field-hint">Session: the symbol graph (shapes/actions Build uses for context) lives in memory only and resets on window reload or restart. Project: the same graph persists across reloads, scoped to this workspace folder — switching workspaces gets you a separate graph, not a shared one.</div>
    </div>

    <div class="settings-section">
      <div class="section-label">System Requirements</div>
      <button class="btn btn-ghost" onclick="vscode.postMessage({command:'check_system_requirements'})">Check what's installed / missing</button>
      <div class="field-hint">Checks for a C/C++ compiler, Python 3, and whether Build's configured provider is actually reachable — with install instructions for anything missing. Opens a report in the Output panel.</div>
    </div>

    <div class="settings-section">
      <div class="section-label">Models</div>
      <div class="field-label">Plan &amp; review model (top model)</div>
      <div class="row-with-button">
        <select id="top-model"><option value="">Select model</option></select>
        <button class="btn btn-ghost btn-sm" onclick="fetchModels('top')">Fetch</button>
      </div>
    </div>

    <div class="settings-section">
      <div class="section-label">Pipeline</div>
      <label class="toggle-row" onclick="toggleSetting('strictPlanMode')">Strict plan mode<div id="toggle-strict-plan-mode" class="toggle-switch"></div></label>
      <label class="toggle-row" onclick="toggleSetting('autoCompact')">Auto-compact<div id="toggle-auto-compact" class="toggle-switch on"></div></label>
      <label class="toggle-row" onclick="toggleSetting('focusChain')">Focus chain<div id="toggle-focus-chain" class="toggle-switch on"></div></label>
      <label class="toggle-row" onclick="toggleSetting('actMode')">Act mode (auto-transpile on save)<div id="toggle-act-mode" class="toggle-switch"></div></label>
      <label class="toggle-row" onclick="toggleSetting('autoApprove')">Auto-approve<div id="toggle-auto-approve" class="toggle-switch"></div></label>
    </div>

    <div class="settings-section">
      <div class="section-label">Backend</div>
      <div class="row-2">
        <select id="backend-select" onchange="vscode.postMessage({command:'set_config', key:'backend', value:this.value}); toggleCppStandardVisibility();">
          <option value="c">C</option>
          <option value="cpp">C++</option>
        </select>
        <select id="cpp-standard-select" onchange="vscode.postMessage({command:'set_config', key:'cppStandard', value:Number(this.value)})">
          <option value="17">C++17</option>
          <option value="20">C++20</option>
          <option value="23">C++23</option>
        </select>
      </div>
    </div>

    <div class="settings-section">
      <div class="section-label">MCP servers</div>
      <div id="mcp-list-settings" class="mcp-list" style="margin-bottom:8px;"></div>
      <button class="btn btn-ghost" onclick="vscode.postMessage({command:'open_mcp_settings'})">Configure MCP servers</button>
    </div>

    <a class="github-link" href="#" onclick="vscode.postMessage({command:'open_external',url:'https://github.com/jjohnco14-svg/Dictum-extensions'})">GitHub Repository</a>
  </div>
</div>

</body></html>`;
    }
}
exports.DictumPanel = DictumPanel;
DictumPanel.viewType = 'dictum.panel';
//# sourceMappingURL=panel.js.map
