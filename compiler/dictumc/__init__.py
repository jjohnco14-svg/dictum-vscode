"""
Dictum Compiler Package v4.1
"""

from .lexer import Lexer, Token, TokenType
from .parser import Parser
from .validator import Validator, ValidationError
from .emit_c import CEmitter
from .emit_cpp import CppEmitter
from .grammar import DictumGrammar, GrammarConstrainedGenerator, resync_from_source
from .transpiler import Transpiler, StdlibTranspiler
from .polyglot_parser import PolyglotParser
from .polyglot_transpiler import PolyglotTranspiler
from .polyglot_ast import (
    PolyglotModule, ExportDecl, PolyglotCall, PolyglotImport,
    UnsafeForeignCall, BuildDirective, ForeignShape, SerializedType,
    PolyglotInterface, ExportedSymbol, ExportedShape,
    SafetyLevel, POLYGLOT_BACKENDS, INTEROP_PATTERNS,
)
from .linker import InterfaceExtractor
from .linker.polyglot_linker import PolyglotLinker
from .linker.binding_generator import CppBindingGenerator
from .ast_nodes import *

__version__ = "0.1.39"
__all__ = [
    # Core pipeline
    "Lexer", "Token", "TokenType",
    "Parser", "PolyglotParser",
    "Validator", "ValidationError",
    "CEmitter", "CppEmitter",
    "DictumGrammar", "GrammarConstrainedGenerator", "resync_from_source",
    "Transpiler", "StdlibTranspiler", "PolyglotTranspiler",
    # Polyglot system
    "PolyglotModule", "ExportDecl", "PolyglotCall", "PolyglotImport",
    "UnsafeForeignCall", "BuildDirective", "ForeignShape", "SerializedType",
    "PolyglotInterface", "ExportedSymbol", "ExportedShape",
    "SafetyLevel", "POLYGLOT_BACKENDS", "INTEROP_PATTERNS",
    "InterfaceExtractor", "PolyglotLinker", "CppBindingGenerator",
]
