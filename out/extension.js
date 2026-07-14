"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
// extension.ts — VS Code extension entry point
const vscode = require("vscode");
const statusBar_1 = require("./statusBar");
const diagnostics_1 = require("./diagnostics");
const panel_1 = require("./panel");
const graph_1 = require("./graph");
const validator_1 = require("./validator");
const settings_1 = require("./settings");
const ollama = require("./ollama");
const commands = require("./commands");
const prereqs_1 = require("./prereqs");
const transpiler_1 = require("./transpiler");
const retryLoop_1 = require("./retryLoop");
const chunking_1 = require("./chunking");
const patchEngine_1 = require("./patchEngine");
const projectScan_1 = require("./projectScan");
const skills_1 = require("./skills");
const toolSchema_1 = require("./toolSchema");
const chunkGrammar_1 = require("./chunkGrammar");
const normalizeDictum_1 = require("./normalizeDictum");
const patternMatch_1 = require("./patternMatch");
const patternGraph_1 = require("./patternGraph");
const fs = require("fs");
const path = require("path");
let _statusBar;
let _panel;
// ── State shared between panel messages ───────────────────────────────────────
let _approvedPlan = [];
let _planDirectives = { backend: 'c', skill: 'general' };
let _lastPlanText = '';
// FIX: needed so the complexity-cap degradation tier (see
// _runComplexityCapDegradation) can re-request a Plan against the user's
// ORIGINAL request with a scope-reduction instruction, rather than having
// no record of what was originally asked once the plan/build/review cycle
// is several retries deep.
let _lastUserPrompt = '';
// FIX: guards _runComplexityCapDegradation from recursing indefinitely —
// see that function and _runGenerate's isInternalReplan param.
let _complexityCapAttempted = false;
let _generatedCode = '';
let _topModel = '';
let _buildModel = '';
let _provider = 'ollama';
let _baseUrl = 'http://localhost:11434';
let _apiKey = '';
let _grammarMode = 'auto';
let _correctionContext = '';
// Unified retry state, shared between _runReview and _runCompileGate.
// Previously these were two independent counters (_reviewAttempt capped at
// a separate MAX_REVIEW_RETRIES; a recursive `attempt` parameter in
// _runCompileGate capped at its own MAX_COMPILE_RETRIES) that could not see
// each other's failures — proven concretely by debug/pipeline_debug.js's
// gap_syntax_only_misses_link_errors scenario, where L2 passed on every
// retry while the compile gate failed identically every retry. See
// retryLoop.ts for the stagnation-detection design.
let _retryState = (0, retryLoop_1.freshRetryState)();
// Tri-state, reset at the start of every Build (see cmdBuild's Build entry
// point): null = not yet tried this build; true = confirmed working;
// false = the provider/model rejected response_format at least once this
// build, so every remaining chunk skips straight to plain prompting
// instead of re-attempting (and re-failing) structured output on every
// single chunk, which would just add a wasted round-trip per chunk for
// no benefit once we already know this provider/model doesn't support it.
let _jsonSchemaSupported = null;
// Integration-level retry state — separate from per-chunk retry states.
// Per-chunk retries (inside _runBuildChunk) catch a single chunk failing
// its own plan items' L2/L2Fields/L3 checks. This state instead tracks
// retries of the FINAL, whole-source check that runs only after every
// chunk has already individually passed — it exists to catch cross-chunk
// integration issues (e.g. an OPERATION chunk calling a TYPE chunk's shape
// with a mismatched field name) that no single chunk's own check could see
// in isolation. Reset once per fresh top-level _runBuild() call, not per
// chunk and not shared with chunk-local retry state.
let _integrationRetryState = (0, retryLoop_1.freshRetryState)();
// Measured Build-provider throughput, used to size chunk token budgets
// adaptively (see chunking.js chunkBudgetFromCalibration). 0 means
// "not yet measured" — _runBuild calibrates automatically on first use
// if this is still 0, so adaptive sizing works without requiring a
// separate manual setup step, while still being available as one
// explicitly (dictum.calibrateBuildProvider command below).
let _calibration = { tokensPerSecond: 0, measuredAt: 0 };
let _canRun = false;
let _lastCompileErrors = '';
let _secrets;
// FIX (bug: duplicate concurrent Plan/Build/Review runs): nothing previously
// guarded these commands against being invoked a second time while a first
// call was still in flight — the panel's "busy" pill is CSS-only and never
// disabled the send button. Against a single-slot local server (KoboldCpp
// serves one generation at a time), a second Build request landing while
// the first was still streaming caused KoboldCpp to abort the first
// generation outright ("Token streaming was interrupted or aborted!" /
// WinError 10053 on Windows), which is what actually produced the
// "Build error: stream timeout" seen in practice — not a genuinely slow
// model. This single flag serializes Generate/Build/Review so a second
// invocation is rejected with a clear message instead of silently racing
// the first one against the same backend.
let _pipelineBusy = false;
function _withPipelineGuard(label, fn) {
    return async (...args) => {
        if (_pipelineBusy) {
            vscode.window.showWarningMessage(`Dictum: ${label} is already running — wait for it to finish before starting another.`);
            _panel.postStatus(`${label} already in progress — ignored duplicate request.`);
            return;
        }
        _pipelineBusy = true;
        try {
            await fn(...args);
        }
        finally {
            _pipelineBusy = false;
        }
    };
}
/**
 * Resolve the API key: explicit override > SecretStorage > in-memory
 * _apiKey > config (legacy plain-text).
 *
 * FIX: previously hardcoded to the 'dictum.apiKey' secret and called from
 * exactly one place (Review's key, after a Build). Both dictum.generate
 * and dictum.build read their OWN _apiKey directly as
 * `apiKey || cfg.xApiKey || ''` instead — which is always '' the moment a
 * key has been saved via the panel, since the webview intentionally sends
 * apiKey: '' ("leave blank to keep" — see panel.js's #api-key placeholder
 * logic) and cfg.apiKey/cfg.buildApiKey are the plain-text settings,
 * which SecretStorage-saved keys never populate. The result: the very
 * first Plan (and Build) request after any reload silently goes out with
 * NO Authorization header at all. Against a cloud provider like NVIDIA
 * NIM this doesn't surface as a clean 401 — the gateway drops the
 * connection once it sees no bearer token while this client is still
 * mid-write on the request body, surfacing here as a raw `read
 * ECONNRESET` instead of an auth error. secretKey is now a parameter so
 * this can serve both Plan/Review's key and Build's
 * ('dictum.buildApiKey'), and override is whatever came directly off the
 * webview form this call (non-empty only when the person just typed
 * something in).
 */
