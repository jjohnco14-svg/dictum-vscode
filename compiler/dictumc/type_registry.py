"""
type_registry.py -- single source of truth for Dictum's primitive type
vocabulary.

WHY THIS EXISTS
----------------
Before this module, the same type vocabulary was independently duplicated
across five files with no mechanism keeping them in sync:
  - parser.py       _TERMINAL_TYPES, _PRIMITIVE_SUFFIXES
  - emit_c.py        self.types  (Dictum name -> C type)
  - emit_cpp.py       self.types  (Dictum name -> C++ type)
  - validator.py     PRIMITIVE_TYPES, NUMERIC_TYPES
  - grammar.py       TYPE_WORDS

This was a confirmed, live source of bugs, not a theoretical risk:
  - `u8`/`u64` were parseable and emittable but missing from the
    validator's own type sets -- any program using them failed validation.
  - `f32` had to be added by hand in all five places to work end-to-end.
  - `bytes` (plural) was accepted by the parser AND listed in grammar.py's
    TYPE_WORDS, but had no entry in the validator's PRIMITIVE_TYPES and no
    C/C++ mapping in either emitter at all -- `keep x as bytes` parsed
    successfully and then failed validation with "Unknown type 'bytes'",
    a contradiction between two files that should never have been able to
    disagree.

Every one of those was the same root cause: five independent lists, no
shared definition. This module is that shared definition. Adding a new
primitive type now means adding ONE entry here; every consumer picks it up
automatically.

WHAT THIS DOES NOT COVER
-------------------------
Recursive/compound type FORMS (`list of <T>`, `unique handle to <T>`,
`raw pointer to <T>`, `const ref <T>`, `handle to bytes`) are parsed by
dedicated control-flow branches in parser.py's parse_type(), not simple
table lookups -- they genuinely need to recurse into an inner type, which
a flat word table can't express. This module only centralizes the leaf
vocabulary (the primitives) and the building-block WORDS those compound
forms consume (exported via WRAPPER_WORDS, so grammar.py's GBNF-facing
TYPE_WORDS can still include them without parser.py's actual parsing logic
needing to change at all).
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple


@dataclass(frozen=True)
class PrimitiveType:
    name: str                      # canonical Dictum name, e.g. "whole number"
    words: Tuple[str, ...]         # word tokens that spell it, e.g. ("whole", "number")
    c_type: str
    cpp_type: Optional[str] = None  # None -> same as c_type
    numeric: bool = False
    terminal: bool = False          # True if usable as a single bare word
                                     # (-> parser._TERMINAL_TYPES)
    var_valid: bool = True          # False if only valid as a return type
                                     # (e.g. `nothing` -- void isn't a
                                     # value a variable can hold)

    @property
    def cpp(self) -> str:
        return self.cpp_type if self.cpp_type is not None else self.c_type


# The canonical list. This is the ONE place to add/change a primitive type.
PRIMITIVES: List[PrimitiveType] = [
    PrimitiveType("whole number",       ("whole", "number"),      "int32_t",      numeric=True),
    PrimitiveType("count",              ("count",),               "size_t",       numeric=True, terminal=True),
    PrimitiveType("fractional number",  ("fractional", "number"), "double",       numeric=True),
    PrimitiveType("decimal number",     ("decimal", "number"),    "double",       numeric=True),
    PrimitiveType("decimal",            ("decimal",),             "double",       numeric=True, terminal=True),
    PrimitiveType("truth value",        ("truth", "value"),       "bool"),
    PrimitiveType("bool",               ("bool",),                "bool",         terminal=True),
    PrimitiveType("byte",               ("byte",),                "uint8_t",      numeric=True, terminal=True),
    # FIX: `bytes` (plural) was parseable (parser._TERMINAL_TYPES) and
    # listed in grammar.py's TYPE_WORDS, but had no validator entry and no
    # C/C++ mapping anywhere -- "Unknown type 'bytes'" on every use despite
    # looking, from the parser's side, exactly as valid as `byte`. Given a
    # real, distinct meaning here: a raw byte pointer (distinct from the
    # opaque `handle to bytes` below).
    PrimitiveType("bytes",              ("bytes",),               "uint8_t*",     terminal=True),
    PrimitiveType("text",               ("text",),                "dictum_text",  cpp_type="const char*", terminal=True),
    PrimitiveType("handle to bytes",    ("handle", "to", "bytes"), "void*"),
    PrimitiveType("opaque pointer",     ("opaque", "pointer"),    "void*"),
    PrimitiveType("nothing",            ("nothing",),             "void",         terminal=True, var_valid=False),
    PrimitiveType("u8",  ("u8",),  "uint8_t",  numeric=True, terminal=True),
    PrimitiveType("u16", ("u16",), "uint16_t", numeric=True, terminal=True),
    PrimitiveType("u32", ("u32",), "uint32_t", numeric=True, terminal=True),
    PrimitiveType("u64", ("u64",), "uint64_t", numeric=True, terminal=True),
    PrimitiveType("i32", ("i32",), "int32_t",  numeric=True, terminal=True),
    PrimitiveType("i64", ("i64",), "int64_t",  numeric=True, terminal=True),
    PrimitiveType("f32", ("f32",), "float",    numeric=True, terminal=True),
    PrimitiveType("result", ("result",), "void*", terminal=True),
]

BY_NAME: Dict[str, PrimitiveType] = {p.name: p for p in PRIMITIVES}

# Building-block words consumed by RECURSIVE type forms that parser.py
# hand-parses (list of <T>, unique/shared/weak/raw handle/pointer to <T>,
# const ref <T>, ref <T>, move <T>). These aren't primitives themselves --
# they're grammatical scaffolding -- but grammar.py's GBNF-facing
# TYPE_WORDS needs them too, so they're exported alongside the primitives
# rather than living as yet another independent hand-typed list.
WRAPPER_WORDS: Set[str] = {
    'list', 'array', 'of',
    'unique', 'shared', 'weak', 'raw', 'handle', 'pointer', 'to',
    'const', 'ref', 'move',
}


def all_type_words() -> Set[str]:
    """Every individual word token across every primitive, plus the
    recursive-form building-block words. For grammar.py's TYPE_WORDS /
    keeping the static .gbnf files' type-word rule in sync."""
    words: Set[str] = set()
    for p in PRIMITIVES:
        words.update(p.words)
    words.update(WRAPPER_WORDS)
    return words


