"use strict";
// toolSchema.js — JSON Schema structured output for "tool mode" Build
// generation (any provider other than koboldcpp — see ollama.js's
// autoGrammarMode()).
//
// Context: before this, "tool mode" was a label with nothing behind it.
// ollama.autoGrammarMode() returns 'tools' for every non-koboldcpp
// provider, but _runBuildChunk simply left `grammar` undefined and asked
// the model to write raw Dictum text from a system prompt alone — no
// structural constraint of any kind, despite the "Tools" name in the UI
// implying otherwise. This module is the actual mechanism: instead of
// trusting the model to produce syntactically valid Dictum text directly,
// the model is constrained (via the provider's `response_format:
// json_schema` feature — OpenAI-standard, and portable across compliant
// providers, not NVIDIA-specific) to emit a small JSON structure
// describing WHICH shapes/actions this chunk defines and their
// signatures; jsonChunkToDictum() then deterministically assembles that
// into the same kind of Dictum text _runBuildChunk already expects
// downstream (patchEngine, validator, emit_c/emit_cpp all keep working
// unchanged — this only changes how chunkText is obtained).
//
// DELIBERATE SCOPE LIMIT (stated plainly, same spirit as
// SOURCE_OF_TRUTH.md's own documented-limitations sections): this schema
// constrains STRUCTURE only — shape/action names, field/param names, and
// declared types as free-text type expressions. It does NOT attempt a
// full JSON mirror of parser.py's recursive statement/expression grammar
// — `body_dictum` is still free Dictum text written by the model. This
// targets exactly the failure mode actually observed in the field
// ("no shape definition found" / "no such action found" — signature-level
// drift), without requiring a full statement-level JSON grammar in one
// pass. That means this schema only makes sense for TYPE and
// OPERATION/MODIFY tier chunks (real shape/action definitions) — NOT
// ARCHITECTURE (a module-header line, not a shape/action) or MEMORY/
// SAFETY (annotations, not definitions). See _runBuildChunk in
// extension.js for where that tier check lives.
const { execFileSync } = require("child_process");
const path = require("path");
Object.defineProperty(exports, "__esModule", { value: true });
exports.CHUNK_JSON_SCHEMA_NAME = 'dictum_chunk';
exports.SCHEMA_APPLICABLE_TIERS = new Set(['TYPE', 'OPERATION', 'MODIFY']);
exports.buildChunkResponseFormat = buildChunkResponseFormat;
exports.jsonChunkToDictum = jsonChunkToDictum;
exports.isSchemaApplicableTier = isSchemaApplicableTier;
function isSchemaApplicableTier(tierName) {
    return exports.SCHEMA_APPLICABLE_TIERS.has(tierName);
}
/**
 * Queries type_registry.py directly for the real primitive type name
 * list (same subprocess convention bridge.js already uses for
 * getRealPrimitiveTypeNames — one implementation of "how do we ask
 * type_registry.py what it knows", not a second hand-copy of the list).
 * Read fresh each call, not cached at module load, so a type_registry.py
 * change is picked up without an extension host restart. Failure here is
 * non-fatal: the schema still works with a shorter, hand-written
 * fallback description — a build shouldn't fail outright because this
 * single informational subprocess call didn't run (e.g. python3 not on
 * PATH in some environment).
 */
function _getPrimitiveTypeNames(ext) {
    const compilerDir = path.join(ext, 'compiler');
    try {
        const out = execFileSync('python3', ['-c',
            'import sys, json; sys.path.insert(0, sys.argv[1]); ' +
            'from dictumc.type_registry import primitive_type_names; ' +
            'print(json.dumps(sorted(primitive_type_names())))',
            compilerDir], { encoding: 'utf8' });
        return JSON.parse(out);
    }
    catch (e) {
        return null;
    }
}
const _FIELD_TYPE_FALLBACK_HINT = "A Dictum type expression: a primitive type name (e.g. 'whole number', " +
    "'text', 'truth value', 'count'), or a compound form built from one (e.g. 'list of whole number', " +
    "'raw pointer to Player', 'handle to bytes', 'const ref Vec2').";
