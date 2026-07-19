"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.checkOllama = checkOllama;
exports.cmdGenerate = cmdGenerate;
exports.cmdBuild = cmdBuild;
exports.cmdReview = cmdReview;
exports.cmdApply = cmdApply;
exports.cmdTranspile = cmdTranspile;
exports.cmdTranspileCompile = cmdTranspileCompile;
exports.cmdRunRepl = cmdRunRepl;
exports.cmdOpenSettings = cmdOpenSettings;
exports.cmdCheckSystemRequirements = cmdCheckSystemRequirements;
// commands.ts — provider-aware AI commands for Dictum VSCode extension
const vscode = require("vscode");
const fs = require("fs");
const path = require("path");
const settings_1 = require("./settings");
const transpiler_1 = require("./transpiler");
const ollama = require("./ollama");
const validator_1 = require("./validator");
const graph_1 = require("./graph");
// ── Ollama / provider check ──────────────────────────────────────────────────
async function checkOllama(statusBar, provider, baseUrl, apiKey) {
    const cfg = (0, settings_1.getConfig)();
    const url = baseUrl || cfg.baseUrl;
    const prov = provider || cfg.provider || 'ollama';
    const key = apiKey || cfg.apiKey || '';
    statusBar.setChecking();
    const running = await ollama.isRunning(url, prov, key || undefined);
    if (!running) {
        statusBar.setOllamaDown();
        return false;
    }
    const models = await ollama.listModels(url, prov, key || undefined);
    const displayModel = models.find((m) => m.includes(cfg.buildModel)) ?? cfg.buildModel;
    statusBar.setOllamaUp(displayModel);
    return true;
}
// ── AI: Generate Plan ────────────────────────────────────────────────────────
async function cmdGenerate(extensionPath, panel, statusBar, prompt, topModel, provider, baseUrl, apiKey) {
    const cfg = (0, settings_1.getConfig)();
    const url = baseUrl || cfg.baseUrl;
    const prov = provider || cfg.provider || 'ollama';
    const key = apiKey || cfg.apiKey || '';
    const running = await ollama.isRunning(url, prov, key || undefined);
    if (!running) {
        vscode.window.showWarningMessage(`${prov} not reachable`);
        statusBar.setOllamaDown();
        return;
    }
    const skillPath = path.join(extensionPath, 'skills', 'SKILL_PLAN.md');
    const systemPrompt = fs.existsSync(skillPath) ? fs.readFileSync(skillPath, 'utf8') : 'You are DictumPlanner.';
    statusBar.setGenerating();
    panel.postStatus('Generating plan…');
    try {
        const result = await ollama.generate({
            baseUrl: url, model: topModel || cfg.topModel,
            provider: prov, apiKey: key || undefined,
            messages: [{ role: 'system', content: systemPrompt }, { role: 'user', content: prompt }],
            stream: false
        });
        (0, validator_1.parsePlanItems)(result);
        panel.postPlan(result);
        statusBar.setReady(cfg.backend);
    }
    catch (e) {
        statusBar.setError('Generation failed');
        panel.postStatus(`Error: ${e.message}`);
    }
}
// ── AI: Build from Plan ──────────────────────────────────────────────────────
async function cmdBuild(extensionPath, panel, statusBar, approvedPlan, buildModel, currentFileUri, provider, baseUrl, apiKey, grammarMode) {
    const cfg = (0, settings_1.getConfig)();
    const url = baseUrl || cfg.baseUrl;
    const prov = provider || cfg.provider || 'ollama';
    const key = apiKey || cfg.apiKey || '';
    const running = await ollama.isRunning(url, prov, key || undefined);
    if (!running) {
        vscode.window.showWarningMessage(`${prov} not reachable`);
        return;
    }
    const effectiveMode = (grammarMode || cfg.grammarMode || 'auto') === 'auto'
        ? ollama.autoGrammarMode(prov)
        : (grammarMode || 'gbnf');
    let grammar;
    if (effectiveMode === 'gbnf') {
        const needsUnsafe = currentFileUri
            ? (() => { try {
                return /\bunsafe:\s*$/m.test(fs.readFileSync(currentFileUri.fsPath, 'utf8'));
            }
            catch {
                return false;
            } })()
            : false;
        const gbnfFile = needsUnsafe ? 'dictum_unsafe.gbnf' : 'dictum_safe.gbnf';
        const gbnfPath = path.join(extensionPath, 'grammar', gbnfFile);
        try {
            grammar = fs.readFileSync(gbnfPath, 'utf8');
        }
        catch { /* skip */ }
    }
    const skillPath = path.join(extensionPath, 'skills', 'SKILL_BUILD.md');
    const systemPrompt = fs.existsSync(skillPath) ? fs.readFileSync(skillPath, 'utf8') : 'You are DictumCoder.';
    const planText = approvedPlan.map((p) => `[PLAN: ${p.category} : ${p.id} : ${p.desc}]`).join('\n');
    const userPrompt = `Implement this plan in Dictum:\n\n${planText}`;
    statusBar.setGenerating();
    panel.postStatus('Building…');
    panel.postGrammar(effectiveMode);
    let generated = '';
    const useStream = prov !== 'openai' && prov !== 'anthropic';
    try {
        const returned = await ollama.generate({
            baseUrl: url, model: buildModel || cfg.buildModel,
            provider: prov, apiKey: key || undefined,
            system: systemPrompt, prompt: userPrompt, grammar,
            stream: useStream,
            onToken: useStream ? ((token) => { generated += token; panel.postBuildOutput(generated); }) : undefined
        });
        if (!useStream) {
            generated = returned;
        }
        // Covers both branches: the streaming path accumulated its own
        // copy from onToken (bypassing generate()'s own stripThinking), and
        // the non-streaming path's `returned` was already stripped once by
        // generate() but stripping again is a no-op when nothing is there.
        generated = ollama.stripThinking(generated);
        panel.postBuildOutput(generated);
        const l2 = (0, validator_1.checkL2Structural)(generated, approvedPlan);
        const violations = (0, validator_1.checkL3)(generated);
        if (currentFileUri)
            (0, graph_1.indexSource)(currentFileUri.fsPath + '#generated', generated);
        panel.postBuildOutput(generated);
        const allChecks = [
            ...l2.passed.map((p) => ({ pass: true, layer: 'L2', id: `${p.category}_${p.id}`, detail: p.desc })),
            ...l2.failed.map((p) => ({ pass: false, layer: 'L2', id: `${p.category}_${p.id}`, detail: p.detail || 'plan item not found in generated code' })),
            ...l2.unverifiable.map((p) => ({ pass: true, layer: 'L2', id: `${p.category}_${p.id}`, detail: `unverifiable: ${p.detail || p.desc}` })),
            ...violations.map((v) => ({ pass: false, layer: 'L3', id: v.rule, detail: v.detail }))
        ];
        if (allChecks.length > 0)
            panel.postChecks(allChecks);
        statusBar.setReady(cfg.backend);
    }
    catch (e) {
        statusBar.setError('Build failed');
        panel.postStatus(`Build error: ${e.message}`);
    }
}
// ── AI: Review ───────────────────────────────────────────────────────────────
async function cmdReview(extensionPath, panel, statusBar, approvedPlan, generatedCode, topModel, provider, baseUrl, apiKey) {
    const cfg = (0, settings_1.getConfig)();
    const url = baseUrl || cfg.baseUrl;
    const prov = provider || cfg.provider || 'ollama';
    const key = apiKey || cfg.apiKey || '';
    const running = await ollama.isRunning(url, prov, key || undefined);
    if (!running) {
        vscode.window.showWarningMessage(`${prov} not reachable`);
        return;
    }
    const skillPath = path.join(extensionPath, 'skills', 'SKILL_REVIEW.md');
    const systemPrompt = fs.existsSync(skillPath) ? fs.readFileSync(skillPath, 'utf8') : 'You are DictumReviewer.';
    const planText = approvedPlan.map((p) => `[PLAN: ${p.category} : ${p.id} : ${p.desc}]`).join('\n');
    const userMsg = `Approved plan:\n${planText}\n\nGenerated code:\n\`\`\`\n${generatedCode}\n\`\`\``;
    statusBar.setGenerating();
    panel.postStatus('Reviewing…');
    try {
        const reviewText = await ollama.generate({
            baseUrl: url, model: topModel || cfg.topModel,
            provider: prov, apiKey: key || undefined,
            messages: [{ role: 'system', content: systemPrompt }, { role: 'user', content: userMsg }],
            stream: false
        });
        const checkLines = (0, validator_1.parseCheckLines)(reviewText);
        const passed = (0, validator_1.reviewPassed)(reviewText);
        panel.postChecks(checkLines);
        panel.postVerdict(passed, reviewText);
        statusBar.setReady(cfg.backend);
    }
    catch (e) {
        statusBar.setError('Review failed');
        panel.postStatus(`Review error: ${e.message}`);
    }
}
// ── Apply: write generated code to the active .dict file ─────────────────────
async function cmdApply(generatedCode, targetUri) {
    if (!generatedCode || !generatedCode.trim()) {
        vscode.window.showWarningMessage('Nothing to apply — run Build first.');
        return;
    }
    let uri = targetUri;
    if (!uri) {
        uri = vscode.window.activeTextEditor?.document.uri;
    }
    if (!uri) {
        // No active file — prompt user to save as a new .dict file
        const newUri = await vscode.window.showSaveDialog({
            filters: { 'Dictum': ['dict', 'dictum'] },
            title: 'Save generated Dictum code as…'
        });
        if (!newUri)
            return;
        uri = newUri;
    }
    try {
        const edit = new vscode.WorkspaceEdit();
        const doc = await vscode.workspace.openTextDocument(uri).then(d => d, async () => {
            // File doesn't exist yet — create it
            edit.createFile(uri, { overwrite: true });
            await vscode.workspace.applyEdit(edit);
            return vscode.workspace.openTextDocument(uri);
        });
        const fullRange = new vscode.Range(doc.positionAt(0), doc.positionAt(doc.getText().length));
        const writeEdit = new vscode.WorkspaceEdit();
        writeEdit.replace(uri, fullRange, generatedCode);
        const ok = await vscode.workspace.applyEdit(writeEdit);
        if (ok) {
            await vscode.window.showTextDocument(doc);
            vscode.window.showInformationMessage('Dictum: generated code applied ✓');
        }
        else {
            vscode.window.showErrorMessage('Dictum: failed to apply generated code.');
        }
    }
    catch (e) {
        vscode.window.showErrorMessage(`Dictum Apply error: ${e.message}`);
    }
}
// ── Transpile: .dict → C/C++ and show the output ─────────────────────────────
async function cmdTranspile(extensionPath, statusBar) {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document.languageId !== 'dictum') {
        vscode.window.showWarningMessage('Open a .dict file to transpile.');
        return;
    }
    const cfg = (0, settings_1.getConfig)();
    const compilerScript = (0, settings_1.getCompilerPath)(extensionPath);
    const filePath = editor.document.uri.fsPath;
    statusBar.setGenerating();
    try {
        const result = await (0, transpiler_1.transpile)(filePath, cfg.pythonPath, compilerScript, cfg.backend, cfg.cppStandard);
        if (!result.success) {
            statusBar.setError('Transpile failed');
            const msgs = result.errors.map(e => `Line ${e.line + 1}: ${e.message}`).join('\n');
            vscode.window.showErrorMessage(`Dictum transpile errors:\n${msgs}`);
            return;
        }
        // Show the C/C++ output in a new untitled document
        const lang = cfg.backend === 'cpp' ? 'cpp' : 'c';
        const doc = await vscode.workspace.openTextDocument({ content: result.code, language: lang });
        await vscode.window.showTextDocument(doc, vscode.ViewColumn.Beside);
        statusBar.setReady(cfg.backend);
    }
    catch (e) {
        statusBar.setError('Transpile error');
        vscode.window.showErrorMessage(`Dictum transpile error: ${e.message}`);
    }
}
// ── Transpile & Compile: .dict → C/C++ → native binary ───────────────────────
async function cmdTranspileCompile(extensionPath, statusBar) {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document.languageId !== 'dictum') {
        vscode.window.showWarningMessage('Open a .dict file to compile.');
        return;
    }
    const cfg = (0, settings_1.getConfig)();
    const compilerScript = (0, settings_1.getCompilerPath)(extensionPath);
    const filePath = editor.document.uri.fsPath;
    statusBar.setGenerating();
    // Step 1: transpile
    let transpileResult;
    try {
        transpileResult = await (0, transpiler_1.transpile)(filePath, cfg.pythonPath, compilerScript, cfg.backend, cfg.cppStandard);
    }
    catch (e) {
        statusBar.setError('Transpile error');
        vscode.window.showErrorMessage(`Dictum transpile error: ${e.message}`);
        return;
    }
    if (!transpileResult.success) {
        statusBar.setError('Transpile failed');
        const msgs = transpileResult.errors.map((e) => `Line ${e.line + 1}: ${e.message}`).join('\n');
        vscode.window.showErrorMessage(`Dictum transpile errors:\n${msgs}`);
        return;
    }
    // Step 2: write C/C++ to a temp file and invoke gcc/clang
    const os = require('os');
    const fs = require('fs');
    const path = require('path');
    const { exec } = require('child_process');
    const { promisify } = require('util');
    const execAsync = promisify(exec);
    const ext = cfg.backend === 'cpp' ? 'cpp' : 'c';
    const stem = path.basename(filePath, path.extname(filePath));
    const tmpC = path.join(os.tmpdir(), `${stem}.${ext}`);
    const outBin = path.join(path.dirname(filePath), stem);
    try {
        fs.writeFileSync(tmpC, transpileResult.code, 'utf8');
        const compiler = cfg.backend === 'cpp' ? `g++ -std=c++${cfg.cppStandard}` : 'gcc -std=c11';
        const runtimeInclude = path.join(extensionPath, 'compiler', 'runtime');
        const cmd = `${compiler} -I"${runtimeInclude}" "${tmpC}" -o "${outBin}" -lm`;
        await execAsync(cmd, { timeout: 30000 });
        statusBar.setReady(cfg.backend);
        vscode.window.showInformationMessage(`Dictum: compiled → ${outBin}`);
    }
    catch (e) {
        statusBar.setError('Compile failed');
        const errText = e.stderr || e.stdout || e.message || 'Unknown compiler error';
        vscode.window.showErrorMessage(`gcc/g++ error:\n${errText.split('\n').slice(0, 5).join('\n')}`);
    }
    finally {
        try {
            require('fs').unlinkSync(tmpC);
        }
        catch { /* ignore */ }
    }
}
// ── REPL: open an interactive terminal running dictumc in REPL mode ───────────
function cmdRunRepl(extensionPath) {
    const cfg = (0, settings_1.getConfig)();
    const compilerScript = (0, settings_1.getCompilerPath)(extensionPath);
    const terminal = vscode.window.createTerminal({ name: 'Dictum REPL' });
    terminal.sendText(`"${cfg.pythonPath}" "${compilerScript}" --repl`);
    terminal.show();
}
// ── Settings / Ollama install ─────────────────────────────────────────────────
function cmdOpenSettings() {
    vscode.commands.executeCommand('workbench.action.openSettings', 'dictum');
}
/**
 * FIX: this used to be cmdInstallOllama() — a single hardcoded
 * vscode.env.openExternal('https://ollama.ai/download') call, regardless
 * of which Build provider was actually configured. Since koboldcpp became
 * the default Build provider (which has no installer at all — it's a
 * single downloadable executable, confirmed against its own docs/releases
 * page), and since gcc/clang and Python were never checked together
 * anywhere in one place, this replaces that single hardcoded action with a
 * real, comprehensive report: everything the pipeline needs, what's
 * actually present vs missing on THIS system right now, and the correct
 * install instructions for whichever specific things are missing.
 */
