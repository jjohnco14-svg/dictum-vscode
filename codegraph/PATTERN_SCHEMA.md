# Codegraph Pattern Schema

One JSON file per pattern in `codegraph/patterns/<pattern_ref>.json`. This
is deliberately the same "filesystem as database, one file per record"
convention this repo already uses elsewhere (`skills/library/<name>/*.dict`,
`graphify-out/cache/ast/*.json`) rather than introducing a new storage
dependency -- no sqlite/chromadb/vector-db needed for what is, at this
scale, a lookup table keyed by a short string.

## Why this exists

Cell 9/10/11 established that GBNF grammar constraints alone cannot stop
a model from filling an open slot with a habit from Python/C/JavaScript
training data (`countdown` instead of `Count`, `<name>` interpolation,
wrong unsafe-token param order, ...). The grammar can restrict *shape*;
it can't teach the model Dictum's *content*. A pattern database gives the
Build phase a way to show the model one or more already-correct,
already-verified Dictum examples for the specific construct it's about
to generate -- few-shot context, not a training run, $0 cost -- so the
model has actually "seen" Dictum before it's asked to write more of it.

This file defines the STORAGE + LOOKUP + BINDING mechanism. As of
2026-07-13, `codegraph/patterns/` is populated with 10 real patterns
(hello-world, import-c, shape-declaration, shape-actions, while-loop,
pointer-ops, unsafe-malloc, atomic-increment, importc-math, importc-
raylib), each cross-validated against `validated_patterns.json` -- 200
real transpiler-run examples (parse->emit->compile->[link->run],
100% pass rate) supplied separately from this mechanism's initial
build. Every pattern's `example` field is a byte-exact real validated
entry (not hand-written), and every pattern's `template`+`params` was
verified to exactly reproduce 20/20 real variations for 7 of the 10
patterns; the other 3 (`while-loop`, `shape-actions`, `importc-raylib`)
have a documented, deliberate coverage limit where body/structural
content varies beyond what the recorded params capture -- see each
pattern's own `common_mistakes`/`requires` fields, and
`pattern_graph_test.py`'s Layer 5 for the automated cross-check.

## File contract

```json
{
  "pattern_ref": "while-loop",
  "category": "OPERATION",
  "description": "An action that declares a counter variable and loops while a threshold condition holds, printing and/or decrementing it each iteration.",
  "params": {
    "action_name": {
      "type": "identifier",
      "required": true,
      "desc": "the action's name"
    },
    "var": {
      "type": "identifier",
      "required": true,
      "desc": "loop variable name, reused identically in keep/while/print/set -- never invent or rename it"
    },
    "init_value": {
      "type": "number",
      "required": true,
      "desc": "the starting value bound in the keep declaration"
    },
    "threshold": {
      "type": "number",
      "required": true,
      "desc": "the comparison value"
    }
  },
  "template": "action {action_name} produces nothing\n    keep {var} as whole number with value {init_value}\n    while {var} is greater than {threshold} repeat\n        print the text {var} and newline\n        set {var} to {var} minus 1\n    end while\nend action",
  "requires": [
    "{var} must be declared via this pattern's own `keep` line -- it does not assume {var} already exists from an earlier chunk."
  ],
  "common_mistakes": [
    "Do not rename or lowercase {var} anywhere in the body.",
    "The closing keyword is `end while`, not `end repeat`."
  ],
  "example": "action countdown produces nothing\n    keep Count as whole number with value 5\n    while Count is greater than 0 repeat\n        print the text Count and newline\n        set Count to Count minus 1\n    end while\nend action"
}
```

### Field-by-field

| Field              | Required | Notes |
|---------------------|----------|-------|
| `pattern_ref`        | yes | Must exactly match the filename (without `.json`). Validated by the loader -- a mismatch is a hard error, not a warning, so a copy/rename mistake can't silently serve the wrong pattern under the right key. |
| `category`           | yes | One of the existing tier names (`ARCHITECTURE`, `TYPE`, `OPERATION`, `MEMORY`, `SAFETY`, `INVARIANT`, `MODIFY`) so a future Plan-phase lookup can filter patterns by the chunk's own tier. Not currently enforced against `type_registry.py`'s tier list by the loader (kept a plain string on purpose -- see "Deliberately NOT done yet" below), but should match one of them in practice. |
| `description`        | yes | One sentence, human- and model-facing. Shown verbatim in the rendered context block. |
| `params`              | yes | Object, may be `{}` for a pattern with no fillable slots. Each value needs `type` and `required`; `desc` is recommended, `options`/`default` only apply to `type: "enum"`. |
| `template`            | yes | The canonical snippet with `{param}` placeholders. Every placeholder used here must have a matching entry in `params` (validated by the loader) -- and vice versa, every *required* param should appear in the template at least once (validated too, since an unused required param is almost certainly a copy/paste mistake). |
| `requires`            | no  | Short prose list of preconditions the pattern itself doesn't enforce (e.g. "the variable must already be declared"). Shown in the rendered context block so the model sees the constraint, not just the shape. |
| `common_mistakes`     | no  | Short, pattern-level list -- a handful of specific known-wrong outputs to avoid, not an exhaustive taxonomy. Mirrors the existing house style in `chunk_grammar.py`'s own comments: name the failure mode, don't over-explain it. |
| `example`             | yes | One fully-bound, concrete instance of the pattern -- no placeholders left in it. This is the actual few-shot demonstration; `template`+`params` exist so a specific instance can also be *bound* on demand (see below), not just shown as a generic example. |

### Param `type` values

- `identifier` -- a Dictum name (variable, shape, action). No syntax
  validation is done by the binder itself (that's the grammar's job) --
  this is a documentation/intent field for now.
- `number` / `integer` -- numeric literal.
- `enum` -- one of a fixed `options` list; `bind_pattern`/`bindPattern`
  reject any value not in that list.
- `text` -- a quoted string's inner content (caller supplies the raw
  text; quoting is the template's job, e.g. `"{text}"`).
- `raw` -- pre-formatted Dictum source dropped in verbatim (e.g. an
  already-indented loop body assembled from other statements). No
  validation beyond "is a string".
- `field_list` -- a list of `[name, dictum_type]` pairs (e.g.
  `[["A", "whole number"], ["B", "raw pointer to whole number"]]`),
  rendered by `bind_pattern` as one indented `    name as dictum_type`
  line per entry, joined with newlines. The one structured (non-scalar)
  param type -- added specifically for shape field lists, which are
  genuinely repeated structure, not a single substitutable value. See
  `shape-declaration.json` / `shape-actions.json`.

### Deliberately NOT done yet (out of scope for "the mechanism")

- No GBNF-fragment field wired into `chunk_grammar.py`'s generation
  logic -- this schema only feeds Build-prompt few-shot context today.
  A `gbnf_fragment` field could be added later without a breaking
  change (loaders ignore unknown keys already), but exact-shape grammar
  generation from a pattern is a separate, larger integration.
- No embedding/similarity search -- lookup is by exact `pattern_ref`
  key only (a Plan-phase `pattern_ref: "while-loop"` reference), not
  free-text retrieval. Matches how `[SKILL: name]` / `[FILE: name]`
  plan directives already work elsewhere in this codebase -- explicit
  reference, not fuzzy matching.
- No enforcement that `category` matches a real tier name, and no
  cross-pattern uniqueness/conflict checking beyond the filename ==
  pattern_ref check. Both are cheap to add once there's a real
  multi-pattern set to validate against (the self-test below already
  has a place for it: `pattern_graph_test.py`).