async function _resolveApiKey(secretKey = 'dictum.apiKey', override) {
    if (override)
        return override;
    if (_secrets) {
        const secret = await _secrets.get(secretKey);
        if (secret)
            return secret;
    }
    if (_apiKey)
        return _apiKey;
    return (0, settings_1.getConfig)().apiKey;
}
function activate(context) {
    _secrets = context.secrets;
    const ext = context.extensionPath;
    _statusBar = new statusBar_1.DictumStatusBar(context);
    const diagnostics = new diagnostics_1.DictumDiagnostics(ext);
    diagnostics.subscribe(context);
    // Silent gcc/clang check — shows one notification if neither is found
    (0, prereqs_1.checkCompilerPrereq)(context);
    // FIX: there was no Python check at all before, despite every Build/
    // Review/compile-gate path ultimately shelling out through
    // compiler/dictumc_cli.py — a missing Python install previously
    // surfaced as a confusing, generic subprocess failure deep inside the
    // compile gate, with no upfront warning.
    {
        const initialCfg = (0, settings_1.getConfig)();
        (0, prereqs_1.checkPythonPrereq)(context, initialCfg.pythonPath || 'python');
    }
    // FIX: this used to be checkOllamaPrereq(context, initialCfg.ollamaUrl),
    // gated on initialCfg.provider === 'ollama'. Three real bugs there,
    // found during this session's KoboldCpp migration:
    //   1. initialCfg.provider is Plan/Review's provider, not Build's —
    //      buildProvider is the field that actually matters here.
    //   2. It only ever checked for and recommended Ollama, even after
    //      koboldcpp became the default Build provider.
    //   3. It checked /api/tags, the wrong endpoint for koboldcpp
    //      specifically (see ollama.js's isRunning() fix).
    // checkBuildProviderPrereq is provider-aware: it reads buildProvider,
    // checks the right endpoint via ollama.isRunning() (already
    // provider-aware itself), and recommends the right tool.
    {
        const initialCfg = (0, settings_1.getConfig)();
        (0, prereqs_1.checkBuildProviderPrereq)(context, initialCfg.buildProvider || 'koboldcpp', initialCfg.buildBaseUrl || 'http://localhost:5001', ollama);
    }
    _panel = new panel_1.DictumPanel(context.extensionUri, context.secrets);
    // Project-scoped code graph persistence: 'session' (default, unchanged
    // prior behavior — in-memory only, lost on reload) or 'project'
    // (persists via context.workspaceState, which VS Code already scopes
    // per-workspace-folder, so the graph survives reloads/restarts for a
    // given project without any manual project-ID bookkeeping).
    graph_1.initStorage(context.workspaceState, (0, settings_1.getConfig)().graphPersistence || 'session');
    // Part 2 (project-wide codegraph): proactively index every .dict file
    // in the workspace, not just ones already open in an editor tab. The
    // open-tab indexing below (onDidOpenTextDocument etc.) still runs and
    // keeps live edits current; this just closes the gap for files the
    // user hasn't clicked on yet. Never fatal to activation.
    if (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0) {
        try {
            const wsRoot = vscode.workspace.workspaceFolders[0].uri.fsPath;
            projectScan_1.scanProject(wsRoot, graph_1);
        }
        catch (e) {
            // Non-fatal: worst case, project-wide context is just missing
            // until the user opens the relevant files themselves (prior
            // behavior, unchanged).
        }
    }
    // RPM throttling: applied per (channel, provider, baseUrl), so Plan/
    // Review and Build each have their own ceiling — including the case
    // where they happen to point at the SAME endpoint (e.g. both set to
    // NVIDIA NIM), which previously collapsed them onto one shared bucket
    // since the key didn't distinguish channels. 0 (the default) means
    // unthrottled — unchanged from prior behavior unless the person
    // explicitly sets a limit.
    {
        const rpmCfg = (0, settings_1.getConfig)();
        ollama.setRpmLimit('plan-review', rpmCfg.provider, ollama.normaliseBaseUrl(rpmCfg.baseUrl), rpmCfg.rpmLimit);
        ollama.setRpmLimit('build', rpmCfg.buildProvider, ollama.normaliseBaseUrl(rpmCfg.buildBaseUrl), rpmCfg.buildRpmLimit);
    }
    // Act Mode: auto-transpile on save when enabled. Previously this setting
    // was read into getConfig() but nothing ever subscribed to save events,
    // so toggling it had no observable effect.
    context.subscriptions.push(vscode.workspace.onDidSaveTextDocument((doc) => {
        if (doc.languageId !== 'dictum')
            return;
        if (!(0, settings_1.getConfig)().actMode)
            return;
        commands.cmdTranspile(ext, _statusBar);
    }));
    context.subscriptions.push(vscode.window.registerWebviewViewProvider(panel_1.DictumPanel.viewType, _panel, {
        webviewOptions: { retainContextWhenHidden: true }
    }));
    // Wire panel callbacks
    _panel.onPlanReceived = (planText) => {
        _approvedPlan = (0, validator_1.parsePlanItems)(planText);
        _planDirectives = (0, validator_1.parsePlanDirectives)(planText);
        _lastPlanText = planText;
    };
    _panel.onCodeGenerated = (code) => { _generatedCode = code; };
    // generate command
    context.subscriptions.push(vscode.commands.registerCommand('dictum.generate', _withPipelineGuard('Plan generation', async (prompt, provider, baseUrl, apiKey, topModel) => {
        const cfg = (0, settings_1.getConfig)();
        const text = prompt ?? await vscode.window.showInputBox({ prompt: 'What should Dictum build?' });
        if (!text)
            return;
        _lastUserPrompt = text;
        _topModel = topModel || cfg.topModel;
        _provider = provider || cfg.provider || 'ollama';
        _baseUrl = baseUrl || cfg.ollamaUrl;
        // FIX: was `apiKey || cfg.apiKey || ''` — cfg.apiKey is the
        // plain-text setting, which is always '' once a key has been
        // saved via the panel (real key lives in SecretStorage instead).
        // See _resolveApiKey()'s comment for the full ECONNRESET chain
        // this caused against cloud providers like NVIDIA NIM.
        _apiKey = await _resolveApiKey('dictum.apiKey', apiKey);
        await _runGenerate(ext, text, _topModel, _baseUrl, _apiKey);
    })));
    // calibrateBuildProvider command — measures real generation throughput
    // (tokens/sec) against the configured Build provider on THIS machine,
    // so chunk sizing can be based on actual measured speed instead of a
    // fixed guess (see chunking.js chunkBudgetFromCalibration). Also run
    // automatically, once, the first time a build happens if this was
    // never called explicitly — see _runBuild.
    context.subscriptions.push(vscode.commands.registerCommand('dictum.calibrateBuildProvider', async () => {
        const cfg = (0, settings_1.getConfig)();
        const url = cfg.buildBaseUrl || 'http://localhost:5001';
        const provider = cfg.buildProvider || 'koboldcpp';
        const key = cfg.buildApiKey || '';
        _panel.postStatus('Calibrating build provider speed…');
        const result = await ollama.calibrate({
            baseUrl: url, model: cfg.buildModel, provider, apiKey: key || undefined,
        });
        if (result.error || !result.tokensPerSecond) {
            _panel.postStatus(`Calibration failed: ${result.error || 'no measurable output'} — chunk sizing will use a conservative default.`);
            return;
        }
        _calibration = { tokensPerSecond: result.tokensPerSecond, measuredAt: Date.now() };
        _panel.postStatus(`Calibration complete: ~${result.tokensPerSecond.toFixed(1)} tokens/sec measured on this machine — chunk sizing will use this.`);
    }));
    // build command
    context.subscriptions.push(vscode.commands.registerCommand('dictum.build', _withPipelineGuard('Build', async (buildModel, provider, baseUrl, apiKey, grammarMode) => {
        if (!_approvedPlan.length && _lastPlanText) {
            _approvedPlan = (0, validator_1.parsePlanItems)(_lastPlanText);
            _planDirectives = (0, validator_1.parsePlanDirectives)(_lastPlanText);
        }
        if (!_approvedPlan.length) {
            vscode.window.showWarningMessage('Generate a plan first.');
            return;
        }
        const cfg = (0, settings_1.getConfig)();
        _buildModel = buildModel || cfg.buildModel;
        // FIX: was `provider || cfg.provider || 'ollama'` — Build silently
        // inherited Plan/Review's shared provider, which meant the instant
        // a cloud provider (e.g. NIM) was configured for Plan/Review
        // quality, Build was dragged along too and GBNF became unreachable
        // (autoGrammarMode() only returns 'gbnf' when provider === 'koboldcpp' —
        // previously this comment said 'ollama', which matched the old,
        // incorrect implementation; Ollama never actually honored arbitrary
        // GBNF passthrough, see ollama.js autoGrammarMode()).
        // Build now defaults to its own buildProvider config, independent
        // of whatever Plan/Review are using.
        // FIX (bug: Review 401 after an auto-chained Build): capture Plan/
        // Review's REAL credentials before Build's own provider/baseUrl/
        // apiKey overwrite _provider/_baseUrl/_apiKey below. The key typed
        // into the panel's Plan & Review field is saved via SecretStorage
        // (panel.js's _setApiKey), NOT into vscode's plain workspace
        // configuration — so `cfg.apiKey` (settings.js's `cfg.get('apiKey',
        // '')`) is always empty for anyone using the panel UI normally.
        // Restoring from `cfg.apiKey` after Build silently discarded a
        // real, present key and sent Review's request with no Authorization
        // header at all. _resolveApiKey() already existed to check
        // SecretStorage first — it just wasn't being called anywhere.
        const reviewProvider = cfg.provider || 'ollama';
        const reviewBaseUrl = cfg.baseUrl || cfg.ollamaUrl;
        const reviewApiKey = await _resolveApiKey('dictum.apiKey');
        _provider = provider || cfg.buildProvider || 'koboldcpp';
        _baseUrl = baseUrl || cfg.buildBaseUrl;
        // FIX: same bug as Plan's _apiKey above, just for Build's own key
        // ('dictum.buildApiKey') — was `apiKey || cfg.buildApiKey || ''`,
        // which is '' the moment the Build key has been saved via the
        // panel and the field left blank ("leave blank to keep") on this
        // reload. Build's own generate() calls were going out with no
        // Authorization header at all, independent of the Review fix
        // above (which only ever covered Plan/Review's key).
        _apiKey = await _resolveApiKey('dictum.buildApiKey', apiKey);
        _grammarMode = grammarMode || 'auto';
        _jsonSchemaSupported = null;
        await _runBuild(ext);
        // FIX: previously the only way to reach Review was the user manually
        // switching tabs and pressing send — _runReview() already knows how
        // to orchestrate its own Build<->Review retry loop internally (see
        // the recursive calls inside _runReview/_runCompileGate above), but
        // nothing ever called it after this initial, non-retry build. Restore
        // Plan/Review's OWN provider config here before calling — _runBuild
        // just overwrote _provider/_baseUrl/_apiKey with Build's config above,
        // and _runReview must not inherit those. Restored from the values
        // captured above, NOT re-read from cfg here — cfg.apiKey is always
        // stale/empty (see comment above).
        _provider = reviewProvider;
        _baseUrl = reviewBaseUrl;
        _apiKey = reviewApiKey || '';
        await _runReview(ext);
    })));
    // review command
    context.subscriptions.push(vscode.commands.registerCommand('dictum.review', _withPipelineGuard('Review', async (provider, baseUrl, apiKey, topModel) => {
        if (!_generatedCode) {
            vscode.window.showWarningMessage('Build first.');
            return;
        }
        if (!_approvedPlan.length && _lastPlanText) {
            _approvedPlan = (0, validator_1.parsePlanItems)(_lastPlanText);
            _planDirectives = (0, validator_1.parsePlanDirectives)(_lastPlanText);
        }
        const cfg = (0, settings_1.getConfig)();
        _provider = provider || cfg.provider || 'ollama';
        _baseUrl = baseUrl || cfg.ollamaUrl;
        // FIX: same masked-blank-field bug as dictum.generate/dictum.build —
        // see _resolveApiKey()'s comment. A standalone Review invocation
        // (not chained after a Build) hit this the same way Plan did.
        _apiKey = await _resolveApiKey('dictum.apiKey', apiKey);
        _topModel = topModel || cfg.topModel;
        await _runReview(ext);
    })));
    // approvePlan command
    context.subscriptions.push(vscode.commands.registerCommand('dictum.approvePlan', (planText) => {
        if (planText && typeof planText === 'string') {
            const items = (0, validator_1.parsePlanItems)(planText);
            if (items.length > 0) {
                _approvedPlan = items;
                _planDirectives = (0, validator_1.parsePlanDirectives)(planText);
            }
        }
        _panel.postStatus(_approvedPlan.length > 0
            ? `Plan approved — ${_approvedPlan.length} item(s) — backend: ${_planDirectives.backend}, skill: ${_planDirectives.skill}`
            : 'No plan items found');
        _statusBar.setReady(_planDirectives.backend);
    }));
    // transpile command
    context.subscriptions.push(vscode.commands.registerCommand('dictum.transpile', () => commands.cmdTranspile(ext, _statusBar)));
    context.subscriptions.push(vscode.commands.registerCommand('dictum.transpileCompile', () => commands.cmdTranspileCompile(ext, _statusBar)));
    context.subscriptions.push(vscode.commands.registerCommand('dictum.runRepl', () => commands.cmdRunRepl(ext)));
    context.subscriptions.push(vscode.commands.registerCommand('dictum.openSettings', () => commands.cmdOpenSettings()));
    context.subscriptions.push(vscode.commands.registerCommand('dictum.checkSystemRequirements', () => commands.cmdCheckSystemRequirements()));
    // run command — gated on _canRun, which is only ever set true after a real
    // gcc/g++ -fsyntax-only check passes (see _runCompileGate). There is no path
    // that sets _canRun true without that check succeeding, so this command is
    // mechanically incapable of launching a binary that doesn't compile.
    context.subscriptions.push(vscode.commands.registerCommand('dictum.run', async () => {
        if (!_canRun) {
            vscode.window.showWarningMessage('Dictum: Build must pass Review and the compile check before running.');
            return;
        }
        await commands.cmdTranspileCompile(ext, _statusBar);
        // cmdTranspileCompile already compiles to a binary next to the active
        // file on success; once it lands, also offer to run it immediately.
        const editor = vscode.window.activeTextEditor;
        if (editor) {
            const stem = path.basename(editor.document.uri.fsPath, path.extname(editor.document.uri.fsPath));
            const binPath = path.join(path.dirname(editor.document.uri.fsPath), stem);
            const terminal = vscode.window.createTerminal({ name: 'Dictum Run' });
            terminal.show();
            terminal.sendText(process.platform === 'win32' ? `& "${binPath}.exe"` : `"${binPath}"`);
        }
    }));
    // apply command
    context.subscriptions.push(vscode.commands.registerCommand('dictum.apply', async () => {
        const uri = vscode.window.activeTextEditor?.document.uri;
        await commands.cmdApply(_generatedCode, uri);
    }));
    // fetchModels command
    context.subscriptions.push(vscode.commands.registerCommand('dictum.fetchModels', async (provider, baseUrl, apiKey, target) => {
        const cfg = (0, settings_1.getConfig)();
        const isBuildTarget = target === 'build';
        const p = provider || (isBuildTarget ? cfg.buildProvider : cfg.provider) || 'ollama';
        const url = baseUrl || (isBuildTarget ? cfg.buildBaseUrl : cfg.ollamaUrl);
        // FIX: same masked-blank-field bug as generate/build/review — was
        // `apiKey || cfg.apiKey || ''` unconditionally, which is both
        // always-empty (SecretStorage-only key) AND always the wrong
        // secret for a Build-target fetch (Plan/Review's key, not
        // Build's). "Fetch" under the Build model dropdown against a
        // cloud provider requiring a key would silently query
        // unauthenticated.
        const key = await _resolveApiKey(isBuildTarget ? 'dictum.buildApiKey' : 'dictum.apiKey', apiKey);
        _panel.postStatus('Fetching models…');
        try {
            const models = await ollama.listModels(url, p, key || undefined);
            _panel.postMessage({ type: 'models_list', models });
        }
        catch (e) {
            if (p === 'lmstudio') {
                _panel.postStatus('LM Studio not reachable. Enable the local server in LM Studio — click the lightning bolt icon → Start Server.');
            }
            else {
                _panel.postStatus(`Fetch failed: ${e.message}`);
            }
        }
    }));
    // newProject — scaffolds a minimal multi-file project skeleton in the
    // current workspace so project_builder.py has something to discover.
    // Previously declared in package.json's command palette but never
    // registered, so clicking it produced a VS Code "command not found" error.
    context.subscriptions.push(vscode.commands.registerCommand('dictum.newProject', async () => {
        const folders = vscode.workspace.workspaceFolders;
        if (!folders || folders.length === 0) {
            vscode.window.showWarningMessage('Dictum: open a folder first to create a project in it.');
            return;
        }
        const root = folders[0].uri.fsPath;
        const mainPath = path.join(root, 'main.dict');
        const manifestPath = path.join(root, 'dictum.project.json');
        if (!fs.existsSync(mainPath)) {
            fs.writeFileSync(mainPath, 'program main:\n    print the text "Hello from Dictum" and newline\nend program\n', 'utf8');
        }
        if (!fs.existsSync(manifestPath)) {
            fs.writeFileSync(manifestPath, JSON.stringify({ name: path.basename(root), entry: 'main.dict' }, null, 2), 'utf8');
        }
        const doc = await vscode.workspace.openTextDocument(mainPath);
        await vscode.window.showTextDocument(doc);
        vscode.window.showInformationMessage('Dictum: project scaffolded — main.dict created.');
    }));
    // buildProject — shells out to project_builder.py against the workspace
    // root, same backend/cpp-standard settings used elsewhere. Also
    // previously declared but never registered.
    context.subscriptions.push(vscode.commands.registerCommand('dictum.buildProject', async () => {
        const folders = vscode.workspace.workspaceFolders;
        if (!folders || folders.length === 0) {
            vscode.window.showWarningMessage('Dictum: open a project folder first.');
            return;
        }
        const cfg = (0, settings_1.getConfig)();
        const root = folders[0].uri.fsPath;
        const scriptPath = path.join(ext, 'compiler', 'project_builder.py');
        const { exec } = require('child_process');
        const { promisify } = require('util');
        const execAsync = promisify(exec);
        _statusBar.setGenerating();
        _panel.postStatus('Building project…');
        try {
            const cmd = `"${cfg.pythonPath}" "${scriptPath}" "${root}" --backend ${_planDirectives.backend || cfg.backend} --cpp-standard ${cfg.cppStandard}`;
            const { stdout } = await execAsync(cmd, { timeout: 60000 });
            _panel.postStatus(`Project build complete:\n${stdout}`);
            _statusBar.setReady(_planDirectives.backend || cfg.backend);
        }
        catch (e) {
            const errText = e.stderr || e.stdout || e.message || 'Unknown error';
            _statusBar.setError('Project build failed');
            vscode.window.showErrorMessage(`Dictum project build failed:\n${errText.split('\n').slice(0, 8).join('\n')}`);
        }
    }));
    for (const doc of vscode.workspace.textDocuments) {
        if (doc.languageId === 'dictum')
            (0, graph_1.indexSource)(doc.uri.fsPath, doc.getText());
    }
    vscode.workspace.onDidChangeTextDocument(e => {
        if (e.document.languageId === 'dictum')
            (0, graph_1.indexSource)(e.document.uri.fsPath, e.document.getText());
    }, null, context.subscriptions);
    vscode.workspace.onDidCloseTextDocument(doc => {
        if (doc.languageId === 'dictum')
            (0, graph_1.clearFile)(doc.uri.fsPath);
    }, null, context.subscriptions);
    // Push config on change
    vscode.workspace.onDidChangeConfiguration(async (e) => {
        if (e.affectsConfiguration('dictum')) {
            await _pushConfig();
            await _pushMcp();
            // Re-apply RPM limits live — without this, changing the rpmLimit/
            // buildRpmLimit setting would silently require a full window
            // reload to take effect, since setRpmLimit() was only otherwise
            // called once at activation.
            const rpmCfg = (0, settings_1.getConfig)();
            ollama.setRpmLimit('plan-review', rpmCfg.provider, ollama.normaliseBaseUrl(rpmCfg.baseUrl), rpmCfg.rpmLimit);
            ollama.setRpmLimit('build', rpmCfg.buildProvider, ollama.normaliseBaseUrl(rpmCfg.buildBaseUrl), rpmCfg.buildRpmLimit);
        }
    }, null, context.subscriptions);
    // FIX: this used to check ONLY _provider (Plan/Review's provider),
    // never Build's — so the startup status pill could show "Connected"
    // while Build's actual provider (buildProvider) was unreachable, or
    // show "Provider not found" while Build was perfectly fine and only
    // Plan/Review's cloud provider needed an API key not yet entered.
    // Since Build is the stage every single generation depends on, both
    // are checked now and the pill reports whichever is actually the
    // blocker, naming which one specifically.
    Promise.all([
        commands.checkOllama(_statusBar, _provider, _baseUrl, _apiKey),
        (async () => {
            const cfg = (0, settings_1.getConfig)();
            return ollama.isRunning(cfg.buildBaseUrl || _baseUrl, cfg.buildProvider || 'koboldcpp', cfg.buildApiKey || undefined).catch(() => false);
        })(),
    ]).then(([planReviewOk, buildOk]) => {
        if (planReviewOk && buildOk) {
            _panel.postStatus('Connected');
        }
        else if (!buildOk && !planReviewOk) {
            _panel.postStatus('Provider not found — configure in settings');
        }
        else if (!buildOk) {
            _panel.postStatus('Build provider not found — configure in settings');
        }
        else {
            _panel.postStatus('Plan/Review provider not found — configure in settings');
        }
    });
    setTimeout(async () => { await _pushConfig(); await _pushMcp(); }, 500);
}
async function _runGenerate(ext, prompt, topModel, baseUrl, apiKey, isInternalReplan = false) {
    // Starting a new Plan is the real "fresh task" boundary — reset retry
    // state here, not just at the end of the previous task's loop, since
    // dictum.generate can be re-triggered by the user while a prior task's
    // retry recursion (_runBuild <-> _runReview / _runCompileGate) is still
    // in flight. Without this, a brand-new unrelated task's first failure
    // could be compared against leftover signature data from a previous,
    // unrelated task and misfire stagnation on attempt one.
    _retryState = (0, retryLoop_1.freshRetryState)();
    _integrationRetryState = (0, retryLoop_1.freshRetryState)();
    _correctionContext = '';
    _canRun = false;
    _panel.postRunState(false);
    // FIX: _runComplexityCapDegradation calls _runGenerate internally to
    // get a reduced-scope re-plan for the SAME task. Resetting
    // _complexityCapAttempted here unconditionally would let that internal
    // call wipe its own "already attempted" guard before it's even
    // finished running once, allowing it to recurse indefinitely against a
    // plan that keeps coming back over-cap. Only a genuinely new,
    // user-initiated Plan request (isInternalReplan === false) clears it.
    if (!isInternalReplan) {
        _complexityCapAttempted = false;
    }
    const cfg = (0, settings_1.getConfig)();
    const url = baseUrl || cfg.ollamaUrl;
    const key = apiKey || cfg.apiKey || '';
    const running = await ollama.isRunning(url, _provider, key || undefined);
    if (!running) {
        _panel.postStatus(`${_provider} not reachable`);
        _statusBar.setOllamaDown();
        return;
    }
    const skillPath = path.join(ext, 'skills', 'SKILL_PLAN.md');
    let system = fs.existsSync(skillPath) ? fs.readFileSync(skillPath, 'utf8') : 'You are DictumPlanner.';
    // Part 3A: layer the active domain skill's Plan-tier guidance on top,
    // if one is configured and has a Plan addendum. 'general' (the
    // default) has none -- system is unchanged in that case.
    const activeSkill = cfg.activeSkill || 'general';
    const skillBundle = (0, skills_1.loadSkill)(ext, activeSkill);
    if (skillBundle.planAddendum) {
        system += '\n\n---\n\n' + skillBundle.planAddendum;
    }
    // Part 1: if the workspace already has indexed code (from the project
    // scan on activation, or files opened earlier this session), surface
    // it so Plan can recognize this as a continuation rather than
    // replanning from a blank slate. Only added when non-empty -- a fresh
    // workspace with nothing indexed yet behaves exactly as before.
    const existingCodeContext = (0, graph_1.buildPromptContext)((0, graph_1.getNodes)(), (0, graph_1.getEdges)());
    if (existingCodeContext && existingCodeContext.trim()) {
        system += '\n\n---\n\n## EXISTING CODE CONTEXT\n\n' +
            'The workspace already has the following defined. Read SKILL_PLAN.md\'s ' +
            '"EXISTING CODE CONTEXT" section for how this changes your planning.\n\n' +
            existingCodeContext;
    }
    _statusBar.setGenerating();
    _panel.postStatus('Generating plan…');
    try {
        const result = await ollama.generate({
            baseUrl: url, model: topModel || cfg.topModel,
            provider: _provider, apiKey: key || undefined,
            messages: [{ role: 'system', content: system }, { role: 'user', content: prompt }],
            stream: false, maxTokens: cfg.maxTokens, channel: 'plan-review'
        });
        _approvedPlan = (0, validator_1.parsePlanItems)(result);
        _planDirectives = (0, validator_1.parsePlanDirectives)(result);
        _lastPlanText = result;
        _topModel = topModel || cfg.topModel;
        _panel.postPlan(result);
        _statusBar.setReady(cfg.backend);
    }
    catch (e) {
        _statusBar.setError('Generation failed');
        // FIX: a transient error (429/503/connection drop) that exhausted
        // its automatic retries is exactly the case where trying again a
        // moment later has a real chance of working — e.g. an upstream
        // shared worker pool freeing up. Offer a one-tap manual retry
        // instead of a dead-end message. Non-transient errors (bad key,
        // bad request) still just report the failure, since retrying
        // would only fail identically.
        if (e.retriesExhausted) {
            _panel.postRetryableError('plan', e.message);
        }
        else {
            _panel.postStatus(`Error: ${e.message}`);
        }
    }
}
/**
 * _runBuild: orchestrates Build across CHUNKS instead of one monolithic
 * generation covering the whole approved plan.
 *
 * WHY: the previous version sent the entire approved plan as one prompt
 * and asked for the entire program back in a single generation. On
 * CPU-only KoboldCpp inference this reliably ran into two compounding
 * problems documented in the fix session: (1) a large plan's generation
 * can exceed the request timeout before every item is reached, regardless
 * of how long the timeout is sized, and (2) grammar-forced keyword
 * repetition across several structurally similar plan items (e.g. two
 * TYPE shapes back-to-back) maximizes the surface area for a stuck-loop
 * heuristic to false-positive on output that is actually fine. Both
 * symptoms showed up as "the same failure repeats -> stagnant -> stopped"
 * after only 2 attempts, even though nothing was actually wrong with
 * grammar constraint itself — see chunking.js's file header for the full
 * writeup.
 *
 * Chunking (chunking.js buildChunks) splits the plan into dependency-safe,
 * token-budgeted pieces and this function builds them one at a time,
 * feeding each chunk the existing workspace symbol table (graph.js
 * buildPromptContext — the "100x small-model boost" mechanism, previously
 * dead code with zero call sites) so later chunks know what earlier chunks
 * already defined without needing the full accumulated text replayed into
 * every prompt.
 *
 * CONTRACT PRESERVED for existing callers (_runDegradation,
 * _runComplexityCapDegradation both call _runBuild(ext) recursively and
 * expect it to build the full _approvedPlan and leave _generatedCode set
 * to the complete result): this function still does exactly that from the
 * outside — chunking is purely an internal implementation detail of how
 * the plan gets built, not a change to what "building" means to callers.
 */
