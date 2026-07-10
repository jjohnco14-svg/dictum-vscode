"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.checkCompilerPrereq = checkCompilerPrereq;
exports.checkPythonPrereq = checkPythonPrereq;
exports.checkBuildProviderPrereq = checkBuildProviderPrereq;
exports.getSystemReport = getSystemReport;
// prereqs.ts — startup checks for required external toolchains (gcc/clang,
// Python, and whichever Build provider is configured) and an on-demand
// consolidated report of what's present/missing.
const vscode = require("vscode");
const child_process_1 = require("child_process");
function run(cmd) {
    return new Promise((resolve) => {
        (0, child_process_1.exec)(cmd, { timeout: 5000 }, (err, stdout, stderr) => {
            resolve({ ok: !err, stdout: stdout || '', stderr: stderr || '' });
        });
    });
}
function installInstructions() {
    switch (process.platform) {
        case 'win32':
            return 'Install MSYS2 (https://www.msys2.org/), then run: pacman -S mingw-w64-ucrt-x86_64-gcc — and add its bin/ folder to PATH.';
        case 'darwin':
            return 'Run: xcode-select --install';
        default:
            return 'Run: sudo apt install build-essential (Debian/Ubuntu) or the equivalent for your distro.';
    }
}
function pythonInstallInstructions() {
    switch (process.platform) {
        case 'win32':
            return 'Install from https://python.org/downloads (check "Add Python to PATH" during setup) or run: winget install Python.Python.3.';
        case 'darwin':
            return 'Run: brew install python3 (or install from https://python.org/downloads).';
        default:
            return 'Run: sudo apt install python3 (Debian/Ubuntu) or the equivalent for your distro.';
    }
}
const GCC_CHECK_DISMISSED_KEY = 'dictum.gccCheckDismissed';
/**
 * Silently checks for a working `gcc` (falling back to `clang`) on activation.
 * If neither is found, shows a single notification with platform-specific
 * install instructions. Does not block activation either way.
 */
async function checkCompilerPrereq(context) {
    if (context.globalState.get(GCC_CHECK_DISMISSED_KEY))
        return;
    const gcc = await run('gcc --version');
    if (gcc.ok)
        return;
    const clang = await run('clang --version');
    if (clang.ok)
        return;
    const instructions = installInstructions();
    const choice = await vscode.window.showWarningMessage(`Dictum needs gcc or clang to compile and run generated code (L4 safety checks). ` +
        `Not found on PATH. ${instructions}`, "Don't show again", 'OK');
    if (choice === "Don't show again") {
        await context.globalState.update(GCC_CHECK_DISMISSED_KEY, true);
    }
}
const PYTHON_CHECK_DISMISSED_KEY = 'dictum.pythonCheckDismissed';
/**
 * Silently checks for a working Python 3 interpreter on activation. This
 * check did not exist before — every single Build/Review/compile-gate
 * path ultimately shells out through compiler/dictumc_cli.py (a Python
 * script), so a missing or broken Python install previously surfaced as a
 * confusing, generic subprocess failure deep inside _runCompileGate, with
 * no upfront warning telling the person what was actually missing.
 * Tries the configured pythonPath setting first (default 'python'), then
 * falls back to 'python3' since that's the more common name on macOS/Linux
 * where bare 'python' often doesn't exist at all.
 */
async function checkPythonPrereq(context, pythonPath) {
    if (context.globalState.get(PYTHON_CHECK_DISMISSED_KEY))
        return;
    const configured = await run(`${pythonPath} --version`);
    if (configured.ok)
        return;
    const fallback = await run('python3 --version');
    if (fallback.ok)
        return;
    const instructions = pythonInstallInstructions();
    const choice = await vscode.window.showWarningMessage(`Dictum needs Python 3 to run its compiler (compiler/dictumc_cli.py). ` +
        `Neither '${pythonPath}' nor 'python3' was found on PATH. ${instructions}`, "Don't show again", 'OK');
    if (choice === "Don't show again") {
        await context.globalState.update(PYTHON_CHECK_DISMISSED_KEY, true);
    }
}
const BUILD_PROVIDER_CHECK_DISMISSED_KEY = 'dictum.buildProviderCheckDismissed';
/**
 * Checks that Build's configured provider is actually reachable on
 * activation. Provider-aware — checks the RIGHT endpoint for whichever
 * provider is configured, and only nudges about installing something
 * local (koboldcpp/ollama/lmstudio) when that's genuinely what's
 * configured, never for a cloud provider the person isn't using.
 *
 * FIX: this replaces the old checkOllamaPrereq, which had three separate,
 * real bugs found during this session's KoboldCpp migration work:
 *   1. It checked `initialCfg.provider`, which is Plan/Review's provider —
 *      NOT Build's. Since the GBNF-related fixes earlier separated
 *      buildProvider into its own config key, this check was reading the
 *      wrong field entirely and could silently never fire even when
 *      Build's real provider (buildProvider) was 'koboldcpp' with nothing
 *      running, or fire incorrectly when Plan/Review happened to be set
 *      to 'ollama' while Build was actually using something else.
 *   2. It always checked Ollama's /api/tags endpoint and always
 *      recommended installing Ollama — regardless of which provider was
 *      actually configured. Since koboldcpp became the default Build
 *      provider, this check was nudging people to install the WRONG tool.
 *   3. /api/tags is also the wrong endpoint for koboldcpp specifically —
 *      confirmed in ollama.js's isRunning() fix: koboldcpp's real,
 *      recommended endpoint is /api/v1/model, not the Ollama-compatibility
 *      shim koboldcpp's own docs say not to rely on.
 */
