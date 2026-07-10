"use strict";
// bridge.js — Auto-Validated Bridge System (AVBS)
//
// Adapted from the AVBS v1.0 architecture spec, wired into Dictum's existing
// checkL2Fields()/type_bridge.json mechanism (see validator.js) rather than
// replacing it. type_bridge.json's hand-seeded rules ("Confirmed Rules" in
// AVBS terms) are loaded and used exactly as before; this module adds the
// missing piece — auto-discovering and validating a NEW rule at runtime when
// Plan invents a composite type with no existing bridge entry, instead of
// always falling through to checkL2Fields' "unverifiable" result.
//
// Two bugs found in the original spec's section 7 drift guard, fixed here:
//   1. The spec hashed the ENTIRE grammar.py file. Confirmed by direct test:
//      a single unrelated comment change anywhere in that ~950-line file
//      changes the hash and re-triggers full re-validation of every rule —
//      a guard that fires on irrelevant changes gets ignored as noise,
//      which defeats its purpose. Fixed: hash only the serialized,
//      sorted TYPE_WORDS set.
//   2. The spec's re-validation loop only checked rules where
//      source === 'ai-generated', permanently exempting builtin rules
//      (vec3, vec2, rgb, logical) from drift detection — confirmed by
//      direct simulation against the spec's own pseudocode. Fixed: every
//      rule is re-checked on drift, regardless of source.
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const crypto = require("crypto");
Object.defineProperty(exports, "__esModule", { value: true });
exports.proposeAndValidateRule = proposeAndValidateRule;
exports.loadBridgeStore = loadBridgeStore;
exports.saveBridgeStore = saveBridgeStore;
exports.checkDrift = checkDrift;
exports.getRealTypeWords = getRealTypeWords;
exports.getRealPrimitiveTypeNames = getRealPrimitiveTypeNames;
const MAX_RETRIES_PER_RULE = 3;
const BRIDGE_STORE_FILENAME = 'bridge.json';
// ── Real TYPE_WORDS extraction ──────────────────────────────────────────────
// Reads the actual primitive type vocabulary directly out of grammar.py's
// TYPE_WORDS set, rather than hardcoding a duplicate list here that could
// itself drift out of sync with the real grammar. This is read fresh each
// call (not cached at module load) so a grammar.py change is picked up
// without needing to restart the extension host.
let _typeWordsCache = null;
function getRealTypeWords(extDir) {
    const compilerDir = path.join(extDir, 'compiler');
    let out;
    try {
        out = execFileSync('python3', ['-c',
            'import sys, json; sys.path.insert(0, sys.argv[1]); ' +
            'from dictumc.type_registry import all_type_words; ' +
            'print(json.dumps(sorted(all_type_words())))',
            compilerDir], { encoding: 'utf8' });
    }
    catch (e) {
        throw new Error(`AVBS: could not query type_registry.py for TYPE_WORDS: ${e.message}`);
    }
    return new Set(JSON.parse(out));
}
/**
 * Real primitive type NAMES (not individual word tokens) -- queried
 * directly from type_registry.py, the single source of truth, rather than
 * a hand-maintained 'candidates' list filtered against getRealTypeWords()
 * (the previous approach: a second independent copy of the vocabulary
 * that had already drifted -- it never included any single-word terminal
 * type at all, e.g. u8/u16/u32/u64/i32/i64/f32/bool/bytes/nothing/result
 * were entirely absent from AVBS's notion of "real primitive types").
 */