async function _runBuild(ext) {
    const cfg = (0, settings_1.getConfig)();
    const url = _baseUrl || cfg.ollamaUrl;
    const key = _apiKey || cfg.apiKey || '';
    const model = _buildModel || cfg.buildModel;
    const running = await ollama.isRunning(url, _provider, key || undefined);
    if (!running) {
        _panel.postStatus(`${_provider} not reachable`);
        return;
    }
    // Backend is decided by the Plan model's [BACKEND: c/cpp] directive, not the
    // static dictum.backend setting — a plan that needs C++ smart pointers now
    // actually gets routed to the C++ emitter instead of being silently built as C.
    const backend = _planDirectives.backend;
    const effectiveMode = _grammarMode === 'auto' ? ollama.autoGrammarMode(_provider) : _grammarMode;
    // `grammar` here is now the STATIC fallback only — dictum_safe.gbnf/
    // dictum_unsafe.gbnf, unscoped to any particular chunk. It stays
    // loaded eagerly (same as before) so there is always something to
    // fall back to; the real per-chunk grammar is generated fresh for
    // EACH chunk inside the loop below via chunkGrammar.js, which is
    // narrower than this file for every tier (see chunk_grammar.py's
    // module docstring for why, and for the documented cases — OPERATION/
    // MODIFY tiers — where per-chunk narrowing is deliberately NOT
    // applied to statement kinds, only to identifiers/types).
    let grammar;
    let grammarLoadError;
    const needsUnsafe = _planDirectives.skill === 'unsafe' || _planDirectives.skill === 'concurrent';
    if (effectiveMode === 'gbnf') {
        // Skill variant now comes from the Plan model's own [SKILL: ...] directive
        // instead of regex-sniffing keywords out of the plan description text.
        const gbnfFile = needsUnsafe ? 'dictum_unsafe.gbnf' : 'dictum_safe.gbnf';
        const gbnfPath = path.join(ext, 'grammar', gbnfFile);
        try {
            grammar = fs.readFileSync(gbnfPath, 'utf8');
            if (!grammar || !grammar.trim()) {
                grammar = undefined;
                grammarLoadError = `${gbnfFile} is empty`;
            }
        }
        catch (e) {
            grammarLoadError = `failed to read ${gbnfFile}: ${e.message}`;
        }
    }
    const reportedMode = (effectiveMode === 'gbnf' && !grammar) ? 'tools' : effectiveMode;
    _panel.postGrammar(reportedMode);
    if (grammarLoadError) {
        _panel.postStatus(`GBNF grammar unavailable (${grammarLoadError}) — falling back to unconstrained generation for this attempt`);
    }
    const staticGrammar = grammar;
    // Load the base build skill, then layer a skill-variant addendum on top if
    // the plan called for unsafe/concurrent code. Falls back gracefully to base
    // skill only if the variant file doesn't exist yet. NOTE: this is the base
    // system prompt shared by every chunk — the per-chunk symbol-table context
    // (buildPromptContext) is layered on top freshly inside _runBuildChunk for
    // EACH chunk/attempt, since the graph grows as earlier chunks complete.
    const skillPath = path.join(ext, 'skills', 'SKILL_BUILD.md');
    let systemBase = fs.existsSync(skillPath) ? fs.readFileSync(skillPath, 'utf8') : 'You are DictumCoder.';
    if (_planDirectives.skill !== 'general') {
        const variantPath = path.join(ext, 'skills', `SKILL_BUILD_${_planDirectives.skill}.md`);
        if (fs.existsSync(variantPath)) {
            systemBase += '\n\n' + fs.readFileSync(variantPath, 'utf8');
        }
    }
    // Part 3A: domain skill (e.g. gamedev) is a separate, orthogonal axis
    // from the safety-tier variant above -- a project can need both at
    // once (e.g. a gamedev task with one lock-free item), so this layers
    // on regardless of _planDirectives.skill.
    const activeSkill = cfg.activeSkill || 'general';
    const domainSkill = (0, skills_1.loadSkill)(ext, activeSkill);
    if (domainSkill.buildAddendum) {
        systemBase += '\n\n' + domainSkill.buildAddendum;
    }
    systemBase += `\n\nTarget backend for this task: ${backend === 'cpp' ? 'C++' : 'C'}.`;
    // Adaptive chunk sizing: use the measured tokens/sec for this Build
    // provider if available (see dictum.calibrateBuildProvider command),
    // otherwise measure it now, automatically, so adaptive sizing works
    // even if the person never ran calibration explicitly. A failed
    // auto-calibration attempt is not fatal — chunking.js falls back to a
    // conservative fixed budget (DEFAULT_CHUNK_TOKEN_BUDGET) either way.
    if (!_calibration.tokensPerSecond) {
        const calResult = await ollama.calibrate({ baseUrl: url, model, provider: _provider, apiKey: key || undefined });
        if (calResult.tokensPerSecond) {
            _calibration = { tokensPerSecond: calResult.tokensPerSecond, measuredAt: Date.now() };
        }
    }
    const koboldTimeoutMs = _provider === 'koboldcpp' ? 60000 : 8000; // short-payload tier; chunk prompts stay small by design
    const maxTokensPerChunk = (0, chunking_1.chunkBudgetFromCalibration)(_calibration.tokensPerSecond, { timeoutMs: koboldTimeoutMs });
    const chunks = (0, chunking_1.buildChunks)(_approvedPlan, maxTokensPerChunk);
    if (chunks.length === 0) {
        _panel.postStatus('Nothing to build — approved plan is empty.');
        return;
    }
    // A degradation-level correction (e.g. "redo everything in safe mode")
    // applies to every chunk equally — captured once here, distinct from
    // each chunk's own local retry-correction (see _runBuildChunk), which
    // must NOT leak between chunks.
    const baseCorrectionContext = _correctionContext;
    _statusBar.setGenerating();
    _panel.postStatus(`Building… (${chunks.length} chunk${chunks.length > 1 ? 's' : ''})`);
    const uri = vscode.window.activeTextEditor?.document.uri;
    let accumulated = '';
    const bindingsChunk = (0, skills_1.getSkillBindingsChunk)(ext, activeSkill);
    if (bindingsChunk) {
        accumulated = bindingsChunk.source.trim() + '\n\n';
        (0, graph_1.indexSource)((uri ? uri.fsPath : ext) + '#skill-bindings', accumulated);
        _panel.postGraph((0, graph_1.getNodes)(), (0, graph_1.getEdges)());
        _panel.postStatus(`Loaded ${activeSkill} skill: ${bindingsChunk.label}`);
    }
    try {
        for (const chunk of chunks) {
            // Per-chunk grammar: only attempted in GBNF mode (tool-mode/
            // JSON-Schema already gets its own tight schema per chunk from
            // toolSchema.js's buildChunkResponseFormat, so this would be
            // redundant work for that path). chunkGrammar.js's contract
            // guarantees null on ANY failure, never a throw — falling back
            // to staticGrammar (the same file every chunk used before this
            // change) keeps this strictly additive: worst case, a chunk
            // generates exactly like it did before per-chunk grammar existed.
            let chunkGrammar = staticGrammar;
            if (effectiveMode === 'gbnf' && staticGrammar) {
                const generated = await (0, chunkGrammar_1.generateChunkGrammar)(ext, cfg.pythonPath, chunk, needsUnsafe);
                if (generated) {
                    chunkGrammar = generated;
                }
                else {
                    _panel.postStatus(`Per-chunk grammar unavailable for chunk ${chunk.index + 1}/${chunk.total} — using the general dictum_${needsUnsafe ? 'unsafe' : 'safe'}.gbnf grammar for this chunk instead.`);
                }
            }
            // Pattern-hint few-shot context: fetched ONCE per chunk (not
            // inside _runBuildChunk's own retry loop, since the matched
            // construct doesn't change between retry attempts of the same
            // chunk — only the correction feedback does). Grammar restricts
            // *shape*; this gives the model one real, transpiler-validated
            // example of the specific construct's *content* — see
            // codegraph/PATTERN_SCHEMA.md for why shape alone wasn't enough
            // (countdown-vs-Count, [Person.name] interpolation, wrong
            // unsafe-token param order, ... all real, all shape-legal).
            // Same fallback contract as chunkGrammar above: a match miss or
            // bridge failure (null/ok:false) just means no example gets
            // added — never a build failure, never a retry burned on it.
            let patternContext = null;
            let deterministicText = null;
            if (cfg.patternHints) {
                const matchedRef = (0, patternMatch_1.matchPatternRef)(chunk.tierName, chunk.items);
                if (matchedRef) {
                    // Fast path first: a handful of constructs (ATOMIC_FAA,
                    // RAW_MALLOC+RAW_FREE) are fully mechanical given the
                    // plan text alone -- deriving {target}Ptr/{target}Result
                    // by concatenation, or pairing size+buffer into the
                    // right RAW_MALLOC/RAW_FREE call shape, needs zero
                    // judgment. Cell 12c showed a 2B model getting this
                    // wrong even WITH the correct example in context
                    // (collapsing Counter/CounterPtr into one identifier,
                    // or substituting `call`/`release` for the unsafe
                    // token) -- so for these specific constructs, skip
                    // asking the model at all rather than hoping the
                    // few-shot example lands this time.
                    const combinedPlanText = chunk.items.map((it) => it && it.desc ? it.desc : '').join(' ');
                    const det = await (0, patternGraph_1.tryDeterministicExpand)(ext, cfg.pythonPath, matchedRef, combinedPlanText);
                    if (det && det.ok === true && det.deterministic === true && typeof det.bound === 'string') {
                        deterministicText = det.bound;
                        _panel.postStatus(`Chunk ${chunk.index + 1}/${chunks.length} — ${matchedRef}: mechanical construct, expanding deterministically (no model call).`);
                    }
                    else {
                        const rendered = await (0, patternGraph_1.renderPatternContext)(ext, cfg.pythonPath, matchedRef);
                        if (rendered && rendered.ok === true && rendered.rendered) {
                            patternContext = rendered.rendered;
                        }
                    }
                }
            }
            const result = await _runBuildChunk(ext, {
                url, key, model, grammar: chunkGrammar, systemBase, backend, cfg, chunk, uri, baseCorrectionContext, accumulatedSoFar: accumulated, patternContext, deterministicText,
            });
            accumulated = result.accumulated;
            if (!result.ok) {
                _generatedCode = accumulated;
                (0, graph_1.clearFile)('#live-progress');
                return _escalateAfterRetriesExhausted(ext, result.decision, result.failure);
            }
        }
        _generatedCode = accumulated;
        (0, graph_1.clearFile)('#live-progress');
        _panel.postGraph((0, graph_1.getNodes)(), (0, graph_1.getEdges)());
        // FINAL INTEGRATION CHECK: every chunk individually satisfied its own
        // plan items, but that does not guarantee cross-chunk correctness —
        // e.g. an OPERATION chunk calling a TYPE chunk's shape with a
        // mismatched field name, or an INVARIANT referencing a field that
        // doesn't actually exist. Re-run the SAME L2/L2Fields/L3 checks the
        // old monolithic build used, now over the whole assembled source
        // against the WHOLE plan, catching exactly this class of issue.
        const l2 = validator_1.checkL2Structural(accumulated, _approvedPlan);
        const fieldCheck = validator_1.checkL2Fields(accumulated, _approvedPlan, ext);
        const violations = validator_1.checkL3(accumulated);
        // FIX (production-readiness Problem 5): `unverifiable` results (e.g. a
        // pointer/handle-typed field the bridge has no candidate for) used to
        // be silently dropped from failCount entirely — zero signal anywhere.
        // They now count toward failCount so they trigger the same
        // retry/escalation path as a confirmed mismatch, instead of a build
        // that silently reports "Build complete" with unverified fields.
        const failCount = l2.failed.length + violations.length + fieldCheck.mismatched.length
            + fieldCheck.unverifiable.length + l2.unverifiable.length;
        if (failCount > 0) {
            const detailLines = [
                ...l2.failed.map(item => `Plan item ${item.category}_${item.id} not satisfied: ${item.detail || item.desc}`),
                ...fieldCheck.mismatched.map(m => `Plan item ${m.planItem.category}_${m.planItem.id}: ${m.detail}`),
                ...violations.map(v => `[L3 ${v.rule}] ${v.detail} (line ${v.line})`),
                ...fieldCheck.unverifiable.map(u => `Plan item ${u.planItem.category}_${u.planItem.id}: field '${u.fieldName}' unverifiable — ${u.detail}`),
                ...l2.unverifiable.map(u => `Plan item ${u.category}_${u.id} could not be verified: ${u.detail || u.desc}`),
            ];
            const detail = detailLines.join('\n');
            // Separate retry state from per-chunk retries — see the module-level
            // comment on _integrationRetryState for why this is tracked
            // independently rather than reusing a chunk's local state.
            const decision = (0, retryLoop_1.decideRetry)(_integrationRetryState, (0, graph_1.getNodes)(), (0, graph_1.getEdges)(), { source: 'integration-check', detail });
            _integrationRetryState = decision.nextState;
            if (!decision.shouldStop) {
                // A whole-plan-wide re-chunk-and-rebuild is the simplest correct
                // response to an integration-level failure: attributing it to
                // exactly one chunk is often genuinely ambiguous (a field-name
                // mismatch could be "wrong" on either the TYPE chunk or the
                // OPERATION chunk's side), and this only happens after every
                // chunk already individually passed, so it should be rare.
                _correctionContext = `Cross-chunk integration check found issues after all chunks were individually built:\n${detail}`;
                _panel.postStatus(`Build chunks complete but integration check found ${failCount} issue(s) — retrying full build (attempt ${_integrationRetryState.attempt})…`);
                _statusBar.setReady(backend);
                return _runBuild(ext);
            }
            return _escalateAfterRetriesExhausted(ext, decision, { source: 'integration-check', detail });
        }
        // Full success — clear both retry states and any leftover correction
        // context so the next fresh build/retry cycle starts clean.
        _integrationRetryState = (0, retryLoop_1.freshRetryState)();
        _correctionContext = '';
        // NOTE: unverifiable field/L2 results are now folded into failCount
        // above, so reaching this point means there were none outstanding.
        _panel.postStatus('Build complete');
        _statusBar.setReady(backend);
    }
    catch (e) {
        _statusBar.setError('Build failed');
        if (e.retriesExhausted) {
            _panel.postRetryableError('build', e.message);
        }
        else {
            _panel.postStatus(`Build error: ${e.message}`);
        }
    }
}
/**
 * Builds ONE chunk, retrying just that chunk (fresh, chunk-local retry
 * state — not shared with other chunks or with the integration-level
 * state) until it either passes its own items' L2/L2Fields/L3 checks or
 * exhausts retries. On exhaustion, returns ok:false with the decision/
 * failure needed to feed the existing escalation path (_escalateAfterRetriesExhausted),
 * so a chunk that can't be fixed by retrying still gets the same L5-fallback
 * and degradation safety net the old monolithic build had.
 */
