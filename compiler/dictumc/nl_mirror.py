#!/usr/bin/env python3
"""
nl_mirror.py -- Phase 4 (NL-appreciation direction, part 4 of 4): a
plain-English paraphrase of what the just-generated Dictum actually
does, meant to be shown to the person before Review runs.

WHY THIS EXISTS
----------------
Phases 1-3 (role-scoped grammar, the general sequential NL expander,
synonym-tolerant keywords) are all about making CORRECT generation
cheaper for the MODEL. This is the one piece of the original ask that's
about the human side: "user facing allows to understand of the
intent". Right now the only way to check what Build actually produced
against what you meant is to read Dictum -- a DSL that's deliberately
NOT human-facing (see SOURCE_OF_TRUTH.md's core design stance). This
module closes that gap by walking the same parsed AST Review already
checks and rendering it as English sentences instead, so confirming
"does this match what I asked for" doesn't require reading the DSL at
all.

DISTINCT FROM summarizer.py (existing, `--summary` CLI flag): that
module produces a compact, Dictum-syntax-shaped structural OUTLINE
("action foo(x:count) -> nothing") -- useful for a quick structural
diff, but still something you have to already read Dictum to parse.
This module produces full English sentences describing BEHAVIOR
("Foo takes a count called x and returns nothing. It declares Result
as a whole number, starting at 0; then checks whether x is greater
than 0..."), meant for someone who has never seen Dictum syntax.

CONTRACT: never raises on a node shape it doesn't specifically handle
-- falls back to a generic, still-readable placeholder rather than
crashing the Build->Review handoff over a cosmetic gap (the same
"never worse than before" contract the rest of this pipeline holds
itself to). Never invents behavior that isn't in the AST -- every
sentence traces back to one real node.
"""
from __future__ import annotations

from .ast_nodes import (
    Node, Program, Module, Shape, Action, VarDecl, Assignment, FuncCall,
    Return, If, While, ForEach, Repeat, Break, Attempt, Literal, Identifier,
    BinaryOp, UnaryOp, FieldAccess, IndexAccess, Assert, Print, UnsafeBlock,
    Use, ImportC, ImportCpp, ImportDict,
)

_ARTICLE_VOWELS = set("aeiouAEIOU")


def _a(word):
    word = word or "value"
    return f"an {word}" if word[0] in _ARTICLE_VOWELS else f"a {word}"


_BIN_OP_WORDS = {
    "+": "plus", "-": "minus", "*": "times", "/": "divided by", "%": "modulo",
    "==": "equal to", "!=": "not equal to", ">": "greater than", "<": "less than",
    ">=": "greater than or equal to", "<=": "less than or equal to",
    "and": "and", "or": "or",
}