async function cmdCheckSystemRequirements() {
    const prereqs = require('./prereqs');
    const cfg = (0, settings_1.getConfig)();
    const items = await prereqs.getSystemReport(cfg, ollama);
    const channel = vscode.window.createOutputChannel('Dictum: System Requirements');
    channel.clear();
    channel.appendLine('Dictum — System Requirements Check');
    channel.appendLine('='.repeat(40));
    channel.appendLine('');
    let allRequiredPresent = true;
    for (const item of items) {
        const icon = item.present ? '✓' : (item.required ? '✗' : '⚠');
        channel.appendLine(`${icon} ${item.name}`);
        channel.appendLine(`    ${item.detail}`);
        if (!item.present && item.installHint) {
            channel.appendLine(`    → ${item.installHint}`);
        }
        channel.appendLine('');
        if (item.required && !item.present)
            allRequiredPresent = false;
    }
    channel.appendLine('='.repeat(40));
    channel.appendLine(allRequiredPresent
        ? 'All required components are present.'
        : 'Some required components are missing — see ✗ items above.');
    channel.show();
    if (!allRequiredPresent) {
        vscode.window.showWarningMessage('Dictum: some required components are missing. See the "Dictum: System Requirements" output panel for details.');
    }
    else {
        vscode.window.showInformationMessage('Dictum: all required components are present.');
    }
}
//# sourceMappingURL=commands.js.map