/**
 * Obtains one chunk's Dictum text, using JSON-schema structured output
 * (toolSchema.js) when applicable, and falling back to the previous
 * plain/unconstrained prompting automatically on the FIRST failure this
 * build — without spending one of the chunk's own limited retry attempts
 * on it. Whether a provider/model actually honors `response_format` is a
 * one-time capability question, not a content mistake the normal
 * retry-with-feedback loop (chunkRetryState in _runBuildChunk) is meant
 * to correct, so it's handled here, once, before that loop ever sees it.
 */
async function _generateChunkDictumText(ext, o) {
    const { url, key, model, provider, cfg, system, prompt, grammar, tierName, accumulatedSoFar, onProgress } = o;
    async function plainAttempt(promptText) {
        let text = '';
        const returned = await ollama.generate({
            baseUrl: url, model, provider, apiKey: key || undefined,
            system, prompt: promptText, grammar, stream: true, maxTokens: cfg.maxTokens, channel: 'build',
            onToken: (token) => {
                text += token;
                onProgress(accumulatedSoFar + text);
            },
        });
        return text || returned || '';
    }
    const attemptJsonSchema = _jsonSchemaSupported !== false &&
        !grammar &&
        ollama.isOpenAICompat(provider) &&
        toolSchema_1.isSchemaApplicableTier(tierName);
    if (!attemptJsonSchema) {
        return plainAttempt(prompt);
    }
    const responseFormat = toolSchema_1.buildChunkResponseFormat(ext);
    const schemaPrompt = prompt + `\n\nRespond with ONLY the JSON object described by the schema — no prose, no markdown code fences.`;
    let rawJson = '';
    try {
        const returned = await ollama.generate({
            baseUrl: url, model, provider, apiKey: key || undefined,
            system, prompt: schemaPrompt, stream: true, maxTokens: cfg.maxTokens, responseFormat, channel: 'build',
            // Deliberately no per-token progress/graph update here: mid-stream
            // content is a partial JSON fragment, not Dictum text, and would
            // misreport progress/symbols if scanned the same way a plain
            // chunk's live output is. One real progress update happens below,
            // after the JSON is parsed and converted.
            onToken: (token) => { rawJson += token; },
        });
        if (!rawJson && returned)
            rawJson = returned;
        const parsed = JSON.parse(rawJson);
        const dictumText = toolSchema_1.jsonChunkToDictum(parsed);
        _jsonSchemaSupported = true;
        onProgress(accumulatedSoFar + dictumText);
        return dictumText;
    }
    catch (e) {
        // Covers both a hard failure from generate() itself (e.g. the
        // provider rejects an unrecognized response_format field with an
        // HTTP 400) and a soft failure where generation succeeded but
        // didn't actually produce valid JSON despite "strict" mode. Either
        // way: this provider/model doesn't reliably support this feature,
        // recorded once so every later chunk this build skips straight to
        // plain prompting instead of re-probing (and re-failing) per chunk.
        _jsonSchemaSupported = false;
        _panel.postStatus(`Structured output unavailable for this provider/model this build (${e.message}) — ` +
            `falling back to plain prompting.`);
        return plainAttempt(prompt);
    }
}
async function _runBuildChunk(ext, ctx) {
    const { url, key, model, grammar, systemBase, backend, cfg, chunk, uri, baseCorrectionContext, accumulatedSoFar, patternContext, deterministicText } = ctx;
    let chunkRetryState = (0, retryLoop_1.freshRetryState)();
    let chunkCorrectionContext = '';
    // eslint-disable-next-line no-constant-condition
    while (true) {
        // Recomputed fresh every attempt: the graph may have just grown from
        // a PRIOR chunk succeeding, or (on a chunk-local retry) an in-progress
        // failed attempt's '#live-progress' scratch entry may still be
        // present — buildPromptContext reads the live graph state either way.
        const graphContext = (0, graph_1.buildPromptContext)((0, graph_1.getNodes)(), (0, graph_1.getEdges)());
        let system = systemBase + (graphContext ? `\n\n${graphContext}` : '');
        const planText = chunk.items.map((p) => `[PLAN: ${p.category} : ${p.id} : ${p.desc}]`).join('\n');
        let prompt = `Implement this part of the plan in Dictum (chunk ${chunk.index + 1}/${chunk.total} — ${chunk.tierName.toLowerCase()}):\n\n${planText}\n\n` +
            `Write only the code for the item(s) above. Do not redefine any symbol already listed under "Project symbols" above — those already exist.`;
        if (patternContext) {
            // Few-shot content, not a persona/system fact — kept in the
            // per-chunk prompt (like planText) rather than folded into
            // `system` (which carries the persistent skill+symbol-table
            // context). Explicitly framed as "reference", not "copy this
            // verbatim" — the goal is showing correct Dictum CONTENT for
            // this construct, with THIS chunk's own names/values still
            // coming from the plan text above, not from the example.
            prompt += `\n\nReference example for this construct (follow this shape and style, but use the plan's own names/values above — do not copy the example's names/values verbatim):\n${patternContext}`;
        }
        if (baseCorrectionContext) {
            prompt += `\n\n${baseCorrectionContext}`;
        }
        if (chunkCorrectionContext) {
            prompt += `\n\nThe previous attempt at THIS chunk failed these checks — fix them:\n${chunkCorrectionContext}`;
        }
        _panel.postStatus(`Building… chunk ${chunk.index + 1}/${chunk.total}: ${chunk.label}`);
        let lastProgressCheckLen = 0;
        const PROGRESS_CHECK_INTERVAL_CHARS = 40; // throttle: don't re-scan on every single token
        function onProgress(combinedSoFar) {
            _panel.postBuildOutput(combinedSoFar);
            if (combinedSoFar.length - lastProgressCheckLen >= PROGRESS_CHECK_INTERVAL_CHARS) {
                lastProgressCheckLen = combinedSoFar.length;
                // Progress is reported against the WHOLE plan (not just this
                // chunk's items) so the panel shows one continuous build
                // progress bar across all chunks, matching pre-chunking UX.
                const prog = (0, validator_1.computeBuildProgress)(combinedSoFar, _approvedPlan);
                _panel.postProgress(prog.percent, prog.verified, prog.total, prog.currentItem?.desc);
                (0, graph_1.indexSource)('#live-progress', combinedSoFar);
                _panel.postGraph((0, graph_1.getNodes)(), (0, graph_1.getEdges)());
            }
        }
        // Deterministic fast path only on this chunk's FIRST attempt --
        // the template is validated across 20 variations so it should
        // always clear L2/L3, but if something about THIS call site
        // still trips a check, retrying with the same static text would
        // just repeat the identical failure forever. Falling back to the
        // normal model+correction-context path on retry keeps the
        // existing retry loop as a safety net rather than a dead end.
        const useDeterministic = chunkRetryState.attempt === 0 && typeof deterministicText === 'string';
        let chunkText;
        if (useDeterministic) {
            chunkText = deterministicText;
            onProgress(accumulatedSoFar + chunkText);
        }
        else {
            chunkText = await _generateChunkDictumText(ext, {
                url, key, model, provider: _provider, cfg,
                system, prompt, grammar, tierName: chunk.tierName,
                accumulatedSoFar, onProgress,
            });
        }
        // Normalize-or-refuse, BEFORE this chunk's text is merged into the
        // accumulated source or checked by L2/L3. Two outcomes:
        //   ok:true  -- fixable-class mistake (repetition, an unplanned/
        //               duplicate field, a missing closer) was corrected;
        //               chunkText is replaced with the cleaned version and
        //               proceeds to the merge/L2 checks below as normal.
        //   ok:false -- unrecoverable-class mistake (e.g. a parameter name
        //               collapsed onto its own type, or reused twice in one
        //               takes-clause) -- there's no substring to select a
        //               fix from, so this is treated EXACTLY like an L2/L3
        //               failure: fed into the same chunkRetryState/
        //               decideRetry path with source 'normalize', so the
        //               next attempt gets it in chunkCorrectionContext just
        //               like any other check failure would.
        // ok:null (bridge itself failed) falls back to the raw chunkText
        // unnormalized -- this step can only help or no-op, never block a
        // build on a bug in the normalizer.
        const normResult = await (0, normalizeDictum_1.normalizeDictum)(ext, cfg.pythonPath, chunkText, chunk.items);
        if (normResult && normResult.ok === true && typeof normResult.text === 'string') {
            chunkText = normResult.text;
        }
        else if (normResult && normResult.ok === false) {
            const detail = `Normalization could not safely recover this output: ${normResult.reason}`;
            const decision = (0, retryLoop_1.decideRetry)(chunkRetryState, (0, graph_1.getNodes)(), (0, graph_1.getEdges)(), { source: 'normalize', detail });
            chunkRetryState = decision.nextState;
            if (!decision.shouldStop) {
                chunkCorrectionContext = detail;
                _panel.postStatus(`Chunk ${chunk.index + 1}/${chunk.total} — unrecoverable output, retrying (attempt ${chunkRetryState.attempt})…`);
                continue; // retry SAME chunk; nothing from this attempt was merged
            }
            return { ok: false, accumulated: accumulatedSoFar, decision, failure: { source: 'normalize', detail } };
        }
        // Part 1: MODIFY-tier chunks patch the existing named block instead
        // of appending a second, duplicate definition next to it. Falls
        // back to plain append automatically (patchEngine.applyChunk's own
        // contract) if the target can't be confidently found, so this can
        // only ever do as well as or better than the prior behavior.
        const patchResult = patchEngine_1.applyChunk(accumulatedSoFar, chunk, chunkText);
        const combined = patchResult.source;
        if (patchResult.mode === 'patch') {
            _panel.postStatus(`Chunk ${chunk.index + 1}/${chunk.total} — patched existing ${patchResult.kind} '${patchResult.target}'`);
        }
        // Validate only THIS chunk's own plan items — earlier/later chunks'
        // items are neither expected nor checked in this specific
        // generation's output.
        const l2 = validator_1.checkL2Structural(combined, chunk.items);
        const fieldCheck = validator_1.checkL2Fields(combined, chunk.items, ext);
        const violations = validator_1.checkL3(combined);
        // FIX (production-readiness Problem 5): see matching fix at the
        // final integration check — unverifiable results must not be
        // zero-signal.
        const failCount = l2.failed.length + violations.length + fieldCheck.mismatched.length
            + fieldCheck.unverifiable.length + l2.unverifiable.length;
        if (failCount > 0) {
            const detailLines = [
                ...l2.failed.map((item) => `Plan item ${item.category}_${item.id} not satisfied: ${item.detail || item.desc}`),
                ...fieldCheck.mismatched.map((m) => `Plan item ${m.planItem.category}_${m.planItem.id}: ${m.detail}`),
                ...violations.map((v) => `[L3 ${v.rule}] ${v.detail} (line ${v.line})`),
                ...fieldCheck.unverifiable.map((u) => `Plan item ${u.planItem.category}_${u.planItem.id}: field '${u.fieldName}' unverifiable — ${u.detail}`),
                ...l2.unverifiable.map((u) => `Plan item ${u.category}_${u.id} could not be verified: ${u.detail || u.desc}`),
            ];
            const detail = detailLines.join('\n');
            const decision = (0, retryLoop_1.decideRetry)(chunkRetryState, (0, graph_1.getNodes)(), (0, graph_1.getEdges)(), { source: 'l2l3-check', detail });
            chunkRetryState = decision.nextState;
            if (!decision.shouldStop) {
                chunkCorrectionContext = detail;
                _panel.postStatus(`Chunk ${chunk.index + 1}/${chunk.total} — ${failCount} issue(s), retrying (attempt ${chunkRetryState.attempt})…`);
                continue; // retry SAME chunk; accumulatedSoFar is untouched, so the failed attempt's text is discarded, not appended
            }
            return { ok: false, accumulated: combined, decision, failure: { source: 'l2l3-check', detail } };
        }
        // This chunk succeeded — commit it into the real file's confirmed
        // index (not just the '#live-progress' scratch entry) so the NEXT
        // chunk's buildPromptContext genuinely sees it as an existing symbol,
        // not just something visible transiently during streaming.
        if (uri) {
            (0, graph_1.indexSource)(uri.fsPath + '#generated', combined);
        }
        _panel.postGraph((0, graph_1.getNodes)(), (0, graph_1.getEdges)());
        return { ok: true, accumulated: combined };
    }
}
async function _runReview(ext) {
    const cfg = (0, settings_1.getConfig)();
    const urlR = _baseUrl || cfg.ollamaUrl;
    const keyR = _apiKey || cfg.apiKey || '';
    const running = await ollama.isRunning(urlR, _provider, keyR || undefined);
    if (!running) {
        _panel.postStatus(`${_provider} not reachable`);
        return;
    }
    const skillPath = path.join(ext, 'skills', 'SKILL_REVIEW.md');
    const system = fs.existsSync(skillPath) ? fs.readFileSync(skillPath, 'utf8') : 'You are DictumReviewer.';
    const planText = _approvedPlan.map((p) => `[PLAN: ${p.category} : ${p.id} : ${p.desc}]`).join('\n');
    const userMsg = `Approved plan:\n${planText}\n\nGenerated code:\n\`\`\`\n${_generatedCode}\n\`\`\``;
    _statusBar.setGenerating();
    _panel.postStatus(_retryState.attempt > 0 ? `Reviewing… (attempt ${_retryState.attempt + 1})` : 'Reviewing…');
    try {
        const reviewText = await ollama.generate({
            baseUrl: urlR, model: _topModel || cfg.topModel,
            provider: _provider, apiKey: keyR || undefined,
            messages: [{ role: 'system', content: system }, { role: 'user', content: userMsg }],
            stream: false, maxTokens: cfg.maxTokens, channel: 'plan-review'
        });
        // FIX: was parsePlanItems(reviewText) — wrong function entirely. SKILL_REVIEW.md
        // instructs the model to emit [CHECK: PASS/FAIL/WARN : ...] lines, not [PLAN: ...]
        // lines, so the old regex structurally could never match. parseCheckLines + the
        // separate [REVIEW: PASS/FAIL] summary token are what the model actually emits.
        const checks = (0, validator_1.parseCheckLines)(reviewText);
        const passed = (0, validator_1.reviewPassed)(reviewText);
        _panel.postChecks(checks);
        _panel.postVerdict(passed, reviewText);
        // FIX: postGraph() was never called anywhere in the Review flow —
        // only during live Build streaming and after Build completes. Graph
        // state itself was never cleared, but the webview's displayed graph
        // simply stopped receiving updates the moment Review started, so it
        // went stale rather than reflecting the current state through the
        // whole Plan→Build→Review cycle.
        _panel.postGraph((0, graph_1.getNodes)(), (0, graph_1.getEdges)());
        const failedChecks = checks.filter(c => !c.pass);
        // FIX (production-readiness Problem 3): the self-declared [REVIEW: PASS]
        // tag ("passed") is informational only and must never override the
        // itemized check evidence. A model that contradicts itself — e.g. one
        // [CHECK: FAIL: ...] line plus a trailing [REVIEW: PASS] — must be
        // treated as failed. The real verdict is derived solely from the
        // itemized checks.
        if (failedChecks.length === 0) {
            // Review passed at the Dictum/semantic level — but that doesn't guarantee
            // the *emitted* C/C++ actually compiles (e.g. a blessed-library header
            // mismatch the validator can't see). This is the hard gate: nothing
            // downstream of here is allowed to offer Run until a real gcc/g++
            // -fsyntax-only (+ link, for FFI imports) check succeeds against the
            // transpiled output.
            const gateOk = await _runCompileGate(ext);
            if (gateOk) {
                _retryState = (0, retryLoop_1.freshRetryState)();
                _correctionContext = '';
                _canRun = true;
                _panel.postRunState(true);
                _statusBar.setReady(_planDirectives.backend);
            }
            // _runCompileGate handles its own retry/exhaustion messaging and state.
            return;
        }
        // Review failed — fold this into the SAME retry decision the compile gate
        // uses, instead of an independent counter (previously _reviewAttempt vs.
        // MAX_REVIEW_RETRIES, entirely separate from the compile gate's own
        // counter). Either kind of failure — L2/L3 text checks here, or a real
        // compiler error in _runCompileGate — now advances the same attempt count
        // and the same stagnation signature, so a build that keeps failing the
        // identical way is caught by one mechanism instead of being invisible to
        // whichever loop isn't currently looking at it. See retryLoop.ts.
        const failureDetail = failedChecks.map(c => `[${c.layer}] ${c.id} — ${c.detail}`).join('\n');
        const decision = (0, retryLoop_1.decideRetry)(_retryState, (0, graph_1.getNodes)(), (0, graph_1.getEdges)(), { source: 'review', detail: failureDetail });
        _retryState = decision.nextState;
        if (!decision.shouldStop) {
            _correctionContext = failureDetail;
            _panel.postStatus(`Review failed ${failedChecks.length} check(s) — retrying build (attempt ${_retryState.attempt})…`);
            _canRun = false;
            _panel.postRunState(false);
            await _runBuild(ext);
            await _runReview(ext);
        }
        else {
            await _escalateAfterRetriesExhausted(ext, decision, { source: 'review', detail: failureDetail });
        }
    }
    catch (e) {
        _statusBar.setError('Review failed');
        if (e.retriesExhausted) {
            _panel.postRetryableError('review', e.message);
        }
        else {
            _panel.postStatus(`Review error: ${e.message}`);
        }
    }
}
/**
 * The compile gate. Transpiles the currently generated Dictum source to
 * C/C++ and runs a real `gcc`/`g++ -fsyntax-only` (plus a real link step
 * for code containing FFI imports) check against it. If it fails, the
 * gcc/g++ stderr is fed back into the unified retry decision shared with
 * _runReview (see retryLoop.ts) — gcc errors are usually more precise than
 * the model's own self-review, so this re-checks compilation directly
 * rather than routing back through _runReview, which would re-judge a
 * fact Review doesn't need to re-judge. Returns true only when the
 * emitted code is mechanically confirmed to compile — this is what
 * _canRun is gated on.
 */
