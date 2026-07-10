"""
Dictum Transpiler v4.0 — orchestrates Lexer → Parser → Validator → Emitter.

Drop-in replacement for the Transpiler class in transpiler.py v3.3.

Key differences from v3.3:
  • Split into lexer.py / parser.py / validator.py / emit_c.py / emit_cpp.py / grammar.py
  • DictumGrammar is wired directly into Parser (no separate pre-pass)
  • All Phase 1-5 bug fixes applied across the pipeline
  • StdlibTranspiler and StdlibTranspilerV2 available for IoT/AI use cases
"""

from __future__ import annotations
from typing import List, Dict, Any, Optional
import os

from .lexer import Lexer, Token
from .parser import Parser
from .validator import Validator, ValidationError
from .emit_c import CEmitter
from .emit_cpp import CppEmitter
from .grammar import DictumGrammar, GrammarConstrainedGenerator
from .ast_nodes import Node, Shape, VarDecl, Action, Program, Module, ImportDict


class Transpiler:
    def __init__(self, source: str, backend: str = 'c',
                 cpp_standard: int = 17, namespace: str = "",
                 source_path: str = ""):
        self.source = source
        self.backend = backend
        self.cpp_standard = cpp_standard
        self.namespace = namespace
        self.source_path = source_path   # MISSING-08: base dir for resolving .dict imports
        self.validation_warnings: List[str] = []

    def _resolve_dict_imports(self, ast: List[Node], base_dir: str,
                              visited: set) -> Dict[str, Dict[str, Any]]:
        """MISSING-08: recursively transpile imported .dict modules.
        Returns {module_name: {c_code, h_code}} for each ImportDict found."""
        modules: Dict[str, Dict[str, Any]] = {}
        for node in ast:
            if not isinstance(node, ImportDict):
                continue
            # Resolve path relative to the importing file's directory
            raw_path = node.file_path
            if not os.path.isabs(raw_path):
                raw_path = os.path.join(base_dir, raw_path)
            raw_path = os.path.normpath(raw_path)

            if raw_path in visited:
                continue   # avoid circular imports
            visited.add(raw_path)

            if not os.path.exists(raw_path):
                raise FileNotFoundError(
                    f"Dictum import: file not found: {raw_path!r} "
                    f"(imported as {node.module_name!r})"
                )

            with open(raw_path, encoding='utf-8') as fh:
                mod_source = fh.read()

            mod_dir = os.path.dirname(raw_path)
            mod_t = Transpiler(mod_source, backend=self.backend,
                               cpp_standard=self.cpp_standard,
                               source_path=raw_path)
            mod_result = mod_t.run(validate=False)
            mod_ast = mod_result['ast']

            # Generate .h by emitting only exported symbols
            if self.backend == 'cpp':
                mod_emitter = CppEmitter(cpp_standard=self.cpp_standard)
            else:
                mod_emitter = CEmitter()

            stem = node.module_name.lower()
            guard = f"DICTUM_{node.module_name.upper()}_H"
            h_lines = [
                f"#ifndef {guard}",
                f"#define {guard}",
                '#include "dictum_core.h"',
                "",
            ]
            # Export all top-level actions and shapes from the module file
            for n in mod_ast:
                if isinstance(n, (Program, Module)):
                    inner = n.body
                else:
                    inner = [n]
                for inner_node in inner:
                    if isinstance(inner_node, Action):
                        params_str = ", ".join(
                            mod_emitter.type_to_c(p[1]) if hasattr(mod_emitter, 'type_to_c')
                            else p[1]
                            for p in inner_node.params
                        )
                        ret = mod_emitter.type_to_c(inner_node.ret_type) if hasattr(mod_emitter, 'type_to_c') else inner_node.ret_type
                        h_lines.append(f"extern {ret} {inner_node.name}({params_str});")
                    elif isinstance(inner_node, Shape):
                        h_lines.append(f"typedef struct {inner_node.name} {inner_node.name};")

            h_lines += ["", f"#endif  /* {guard} */", ""]

            # Recurse into the module's own imports
            nested = self._resolve_dict_imports(mod_ast, mod_dir, visited)
            modules.update(nested)
            modules[node.module_name] = {
                'c_code': mod_result['code'],
                'h_code': "\n".join(h_lines),
                'stem': stem,
                'source_path': raw_path,
            }

        return modules

    def run(self, validate: bool = True, summary: bool = False,
            namespace: str = "", grammar_guided: bool = False) -> Dict[str, Any]:
        if namespace:
            self.namespace = namespace

        # 1. Lex
        lexer = Lexer(self.source)
        tokens = lexer.tokenize()

        # 2. Optional grammar constraint — wired into parser
        grammar: Optional[DictumGrammar] = None
        if grammar_guided:
            cpp_mode = (self.backend == 'cpp')
            grammar = DictumGrammar(cpp_mode=cpp_mode, strict=True)

        # 3. Parse (grammar is passed in, so state machine advances with each token)
        parser = Parser(tokens, grammar=grammar)
        ast = parser.parse()

        # 4. Validate
        if validate:
            validator = Validator(cpp_mode=(self.backend == 'cpp'))
            ok, errors, warnings = validator.validate(ast)
            if not ok:
                raise ValidationError("\n".join(errors))
            self.validation_warnings = warnings
        else:
            self.validation_warnings = []

        # 5. Emit
        if self.backend == 'cpp':
            emitter = CppEmitter(cpp_standard=self.cpp_standard)
            emitter.namespace = self.namespace
        else:
            emitter = CEmitter()

        if hasattr(emitter, 'local_modules'):
            emitter.local_modules = {n.name for n in ast if isinstance(n, Module)}
        # BUGFIX (missing dictum_error.h for a top-level action's `produce
        # failure`): emit_node only ever sees one top-level node at a time,
        # so it can't tell whether SOME OTHER top-level node uses `produce
        # failure`/`attempt`. Scan the whole file once, up front, and let
        # the emitter's include-gating consult that instead of only its
        # own subtree.
        if hasattr(emitter, '_has_produce_failure'):
            emitter._file_has_produce_failure = any(emitter._has_produce_failure(n) for n in ast)
        if hasattr(emitter, '_has_attempt_nodes'):
            emitter._file_has_attempt_nodes = any(emitter._has_attempt_nodes(n) for n in ast)

        for node in ast:
            emitter.emit_node(node)
        code = emitter.get_output()

        result: Dict[str, Any] = {
            "ast": ast,
            "code": code,
            "warnings": self.validation_warnings,
            "makefile": emitter.get_makefile() if hasattr(emitter, 'get_makefile') else None,
            "ldflags": emitter.get_ldflags() if hasattr(emitter, 'get_ldflags') else ["-lm"],
        }

        # MISSING-08: resolve .dict module imports
        base_dir = os.path.dirname(os.path.abspath(self.source_path)) if self.source_path else os.getcwd()
        dict_modules = self._resolve_dict_imports(ast, base_dir, set())
        if dict_modules:
            result['dict_modules'] = dict_modules   # {ModuleName: {c_code, h_code, stem}}

        if summary:
            from .summarizer import Summarizer
            summarizer = Summarizer()
            result["summary"] = "\n".join(summarizer.summarize(n) for n in ast)

        # Header output for exported symbols
        if self._has_exports(ast):
            if self.backend == 'c':
                result["h_code"] = emitter.get_header_output(ast)
            elif self.backend == 'cpp':
                result["hpp_code"] = emitter.get_header_output(ast)

        return result

    @staticmethod
    def _has_exports(nodes: List[Node]) -> bool:
        for n in nodes:
            if isinstance(n, (Shape, VarDecl, Action)) and getattr(n, 'export', False):
                return True
            if isinstance(n, (Program, Module)) and Transpiler._has_exports(n.body):
                return True
        return False