class NLMirror:
    """Walks a parsed Dictum AST (list[Node] from Parser.parse(), or a
    single top-level Node) and produces an English paraphrase."""

    def mirror(self, nodes):
        if isinstance(nodes, Node):
            nodes = [nodes]
        paras = [self._top(n) for n in nodes]
        return "\n\n".join(p for p in paras if p)

    # ---- top-level declarations ----
    def _top(self, node):
        if isinstance(node, Program):
            return self._container("program", node.name, node.body)
        if isinstance(node, Module):
            return self._container("module", node.name, node.body)
        if isinstance(node, Shape):
            return self._shape(node)
        if isinstance(node, Action):
            return self._action(node)
        if isinstance(node, (ImportC, ImportCpp)):
            alias = getattr(node, "alias", None) or getattr(node, "action_name", "")
            return f"Imports an external C/C++ function, available here as '{alias}'."
        if isinstance(node, ImportDict):
            return f"Imports the '{getattr(node, 'module_name', '?')}' module from another Dictum file."
        if isinstance(node, Use):
            return f"Brings in '{getattr(node, 'path', '?')}' for use in this file."
        return f"(A {type(node).__name__} declaration -- not yet described in plain English.)"

    def _container(self, kind, name, body):
        lines = [f"This {kind}, {name}, does the following:"]
        for child in body:
            lines.append("- " + self._member(child))
        return "\n".join(lines)

    def _member(self, node):
        if isinstance(node, Shape):
            return self._shape(node, inline=True)
        if isinstance(node, Action):
            return self._action(node, inline=True)
        if isinstance(node, VarDecl):
            return self._var_decl_sentence(node)
        return self._stmt(node)

    def _shape(self, node: Shape, inline=False):
        field_list = self._join_and([f"{n} ({t})" for n, t in node.fields])
        head = f"Declares a shape called {node.name}"
        head += f", holding {field_list}." if field_list else " with no fields."
        parts = [head]
        for m in node.methods:
            parts.append("It has a method: " + self._action(m, inline=True))
        if node.constructors:
            parts.append(f"It has {len(node.constructors)} constructor(s).")
        if node.destructor:
            parts.append("It has a destructor.")
        return " ".join(parts) if inline else "\n  ".join(parts)

    def _action(self, node, inline=False):
        params = self._join_and([f"{n} ({t})" for n, t in node.params]) if node.params else None
        ret = getattr(node, "ret_type", "") or "nothing"
        head = f"Defines an action called {node.name}"
        head += f" that takes {params}" if params else " that takes no parameters"
        head += f" and returns {ret}." if ret != "nothing" else " and returns nothing."
        body_sentences = [s for s in (self._stmt(s) for s in node.body) if s]
        if body_sentences:
            head += " It " + self._join_semicolon(body_sentences) + "."
        return head

    # ---- statements ----
    def _stmt(self, node):
        if isinstance(node, VarDecl):
            return self._var_decl_clause(node)
        if isinstance(node, Assignment):
            return f"sets {node.target} to {self._expr(node.value)}"
        if isinstance(node, FuncCall):
            return self._call_phrase(node)
        if isinstance(node, Return):
            return f"returns {self._expr(node.value)}"
        if isinstance(node, Print):
            parts = self._join_and([self._expr(p) for p in node.parts])
            return f"prints {parts}"
        if isinstance(node, If):
            cond = self._expr(node.cond)
            then_s = self._join_semicolon([s for s in (self._stmt(s) for s in node.then_body) if s])
            out = f"checks whether {cond}; if so, it {then_s}" if then_s else f"checks whether {cond}"
            if node.else_body:
                else_s = self._join_semicolon([s for s in (self._stmt(s) for s in node.else_body) if s])
                if else_s:
                    out += f", otherwise it {else_s}"
            return out
        if isinstance(node, While):
            body_s = self._join_semicolon([s for s in (self._stmt(s) for s in node.body) if s])
            cond = self._expr(node.cond)
            return (f"repeats while {cond}, and each time it {body_s}"
                    if body_s else f"repeats while {cond}")
        if isinstance(node, ForEach):
            body_s = self._join_semicolon([s for s in (self._stmt(s) for s in node.body) if s])
            head = f"goes through each {node.item} in {node.collection}"
            return f"{head}, and for each one it {body_s}" if body_s else head
        if isinstance(node, Repeat):
            body_s = self._join_semicolon([s for s in (self._stmt(s) for s in node.body) if s])
            count = self._expr(node.count)
            counter = f", counting with {node.counter}," if node.counter else ""
            head = f"repeats {count} times{counter}"
            return f"{head} and each time it {body_s}" if body_s else head
        if isinstance(node, Break):
            return "stops the loop early"
        if isinstance(node, Attempt):
            call_s = self._call_phrase(node.call, verb="call") if node.call else "perform an operation"
            succ = self._join_semicolon([s for s in (self._stmt(s) for s in node.success_body) if s]) or None
            fail = self._join_semicolon([s for s in (self._stmt(s) for s in node.failure_body) if s]) or None
            out = f"attempts to {call_s}"
            if succ:
                out += f"; on success it {succ}"
            if fail:
                out += f"; on failure it {fail}"
            return out
        if isinstance(node, Assert):
            return f"asserts that {self._expr(node.cond)}"
        if isinstance(node, UnsafeBlock):
            body_s = self._join_semicolon([s for s in (self._stmt(s) for s in node.body) if s])
            return (f"performs unsafe, low-level operations: {body_s}"
                    if body_s else "performs an unsafe, low-level block")
        return f"does something else ({type(node).__name__}, not yet described in plain English)"

    def _call_phrase(self, node: FuncCall, verb="calls"):
        args = self._join_and([self._expr(a) for a in node.args]) if node.args else None
        return f"{verb} {node.name}" + (f" with {args}" if args else "")

    def _var_decl_sentence(self, node: VarDecl):
        return "Declares " + self._var_decl_clause(node) + "."

    def _var_decl_clause(self, node: VarDecl):
        base = f"declares {node.name} as {_a(node.type)}"
        if node.value is not None:
            base += f", starting at {self._expr(node.value)}"
        return base

    # ---- expressions ----
    def _expr(self, node):
        if node is None:
            return "nothing"
        if isinstance(node, Literal):
            v = node.value
            if isinstance(v, str):
                return f'"{v}"'
            if isinstance(v, bool):
                return "true" if v else "false"
            return str(v)
        if isinstance(node, Identifier):
            return node.name
        if isinstance(node, BinaryOp):
            op = _BIN_OP_WORDS.get(node.op, node.op)
            return f"{self._expr(node.left)} {op} {self._expr(node.right)}"
        if isinstance(node, UnaryOp):
            if node.op == "&":
                return f"the address of {self._expr(node.operand)}"
            if node.op == "*":
                return f"the value pointed to by {self._expr(node.operand)}"
            return f"{node.op}{self._expr(node.operand)}"
        if isinstance(node, FieldAccess):
            return f"{node.obj}'s {node.field}"
        if isinstance(node, IndexAccess):
            return f"{node.collection}[{self._expr(node.index)}]"
        if isinstance(node, FuncCall):
            args = self._join_and([self._expr(a) for a in node.args]) if node.args else None
            return f"the result of calling {node.name}" + (f" with {args}" if args else "")
        return str(node)

    # ---- joins ----
    @staticmethod
    def _join_and(items):
        items = [i for i in items if i]
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return ", ".join(items[:-1]) + f", and {items[-1]}"

    @staticmethod
    def _join_semicolon(items):
        items = [i for i in items if i]
        return "; then ".join(items)