async function _runCompileGate(ext) {
    const cfg = (0, settings_1.getConfig)();
    const backend = _planDirectives.backend;
    _panel.postStatus(_retryState.attempt > 0 ? `Checking compile… (attempt ${_retryState.attempt + 1})` : 'Checking compile…');
    const compilerScript = (0, settings_1.getCompilerPath)(ext);
    const tmpDictPath = path.join(require('os').tmpdir(), `dictum_gate_${Date.now()}.dict`);
    fs.writeFileSync(tmpDictPath, _generatedCode, 'utf8');
    let transpileResult;
    let ldflags = '-lm';
    try {
        transpileResult = await (0, transpiler_1.transpile)(tmpDictPath, cfg.pythonPath, compilerScript, backend, cfg.cppStandard);
        if (transpileResult.success) {
            // FIX (link-flag plumbing): must run before the temp .dict is
            // deleted below. This was never queried before, so
            // compileCheck() always linked with only "-lm" regardless of
            // stdlib modules or #[link] directives actually used.
            ldflags = await (0, transpiler_1.getLdflags)(tmpDictPath, cfg.pythonPath, compilerScript, backend);
        }
    }
    finally {
        try {
            fs.unlinkSync(tmpDictPath);
        }
        catch { /* ignore */ }
    }
    if (!transpileResult.success) {
        // Dictum-level transpile failed outright — feed those errors back as a
        // correction and retry Build, same unified budget as the gcc-level
        // failures below.
        _lastCompileErrors = transpileResult.errors.map(e => `Line ${e.line + 1}: ${e.message}`).join('\n');
    }
    else {
        const runtimeInclude = path.join(ext, 'compiler', 'runtime');
        const gccResult = await (0, transpiler_1.compileCheck)(transpileResult.code, backend, cfg.cppStandard, runtimeInclude, ldflags);
        if (gccResult.ok) {
            _panel.postStatus('Compile check passed ✓');
            return true;
        }
        _lastCompileErrors = gccResult.errors;
    }
    const decision = (0, retryLoop_1.decideRetry)(_retryState, (0, graph_1.getNodes)(), (0, graph_1.getEdges)(), { source: 'compile-gate', detail: _lastCompileErrors });
    _retryState = decision.nextState;
    if (!decision.shouldStop) {
        _correctionContext = `The generated code does not compile. Compiler output:\n${_lastCompileErrors}`;
        _panel.postStatus(`Compile check failed — retrying build (attempt ${_retryState.attempt})…`);
        await _runBuild(ext);
        // Re-gate directly against the new build rather than routing back through
        // _runReview, since both now share the same _retryState — there is only
        // one budget to exhaust, not two that could compose into an unbounded loop.
        return _runCompileGate(ext);
    }
    return _escalateAfterRetriesExhausted(ext, decision, { source: 'compile-gate', detail: _lastCompileErrors });
}
/**
 * Fires once the unified retry loop (retryLoop.ts) reports either
 * stagnation (the same failure repeated with no real change) or the
 * backstop ceiling (genuinely different attempts that never converged).
 * Both _runReview and _runCompileGate's terminal-stop paths call this
 * instead of independently reporting failure, so there is exactly one
 * place this escalation chain is implemented.
 *
 * Order, and why: L5 fallback is tried FIRST, degradation only if even
 * fallback can't produce compiling code. Degradation means giving the
 * user LESS than they asked for (safe code instead of the unsafe/
 * concurrent code the plan called for); L5 fallback means trying HARDER
 * to give them what they actually asked for, using a stronger model.
 * Reaching for "give up on the request" before "try a stronger reviewer"
 * would be backwards.
 *
 * Returns whether the escalation chain ultimately produced code that
 * passes a REAL compile check — this is what _runCompileGate's callers
 * gate _canRun on, same contract as a normal compile-gate pass.
 */