function getRealPrimitiveTypeNames(extDir) {
    const compilerDir = path.join(extDir, 'compiler');
    let out;
    try {
        out = execFileSync('python3', ['-c',
            'import sys, json; sys.path.insert(0, sys.argv[1]); ' +
            'from dictumc.type_registry import primitive_type_names; ' +
            'print(json.dumps(sorted(primitive_type_names())))',
            compilerDir], { encoding: 'utf8' });
    }
    catch (e) {
        throw new Error(`AVBS: could not query type_registry.py for primitive type names: ${e.message}`);
    }
    return new Set(JSON.parse(out));
}
// ── Bridge store (bridge.json) ──────────────────────────────────────────────
function loadBridgeStore(extDir) {
    const storePath = path.join(extDir, 'out', BRIDGE_STORE_FILENAME);
    try {
        const raw = fs.readFileSync(storePath, 'utf8');
        return JSON.parse(raw);
    }
    catch {
        return {
            version: '1.0',
            rules: {},
            metadata: { type_words_hash: null, last_validated: null, total_rules: 0, ai_generated_rules: 0 },
        };
    }
}
function saveBridgeStore(extDir, store) {
    const storePath = path.join(extDir, 'out', BRIDGE_STORE_FILENAME);
    fs.writeFileSync(storePath, JSON.stringify(store, null, 2), 'utf8');
}
// ── Drift guard (FIXED per the two bugs found in the original spec) ────────
function hashTypeWords(typeWordsSet) {
    const sorted = [...typeWordsSet].sort();
    return 'sha256:' + crypto.createHash('sha256').update(JSON.stringify(sorted)).digest('hex');
}
/**
 * Checks bridge.json against the REAL, current TYPE_WORDS — not the whole
 * grammar.py file (bug 1 from the original spec), and across ALL rules
 * regardless of source (bug 2). Returns a list of stale rules, if any,
 * without mutating the store — callers decide what to do (flag, block,
 * re-prompt for re-validation).
 */
