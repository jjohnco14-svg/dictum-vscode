"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.parseStderr = parseStderr;
exports.compileCheck = compileCheck;
exports.runtimeQualityCheck = runtimeQualityCheck;
exports.transpile = transpile;
exports.validate = validate;
exports.getLdflags = getLdflags;
// transpiler.ts — wrapper around dictumc_cli.py
const child_process_1 = require("child_process");
const util_1 = require("util");
const fs = require("fs");
const path = require("path");
const os = require("os");
const execAsync = (0, util_1.promisify)(child_process_1.exec);
function parseStderr(stderr) {
    const errors = [];
    const warnings = [];
    for (const line of stderr.split('\n')) {
        const lineErrMatch = line.match(/dictumc:\s*error:\s*\[Line (\d+)\]\s*(.*)/i);
        if (lineErrMatch) {
            errors.push({ line: Math.max(0, parseInt(lineErrMatch[1]) - 1), message: lineErrMatch[2].trim(), severity: 'error' });
            continue;
        }
        const bareLineMatch = line.match(/\[Line (\d+)\]\s*(.*)/);
        if (bareLineMatch) {
            errors.push({ line: Math.max(0, parseInt(bareLineMatch[1]) - 1), message: bareLineMatch[2].trim(), severity: 'error' });
            continue;
        }
        const warnMatch = line.match(/dictumc:\s*warning:\s*(.*)/i);
        if (warnMatch) {
            warnings.push({ line: 0, message: warnMatch[1].trim(), severity: 'warning' });
            continue;
        }
        if (line.includes('Ownership violation:'))
            errors.push({ line: 0, message: line.trim(), severity: 'error' });
    }
    return { errors, warnings };
}
/**
 * Runs a fast syntax-only check against a real C/C++ compiler. This is
 * deliberately separate from transpile()'s own success flag — transpile()
 * only reports whether the Dictum compiler itself errored (lexer/parser/
 * validator level), not whether the *emitted* C/C++ actually compiles.
 * A Dictum-level pass can still emit C that gcc rejects (e.g. a missing
 * blessed-library header, a name collision with a reserved word the
 * validator didn't catch). This function is the hard gate between
 * "Review passed" and a Run button being enabled — no binary should ever
 * be offered to run unless this returns ok: true.
 */
async function compileCheck(code, backend, cppStandard = 17, runtimeIncludeDir, linkFlags) {
    const ext = backend === 'cpp' ? 'cpp' : 'c';
    const tmpFile = path.join(os.tmpdir(), `dictum_syntaxcheck_${Date.now()}_${Math.random().toString(36).slice(2)}.${ext}`);
    const tmpBin = tmpFile + '.bin';
    try {
        fs.writeFileSync(tmpFile, code, 'utf8');
        const compiler = backend === 'cpp' ? `g++ -std=c++${cppStandard}` : 'gcc -std=c11';
        const includeFlag = runtimeIncludeDir ? `-I"${runtimeIncludeDir}"` : '';
        const extraLd = linkFlags ? linkFlags : '';
        // Phase 1: fast syntax-only check (catches most errors cheaply)
        const syntaxCmd = `${compiler} -fsyntax-only ${includeFlag} "${tmpFile}"`;
        await execAsync(syntaxCmd, { timeout: 15000 });
        // Phase 2: real compile-and-link to catch undefined external symbols
        // (e.g. typo'd FFI imports, missing -l flags for blessed libraries,
        // OR a call into a stdlib function that the registry advertises but
        // the runtime never actually implements).
        //
        // FIX (production-readiness Problem 0): this used to run ONLY for
        // files containing `extern` declarations, on the assumption that
        // "pure Dictum programs can't have this class of bug." That
        // assumption is false — a pure-Dictum program calling a stdlib
        // function with no real (non-stub) implementation in
        // compiler/runtime/*.h links against nothing, `-fsyntax-only` never
        // sees it (the call itself is syntactically fine), and the gate
        // reported "Compile check passed" while the program was
        // guaranteed to fail — or worse, silently return garbage forever
        // if it happened to hit a stub that returns NULL/0 instead of
        // erroring. Undefined references to stdlib functions are exactly
        // as real a bug as undefined references to FFI symbols, so the
        // link step now always runs, regardless of whether the file uses
        // `extern`.
        const linkCmd = `${compiler} ${includeFlag} "${tmpFile}" -o "${tmpBin}" -lm ${extraLd}`;
        await execAsync(linkCmd, { timeout: 15000 });
        return { ok: true, errors: '' };
    }
    catch (e) {
        const stderr = e.stderr || e.stdout || e.message || 'Unknown compiler error';
        return { ok: false, errors: stderr };
    }
    finally {
        try {
            fs.unlinkSync(tmpFile);
        }
        catch { /* ignore */ }
        try {
            fs.unlinkSync(tmpBin);
        }
        catch { /* ignore */ }
    }
}
/**
 * Runtime quality gate for the overnight random-task pool (Option A from
 * the design discussion: no ground-truth oracle exists for a randomly
 * generated task, so this checks the things that ARE checkable without
 * knowing the "correct" answer -- does it terminate, does it exit
 * cleanly, does it trigger a sanitizer.
 *
 * Compiles the code a SECOND time with sanitizers enabled (ASan+UBSan
 * together for general/unsafe-skill tasks; TSan separately for
 * concurrent-skill tasks, since gcc cannot combine ASan and TSan in one
 * binary), actually runs the resulting binary with a timeout, and reports
 * exactly which check failed.
 *
 * Deliberately NOT part of compileCheck()/the normal compile gate -- this
 * is slower (real execution, not just compile) and only makes sense to
 * run once per finished task in the random-pool quality loop, not on
 * every Build retry attempt.
 */