async function _escalateAfterRetriesExhausted(ext, decision, failure) {
    _panel.postStatus((0, retryLoop_1.describeStop)(decision, failure));
    _statusBar.setError(decision.reason === 'stagnant' ? `${failure.source === 'review' ? 'Review' : 'Compile'} stuck — same failure repeating` : `${failure.source === 'review' ? 'Review' : 'Compile'} failed — retries exhausted`);
    const fallbackFixed = await _runL5Fallback(ext, decision, failure);
    if (fallbackFixed) {
        return true;
    }
    const degraded = await _runDegradation(ext);
    if (degraded) {
        return true;
    }
    // Both escalation paths exhausted — this is a true terminal failure.
    // TODO: panel.ts currently only has a generic postStatus(string)
    // channel; a structured "build quality" message type (the
    // EXCELLENT/GOOD/DEGRADED/FAILED distinction sketched in earlier
    // design discussion) would let the panel render this more clearly
    // than a status string, but that's a panel UI change, not part of
    // this scope — wiring the string through honestly for now.
    _panel.postStatus(`Manual fix needed: the build failed, L5 fallback could not fix it, and safe-mode ` +
        `degradation also failed to produce compiling code. Last error:\n${_lastCompileErrors || failure.detail}`);
    _retryState = (0, retryLoop_1.freshRetryState)();
    _correctionContext = '';
    _canRun = false;
    _panel.postRunState(false);
    return false;
}
/**
 * L5 fallback: hands the failing code directly to the top model (the same
 * one used for Plan) with SKILL_FALLBACK.md, asking it to fix the named
 * failure directly rather than judge it. This is a single attempt, not a
 * loop — if the top model also can't fix it (or reports the plan itself
 * is unachievable), retrying fallback in a loop would just reinvent the
 * exact problem this whole mechanism exists to solve.
 */