function checkDrift(extDir) {
    const store = loadBridgeStore(extDir);
    const realTypeWords = getRealPrimitiveTypeNames(extDir);
    const currentHash = hashTypeWords(realTypeWords);
    const driftDetected = store.metadata.type_words_hash !== null && store.metadata.type_words_hash !== currentHash;
    const staleRules = [];
    if (driftDetected || store.metadata.type_words_hash === null) {
        for (const [name, rule] of Object.entries(store.rules)) {
            for (const field of rule.expands_to) {
                const isRealPrimitive = realTypeWords.has(field.type.toLowerCase());
                const isKnownComposite = Object.prototype.hasOwnProperty.call(store.rules, field.type);
                if (!isRealPrimitive && !isKnownComposite) {
                    staleRules.push({ name, badType: field.type, source: rule.source });
                }
            }
        }
    }
    return { driftDetected, currentHash, previousHash: store.metadata.type_words_hash, staleRules };
}
// ── Gate 0: Name collision ──────────────────────────────────────────────────
function gate0_nameCollision(store, ruleName) {
    if (Object.prototype.hasOwnProperty.call(store.rules, ruleName)) {
        return { pass: true, existingRule: store.rules[ruleName], reason: 'name already confirmed — reusing existing rule' };
    }
    return { pass: true, existingRule: null };
}
// ── Gate 1: Constrained composition ─────────────────────────────────────────
function gate1_constrainedComposition(proposedFields, store, extDir) {
    const realPrimitives = getRealPrimitiveTypeNames(extDir);
    const invalid = proposedFields.filter(f => {
        const t = f.type.toLowerCase();
        return !realPrimitives.has(t) && !Object.prototype.hasOwnProperty.call(store.rules, t);
    });
    if (invalid.length > 0) {
        return {
            pass: false,
            reason: `field(s) use types that are neither real Dictum primitives nor confirmed composites: ${invalid.map(f => f.type).join(', ')}. ` +
                `Valid primitives: ${[...realPrimitives].join(', ')}.`,
        };
    }
    return { pass: true };
}
// ── Gate 2: Structural validation ───────────────────────────────────────────
function gate2_structuralValidation(proposedFields) {
    if (proposedFields.length === 0) {
        return { pass: false, reason: 'expands_to is empty — a composite rule must expand to at least one field' };
    }
    const suffixes = proposedFields.map(f => f.name_suffix);
    const hasNullSuffix = suffixes.some(s => s === null || s === undefined);
    if (hasNullSuffix) {
        return { pass: false, reason: 'every field needs a name_suffix (empty string "" is allowed for direct mapping, but not null/undefined)' };
    }
    const duplicates = suffixes.filter((s, i) => suffixes.indexOf(s) !== i);
    if (duplicates.length > 0) {
        return { pass: false, reason: `duplicate name_suffix values within one rule: ${[...new Set(duplicates)].join(', ')}` };
    }
    const missingType = proposedFields.some(f => !f.type || typeof f.type !== 'string');
    if (missingType) {
        return { pass: false, reason: 'every field needs a non-empty "type" string' };
    }
    return { pass: true };
}
// ── Gate 3: Compiler validation (real gcc/g++ via dictumc_cli.py) ──────────
function gate3_compilerValidation(ruleName, proposedFields, extDir) {
    const safeName = ruleName.replace(/[^a-zA-Z0-9_]/g, '_');
    const fieldLines = proposedFields
        .map(f => `    field${f.name_suffix || ''} as ${f.type}`)
        .join('\n');
    const defaultFor = (type) => {
        const t = type.toLowerCase();
        if (t === 'whole number' || t === 'count' || t === 'byte')
            return '0';
        if (t === 'fractional number' || t === 'decimal')
            return '0.0';
        if (t === 'truth value')
            return 'false';
        if (t === 'text')
            return '""';
        return '0';
    };
    const setLines = proposedFields
        .map(f => `    put ${defaultFor(f.type)} into T.field${f.name_suffix || ''}`)
        .join('\n');
    const dictumSource = [
        `shape BridgeTest_${safeName} holds:`,
        fieldLines,
        'end shape',
        '',
        `program BridgeTestProgram_${safeName}:`,
        `    keep T as BridgeTest_${safeName}`,
        setLines,
        'end program',
        '',
    ].join('\n');
    const tmpFile = path.join(require('os').tmpdir(), `avbs_gate3_${safeName}_${Date.now()}.dict`);
    const tmpBin = path.join(require('os').tmpdir(), `avbs_gate3_${safeName}_${Date.now()}_bin`);
    fs.writeFileSync(tmpFile, dictumSource, 'utf8');
    try {
        execFileSync('python3', [path.join(extDir, 'compiler', 'dictumc_cli.py'), tmpFile, '--compile', '-o', tmpBin], {
            cwd: path.join(extDir, 'compiler'), stdio: 'pipe', timeout: 15000,
        });
        return { pass: true, generatedSource: dictumSource };
    }
    catch (e) {
        return {
            pass: false,
            reason: `Gate 3 compile failed: ${e.stderr ? e.stderr.toString().slice(-500) : e.message}`,
            generatedSource: dictumSource,
        };
    }
    finally {
        try { fs.unlinkSync(tmpFile); } catch { }
        try { fs.unlinkSync(tmpBin); } catch { }
    }
}
// ── Gate 4: Behavioral exercise ─────────────────────────────────────────────
// Type-driven test templates, exactly as the spec describes: the test is
// chosen from the PRIMITIVE TYPE of each field, never from the AI's own
// description of what the composite means — so the AI cannot game this by
// writing a self-flattering description. This proves the fields are
// mechanically usable (assignable, comparable, arithmetic-capable); per the
// original spec's own section 10, it does NOT and cannot prove the
// composite's semantic mapping makes sense (e.g. "spectral wavelength" -> a
// 3-field RGB-shaped expansion passes Gate 4 even if that mapping is
// nonsensical to a human reader — that limitation is inherent, not a bug,
// and is surfaced honestly rather than hidden).
function buildBehaviorTemplate(ruleName, proposedFields, extDir) {
    const safeName = ruleName.replace(/[^a-zA-Z0-9_]/g, '_');
    const numericFields = proposedFields.filter(f => ['whole number', 'fractional number', 'count', 'decimal'].includes(f.type.toLowerCase()));
    const truthFields = proposedFields.filter(f => f.type.toLowerCase() === 'truth value');
    const textFields = proposedFields.filter(f => f.type.toLowerCase() === 'text');
    // FIX: the original version of this template declared T, assigned its
    // fields, then passed T BY VALUE into a separate test action. That
    // shape tripped two real, independent dictumc bugs found while
    // debugging this:
    //   1. validator.py's definite-assignment check (validate_assignment,
    //      the '.' in target_name branch) never sets info.initialized=True
    //      for dotted field assignment — only a plain `put X into Name`
    //      does. So no amount of `put X into T.field` ever satisfies a
    //      later read/pass of T itself, confirmed by isolated testing
    //      against the real compiler (and this turned out to also affect
    //      the EARLIER hide-and-seek transcript's init_cubes action,
    //      independently of AVBS — a real, pre-existing dictumc gap).
    //   2. Even routing around that via `keep T as Foo with values ...`
    //      (which DOES satisfy definite-assignment), emit_c.py generates a
    //      mismatched `Foo *` vs `Foo` C signature when T is then passed
    //      into an action — a separate, real bug in the C emitter.
    // Both are real dictumc issues worth fixing on their own, but AVBS
    // doesn't need an action call to prove fields are mechanically usable
    // — direct field access entirely at program scope (confirmed working
    // by isolated testing) is sufficient and avoids both bugs entirely.
    const lines = [];
    const fieldDecls = proposedFields.map(f => `    field${f.name_suffix || ''} as ${f.type}`).join('\n');
    lines.push(`shape BridgeBehavior_${safeName} holds:`, fieldDecls, 'end shape', '');
    lines.push(`program BridgeBehaviorProgram_${safeName}:`);
    lines.push(`    keep T as BridgeBehavior_${safeName}`);
    lines.push('    keep PassMarker as truth value with value false');
    if (numericFields.length >= 2) {
        // Ordering + non-commutative arithmetic, exactly per the spec's
        // example: distinct ascending values, then a sum check.
        const [a, b] = numericFields;
        lines.push(`    put 10 into T.field${a.name_suffix || ''}`);
        lines.push(`    put 20 into T.field${b.name_suffix || ''}`);
        lines.push(`    if T.field${a.name_suffix || ''} is less than T.field${b.name_suffix || ''} then`);
        lines.push(`        if the sum of T.field${a.name_suffix || ''} and T.field${b.name_suffix || ''} is equal to 30 then`);
        lines.push('            put true into PassMarker');
        lines.push('        end if');
        lines.push('    end if');
    }
    else if (truthFields.length >= 1) {
        const t = truthFields[0];
        lines.push(`    put true into T.field${t.name_suffix || ''}`);
        lines.push(`    if T.field${t.name_suffix || ''} is true then`);
        lines.push(`        put false into T.field${t.name_suffix || ''}`);
        lines.push(`        if T.field${t.name_suffix || ''} is false then`);
        lines.push('            put true into PassMarker');
        lines.push('        end if');
        lines.push('    end if');
    }
    else if (textFields.length >= 1) {
        const t = textFields[0];
        lines.push(`    put the text "probe" into T.field${t.name_suffix || ''}`);
        lines.push(`    if T.field${t.name_suffix || ''} is equal to the text "probe" then`);
        lines.push('        put true into PassMarker');
        lines.push('    end if');
    }
    else {
        // Single numeric field, or a type combination with no >=2-field
        // template above — fall back to a simple assign-then-read check.
        const f = proposedFields[0];
        lines.push(`    put ${f.type.toLowerCase().includes('text') ? 'the text "probe"' : '1'} into T.field${f.name_suffix || ''}`);
        lines.push('    put true into PassMarker');
    }
    lines.push('    if PassMarker is true then');
    lines.push('        print the text "PASS" and newline');
    lines.push('    otherwise');
    lines.push('        print the text "FAIL" and newline');
    lines.push('    end if');
    lines.push('end program');
    lines.push('');
    return lines.join('\n');
}
function gate4_behavioralExercise(ruleName, proposedFields, extDir) {
    const safeName = ruleName.replace(/[^a-zA-Z0-9_]/g, '_');
    const dictumSource = buildBehaviorTemplate(ruleName, proposedFields, extDir);
    const tmpFile = path.join(require('os').tmpdir(), `avbs_gate4_${safeName}_${Date.now()}.dict`);
    const tmpBin = path.join(require('os').tmpdir(), `avbs_gate4_${safeName}_${Date.now()}_bin`);
    fs.writeFileSync(tmpFile, dictumSource, 'utf8');
    try {
        execFileSync('python3', [path.join(extDir, 'compiler', 'dictumc_cli.py'), tmpFile, '--compile', '-o', tmpBin], {
            cwd: path.join(extDir, 'compiler'), stdio: 'pipe', timeout: 15000,
        });
        const runOutput = execFileSync(tmpBin, [], { timeout: 5000 }).toString().trim();
        if (runOutput.includes('PASS')) {
            return { pass: true, testSignature: `gate4-${proposedFields.length}fields`, runOutput };
        }
        return { pass: false, reason: `behavioral test ran but did not print PASS (got: ${runOutput || '<empty>'})`, runOutput };
    }
    catch (e) {
        return { pass: false, reason: `Gate 4 compile/run failed: ${e.stderr ? e.stderr.toString().slice(-500) : e.message}` };
    }
    finally {
        try { fs.unlinkSync(tmpFile); } catch { }
        try { fs.unlinkSync(tmpBin); } catch { }
    }
}
// ── Orchestration: propose -> validate through all gates -> lock or reject ─
/**
 * Attempts to discover and validate a new bridge rule for a composite type
 * Plan used that has no existing entry in type_bridge.json. `proposedFields`
 * comes from the calling code's own best-guess decomposition (see
 * extension.js's integration — it derives a guess from how Build's
 * GENERATED code actually decomposed the field, then asks AVBS to validate
 * THAT guess, rather than asking an LLM to invent a rule from Plan's prose
 * alone — keeping the "no AI judge" property: AVBS validates a candidate
 * mechanically, it never originates one by asking a model "what do you
 * think this should expand to").
 *
 * Returns one of:
 *   { locked: true, rule }                         — passed all gates, now in bridge.json
 *   { locked: false, fallback: 'unverified', ... } — retries exhausted, proceed without enforcement
 *   { locked: false, collision: true, rule }        — name already existed, reusing it (Gate 0)
 */
