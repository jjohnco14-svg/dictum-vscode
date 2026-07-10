"use strict";
// skills.js -- Part 3A (skills as curated library bundles).
//
// A "skill" here is deliberately NOT a compiler change (see
// SOURCE_OF_TRUTH.md's Skills section for why -- Option A vs Option B).
// It's a bundle of three things, all in user-space Dictum/markdown, that
// together specialize Dictum for a domain without touching the language:
//   1. skills/SKILL_PLAN_<name>.md   -- Plan-tier domain guidance (optional)
//   2. skills/SKILL_BUILD_<name>.md  -- Build-tier domain guidance (optional)
//   3. skills/library/<name>/*.dict  -- real Dictum shapes/import_c bindings,
//      auto-prepended to every build so the model never has to (re)write
//      them and can't get them wrong.
Object.defineProperty(exports, "__esModule", { value: true });
exports.listSkills = listSkills;
exports.loadSkill = loadSkill;
exports.getSkillBindingsChunk = getSkillBindingsChunk;

const fs = require("fs");
const path = require("path");

function listSkills(extDir) {
    const libDir = path.join(extDir, 'skills', 'library');
    if (!fs.existsSync(libDir)) return ['general'];
    const names = fs.readdirSync(libDir, { withFileTypes: true })
        .filter(e => e.isDirectory())
        .map(e => e.name);
    return ['general', ...names];
}

/**
 * Loads everything known about a named skill. `name: 'general'` (the
 * default / no-op skill) always resolves to an empty bundle -- there is
 * nothing to layer on top of the base skill files.
 */
function loadSkill(extDir, name) {
    if (!name || name === 'general') {
        return { name: 'general', planAddendum: null, buildAddendum: null, bindingsFiles: [] };
    }
    const planPath = path.join(extDir, 'skills', `SKILL_PLAN_${name}.md`);
    const buildPath = path.join(extDir, 'skills', `SKILL_BUILD_${name}.md`);
    const libDir = path.join(extDir, 'skills', 'library', name);

    const planAddendum = fs.existsSync(planPath) ? fs.readFileSync(planPath, 'utf8') : null;
    const buildAddendum = fs.existsSync(buildPath) ? fs.readFileSync(buildPath, 'utf8') : null;

    let bindingsFiles = [];
    if (fs.existsSync(libDir)) {
        bindingsFiles = fs.readdirSync(libDir)
            .filter(f => f.endsWith('.dict'))
            .map(f => path.join(libDir, f))
            .sort();
    }
    return { name, planAddendum, buildAddendum, bindingsFiles };
}

/**
 * Returns the skill's bindings content ready to use as the FIRST chunk of
 * a build (before any Plan-derived chunk), or null if the skill has none.
 * Concatenates all .dict files found in the skill's library directory --
 * a skill can ship more than one bindings file (e.g. a core one plus an
 * optional extras one) and they all land together as one prepended block.
 */
function getSkillBindingsChunk(extDir, name) {
    const skill = loadSkill(extDir, name);
    if (skill.bindingsFiles.length === 0) return null;
    const parts = skill.bindingsFiles.map(f => fs.readFileSync(f, 'utf8'));
    return {
        tierName: 'SKILL_BINDINGS',
        label: `${name} skill bindings (${skill.bindingsFiles.length} file(s), auto-included)`,
        source: parts.join('\n\n'),
    };
}