async function _runL5Fallback(ext, decision, failure) {
    const cfg = (0, settings_1.getConfig)();
    const url = _baseUrl || cfg.ollamaUrl;
    const key = _apiKey || cfg.apiKey || '';
    const running = await ollama.isRunning(url, _provider, key || undefined);
    if (!running) {
        _panel.postStatus(`L5 fallback skipped — ${_provider} not reachable`);
        return false;
    }
    if (!_topModel) {
        _panel.postStatus('L5 fallback skipped — no top model configured');
        return false;
    }
    const skillPath = path.join(ext, 'skills', 'SKILL_FALLBACK.md');
    const system = fs.existsSync(skillPath) ? fs.readFileSync(skillPath, 'utf8') : 'You are DictumFallbackRepairer.';
    const planText = _approvedPlan.map((p) => `[PLAN: ${p.category} : ${p.id} : ${p.desc}]`).join('\n');
    const reasonLabel = decision.reason === 'stagnant' ? 'STAGNATION' : 'BACKSTOP';
    const userMsg = `Approved plan:\n${planText}\n\n` +
        `Most recently generated Dictum source:\n\`\`\`\n${_generatedCode}\n\`\`\`\n\n` +
        `Retry loop gave up because: ${reasonLabel}\n\n` +
        `Failure detail (${failure.source}):\n${failure.detail}`;
    _statusBar.setGenerating();
    _panel.postStatus('Retries exhausted — escalating to L5 fallback (top model)…');
    try {
        // Note: this call rewrites the ENTIRE generated source, not one
        // chunk — for anything beyond a small program, dictum.maxTokens
        // (default 4096) may need to be raised in settings, or this
        // response can come back truncated. Left as the user's configured
        // value rather than a separate hardcoded ceiling here, matching
        // Plan/Review/Build's own maxTokens sourcing.
        const fallbackText = await ollama.generate({
            baseUrl: url, model: _topModel, provider: _provider, apiKey: key || undefined,
            messages: [{ role: 'system', content: system }, { role: 'user', content: userMsg }],
            stream: false, maxTokens: cfg.maxTokens, channel: 'plan-review',
        });
        const result = (0, validator_1.parseFallbackResult)(fallbackText);
        if (result.outcome === 'plan_unachievable') {
            _panel.postStatus(`L5 fallback: the plan cannot be implemented as written — ${result.explanation}`);
            return false;
        }
        if (result.outcome === 'unparseable') {
            _panel.postStatus('L5 fallback produced an unparseable response — could not extract corrected source.');
            return false;
        }
        // result.outcome === 'fixed' — verify with a REAL compile check before
        // trusting it. The top model fixing the code is a claim, not a fact,
        // until the same gate every other code path goes through confirms it.
        _generatedCode = result.fixedSource;
        (0, graph_1.indexSource)(ext + '#fallback-fixed', _generatedCode);
        _panel.postGraph((0, graph_1.getNodes)(), (0, graph_1.getEdges)());
        const verified = await _verifyCompiles(ext, _generatedCode);
        if (verified) {
            _panel.postStatus('L5 fallback produced code that compiles ✓');
            _retryState = (0, retryLoop_1.freshRetryState)();
            _correctionContext = '';
            _canRun = true;
            _panel.postRunState(true);
            _statusBar.setReady(_planDirectives.backend);
            return true;
        }
        _panel.postStatus('L5 fallback\'s fix still does not compile — proceeding to degradation.');
        return false;
    }
    catch (e) {
        _panel.postStatus(`L5 fallback error: ${e.message}`);
        return false;
    }
}
/**
 * Degradation: the true last resort before telling the user a manual fix
 * is needed. Only reachable when the original plan called for unsafe or
 * concurrent code (general-purpose plans have nothing to degrade FROM —
 * if a general-purpose build can't be fixed even by L5 fallback, safe-mode
 * generation wouldn't be any more likely to succeed, since it's already
 * what was being attempted). Re-runs Build against the SAME approved plan
 * but forces skill='general', explicitly trading the unsafe/concurrent
 * capability the user asked for in exchange for a much higher chance of
 * actually producing something that compiles. The user is told plainly
 * that this happened — this is not silently substituted.
 */