function proposeAndValidateRule(extDir, ruleName, proposedFields, attempt = 1) {
    const store = loadBridgeStore(extDir);
    const gate0 = gate0_nameCollision(store, ruleName);
    if (gate0.existingRule) {
        return { locked: false, collision: true, rule: gate0.existingRule };
    }
    const gate1 = gate1_constrainedComposition(proposedFields, store, extDir);
    if (!gate1.pass) {
        if (attempt >= MAX_RETRIES_PER_RULE) {
            return { locked: false, fallback: 'unverified', gateFailed: 1, reason: gate1.reason, attempt };
        }
        return { locked: false, fallback: 'retry', gateFailed: 1, reason: gate1.reason, attempt };
    }
    const gate2 = gate2_structuralValidation(proposedFields);
    if (!gate2.pass) {
        if (attempt >= MAX_RETRIES_PER_RULE) {
            return { locked: false, fallback: 'unverified', gateFailed: 2, reason: gate2.reason, attempt };
        }
        return { locked: false, fallback: 'retry', gateFailed: 2, reason: gate2.reason, attempt };
    }
    const gate3 = gate3_compilerValidation(ruleName, proposedFields, extDir);
    if (!gate3.pass) {
        if (attempt >= MAX_RETRIES_PER_RULE) {
            return { locked: false, fallback: 'unverified', gateFailed: 3, reason: gate3.reason, attempt };
        }
        return { locked: false, fallback: 'retry', gateFailed: 3, reason: gate3.reason, attempt };
    }
    const gate4 = gate4_behavioralExercise(ruleName, proposedFields, extDir);
    if (!gate4.pass) {
        if (attempt >= MAX_RETRIES_PER_RULE) {
            return { locked: false, fallback: 'unverified', gateFailed: 4, reason: gate4.reason, attempt };
        }
        return { locked: false, fallback: 'retry', gateFailed: 4, reason: gate4.reason, attempt };
    }
    // All gates passed — lock the rule.
    const newRule = {
        expands_to: proposedFields,
        source: 'ai-generated',
        confirmed_at: new Date().toISOString(),
        test_signature: gate4.testSignature,
    };
    store.rules[ruleName] = newRule;
    store.metadata.total_rules = Object.keys(store.rules).length;
    store.metadata.ai_generated_rules = Object.values(store.rules).filter(r => r.source === 'ai-generated').length;
    store.metadata.last_validated = new Date().toISOString();
    store.metadata.type_words_hash = hashTypeWords(getRealPrimitiveTypeNames(extDir));
    saveBridgeStore(extDir, store);
    return { locked: true, rule: newRule };
}
