"""
Dictum Polyglot AST Nodes — v1.1.0
Extends ast_nodes.py with cross-language module declarations,
@export annotations, backend hints, and FFI type constraints.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple


# ---------------------------------------------------------------------------
# Supported polyglot backends
# ---------------------------------------------------------------------------

POLYGLOT_BACKENDS = frozenset({
    'c', 'cpp',
    'python', 'go', 'rust', 'javascript', 'wasm',
})

# Communication patterns between modules
INTEROP_PATTERNS = frozenset({
    'ffi',          # In-process C ABI shared library
    'grpc',         # gRPC / Protobuf
    'http',         # HTTP/REST + JSON
    'msgqueue',     # Redis / NATS async
    'wasm',         # WebAssembly
})

# ---------------------------------------------------------------------------
# Safety levels — critical for C/C++ FFI
# ---------------------------------------------------------------------------

class SafetyLevel:
    SAFE   = 'safe'    # All bounds-checked, no raw pointers across boundary
    UNSAFE = 'unsafe'  # Raw C pointers, manual memory, no checks
    CHECKED= 'checked' # Unsafe C ABI but with runtime assertions injected


# ---------------------------------------------------------------------------
# Polyglot module declaration
# ---------------------------------------------------------------------------

@dataclass
class PolyglotModule:
    """
    `polyglot module <name> uses <backend>`
    Declares a module that will be compiled to a specific backend language.
    """
    name: str = ''
    backend: str = 'c'              # one of POLYGLOT_BACKENDS
    safety: str = SafetyLevel.SAFE  # safe | unsafe | checked
    interop: str = 'ffi'            # one of INTEROP_PATTERNS
    body: List[Any] = field(default_factory=list)
    export: bool = False
    line: int = 0


@dataclass
class ExportDecl:
    """
    `@export` annotation on an action/shape — marks it for polyglot linking.
    Applied to the next declaration in the AST.
    """
    name: str = ''
    c_name: str = ''          # override C symbol name if different
    safety: str = SafetyLevel.SAFE
    calling_conv: str = 'cdecl'   # cdecl | stdcall | fastcall
    thread_safe: bool = False
    line: int = 0


@dataclass
class PolyglotCall:
    """
    Cross-module function call: `call <module>.<function> with <args> giving <result>`
    where <module> is a polyglot module compiled to a different backend.
    """
    module: str = ''
    function: str = ''
    args: List[Any] = field(default_factory=list)
    result_name: str = ''
    safety: str = SafetyLevel.SAFE
    line: int = 0


@dataclass
class PolyglotImport:
    """
    `polyglot import <module>` — import a compiled polyglot module by name.
    Generates binding glue for the current module's backend.
    """
    module_name: str = ''
    alias: str = ''
    pattern: str = 'ffi'
    line: int = 0


@dataclass
class SerializedType:
    """
    Marks a shape for cross-language serialisation (JSON/MsgPack/Protobuf).
    `@serializable` annotation or `shape ... is serializable holds ...`
    """
    shape_name: str = ''
    format: str = 'json'   # json | msgpack | protobuf
    line: int = 0


@dataclass
class ForeignShape:
    """
    A shape whose layout is defined by a foreign language's struct.
    Used when Dictum calls into a pre-compiled C library.
    `shape <Name> from foreign C holds ...`
    """
    name: str = ''
    source_language: str = 'c'
    fields: List[Tuple[str, str]] = field(default_factory=list)
    packed: bool = False
    line: int = 0


@dataclass
class UnsafeForeignCall:
    """
    Direct unsafe foreign call bypassing the linker's type checking.
    `unsafe call foreign "<symbol>" with <args> giving <result>`
    """
    symbol: str = ''
    args: List[Any] = field(default_factory=list)
    result_name: str = ''
    result_type: str = ''
    line: int = 0


@dataclass
class BuildDirective:
    """
    Compiler/linker directive embedded in Dictum source.
    `#[link "libcurl"]`, `#[cflags "-O3"]`, `#[ldflags "-lpthread"]`
    """
    kind: str = ''     # 'link' | 'cflags' | 'ldflags' | 'include_path'
    value: str = ''
    line: int = 0


# ---------------------------------------------------------------------------
# Interface definition — the public contract of a polyglot module
# ---------------------------------------------------------------------------

@dataclass
class PolyglotInterface:
    """
    Computed from all @export declarations in a module.
    Passed to the linker to generate binding code.
    """
    module_name: str = ''
    backend: str = 'c'
    safety: str = SafetyLevel.SAFE
    interop: str = 'ffi'
    exports: List['ExportedSymbol'] = field(default_factory=list)
    shapes: List['ExportedShape'] = field(default_factory=list)
    build_directives: List[BuildDirective] = field(default_factory=list)


@dataclass
class ExportedSymbol:
    """A single exported function in the polyglot interface."""
    name: str = ''
    c_name: str = ''
    params: List[Tuple[str, str]] = field(default_factory=list)   # (name, dictum_type)
    ret_type: str = ''
    safety: str = SafetyLevel.SAFE
    calling_conv: str = 'cdecl'
    thread_safe: bool = False


@dataclass
class ExportedShape:
    """A struct/shape exported across language boundaries."""
    name: str = ''
    fields: List[Tuple[str, str]] = field(default_factory=list)
    packed: bool = False
    serializable: bool = False
    serialization_format: str = 'json'