async function runtimeQualityCheck(code, backend, cppStandard = 17, runtimeIncludeDir, needsConcurrencyCheck = false, runTimeoutMs = 5000) {
    const ext = backend === 'cpp' ? 'cpp' : 'c';
    const tag = `${Date.now()}_${Math.random().toString(36).slice(2)}`;
    const tmpFile = path.join(os.tmpdir(), `dictum_qualitycheck_${tag}.${ext}`);
    const tmpBin = tmpFile + '.bin';
    const compiler = backend === 'cpp' ? `g++ -std=c++${cppStandard}` : 'gcc -std=c11';
    const includeFlag = runtimeIncludeDir ? `-I"${runtimeIncludeDir}"` : '';
    // -g for symbolized sanitizer output (otherwise reports are addresses,
    // not useful in an unattended report someone reads in the morning).
    const sanFlags = needsConcurrencyCheck ? '-fsanitize=thread -g' : '-fsanitize=address,undefined -g';
    try {
        fs.writeFileSync(tmpFile, code, 'utf8');
        const compileCmd = `${compiler} ${sanFlags} ${includeFlag} "${tmpFile}" -o "${tmpBin}" -lm`;
        try {
            await execAsync(compileCmd, { timeout: 20000 });
        }
        catch (e) {
            // Should be rare -- this code already passed the real compileCheck
            // gate before reaching here. If sanitizer instrumentation itself
            // fails to compile, that's worth knowing about as its own
            // category, distinct from a runtime failure of working code.
            return { ok: false, failureKind: 'compile', detail: (e.stderr || e.message || 'sanitizer build failed').toString() };
        }
        let runResult;
        try {
            runResult = await execAsync(`"${tmpBin}"`, {
                timeout: runTimeoutMs,
                // UBSan, unlike ASan/TSan, does NOT abort by default on a
                // detected violation -- it prints the diagnostic to stderr
                // and exits 0. Confirmed by direct testing: a genuine signed
                // integer overflow produced the correct UBSan diagnostic on
                // stderr but exit code 0, meaning execAsync's catch branch
                // (where all the sanitizer-text classification logic lives)
                // never ran, and the violation silently reported as a pass.
                // halt_on_error=1 makes UBSan behave consistently with the
                // other two sanitizers (real non-zero exit on a real
                // violation) instead of needing separate success-path
                // parsing as the only signal.
                env: { ...process.env, UBSAN_OPTIONS: 'halt_on_error=1:print_stacktrace=1' },
            });
        }
        catch (e) {
            if (e.killed && e.signal === 'SIGTERM') {
                return { ok: false, failureKind: 'timeout', detail: `Did not exit within ${runTimeoutMs}ms — possible infinite loop or deadlock.` };
            }
            const stderr = (e.stderr || '').toString();
            const stdout = (e.stdout || '').toString();
            const combined = stderr + stdout;
            if (/AddressSanitizer/.test(combined)) {
                return { ok: false, failureKind: 'asan', detail: combined.slice(0, 4000), exitInfo: `signal=${e.signal} code=${e.code}` };
            }
            if (/UndefinedBehaviorSanitizer|runtime error:/.test(combined)) {
                return { ok: false, failureKind: 'ubsan', detail: combined.slice(0, 4000), exitInfo: `signal=${e.signal} code=${e.code}` };
            }
            if (/ThreadSanitizer/.test(combined)) {
                return { ok: false, failureKind: 'tsan', detail: combined.slice(0, 4000), exitInfo: `signal=${e.signal} code=${e.code}` };
            }
            // Non-sanitizer crash: a segfault that the sanitizer didn't
            // characterize, or a non-zero exit some other way.
            return { ok: false, failureKind: 'crash', detail: combined.slice(0, 4000) || 'Process exited abnormally with no captured output.', exitInfo: `signal=${e.signal} code=${e.code}` };
        }
        // Defense in depth: even with halt_on_error set, don't trust exit
        // code 0 alone. If sanitizer diagnostic text somehow made it to
        // stdout/stderr without a non-zero exit (e.g. a future sanitizer
        // version changing defaults again), catch it here instead of
        // silently reporting a pass.
        const successCombined = (runResult.stderr || '') + (runResult.stdout || '');
        if (/AddressSanitizer/.test(successCombined)) {
            return { ok: false, failureKind: 'asan', detail: successCombined.slice(0, 4000) };
        }
        if (/UndefinedBehaviorSanitizer|runtime error:/.test(successCombined)) {
            return { ok: false, failureKind: 'ubsan', detail: successCombined.slice(0, 4000) };
        }
        if (/ThreadSanitizer/.test(successCombined)) {
            return { ok: false, failureKind: 'tsan', detail: successCombined.slice(0, 4000) };
        }
        return { ok: true, failureKind: 'none', detail: 'Ran to completion with no timeout, crash, or sanitizer violation.' };
    }
    finally {
        try {
            fs.unlinkSync(tmpFile);
        }
        catch { /* ignore */ }
        try {
            fs.unlinkSync(tmpBin);
        }
        catch { /* ignore */ }
    }
}
async function transpile(filePath, pythonPath, compilerScript, backend, cppStandard = 17, useStdlib = true) {
    // FIX: this used to never pass --stdlib, so STDLIB_ACTION_FAMILIES
    // registration (File/Json/Http/Net/Mutex/Math/... recognition in the
    // validator/emitter) was never actually enabled for a real Build-stage
    // compile — only for someone invoking dictumc_cli.py by hand with the
    // flag set explicitly. Defaults to on; callers that specifically want
    // the bare grammar (e.g. a strict-grammar diagnostic pass) can pass
    // useStdlib=false.
    let cmd = `"${pythonPath}" "${compilerScript}" "${filePath}" --backend ${backend}`;
    if (useStdlib)
        cmd += ' --stdlib';
    if (backend === 'cpp')
        cmd += ` --cpp-standard ${cppStandard}`;
    try {
        const { stdout, stderr } = await execAsync(cmd, { timeout: 30000 });
        const sp = parseStderr(stdout);
        const ss = parseStderr(stderr);
        const errors = [...sp.errors, ...ss.errors];
        const warnings = [...sp.warnings, ...ss.warnings];
        if (errors.length > 0)
            return { code: '', errors, warnings, success: false };
        return { code: stdout, errors, warnings, success: true };
    }
    catch (e) {
        const stdout = e.stdout || '';
        const stderr = e.stderr || e.message || '';
        const sp = parseStderr(stdout);
        const ss = parseStderr(stderr);
        const errors = [...sp.errors, ...ss.errors];
        const warnings = [...sp.warnings, ...ss.warnings];
        if (errors.length === 0) {
            const msg = (stdout || stderr).split('\n')[0].trim();
            errors.push({ line: 0, message: msg || 'Transpile failed', severity: 'error' });
        }
        return { code: '', errors, warnings, success: false };
    }
}
/**
 * Asks dictumc for the real linker flags this program needs (-lm, plus
 * -lpthread/-lssl/-lcrypto/-lrt for Mutex/Thread/Tls/Shm/Timer, plus any
 * blessed-library #[link "x"] directive). Used to be nothing — every
 * caller of compileCheck() either passed no linkFlags at all or hardcoded
 * "-lm", so anything beyond math would compile and then fail to link.
 * Falls back to "-lm" on any failure so a flaky query never blocks a
 * build that would otherwise succeed.
 */
