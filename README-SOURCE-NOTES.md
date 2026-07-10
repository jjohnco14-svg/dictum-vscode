# What this is

This is `dictum-0_1_45-bugfixes.vsix` unzipped — a VS Code extension package is
just a zip file. This folder is what was **shipped**, not necessarily what's in
your working dev repo.

## What's real, editable source in here
- `compiler/**/*.py` — full, untouched Python source (parser, validator, emit_c,
  emit_cpp, grammar, type_registry, linker). This is genuinely your compiler
  source, not a build artifact.
- `out/*.js` — NOT minified. Plain `tsc` output, comments intact, readable and
  directly editable. Functionally complete but has no TypeScript types anymore.

## What's missing (by design, via .vscodeignore — this is normal)
- `src/*.ts` — the original TypeScript that compiles to `out/*.js`. Not shipped.
- `tsconfig.json` — not shipped.
- Your test suite: `foundation_test.py`, `architecture_test.py`, `gbnf_check.py`,
  `extensions_test.py`, `e2e_test.js`, `simulate_vibecoding.js`,
  `mock_llm_server.js`. None of these are in the vsix. Only `compiler/run_selftest.py`
  shipped.

## Why this matters for your fix-loop problem
If you (or Roo, in a past session) have been editing files in one tree while
`verify_pipeline.sh` / vsce packaging runs from a different tree, a real fix
in tree A will never show up as fixed when checked against tree B. Before
doing anything else:

1. Find your actual dev repo (the one with `src/`, `tsconfig.json`, and the
   full test suite).
2. Diff it against this unpacked folder:
   `diff -rq /path/to/real-repo/out extracted/extension/out`
   and
   `diff -rq /path/to/real-repo/compiler extracted/extension/compiler`
3. If they differ, your dev repo is the source of truth going forward — point
   Roo Code at THAT directory, not this one. Rebuild and re-package from there
   only.
4. If your dev repo is missing/lost and this unpacked copy is all you have,
   treat `out/*.js` as your real source from now on: edit the `.js` directly,
   drop the `tsc` compile step (delete/ignore the `compile`/`watch` scripts),
   and run `vsce package` straight from this folder. You lose TS type-checking
   but nothing else — the code already runs exactly as-is.

## The fix-loop discipline (this is the actual time-saver)
Every "is it fixed" question gets answered by exactly one thing:
`./verify_pipeline.sh` printing `ALL GREEN` at the end, tail pasted as proof.
Not a diff read-through, not "this looks right now." One bug = one Roo Code
task, closed only when the gate script proves it — not when the conversation
feels done. This is what turns a 5-hour session into a bounded, checkable
15-30 minute cycle per bug.