function _typeFieldDescription(ext) {
    const names = _getPrimitiveTypeNames(ext);
    if (!names || !names.length)
        return _FIELD_TYPE_FALLBACK_HINT;
    return `A Dictum type expression. Either one of these primitive type names verbatim: ` +
        `${names.map((n) => `'${n}'`).join(', ')} — or a compound form built from one of them, ` +
        `e.g. 'list of whole number', 'raw pointer to Player', 'handle to bytes', 'const ref Vec2'.`;
}
function _fieldSchema(typeDescription) {
    return {
        type: 'object',
        properties: {
            name: { type: 'string', description: 'Field or parameter name, exactly as it should appear in Dictum source.' },
            type: { type: 'string', description: typeDescription },
        },
        required: ['name', 'type'],
        additionalProperties: false,
    };
}
/**
 * Builds the OpenAI-standard `response_format: {type: "json_schema", ...}`
 * object for a single Build chunk. Caller (ollama.js's _generateOnce)
 * passes this through as-is in the request body — it doesn't need to
 * know anything about Dictum's shape.
 */
function buildChunkResponseFormat(ext) {
    const typeDescription = _typeFieldDescription(ext);
    const fieldSchema = _fieldSchema(typeDescription);
    const schema = {
        type: 'object',
        properties: {
            shapes: {
                type: 'array',
                description: 'Shape (struct-like type) declarations to emit for this chunk. Empty array if this chunk defines no shapes.',
                items: {
                    type: 'object',
                    properties: {
                        name: { type: 'string', description: 'Shape name, exactly matching the plan item.' },
                        fields: { type: 'array', items: fieldSchema },
                    },
                    required: ['name', 'fields'],
                    additionalProperties: false,
                },
            },
            actions: {
                type: 'array',
                description: 'Action (function) declarations to emit for this chunk. Empty array if this chunk defines no actions.',
                items: {
                    type: 'object',
                    properties: {
                        name: { type: 'string', description: 'Action name, exactly matching the plan item.' },
                        params: { type: 'array', items: fieldSchema, description: 'Empty array for a zero-argument action — do NOT invent a placeholder parameter.' },
                        produces: { type: 'string', description: `Return type. ${typeDescription} Use 'nothing' for no return value.` },
                        body_dictum: {
                            type: 'string',
                            description: 'The action BODY ONLY, as one or more raw Dictum statement lines, newline-separated. ' +
                                'Do NOT include the "action ... :" header line or the "end action" line — both are ' +
                                'generated automatically from the fields above.',
                        },
                    },
                    required: ['name', 'params', 'produces', 'body_dictum'],
                    additionalProperties: false,
                },
            },
        },
        required: ['shapes', 'actions'],
        additionalProperties: false,
    };
    return {
        type: 'json_schema',
        json_schema: {
            name: exports.CHUNK_JSON_SCHEMA_NAME,
            strict: true,
            schema,
        },
    };
}
function _emitField(f) {
    return `${f.name} as ${f.type}`;
}
function _emitShape(s) {
    const fieldLines = (s.fields || []).map((f) => `    ${_emitField(f)}`).join('\n');
    return `shape ${s.name} holds:\n${fieldLines}\nend shape`;
}
function _emitAction(a) {
    // Zero-argument actions drop the "takes ..." clause entirely per
    // LANGUAGE_REFERENCE.md ("action greet produces nothing:"), not
    // "takes nothing" — this is a real, verified syntax rule, not a
    // simplification, and getting it wrong here would make every
    // zero-param action fail to parse.
    const params = a.params || [];
    const header = params.length
        ? `action ${a.name} takes ${params.map(_emitField).join(' and ')} produces ${a.produces}:`
        : `action ${a.name} produces ${a.produces}:`;
    // Dictum's grammar doesn't treat indentation as significant (only as
    // convention — see LANGUAGE_REFERENCE.md), so body lines are
    // re-indented consistently regardless of whatever whitespace the
    // model itself produced, rather than trusting it verbatim.
    const bodyLines = (a.body_dictum || '')
        .split('\n')
        .map((l) => l.trim())
        .filter((l) => l.length > 0);
    const body = bodyLines.map((l) => `    ${l}`).join('\n');
    return `${header}\n${body}\nend action`;
}
/**
 * Deterministically assembles a parsed {shapes, actions} JSON object (the
 * model's structured-output response) into the same shape of Dictum text
 * chunk that _runBuildChunk previously got directly from unconstrained
 * model output — so everything downstream (patchEngine.applyChunk,
 * validator checks, emit_c/emit_cpp) needs no changes at all.
 */
function jsonChunkToDictum(parsed) {
    const shapes = (parsed.shapes || []).map(_emitShape);
    const actions = (parsed.actions || []).map(_emitAction);
    return [...shapes, ...actions].join('\n\n');
}