def mirror(nodes) -> str:
    return NLMirror().mirror(nodes)


def _bridge_main():
    import json
    import sys as _sys
    from .lexer import Lexer
    from .parser import Parser
    try:
        payload = json.load(_sys.stdin)
        code = payload.get("code", "")
        tokens = Lexer(code).tokenize()
        nodes = Parser(tokens).parse()
        json.dump({"ok": True, "narration": mirror(nodes)}, _sys.stdout)
    except Exception as e:
        json.dump({"ok": False, "error": str(e)}, _sys.stdout)


def _self_test():
    from .lexer import Lexer
    from .parser import Parser
    sample = '''
shape Request holds
    http_method as text
    path as text
end shape

action main produces nothing
    keep Count as whole number with value 5
    print the text Count
    while Count is greater than 0 repeat
        set Count to Count minus 1
    end while
    call helper with Count giving Result
    if Result is equal to 0 then
        print the text "done"
    end if
end action
'''
    tokens = Lexer(sample).tokenize()
    nodes = Parser(tokens).parse()
    out = mirror(nodes)
    assert out, "empty narration"
    print(out)
    print("\nnl_mirror self-test OK")


if __name__ == "__main__":
    import sys as _sys
    if "--bridge" in _sys.argv:
        _bridge_main()
    elif "--self-test" in _sys.argv:
        _self_test()
    else:
        _sys.stderr.write("usage: nl_mirror.py --bridge < payload.json  |  nl_mirror.py --self-test\n")
        _sys.exit(1)
