"""
dictumc.summarizer — produces a one-line-per-top-level-declaration summary
of a parsed Dictum AST. Used by the --summary CLI flag and the `summary=True`
option on Transpiler.run().

This module did not previously exist, despite being imported (inside an
`if summary:` branch) by transpiler.py in two places. Any use of the
documented --summary flag crashed with ModuleNotFoundError before this fix —
a real, previously-undiscovered gap, found while investigating an unrelated
import-shimming task.
"""
from __future__ import annotations
from typing import List

from .ast_nodes import (
    Node, Program, Module, Shape, Action, VarDecl, Method,
    Constructor, Destructor, ImportC, ImportCpp, ImportDict, Use,
)


class Summarizer:
    """Produces a compact, human-readable summary of top-level AST nodes."""

    def summarize(self, node: Node) -> str:
        if isinstance(node, Program):
            return self._summarize_container("program", node)
        if isinstance(node, Module):
            return self._summarize_container("module", node)
        if isinstance(node, Shape):
            return self._summarize_shape(node)
        if isinstance(node, Action):
            return self._summarize_action(node)
        if isinstance(node, VarDecl):
            return f"  global {node.name}: {node.type}"
        if isinstance(node, (ImportC, ImportCpp)):
            alias = getattr(node, "alias", None) or getattr(node, "action_name", "")
            return f"  import (C/C++): {alias}"
        if isinstance(node, ImportDict):
            return f"  import (dict module): {getattr(node, 'module_name', '?')}"
        if isinstance(node, Use):
            return f"  use: {getattr(node, 'name', '?')}"
        # Fall back to the node's class name rather than silently dropping
        # an unrecognized top-level node from the summary.
        return f"  {type(node).__name__}"

    def _summarize_container(self, kind: str, node) -> str:
        lines = [f"{kind} {node.name}:"]
        for child in node.body:
            lines.append(self._summarize_member(child))
        return "\n".join(lines)

    def _summarize_member(self, node: Node) -> str:
        # Covers the declarations that matter most for a structural overview
        # (shapes, actions, top-level variables). Statement-level nodes
        # inside a program/module body (Assignment, FuncCall, If, While,
        # etc.) fall through to the generic class-name line below — this is
        # a structural summary of what's declared, not a full statement
        # trace of what each program does line by line.
        if isinstance(node, Shape):
            return self._summarize_shape(node, indent="  ")
        if isinstance(node, Action):
            return self._summarize_action(node, indent="  ")
        if isinstance(node, VarDecl):
            return f"  keep {node.name} as {node.type}"
        return f"  {type(node).__name__}"

    def _summarize_shape(self, node: Shape, indent: str = "") -> str:
        field_list = ", ".join(f"{n}:{t}" for n, t in node.fields)
        parts = [f"{indent}shape {node.name} ({field_list})"]
        for m in node.methods:
            parts.append(self._summarize_action(m, indent=indent + "  "))
        if node.constructors:
            parts.append(f"{indent}  + {len(node.constructors)} constructor(s)")
        if node.destructor:
            parts.append(f"{indent}  + destructor")
        return "\n".join(parts)

    def _summarize_action(self, node, indent: str = "") -> str:
        params = ", ".join(f"{n}:{t}" for n, t in node.params)
        ret = getattr(node, "ret_type", "") or "nothing"
        name = getattr(node, "name", "<anonymous>")
        return f"{indent}action {name}({params}) -> {ret}"