async function _runDegradation(ext) {
    const originalSkill = _planDirectives.skill;
    if (originalSkill === 'general') {
        // FIX: previously this returned false immediately — "nothing to fall
        // back from" was true in the unsafe/concurrent->general sense, but
        // a general-purpose plan can still be too COMPLEX for the build
        // model to land in the attempts it has left (this is exactly what
        // happened with the "create a 3d game" transcript: 10 plan items,
        // 3 shapes, 6 actions, general skill, no safety-tier to drop to).
        // Add a complexity-cap tier: force a re-plan that keeps the same
        // user intent but caps shape/action count and forbids concurrency,
        // rather than jumping straight to "manual fix needed" with no
        // attempt to simplify the SCOPE rather than the SAFETY TIER.
        return await _runComplexityCapDegradation(ext);
    }
    _panel.postStatus(`Falling back to safe-mode generation: the plan called for '${originalSkill}' code, but neither ` +
        `the build model nor L5 fallback could produce a working version. Retrying with safe, general-purpose ` +
        `Dictum only — this means the result will NOT use manual memory management or raw atomics as originally planned.`);
    _statusBar.setGenerating();
    const savedSkill = _planDirectives.skill;
    const savedCorrection = _correctionContext;
    _planDirectives = { ..._planDirectives, skill: 'general' };
    _correctionContext =
        `The previous attempts to implement this plan using '${savedSkill}' (unsafe/manual-memory or ` +
            `concurrent/atomics) code failed repeatedly and could not be fixed even with senior-model review. ` +
            `Re-implement the same plan using ONLY safe, general-purpose Dictum — no unsafe blocks, no raw ` +
            `pointers, no manual memory management, no atomics. Prefer correctness and compilability over ` +
            `matching the original unsafe/concurrent approach.`;
    try {
        await _runBuild(ext);
        const verified = await _verifyCompiles(ext, _generatedCode);
        if (verified) {
            _panel.postStatus(`Build complete with safe-mode fallback. The result does not include the unsafe/concurrent ` +
                `techniques originally planned — review before relying on it for that purpose.`);
            _retryState = (0, retryLoop_1.freshRetryState)();
            _correctionContext = '';
            _canRun = true;
            _panel.postRunState(true);
            _statusBar.setReady(_planDirectives.backend);
            return true;
        }
        _panel.postStatus('Safe-mode fallback also failed to produce compiling code.');
        // Restore the original skill so the failure message accurately
        // reflects what was actually being attempted, rather than silently
        // leaving _planDirectives mutated to 'general' after a failed
        // degradation attempt.
        _planDirectives = { ..._planDirectives, skill: savedSkill };
        return false;
    }
    catch (e) {
        _panel.postStatus(`Degradation error: ${e.message}`);
        _planDirectives = { ..._planDirectives, skill: savedSkill };
        return false;
    }
    finally {
        _correctionContext = savedCorrection;
    }
}
/**
 * Intermediate degradation tier for already-general-skill plans. There is
 * no safety tier left to drop (general is already the floor), but a plan
 * can still fail purely on COMPLEXITY — too many shapes/actions for the
 * build model to land correctly within the attempts it has left, even
 * though each individual piece is simple in isolation. This caps scope
 * (shape count, action count, forbids concurrency primitives even though
 * 'general' plans shouldn't have them anyway) and forces a re-plan, rather
 * than jumping straight from "L5 fallback failed" to "manual fix needed"
 * with no attempt to simplify the actual scope of what's being attempted.
 *
 * Only fires once per build (see _complexityCapAttempted) — if a capped
 * re-plan also fails, there genuinely is nowhere further to degrade to,
 * and the manual-fix message in _runEscalation's caller is the correct
 * outcome.
 */
const COMPLEXITY_CAP = { maxShapes: 5, maxActions: 3 };
function _countPlanComplexity(plan) {
    const shapeCount = plan.filter(p => p.category === 'TYPE').length;
    const actionCount = plan.filter(p => p.category === 'OPERATION').length;
    return { shapeCount, actionCount };
}
async function _runComplexityCapDegradation(ext) {
    if (_complexityCapAttempted) {
        _panel.postStatus('Complexity-cap degradation already attempted once for this build — not retrying again.');
        return false;
    }
    const { shapeCount, actionCount } = _countPlanComplexity(_approvedPlan);
    const overCap = shapeCount > COMPLEXITY_CAP.maxShapes || actionCount > COMPLEXITY_CAP.maxActions;
    if (!overCap) {
        _panel.postStatus('Degradation skipped — the plan is already within the complexity cap ' +
            `(${shapeCount} shape(s), ${actionCount} action(s); cap is ${COMPLEXITY_CAP.maxShapes}/${COMPLEXITY_CAP.maxActions}). ` +
            'Nothing left to simplify.');
        return false;
    }
    _complexityCapAttempted = true;
    _panel.postStatus(`Plan has ${shapeCount} shape(s) and ${actionCount} action(s) — over the ` +
        `${COMPLEXITY_CAP.maxShapes}/${COMPLEXITY_CAP.maxActions} complexity cap used as a last-resort ` +
        `simplification before giving up. Requesting a re-plan with reduced scope (same intent, fewer ` +
        `moving parts) rather than retrying the same overcomplex plan again.`);
    _statusBar.setGenerating();
    const savedCorrection = _correctionContext;
    const savedPlan = _approvedPlan;
    const savedPlanText = _lastPlanText;
    _correctionContext =
        `The previous plan (${shapeCount} shapes, ${actionCount} actions) repeatedly failed to build, ` +
            `even after senior-model fallback review. Produce a SIMPLER plan for the same user request: ` +
            `at most ${COMPLEXITY_CAP.maxShapes} shapes, at most ${COMPLEXITY_CAP.maxActions} actions, ` +
            `single file, no concurrency. Prefer a smaller but working result over the full original scope — ` +
            `for example, collapse multiple related data shapes into one combined shape, or combine several ` +
            `small actions into one if that keeps the same observable behavior.`;
    try {
        await _runGenerate(ext, _lastUserPrompt || '', _topModel, _baseUrl, _apiKey, true);
        const { shapeCount: newShapes, actionCount: newActions } = _countPlanComplexity(_approvedPlan);
        if (newShapes > COMPLEXITY_CAP.maxShapes || newActions > COMPLEXITY_CAP.maxActions) {
            _panel.postStatus('Re-plan did not actually reduce scope below the cap — restoring the original plan.');
            _approvedPlan = savedPlan;
            _lastPlanText = savedPlanText;
            return false;
        }
        await _runBuild(ext);
        const verified = await _verifyCompiles(ext, _generatedCode);
        if (verified) {
            _panel.postStatus(`Build complete with a reduced-scope plan (${newShapes} shape(s), ${newActions} ` +
                `action(s)) — this covers a smaller version of the original request. Review against what you ` +
                `originally asked for before relying on it.`);
            _retryState = (0, retryLoop_1.freshRetryState)();
            _correctionContext = '';
            _canRun = true;
            _panel.postRunState(true);
            _statusBar.setReady(_planDirectives.backend);
            return true;
        }
        _panel.postStatus('Reduced-scope plan still failed to produce compiling code.');
        return false;
    }
    catch (e) {
        _panel.postStatus(`Complexity-cap degradation error: ${e.message}`);
        return false;
    }
    finally {
        _correctionContext = savedCorrection;
    }
}
/**
 * Shared real-compile verification used by both escalation paths above.
 * Deliberately the SAME check _runCompileGate itself uses (transpile, then
 * compileCheck) — fallback/degradation claiming success is not enough;
 * both paths must pass the identical real gate everything else does
 * before _canRun is ever set true.
 */
async function _verifyCompiles(ext, code) {
    const cfg = (0, settings_1.getConfig)();
    const backend = _planDirectives.backend;
    const compilerScript = (0, settings_1.getCompilerPath)(ext);
    const tmpDictPath = path.join(require('os').tmpdir(), `dictum_verify_${Date.now()}.dict`);
    fs.writeFileSync(tmpDictPath, code, 'utf8');
    try {
        const transpileResult = await (0, transpiler_1.transpile)(tmpDictPath, cfg.pythonPath, compilerScript, backend, cfg.cppStandard);
        if (!transpileResult.success)
            return false;
        const runtimeInclude = path.join(ext, 'compiler', 'runtime');
        const ldflags = await (0, transpiler_1.getLdflags)(tmpDictPath, cfg.pythonPath, compilerScript, backend);
        const gccResult = await (0, transpiler_1.compileCheck)(transpileResult.code, backend, cfg.cppStandard, runtimeInclude, ldflags);
        return gccResult.ok;
    }
    finally {
        try {
            fs.unlinkSync(tmpDictPath);
        }
        catch { /* ignore */ }
    }
}
async function _pushConfig() {
    if (_panel && _panel._pushConfig) {
        await _panel._pushConfig();
    }
}
// _pushConfig is the private method variant; also expose as _pushConfigToPanel alias
const _pushConfigToPanel = _pushConfig;
async function _pushMcp() {
    if (_panel && _panel._pushMcpServers) {
        _panel._pushMcpServers();
    }
}
// _pushMcp is also called as _pushMcpToPanel
const _pushMcpToPanel = _pushMcp;
function deactivate() { }
//# sourceMappingURL=extension.js.map