async function getLdflags(filePath, pythonPath, compilerScript, backend, useStdlib = true) {
    let cmd = `"${pythonPath}" "${compilerScript}" "${filePath}" --backend ${backend} --print-ldflags`;
    if (useStdlib)
        cmd += ' --stdlib';
    try {
        const { stdout } = await execAsync(cmd, { timeout: 15000 });
        const flags = stdout.trim();
        return flags.length ? flags : '-lm';
    }
    catch {
        return '-lm';
    }
}
async function validate(source, pythonPath, compilerScript) {
    const tmpFile = path.join(os.tmpdir(), `dictum_validate_${Date.now()}.dict`);
    try {
        fs.writeFileSync(tmpFile, source, 'utf8');
        const cmd = `"${pythonPath}" "${compilerScript}" "${tmpFile}" --backend c --validate`;
        const { stdout, stderr } = await execAsync(cmd, { timeout: 15000 }).catch((e) => ({
            stdout: e.stdout || '', stderr: e.stderr || e.message || ''
        }));
        const sp = parseStderr(stdout);
        const ss = parseStderr(stderr);
        return [...sp.errors, ...ss.errors, ...sp.warnings, ...ss.warnings];
    }
    finally {
        try {
            fs.unlinkSync(tmpFile);
        }
        catch { /* ignore */ }
    }
}
//# sourceMappingURL=transpiler.js.map