async function checkBuildProviderPrereq(context, buildProvider, buildBaseUrl, ollamaModule) {
    if (context.globalState.get(BUILD_PROVIDER_CHECK_DISMISSED_KEY))
        return;
    // Cloud providers need no local install — nothing to nudge about.
    if (!['ollama', 'koboldcpp', 'lmstudio'].includes(buildProvider))
        return;
    const reachable = await ollamaModule.isRunning(buildBaseUrl, buildProvider).catch(() => false);
    if (reachable)
        return;
    const providerInfo = {
        koboldcpp: {
            label: 'KoboldCpp',
            note: 'Build is configured to use KoboldCpp for real GBNF-grammar-constrained generation.',
            url: 'https://github.com/LostRuins/koboldcpp/releases',
            urlLabel: 'Open KoboldCpp releases',
        },
        ollama: {
            label: 'Ollama',
            note: 'Build is configured to use Ollama. Note: Ollama\'s real API has no grammar-constraint field — ' +
                'Build will run unconstrained (tools mode) even though Ollama is reachable. Consider KoboldCpp for real GBNF support.',
            url: 'https://ollama.ai/download',
            urlLabel: 'Open ollama.ai/download',
        },
        lmstudio: {
            label: 'LM Studio',
            note: 'Build is configured to use LM Studio. Make sure its local server is started ' +
                '(lightning bolt icon → Start Server) — it does not start automatically when the app opens.',
            url: 'https://lmstudio.ai/download',
            urlLabel: 'Open lmstudio.ai/download',
        },
    }[buildProvider];
    const choice = await vscode.window.showWarningMessage(`Dictum: ${providerInfo.label} not detected at ${buildBaseUrl}. ${providerInfo.note}`, providerInfo.urlLabel, "Don't show again");
    if (choice === providerInfo.urlLabel) {
        vscode.env.openExternal(vscode.Uri.parse(providerInfo.url));
    }
    else if (choice === "Don't show again") {
        await context.globalState.update(BUILD_PROVIDER_CHECK_DISMISSED_KEY, true);
    }
}
/**
 * On-demand, consolidated system report — everything Dictum's pipeline
 * needs, checked fresh (not gated by any "don't show again" dismissal,
 * since this is explicitly requested, not a passive nudge), with a clear
 * present/missing verdict and install instructions for each missing item.
 * Intended for a command (e.g. "Dictum: Check System Requirements") rather
 * than automatic activation-time nudging.
 */
async function getSystemReport(cfg, ollamaModule) {
    const items = [];
    // 1. C/C++ compiler — required for the compile gate (L4), every build.
    const gcc = await run('gcc --version');
    const clang = gcc.ok ? { ok: true, stdout: '' } : await run('clang --version');
    items.push({
        name: 'C/C++ compiler (gcc or clang)',
        present: gcc.ok || clang.ok,
        detail: gcc.ok ? 'gcc found' : clang.ok ? 'clang found' : 'neither gcc nor clang found on PATH',
        installHint: installInstructions(),
        required: true,
    });
    // 2. Python 3 — required to run compiler/dictumc_cli.py at all.
    const pyConfigured = await run(`${cfg.pythonPath || 'python'} --version`);
    const pyFallback = pyConfigured.ok ? { ok: true, stdout: '' } : await run('python3 --version');
    items.push({
        name: 'Python 3 (runs the Dictum compiler)',
        present: pyConfigured.ok || pyFallback.ok,
        detail: pyConfigured.ok ? `'${cfg.pythonPath || 'python'}' found`
            : pyFallback.ok ? `'python3' found ('${cfg.pythonPath || 'python'}' was not)`
                : `neither '${cfg.pythonPath || 'python'}' nor 'python3' found on PATH`,
        installHint: pythonInstallInstructions(),
        required: true,
    });
    // 3. Build's configured provider — required for real generation.
    const buildProvider = cfg.buildProvider || 'koboldcpp';
    const buildBaseUrl = cfg.buildBaseUrl || 'http://localhost:5001';
    const isLocalProvider = ['ollama', 'koboldcpp', 'lmstudio'].includes(buildProvider);
    if (isLocalProvider) {
        const reachable = await ollamaModule.isRunning(buildBaseUrl, buildProvider).catch(() => false);
        const urls = {
            koboldcpp: 'https://github.com/LostRuins/koboldcpp/releases (single-file executable, no installer)',
            ollama: 'https://ollama.ai/download',
            lmstudio: 'https://lmstudio.ai/download',
        };
        items.push({
            name: `Build provider: ${buildProvider} (at ${buildBaseUrl})`,
            present: reachable,
            detail: reachable ? 'reachable' : 'not reachable — is it running?',
            installHint: urls[buildProvider],
            required: true,
        });
        if (buildProvider === 'ollama') {
            items.push({
                name: 'GBNF grammar support',
                present: false,
                detail: 'Ollama has no real grammar-constraint API field — Build runs unconstrained (tools mode) ' +
                    'on Ollama regardless of the grammarMode setting. KoboldCpp is the provider with real GBNF support.',
                installHint: 'https://github.com/LostRuins/koboldcpp/releases',
                required: false,
            });
        }
    }
    else {
        items.push({
            name: `Build provider: ${buildProvider} (cloud)`,
            present: true,
            detail: 'cloud provider — no local install needed, assumed reachable (API key required)',
            installHint: '',
            required: false,
        });
    }
    return items;
}
//# sourceMappingURL=prereqs.js.map