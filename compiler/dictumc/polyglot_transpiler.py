"""
Dictum Polyglot Transpiler — v1.1.0

Extends the base Transpiler with the full polyglot pipeline:
  1. Parse with PolyglotParser (handles @export, polyglot module, etc.)
  2. Run base validation
  3. Extract PolyglotInterface objects from AST
  4. Run CppBindingGenerator for each module
  5. Run PolyglotLinker to produce unified glue + build files
  6. Return everything in result dict

Usage:
    t = PolyglotTranspiler(source, backend='c', safety='safe')
    result = t.run()
    # result['code']           — main transpiled C/C++
    # result['polyglot_files'] — dict of generated binding files
    # result['interfaces']     — extracted PolyglotInterface objects
"""

from __future__ import annotations
import os
from typing import Dict, Any, List, Optional

from .transpiler import Transpiler, StdlibTranspiler
from .lexer import Lexer
from .validator import Validator, ValidationError
from .emit_c import CEmitter
from .emit_cpp import CppEmitter
from .grammar import DictumGrammar
from .polyglot_parser import PolyglotParser
from .polyglot_ast import SafetyLevel, PolyglotModule, PolyglotInterface
from .linker import InterfaceExtractor
from .linker.polyglot_linker import PolyglotLinker


class PolyglotTranspiler(Transpiler):
    """
    Full polyglot-aware transpiler.

    Extra init params vs base Transpiler:
      safety      — 'safe' | 'unsafe' | 'checked'  (default: 'safe')
      project_name — used in Makefile / CMakeLists project name
      output_dir   — where to write polyglot binding files
    """

    def __init__(self, source: str,
                 backend: str = 'c',
                 cpp_standard: int = 17,
                 namespace: str = '',
                 safety: str = SafetyLevel.SAFE,
                 project_name: str = 'dictum_project',
                 output_dir: str = 'build/polyglot'):
        super().__init__(source, backend, cpp_standard, namespace)
        self.safety = safety
        self.project_name = project_name
        self.output_dir = output_dir

    def run(self, validate: bool = True,
            summary: bool = False,
            namespace: str = '',
            grammar_guided: bool = False,
            link: bool = True,
            write_files: bool = False) -> Dict[str, Any]:
        """
        Run the full polyglot pipeline.

        Extra kwargs vs base Transpiler.run():
          link        — run the polyglot linker (default True)
          write_files — write generated files to output_dir (default False)
        """
        if namespace:
            self.namespace = namespace

        # 1. Lex
        lexer = Lexer(self.source)
        tokens = lexer.tokenize()

        # 2. Parse with polyglot-aware parser
        grammar: Optional[DictumGrammar] = None
        if grammar_guided:
            grammar = DictumGrammar(cpp_mode=(self.backend == 'cpp'))

        parser = PolyglotParser(tokens, grammar=grammar)
        ast = parser.parse()

        # 3. Validate
        if validate:
            validator = Validator(cpp_mode=(self.backend == 'cpp'))
            ok, errors, warnings = validator.validate(ast)
            if not ok:
                raise ValidationError('\n'.join(errors))
            self.validation_warnings = warnings
        else:
            self.validation_warnings = []

        # 4. Emit main code
        if self.backend == 'cpp':
            emitter = CppEmitter(cpp_standard=self.cpp_standard)
            emitter.namespace = self.namespace
        else:
            emitter = CEmitter()

        for node in ast:
            emitter.emit_node(node)
        code = emitter.get_output()

        result: Dict[str, Any] = {
            'code': code,
            'warnings': self.validation_warnings,
            'ast': ast,
        }

        if not link:
            return result

        # 5. Extract polyglot interfaces
        extractor = InterfaceExtractor()
        interfaces = extractor.extract(ast,
                                       default_backend=self.backend,
                                       default_safety=self.safety)
        result['interfaces'] = interfaces

        if not interfaces:
            result['polyglot_files'] = {}
            return result

        # 6. Run linker
        linker = PolyglotLinker(
            interfaces=interfaces,
            output_dir=self.output_dir,
            project_name=self.project_name,
        )
        polyglot_files = linker.link()
        result['polyglot_files'] = polyglot_files

        # 7. Optionally write to disk
        if write_files:
            linker.write(polyglot_files)
            result['output_dir'] = self.output_dir

        # 8. Header output
        if self._has_exports(ast):
            if self.backend == 'c':
                result['h_code'] = emitter.get_header_output(ast)
            elif self.backend == 'cpp':
                result['hpp_code'] = emitter.get_header_output(ast)

        return result