def terminal_type_words() -> Set[str]:
    """Single bare-word complete types. For parser._TERMINAL_TYPES."""
    return {p.name for p in PRIMITIVES if p.terminal}


def primitive_suffixes() -> Dict[str, List[str]]:
    """Multi-word compound types expressible as a simple
    {first_word: [remaining_words]} lookup (`whole` -> `number`, `opaque`
    -> `pointer`, etc). For parser._PRIMITIVE_SUFFIXES. Excludes compounds
    with hand-coded control flow elsewhere (`handle to bytes` has its own
    branch in parse_type() because `handle` alone is also a distinct,
    valid nominal-handle-reference form -- it can't be a flat suffix
    lookup without breaking that other meaning)."""
    out: Dict[str, List[str]] = {}
    for p in PRIMITIVES:
        if len(p.words) > 1 and not p.terminal and p.words[0] != 'handle':
            out.setdefault(p.words[0], []).extend(p.words[1:])
    return out


def c_type_map() -> Dict[str, str]:
    """Dictum name -> C type. For emit_c.py's self.types."""
    return {p.name: p.c_type for p in PRIMITIVES}


def cpp_type_map() -> Dict[str, str]:
    """Dictum name -> C++ type. For emit_cpp.py's self.types."""
    return {p.name: p.cpp for p in PRIMITIVES}


def primitive_type_names() -> Set[str]:
    """All valid canonical primitive type names. For validator.py's
    PRIMITIVE_TYPES."""
    return set(BY_NAME.keys())


def numeric_type_names() -> Set[str]:
    """Primitive type names valid in arithmetic/comparison contexts. For
    validator.py's NUMERIC_TYPES."""
    return {p.name for p in PRIMITIVES if p.numeric}


def non_variable_type_names() -> Set[str]:
    """Primitive type names that are valid types in general (e.g. as an
    action return type) but not valid as a `keep <name> as <type>`
    variable declaration's type. For validator.py's VarDecl check."""
    return {p.name for p in PRIMITIVES if not p.var_valid}