# ---------------------------------------------------------------------------
# Stdlib-aware transpiler (Phase 7 equivalent, minimal deps)
# ---------------------------------------------------------------------------

class StdlibTranspiler(Transpiler):
    """
    Transpiler that extends the validator and emitters with stdlib type/action
    registrations from STDLIB_ACTION_FAMILIES, enabling IoT/AI stdlib calls
    without explicit `import from C` declarations.
    """

    def run(self, validate: bool = True, summary: bool = False,
            namespace: str = "", grammar_guided: bool = False) -> Dict[str, Any]:
        from .stdlib_registry import (
            DICTUM_STDLIB_TYPES, STDLIB_ACTION_FAMILIES,
            extend_validator, extend_emitter, detect_stdlib_includes,
            auto_inject_stdlib_imports,
        )

        if namespace:
            self.namespace = namespace

        lexer = Lexer(self.source)
        tokens = lexer.tokenize()
        grammar = DictumGrammar(cpp_mode=(self.backend == 'cpp'), strict=True) if grammar_guided else None
        parser = Parser(tokens, grammar=grammar)
        ast = parser.parse()
        ast = auto_inject_stdlib_imports(ast)
        stdlib_headers, needs_robotics = detect_stdlib_includes(ast)

        if validate:
            validator = Validator(cpp_mode=(self.backend == 'cpp'))
            extend_validator(validator)
            ok, errors, warnings = validator.validate(ast)
            if not ok:
                raise ValidationError("\n".join(errors))
            self.validation_warnings = warnings
        else:
            self.validation_warnings = []

        if self.backend == 'cpp':
            emitter = CppEmitter(cpp_standard=self.cpp_standard)
            emitter.namespace = self.namespace
        else:
            emitter = CEmitter()
        extend_emitter(emitter)

        if hasattr(emitter, 'local_modules'):
            emitter.local_modules = {n.name for n in ast if isinstance(n, Module)}
        if hasattr(emitter, '_has_produce_failure'):
            emitter._file_has_produce_failure = any(emitter._has_produce_failure(n) for n in ast)
        if hasattr(emitter, '_has_attempt_nodes'):
            emitter._file_has_attempt_nodes = any(emitter._has_attempt_nodes(n) for n in ast)

        for node in ast:
            emitter.emit_node(node)
        code = emitter.get_output()

        # Inject stdlib includes
        if stdlib_headers:
            lines = code.split("\n")
            last_inc = max((i for i, ln in enumerate(lines)
                            if ln.strip().startswith('#include')), default=-1)
            inserts = ["", "/* ── Dictum stdlib ── */"]
            for h in sorted(stdlib_headers):
                inserts.append(f'#include "{h}"')
            if needs_robotics:
                inserts.append('#include "dictum_robotics.h"')
            if last_inc >= 0:
                lines = lines[:last_inc + 1] + inserts + lines[last_inc + 1:]
            code = "\n".join(lines)

        result: Dict[str, Any] = {
            "ast": ast,
            "code": code,
            "warnings": self.validation_warnings,
            "stdlib_headers": sorted(stdlib_headers),
            "needs_robotics": needs_robotics,
            "makefile": emitter.get_makefile() if hasattr(emitter, 'get_makefile') else None,
            "ldflags": emitter.get_ldflags() if hasattr(emitter, 'get_ldflags') else ["-lm"],
        }

        if summary:
            from .summarizer import Summarizer
            summarizer = Summarizer()
            result["summary"] = "\n".join(summarizer.summarize(n) for n in ast)

        if self._has_exports(ast):
            if self.backend == 'c':
                result["h_code"] = emitter.get_header_output(ast)
            elif self.backend == 'cpp':
                result["hpp_code"] = emitter.get_header_output(ast)

        return result
