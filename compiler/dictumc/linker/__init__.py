"""
Dictum Polyglot Interface Extractor — v1.1.0

Walks the AST after parsing and collects:
  - All PolyglotModule nodes → PolyglotInterface
  - All @export-decorated Action/Shape nodes
  - All BuildDirective nodes
  - All @serializable shapes

The extracted interfaces are the input to the Polyglot Linker.
"""

from __future__ import annotations
from typing import List, Dict, Optional, Tuple

from ..ast_nodes import Node, Action, Shape, Module, Program, VarDecl
from ..polyglot_ast import (
    PolyglotModule, ExportDecl, BuildDirective, ForeignShape, SerializedType,
    PolyglotInterface, ExportedSymbol, ExportedShape, SafetyLevel,
)


class InterfaceExtractor:
    """
    Extracts polyglot interfaces from a parsed AST.

    Returns a dict of module_name → PolyglotInterface.
    The 'default' key holds exports from the top-level program, if any.
    """

    def __init__(self):
        self.interfaces: Dict[str, PolyglotInterface] = {}
        self._current_module: Optional[str] = None
        self._current_backend: str = 'c'
        self._current_safety: str = SafetyLevel.SAFE
        self._current_interop: str = 'ffi'

    def extract(self, ast: List[Node],
                default_backend: str = 'c',
                default_safety: str = SafetyLevel.SAFE) -> Dict[str, PolyglotInterface]:
        self._current_backend = default_backend
        self._current_safety = default_safety

        # Create a default interface for top-level exports
        self.interfaces['default'] = PolyglotInterface(
            module_name='default',
            backend=default_backend,
            safety=default_safety,
            interop='ffi',
        )

        for node in ast:
            self._walk(node)

        # Remove empty default
        if not self.interfaces['default'].exports and not self.interfaces['default'].shapes:
            del self.interfaces['default']

        return self.interfaces

    # ------------------------------------------------------------------
    def _walk(self, node: Node) -> None:
        if isinstance(node, PolyglotModule):
            self._extract_module(node)
        elif isinstance(node, (Program, Module)):
            for child in node.body:
                self._walk(child)
        elif isinstance(node, BuildDirective):
            self._add_build_directive(node)
        elif isinstance(node, Action):
            self._check_export_action(node)
        elif isinstance(node, Shape):
            self._check_export_shape(node)
        elif isinstance(node, ForeignShape):
            self._register_foreign_shape(node)

    def _extract_module(self, node: PolyglotModule) -> None:
        iface = PolyglotInterface(
            module_name=node.name,
            backend=node.backend,
            safety=node.safety,
            interop=node.interop,
        )
        self.interfaces[node.name] = iface

        old_mod = self._current_module
        old_back = self._current_backend
        old_safe = self._current_safety
        old_inter = self._current_interop

        self._current_module = node.name
        self._current_backend = node.backend
        self._current_safety = node.safety
        self._current_interop = node.interop

        for child in node.body:
            self._walk(child)

        self._current_module = old_mod
        self._current_backend = old_back
        self._current_safety = old_safe
        self._current_interop = old_inter

    def _add_build_directive(self, node: BuildDirective) -> None:
        key = self._current_module or 'default'
        if key in self.interfaces:
            self.interfaces[key].build_directives.append(node)

    def _check_export_action(self, node: Action) -> None:
        exp: Optional[ExportDecl] = getattr(node, '_polyglot_export', None)
        if exp is None and not getattr(node, 'export', False):
            return
        key = self._current_module or 'default'
        iface = self.interfaces.get(key)
        if iface is None:
            return

        safety = exp.safety if exp else self._current_safety
        c_name = (exp.c_name if exp and exp.c_name else None) or node.name
        calling_conv = exp.calling_conv if exp else 'cdecl'
        thread_safe = exp.thread_safe if exp else False

        sym = ExportedSymbol(
            name=node.name,
            c_name=c_name,
            params=node.params,
            ret_type=node.ret_type,
            safety=safety,
            calling_conv=calling_conv,
            thread_safe=thread_safe,
        )
        iface.exports.append(sym)

    def _check_export_shape(self, node: Shape) -> None:
        exp: Optional[ExportDecl] = getattr(node, '_polyglot_export', None)
        serializable_fmt = getattr(node, '_serializable', None)
        if exp is None and not getattr(node, 'export', False) and serializable_fmt is None:
            return
        key = self._current_module or 'default'
        iface = self.interfaces.get(key)
        if iface is None:
            return

        es = ExportedShape(
            name=node.name,
            fields=node.fields,
            packed=node.is_packed,
            serializable=serializable_fmt is not None,
            serialization_format=serializable_fmt or 'json',
        )
        iface.shapes.append(es)

    def _register_foreign_shape(self, node: ForeignShape) -> None:
        # Foreign shapes are noted but don't need to be re-exported
        pass
