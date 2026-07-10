"""
Dictum Validator — semantic analysis / type checking / ownership tracking.
Extracted from transpiler.py v3.3.

Fixes applied:
  MISSING-09: 'truth value' and 'bool' consistently mapped; 'decimal number'
              and 'decimal' now accepted as aliases for 'fractional number'.
  BUG-01:     Validator now auto-declares undeclared assignment targets via
              infer_type() rather than emitting an error (mirrors emitter
              behaviour where a declaration is auto-generated).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Set

from .ast_nodes import (
    Node, Program, Module, Shape, Method, Constructor, Destructor,
    VarDecl, Assignment, Action, FuncCall, Return, If, While, ForEach,
    Repeat, Attempt, Literal, Identifier, BinaryOp, UnaryOp,
    FieldAccess, IndexAccess, Assert, Print, ImportC, ImportCpp,
    UnsafeBlock, UnsafeToken, VerifyToken, ExternFn, Transmute, Use, Bind, NewExpr, LambdaExpr,
    Possibilities, HandleTypeDecl, Break,
)


class ValidationError(Exception):
    pass


@dataclass
class VarInfo:
    name: str
    type: str
    initialized: bool = False
    is_handle: bool = False
    is_array: bool = False
    array_size: Optional[int] = None
    released: bool = False
    line: int = 0


@dataclass
class ActionSig:
    name: str
    params: List[Tuple[str, str]]
    ret_type: str
    line: int = 0
    template_params: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class ShapeDef:
    name: str
    fields: Dict[str, str]
    line: int = 0


class Scope:
    def __init__(self, parent: Optional['Scope'] = None):
        self.parent = parent
        self.vars: Dict[str, VarInfo] = {}
        self.children: List['Scope'] = []
        if parent:
            parent.children.append(self)

    def declare(self, info: VarInfo) -> None:
        if info.name in self.vars:
            raise ValidationError(
                f"Variable '{info.name}' already declared in this scope (line {info.line})"
            )
        self.vars[info.name] = info

    def resolve(self, name: str) -> Optional[VarInfo]:
        if name in self.vars:
            return self.vars[name]
        if self.parent:
            return self.parent.resolve(name)
        return None

    def all_vars(self) -> Dict[str, VarInfo]:
        result = {}
        if self.parent:
            result.update(self.parent.all_vars())
        result.update(self.vars)
        return result


class Validator:
    # SOURCE OF TRUTH: derived from type_registry.py -- see that module's
    # docstring. Was previously an independent hand-typed set; the drift
    # this caused is exactly what surfaced as real bugs (u8/u64 missing
    # here despite being parseable/emittable; `bytes` parseable but not
    # listed here at all, so "Unknown type 'bytes'" on every use).
    from .type_registry import primitive_type_names, numeric_type_names
    PRIMITIVE_TYPES: Set[str] = primitive_type_names()

    CPP_ONLY_PREFIXES = {
        'unique handle to ':  "Smart pointers require --backend cpp",
        'shared handle to ':  "Smart pointers require --backend cpp",
        'weak handle to ':    "Smart pointers require --backend cpp",
        'raw handle to ':     "Smart pointers require --backend cpp",
        'const ref ':         "References require --backend cpp",
        'ref ':               "References require --backend cpp",
        'move ':              "Move semantics require --backend cpp",
    }

    NUMERIC_TYPES: Set[str] = numeric_type_names()

    def __init__(self, cpp_mode: bool = False):
        self.shapes: Dict[str, ShapeDef] = {}
        self.actions: Dict[str, ActionSig] = {}
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.in_unsafe = False
        self.cpp_mode = cpp_mode
        self.classes: Dict[str, Shape] = {}
        self.current_class: Optional[str] = None
        self.needed_headers: Set[str] = set()
        # Nominal handle types declared via `define handle Name`.
        # Two different handle types are never interchangeable even
        # though both are opaque pointers under the hood — passing a
        # Stmt where a Db is expected is a compile-time error.
        self.handle_types: Set[str] = set()
        # Tracks nesting depth inside while/for/repeat bodies, so a
        # `stop repeating` (Break) outside any loop can be flagged as an
        # error instead of silently emitting a dangling `break;`.
        self.loop_depth: int = 0

    def error(self, msg: str, line: int = 0) -> None:
        self.errors.append(f"[Line {line}] {msg}" if line else msg)

    def warning(self, msg: str, line: int = 0) -> None:
        self.warnings.append(f"[Line {line}] {msg}" if line else msg)

    def is_valid_type(self, t: str) -> bool:
        base = t
        if '.' in base:
            base = base.split('.')[-1]
        # BUG-C FIX: strip collection wrappers before checking the element type.
        # The parser accepts both `list of <T>` and `<T> list` forms; the validator
        # must recognise both so it doesn't reject valid array declarations.
        for prefix in ('list of ', 'array of '):
            if base.startswith(prefix):
                return self.is_valid_type(base[len(prefix):].strip())
        for suffix in (' list', ' array'):
            if base.endswith(suffix):
                return self.is_valid_type(base[:-len(suffix)].strip())
        # pointer prefix
        if base.startswith('*'):
            return self.is_valid_type(base[1:].strip())
        if base.startswith('raw pointer to '):
            return self.is_valid_type(base[len('raw pointer to '):].strip())
        if base in self.PRIMITIVE_TYPES or base in self.shapes or base in self.handle_types:
            return True
        if not self.cpp_mode:
            for prefix in self.CPP_ONLY_PREFIXES:
                if t.startswith(prefix):
                    return self.is_valid_type(t[len(prefix):].strip())
            if t.startswith('action taking '):
                return False
            return False
        for prefix in ['unique handle to ', 'shared handle to ', 'weak handle to ', 'raw handle to ']:
            if t.startswith(prefix):
                return self.is_valid_type(t[len(prefix):].strip())
        for prefix in ['const ref ', 'ref ', 'move ']:
            if t.startswith(prefix):
                return self.is_valid_type(t[len(prefix):].strip())
        if t.startswith('action taking ') or t.startswith('*'):
            return True
        return False

    # ------------------------------------------------------------------
    def collect_globals(self, nodes: List[Node]) -> None:
        for node in nodes:
            if isinstance(node, Shape):
                fields = {fname: ftype for fname, ftype in node.fields}
                self.shapes[node.name] = ShapeDef(name=node.name, fields=fields, line=node.line)
                if self.cpp_mode and (node.methods or node.constructors or node.destructor or node.parent):
                    self.classes[node.name] = node
            elif isinstance(node, Action):
                self.actions[node.name] = ActionSig(
                    name=node.name, params=node.params, ret_type=node.ret_type,
                    line=node.line, template_params=node.template_params)
            elif isinstance(node, ExternFn):
                self.actions[node.name] = ActionSig(
                    name=node.name, params=node.params, ret_type=node.ret_type, line=node.line)
            elif isinstance(node, HandleTypeDecl):
                self.handle_types.add(node.name)
            elif isinstance(node, Module):
                self.collect_globals(node.body)
                for inner in node.body:
                    if isinstance(inner, Action):
                        self.actions[f"{node.name}.{inner.name}"] = ActionSig(
                            name=inner.name, params=inner.params, ret_type=inner.ret_type,
                            line=inner.line, template_params=inner.template_params)
            elif isinstance(node, Program):
                self.collect_globals(node.body)

    def validate(self, nodes: List[Node]) -> Tuple[bool, List[str], List[str]]:
        self.errors.clear(); self.warnings.clear()
        self.collect_globals(nodes)
        for node in nodes:
            self.validate_top_level(node)
        return (len(self.errors) == 0, self.errors, self.warnings)

    def validate_top_level(self, node: Node) -> None:
        if isinstance(node, Program):
            scope = Scope()
            for stmt in node.body:
                if isinstance(stmt, VarDecl):
                    self.declare_var(stmt, scope)
                elif isinstance(stmt, Action):
                    pass
                elif isinstance(stmt, Shape):
                    pass
            for stmt in node.body:
                if isinstance(stmt, (VarDecl, Action, Shape, Module, ImportC, ExternFn, UnsafeBlock, ImportCpp, HandleTypeDecl)):
                    self.validate_node(stmt, scope)
                else:
                    self.validate_statement(stmt, scope)
            self.check_scope_ownership(scope, "program exit")
        elif isinstance(node, Module):
            scope = Scope()
            for stmt in node.body:
                if isinstance(stmt, VarDecl):
                    self.declare_var(stmt, scope)
            for stmt in node.body:
                self.validate_node(stmt, scope)
            self.check_scope_ownership(scope, f"module '{node.name}' exit")
        elif isinstance(node, (Action, Shape, ImportC, ExternFn, ImportCpp)):
            scope = Scope()
            self.validate_node(node, scope)

    def validate_node(self, node: Node, scope: Scope) -> None:
        if isinstance(node, VarDecl):           self.validate_vardecl(node, scope)
        elif isinstance(node, Assignment):      self.validate_assignment(node, scope)
        elif isinstance(node, Action):          self.validate_action(node, scope)
        elif isinstance(node, If):              self.validate_if(node, scope)
        elif isinstance(node, While):           self.validate_while(node, scope)
        elif isinstance(node, ForEach):         self.validate_foreach(node, scope)
        elif isinstance(node, Repeat):          self.validate_repeat(node, scope)
        elif isinstance(node, Attempt):         self.validate_attempt(node, scope)
        elif isinstance(node, Return):          self.validate_return(node, scope)
        elif isinstance(node, Assert):          self.validate_assert(node, scope)
        elif isinstance(node, Print):           self.validate_print(node, scope)
        elif isinstance(node, FuncCall):        self.validate_funccall(node, scope)
        elif isinstance(node, Shape):           self.validate_shape(node, scope)
        elif isinstance(node, ImportC):         self.validate_import(node, scope)
        elif isinstance(node, ImportCpp):       self.validate_import_cpp(node, scope)
        elif isinstance(node, ExternFn):        pass
        elif isinstance(node, HandleTypeDecl):  pass  # already registered in collect_globals
        elif isinstance(node, Break):
            if self.loop_depth <= 0:
                self.error("'stop repeating' used outside of any loop", node.line)
        elif isinstance(node, UnsafeBlock):     self.validate_unsafe(node, scope)
        elif isinstance(node, UnsafeToken):     self.validate_unsafe_token(node, scope)
        elif isinstance(node, VerifyToken):     pass  # L2 — emitted as C comment, no type-check needed
        elif isinstance(node, (Program, Module)): self.validate_top_level(node)
        elif isinstance(node, Method):          self.validate_method(node, scope)
        elif isinstance(node, Constructor):     self.validate_constructor(node, scope)
        elif isinstance(node, Destructor):      self.validate_destructor(node, scope)
        elif isinstance(node, NewExpr):
            if not self.cpp_mode:
                self.error("'new' expressions require --backend cpp", node.line)
            else:
                self.validate_new_expr(node, scope)
        elif isinstance(node, LambdaExpr):
            if not self.cpp_mode:
                self.error("Lambda expressions require --backend cpp", node.line)
            else:
                self.validate_lambda(node, scope)
        elif isinstance(node, Use):
            # Purely declarative — `use ModuleName` / `use system Name` has
            # no fields to type-check; it's consumed by the emitter for
            # #include generation. Confirmed via ast_nodes.py: only `path`
            # and `is_system`, no sub-expressions. This was already a
            # legitimate no-op before the stricter else-clause below was
            # added — without this explicit case, the new else clause would
            # have incorrectly flagged every real `use` statement as an
            # error (confirmed: this broke 4 golden tests before this case
            # was added).
            pass
        elif isinstance(node, Bind):
            # `bind` declares an FFI binding signature (name/params/ret_type/
            # alias) — purely declarative, consumed by the emitter, no
            # sub-expressions to validate. Same situation as Use above.
            pass
        elif isinstance(node, Possibilities):
            # No validation logic exists for this node anywhere in this
            # file as of this fix — kept as an explicit no-op (matching its
            # prior silent behavior) rather than risk breaking something
            # that may rely on it, but unlike Use/Bind this one has NOT been
            # confirmed to be purely declarative. Flagging honestly: this
            # may need real validation logic that doesn't exist yet.
            pass
        elif isinstance(node, Transmute):
            # NOT confirmed to be a safe no-op — Transmute wraps a sub-
            # expression (`expr`) and a target `type` for reinterpretation,
            # which arguably needs the same kind of type-checking other
            # expression-bearing nodes get. Kept as a no-op here only to
            # avoid regressing existing behavior while fixing the unrelated
            # silent-unrecognized-keyword bug — this is a real, separate gap,
            # not a verified-safe case like Use/Bind above.
            pass
        elif isinstance(node, Identifier):
            # A bare identifier reference used as a standalone statement has
            # no effect — it reads a variable and discards the value. This
            # is almost always a typo'd or unrecognized keyword: the parser's
            # statement dispatch (parse_statement) falls through to
            # parse_expression for any word it doesn't recognize as a
            # keyword, so e.g. `display "hello"` silently parses as a bare
            # Identifier("display") statement followed by a separate bare
            # Literal("hello") statement — neither of which the previous
            # validator dispatch had a case for, so both were silently
            # accepted and produced zero emitted code, no error, no warning.
            self.error(
                f"'{node.name}' is not a recognized statement keyword and has no effect "
                f"used on its own here. If you meant to call an action, use "
                f"'call {node.name} with ...' or check for a typo in a keyword "
                f"(e.g. 'print the text ... and newline', not '{node.name} ...').",
                node.line,
            )
        elif isinstance(node, Literal):
            # A bare literal as a standalone statement (e.g. the orphaned
            # string left over after a misparsed `display "..."` line) has
            # no effect and almost always indicates a statement that failed
            # to parse as intended.
            self.error(
                f"a bare value ({node.value!r}) cannot be used as a statement on its own "
                f"— it has no effect. Check the preceding line for a missing or "
                f"unrecognized statement keyword.",
                node.line,
            )
        else:
            # Anything reaching here is a node type with no explicit case in
            # this dispatch and no effect when silently skipped — flag it
            # rather than continue the previous behavior of doing nothing.
            # This is deliberately a hard error, not a warning: a node that
            # falls through to here either represents new AST surface this
            # validator hasn't been taught about yet (a real gap that should
            # be visible immediately) or a statement that produces no code,
            # which is never the user's intent.
            self.error(
                f"statement of type '{type(node).__name__}' is not recognized "
                f"by the validator and would produce no emitted code.",
                getattr(node, 'line', 0),
            )

    def validate_statement(self, node: Node, scope: Scope) -> None:
        self.validate_node(node, scope)

    # ------------------------------------------------------------------
    # Variable declaration & assignment
    # ------------------------------------------------------------------
    def declare_var(self, node: VarDecl, scope: Scope) -> None:
        is_handle = node.type == "handle to bytes"
        is_smart = self.cpp_mode and any(
            node.type.startswith(p) for p in
            ['unique handle to ', 'shared handle to ', 'weak handle to ', 'raw handle to '])
        is_array = isinstance(node.value, Literal) and isinstance(node.value.value, list)
        array_size = len(node.value.value) if is_array else None
        if isinstance(node.value, UnaryOp) and node.value.op == "room_for":
            is_handle = True
        initialized = (node.value is not None) or isinstance(node.value, NewExpr)
        info = VarInfo(name=node.name, type=node.type, initialized=initialized,
                       is_handle=is_handle or is_smart, is_array=is_array,
                       array_size=array_size, line=node.line)
        scope.declare(info)
        if is_array:
            scope.declare(VarInfo(name=f"{node.name}_count", type="count",
                                  initialized=True, line=node.line))
            scope.declare(VarInfo(name=f"{node.name}_size", type="count",
                                  initialized=True, line=node.line))

    def validate_vardecl(self, node: VarDecl, scope: Scope) -> None:
        from .type_registry import non_variable_type_names
        if node.type in non_variable_type_names():
            self.error(
                f"'{node.type}' is not a valid variable type (it's a return-type-only "
                f"marker, e.g. `action ... produces nothing`) -- '{node.name}' can't be "
                f"declared as one", node.line)
            return
        if not self.is_valid_type(node.type):
            if not self.cpp_mode:
                for prefix, msg in self.CPP_ONLY_PREFIXES.items():
                    if node.type.startswith(prefix):
                        self.error(f"{msg}. Type is valid but wrapper requires C++ backend.", node.line)
                        return
            self.error(f"Unknown type '{node.type}'", node.line)
            return
        if node.value:
            self.check_expression(node.value, scope)
            if node.type in self.handle_types:
                value_type = self.infer_type(node.value, scope)
                if value_type in self.handle_types and value_type != node.type:
                    self.error(
                        f"Type mismatch: '{node.name}' declared as handle '{node.type}' "
                        f"but initialized with a handle '{value_type}' value",
                        node.line)
        if scope.resolve(node.name) is None or scope.vars.get(node.name) is None:
            self.declare_var(node, scope)

    def validate_assignment(self, node: Assignment, scope: Scope) -> None:
        self.check_expression(node.value, scope)
        target_name = node.target
        if '.' in target_name:
            parts = target_name.split('.')
            base = parts[0]
            info = scope.resolve(base)
            if info is None:
                self.error(f"Assignment to unknown variable '{base}'", node.line)
                return
            # FIX: field-level assignment must also mark the containing
            # struct/shape variable as initialized, mirroring the plain-
            # variable branch below — otherwise later whole-value use of
            # the shape (passing it to a call, returning it, etc.) always
            # spuriously fails "Use of uninitialized variable" even when
            # every field has been explicitly set.
            info.initialized = True
        else:
            info = scope.resolve(target_name)
            if info is None:
                # BUG-01 FIX: auto-declare on first assignment
                inferred_type = self.infer_type(node.value, scope)
                if inferred_type:
                    scope.declare(VarInfo(name=target_name, type=inferred_type,
                                          initialized=True, line=node.line))
                else:
                    self.error(f"Assignment to unknown variable '{target_name}'", node.line)
            else:
                if info.type in self.handle_types:
                    value_type = self.infer_type(node.value, scope)
                    if value_type in self.handle_types and value_type != info.type:
                        self.error(
                            f"Type mismatch: '{target_name}' is handle '{info.type}' "
                            f"but assigned a handle '{value_type}' value",
                            node.line)
                info.initialized = True

    def validate_action(self, node: Action, scope: Scope) -> None:
        if not self.cpp_mode and node.template_params:
            self.error("Templates require --backend cpp", node.line)
        body_scope = Scope(parent=scope)
        template_type_names = {tp[0] for tp in node.template_params}
        template_type_names.update({tp[1] for tp in node.template_params})
        old = self.PRIMITIVE_TYPES.copy()
        self.PRIMITIVE_TYPES.update(template_type_names)
        for pname, ptype in node.params:
            body_scope.declare(VarInfo(name=pname, type=ptype, initialized=True,
                                       is_handle=(ptype == "handle to bytes"), line=node.line))
        for stmt in node.body:
            self.validate_statement(stmt, body_scope)
        self.PRIMITIVE_TYPES = old
        self.check_scope_ownership(body_scope, f"action '{node.name}' exit")

    def validate_method(self, node: Method, scope: Scope) -> None:
        body_scope = Scope(parent=scope)
        if self.current_class and self.current_class in self.shapes:
            for fname, ftype in self.shapes[self.current_class].fields.items():
                body_scope.declare(VarInfo(name=fname, type=ftype, initialized=True, line=node.line))
        for pname, ptype in node.params:
            body_scope.declare(VarInfo(name=pname, type=ptype, initialized=True, line=node.line))
        for stmt in node.body:
            self.validate_statement(stmt, body_scope)

    def validate_constructor(self, node: Constructor, scope: Scope) -> None:
        body_scope = Scope(parent=scope)
        if self.current_class and self.current_class in self.shapes:
            for fname, ftype in self.shapes[self.current_class].fields.items():
                body_scope.declare(VarInfo(name=fname, type=ftype, initialized=True, line=node.line))
        for pname, ptype in node.params:
            body_scope.declare(VarInfo(name=pname, type=ptype, initialized=True, line=node.line))
        for stmt in node.body:
            self.validate_statement(stmt, body_scope)

    def validate_destructor(self, node: Destructor, scope: Scope) -> None:
        body_scope = Scope(parent=scope)
        if self.current_class and self.current_class in self.shapes:
            for fname, ftype in self.shapes[self.current_class].fields.items():
                body_scope.declare(VarInfo(name=fname, type=ftype, initialized=True, line=node.line))
        for stmt in node.body:
            self.validate_statement(stmt, body_scope)

    def validate_new_expr(self, node: NewExpr, scope: Scope) -> Optional[str]:
        type_name = node.type_name.split('.')[-1] if '.' in node.type_name else node.type_name
        if type_name not in self.shapes and type_name not in self.classes:
            self.warning(f"new of unknown type '{node.type_name}'", node.line)
        for arg in node.args:
            self.check_expression(arg, scope)
        return node.type_name

    def validate_lambda(self, node: LambdaExpr, scope: Scope) -> Optional[str]:
        body_scope = Scope(parent=scope)
        for pname, ptype in node.params:
            body_scope.declare(VarInfo(name=pname, type=ptype, initialized=True, line=node.line))
        for stmt in node.body:
            self.validate_statement(stmt, body_scope)
        return f"action taking {', '.join(p[1] for p in node.params)} produces {node.ret_type}"

    def check_scope_ownership(self, scope: Scope, context: str) -> None:
        reported = set()
        for name, info in scope.all_vars().items():
            if info.is_handle and not info.released:
                if self.cpp_mode and info.type.startswith(('unique handle to ', 'shared handle to ')):
                    continue
                key = (name, context, info.line)
                if key in reported: continue
                reported.add(key)
                self.error(f"Ownership violation: handle '{name}' not released at {context}", info.line)

    def validate_if(self, node: If, scope: Scope) -> None:
        self.check_expression(node.cond, scope)
        then_scope = Scope(parent=scope)
        for stmt in node.then_body:
            self.validate_statement(stmt, then_scope)
        if node.else_body:
            else_scope = Scope(parent=scope)
            for stmt in node.else_body:
                self.validate_statement(stmt, else_scope)

    def validate_while(self, node: While, scope: Scope) -> None:
        self.check_expression(node.cond, scope)
        body_scope = Scope(parent=scope)
        self.loop_depth += 1
        for stmt in node.body:
            self.validate_statement(stmt, body_scope)
        self.loop_depth -= 1

    def validate_foreach(self, node: ForEach, scope: Scope) -> None:
        coll_info = scope.resolve(node.collection)
        if coll_info is None:
            self.error(f"For-each on unknown collection '{node.collection}'", node.line)
        body_scope = Scope(parent=scope)
        item_type = coll_info.type if coll_info else "whole number"
        body_scope.declare(VarInfo(name=node.item, type=item_type, initialized=True, line=node.line))
        self.loop_depth += 1
        for stmt in node.body:
            self.validate_statement(stmt, body_scope)
        self.loop_depth -= 1

    def validate_repeat(self, node: Repeat, scope: Scope) -> None:
        self.check_expression(node.count, scope)
        body_scope = Scope(parent=scope)
        body_scope.declare(VarInfo(name=node.counter, type="whole number", initialized=True, line=node.line))
        self.loop_depth += 1
        for stmt in node.body:
            self.validate_statement(stmt, body_scope)
        self.loop_depth -= 1

    def validate_attempt(self, node: Attempt, scope: Scope) -> None:
        if node.call is not None:
            self.validate_funccall(node.call, scope)
        if node.result_name:
            existing = scope.resolve(node.result_name)
            if existing is None:
                ret_type = "whole number"
                if node.call and node.call.name in self.actions:
                    ret_type = self.actions[node.call.name].ret_type
                scope.declare(VarInfo(name=node.result_name, type=ret_type,
                                      initialized=True, line=node.line))
            else:
                existing.initialized = True
        for stmt in node.success_body:
            self.validate_statement(stmt, scope)
        if node.failure_name:
            fail_scope = Scope(parent=scope)
            fail_scope.declare(VarInfo(name=node.failure_name, type="text",
                                       initialized=True, line=node.line))
            for stmt in node.failure_body:
                self.validate_statement(stmt, fail_scope)
        else:
            for stmt in node.failure_body:
                self.validate_statement(stmt, scope)

    def validate_return(self, node: Return, scope: Scope) -> None:
        self.check_expression(node.value, scope)
        if isinstance(node.value, FuncCall):
            if node.value.name in ('success', '__produce_success') and node.value.args:
                arg = node.value.args[0]
                if isinstance(arg, Identifier):
                    info = scope.resolve(arg.name)
                    if info and info.is_handle:
                        info.released = True

    def validate_assert(self, node: Assert, scope: Scope) -> None:
        self.check_expression(node.cond, scope)

    def validate_print(self, node: Print, scope: Scope) -> None:
        for part in node.parts:
            self.check_expression(part, scope)

    def validate_funccall(self, node: FuncCall, scope: Scope) -> None:
        if node is None:
            return
        if node.name == "release":
            if len(node.args) != 1:
                self.error("release requires exactly one argument", node.line)
                return
            arg = node.args[0]
            if isinstance(arg, Identifier):
                info = scope.resolve(arg.name)
                if info and info.is_handle:
                    info.released = True
            return
        if node.name == "__defer_release":
            return
        if '->' in node.name:
            parts = node.name.split('->')
            obj_name = parts[0]
            obj_info = scope.resolve(obj_name)
            for arg in node.args:
                self.check_expression(arg, scope)
            return
        if node.name in self.actions:
            sig = self.actions[node.name]
            if len(node.args) != len(sig.params):
                self.error(f"Action '{node.name}' expects {len(sig.params)} args, got {len(node.args)}", node.line)
            else:
                for (pname, ptype), arg in zip(sig.params, node.args):
                    self._check_handle_type_match(ptype, arg, scope, node)
        else:
            if not node.name.startswith('_'):
                self.warning(f"Call to unknown action '{node.name}'", node.line)
        for arg in node.args:
            self.check_expression(arg, scope)

    def _check_handle_type_match(self, expected_type: str, arg: Node, scope: 'Scope', call: FuncCall) -> None:
        """Catch passing one nominal handle type where another is expected
        (e.g. a Stmt where a Db is required). Only fires when the expected
        param type is a declared `define handle Name` type — generic
        `handle to bytes` / `opaque pointer` / untyped args are left alone
        so this never produces false positives on existing code."""
        if expected_type not in self.handle_types:
            return
        actual_type = self.infer_type(arg, scope)
        if actual_type is None:
            return
        if actual_type in self.handle_types and actual_type != expected_type:
            arg_desc = arg.name if isinstance(arg, Identifier) else 'expression'
            self.error(
                f"Type mismatch in call to '{call.name}': parameter expects "
                f"handle '{expected_type}' but '{arg_desc}' is handle '{actual_type}'",
                call.line)

    def validate_shape(self, node: Shape, scope: Scope) -> None:
        if not self.cpp_mode:
            if node.methods: self.error("Class methods require --backend cpp", node.line)
            if node.constructors: self.error("Constructors require --backend cpp", node.line)
            if node.destructor: self.error("Destructors require --backend cpp", node.line)
            if node.parent: self.error("Inheritance requires --backend cpp", node.line)
        old = self.current_class; self.current_class = node.name
        for m in node.methods: self.validate_method(m, scope)
        for c in node.constructors: self.validate_constructor(c, scope)
        if node.destructor: self.validate_destructor(node.destructor, scope)
        self.current_class = old

    def validate_import(self, node: ImportC, scope: Scope) -> None:
        self.actions[node.alias] = ActionSig(
            name=node.alias,
            params=[(f"arg{i}", p) for i, p in enumerate(node.params)],
            ret_type=node.ret_type, line=node.line)

    def validate_import_cpp(self, node: ImportCpp, scope: Scope) -> None:
        if node.item_type == 'action':
            self.actions[node.alias] = ActionSig(
                name=node.alias,
                params=[(f"arg{i}", p) for i, p in enumerate(node.params)],
                ret_type=node.ret_type, line=node.line)
        elif node.item_type == 'container':
            self.PRIMITIVE_TYPES.add(node.alias)

    def validate_unsafe(self, node: UnsafeBlock, scope: Scope) -> None:
        old = self.in_unsafe
        self.in_unsafe = True
        self._l3_validate_body(node.body, node.line)
        for stmt in node.body:
            self.validate_statement(stmt, scope)
        self.in_unsafe = old

    # ── L3 composition rule checker ─────────────────────────────────────────
    # Runs once over the full body list, enforcing ordering constraints.
    # These mirror the GBNF composition rules in dictum_unsafe.gbnf.

    # All known special token names — unknown names trigger a warning
    _KNOWN_UNSAFE_TOKENS: frozenset = frozenset({
        'ATOMIC_LOAD','ATOMIC_STORE','ATOMIC_ADD','ATOMIC_SUB','ATOMIC_AND',
        'ATOMIC_OR','ATOMIC_XOR','ATOMIC_CAS_32','ATOMIC_CAS_64','ATOMIC_CAS_PTR',
        'ATOMIC_FAA','ATOMIC_FAS',
        'BARRIER_ACQUIRE','BARRIER_RELEASE','BARRIER_SEQ_CST','BARRIER_ACQ_REL',
        'BARRIER_RELAXED','COMPILER_BARRIER',
        'CAS_LOOP_32','CAS_LOOP_64','CAS_LOOP_PTR','DCAS_LOOP_128',
        'HP_READ','HP_PROTECT','HP_CLEAR','HP_RETIRE','HP_SCAN',
        'RCU_READ_LOCK','RCU_READ_UNLOCK','RCU_SYNCHRONIZE',
        'RCU_ASSIGN_POINTER','RCU_DEREFERENCE',
        'SIMD_LOAD_F32','SIMD_LOADU_F32','SIMD_LOAD_I32','SIMD_LOADU_I32',
        'SIMD_LOAD_F64','SIMD_LOADU_F64','SIMD_LOAD_I64','SIMD_LOADU_I64',
        'SIMD_STORE_F32','SIMD_STOREU_F32','SIMD_STORE_I32','SIMD_STOREU_I32',
        'SIMD_ADD_F32','SIMD_SUB_F32','SIMD_MUL_F32','SIMD_DIV_F32',
        'SIMD_SQRT_F32','SIMD_FMA_F32','SIMD_MIN_F32','SIMD_MAX_F32',
        'SIMD_SHUFFLE_F32','SIMD_UNPACKLO_F32','SIMD_UNPACKHI_F32',
        'SIMD_BROADCAST_F32','SIMD_BLEND_F32',
        'RAW_MEMCPY','RAW_MEMSET','RAW_MEMCMP','RAW_MEMMOVE',
        'RAW_MALLOC','RAW_FREE','RAW_REALLOC','RAW_CALLOC',
        'PUN_INT_TO_FLOAT','PUN_FLOAT_TO_INT','PUN_PTR_TO_INT','PUN_INT_TO_PTR',
        'PUN_READ_UNALIGNED_16','PUN_READ_UNALIGNED_32','PUN_READ_UNALIGNED_64',
        'FFI_LOAD','FFI_SYMBOL','FFI_CALL_VOID','FFI_CALL_INT',
        'FFI_CALL_FLOAT','FFI_CALL_PTR','FFI_CLOSE',
        'ALIGNED_ALLOC_16','ALIGNED_ALLOC_32','ALIGNED_ALLOC_64',
        'ALIGN_UP','ALIGN_DOWN','IS_ALIGNED',
        'BIT_SET','BIT_CLEAR','BIT_TOGGLE','BIT_TEST','BIT_COUNT',
        'BIT_REVERSE','BIT_SCAN_FORWARD','BIT_SCAN_REVERSE',
        'SWAP_ENDIAN_16','SWAP_ENDIAN_32','SWAP_ENDIAN_64',
        'HTON_16','HTON_32','HTON_64','NTOH_16','NTOH_32','NTOH_64',
    })

    _CAS_TOKENS: frozenset = frozenset({
        'ATOMIC_CAS_32','ATOMIC_CAS_64','ATOMIC_CAS_PTR',
        'CAS_LOOP_32','CAS_LOOP_64','CAS_LOOP_PTR','DCAS_LOOP_128',
    })
    _BARRIER_TOKENS: frozenset = frozenset({
        'BARRIER_ACQUIRE','BARRIER_RELEASE','BARRIER_SEQ_CST',
        'BARRIER_ACQ_REL','BARRIER_RELAXED','COMPILER_BARRIER',
    })
    _ALLOC_TOKENS: frozenset = frozenset({
        'RAW_MALLOC','RAW_CALLOC','RAW_REALLOC',
        'ALIGNED_ALLOC_16','ALIGNED_ALLOC_32','ALIGNED_ALLOC_64',
    })
    _SIMD_ALIGNED_LOADS: frozenset = frozenset({
        'SIMD_LOAD_F32','SIMD_LOAD_I32','SIMD_LOAD_F64','SIMD_LOAD_I64',
    })

    def _l3_validate_body(self, stmts: list, block_line: int) -> None:
        """L3 composition rule checker — runs over the flat token list."""
        unsafe_stmts = [s for s in stmts if isinstance(s, UnsafeToken)]
        names = [s.name for s in unsafe_stmts]

        # ── Rule 1: CAS → barrier ──────────────────────────────────────────
        for i, tok in enumerate(unsafe_stmts):
            if tok.name in self._CAS_TOKENS:
                next_tok = unsafe_stmts[i + 1] if i + 1 < len(unsafe_stmts) else None
                if next_tok is None or next_tok.name not in self._BARRIER_TOKENS:
                    self.error(
                        f"L3: [{tok.name}] must be immediately followed by a barrier "
                        f"(BARRIER_ACQUIRE/RELEASE/SEQ_CST/ACQ_REL). "
                        f"Got: {next_tok.name if next_tok else 'end of block'}",
                        tok.line,
                    )

        # ── Rule 2: HP_PROTECT → HP_CLEAR or HP_RETIRE in same block ───────
        for tok in unsafe_stmts:
            if tok.name == 'HP_PROTECT':
                hp_var = tok.params[0] if tok.params else ''
                cleared = any(
                    s.name in ('HP_CLEAR', 'HP_RETIRE') and
                    (s.params[0] if s.params else '') == hp_var
                    for s in unsafe_stmts
                )
                if not cleared:
                    self.error(
                        f"L3: [HP_PROTECT] on '{hp_var}' has no matching "
                        f"[HP_CLEAR] or [HP_RETIRE] in this unsafe block.",
                        tok.line,
                    )

        # ── Rule 3: RAW_MALLOC → RAW_FREE pairing ───────────────────────────
        alloc_vars: dict = {}
        for tok in unsafe_stmts:
            if tok.name in self._ALLOC_TOKENS:
                result_var = tok.result
                if result_var:
                    alloc_vars[result_var] = tok
            elif tok.name == 'RAW_FREE':
                freed = tok.params[0] if tok.params else ''
                alloc_vars.pop(freed, None)
        for var, tok in alloc_vars.items():
            self.error(
                f"L3: [{tok.name}] allocates '{var}' but no [RAW_FREE] found "
                f"in this unsafe block (memory leak).",
                tok.line,
            )

        # ── Rule 4: FFI_LOAD → FFI_CLOSE pairing ────────────────────────────
        ffi_handles: dict = {}
        for tok in unsafe_stmts:
            if tok.name == 'FFI_LOAD':
                handle = tok.params[1] if len(tok.params) > 1 else ''
                if handle:
                    ffi_handles[handle] = tok
            elif tok.name == 'FFI_CLOSE':
                closed = tok.params[0] if tok.params else ''
                ffi_handles.pop(closed, None)
        for handle, tok in ffi_handles.items():
            self.error(
                f"L3: [FFI_LOAD] opens handle '{handle}' but no [FFI_CLOSE] "
                f"found in this unsafe block (handle leak).",
                tok.line,
            )

        # ── Rule 5: Aligned SIMD load needs IS_ALIGNED guard ────────────────
        for i, tok in enumerate(unsafe_stmts):
            if tok.name in self._SIMD_ALIGNED_LOADS:
                # Check if IS_ALIGNED appears before this token in same block
                preceding = [s.name for s in unsafe_stmts[:i]]
                if 'IS_ALIGNED' not in preceding:
                    self.warning(
                        f"L3: [{tok.name}] is an aligned SIMD load but no "
                        f"[IS_ALIGNED] check precedes it. Use SIMD_LOADU_* for "
                        f"unaligned data, or add [IS_ALIGNED: ptr : 32 : ok] guard.",
                        tok.line,
                    )

        # ── Rule 6: Unknown token names ──────────────────────────────────────
        for tok in unsafe_stmts:
            if tok.name not in self._KNOWN_UNSAFE_TOKENS:
                self.warning(
                    f"L3: Unknown unsafe token [{tok.name}] — "
                    f"not in the verified token library.",
                    tok.line,
                )

    def validate_unsafe_token(self, node: UnsafeToken, scope: Scope) -> None:
        """Per-token semantic checks (result var declaration, param count)."""
        if not self.in_unsafe:
            self.error(
                f"[{node.name}] used outside an unsafe block.",
                node.line,
            )
            return

        # Auto-declare result variable into scope so subsequent code can use it
        if node.result and not scope.resolve(node.result):
            # Infer type from token category
            result_type = self._infer_token_result_type(node.name)
            try:
                scope.declare(VarInfo(
                    name=node.result,
                    type=result_type,
                    initialized=True,
                    line=node.line,
                ))
            except Exception:
                pass  # Already declared — fine

    def _infer_token_result_type(self, token_name: str) -> str:
        """Infer the C type of a special token's result variable."""
        if token_name.startswith('SIMD_') and 'I' in token_name:
            return 'u64'   # __m256i → treat as u64 in Dictum type system
        if token_name.startswith('SIMD_'):
            return 'fractional number'  # __m256 → float vector
        if token_name in ('ATOMIC_CAS_32','ATOMIC_CAS_64','ATOMIC_CAS_PTR',
                          'CAS_LOOP_32','CAS_LOOP_64','CAS_LOOP_PTR','DCAS_LOOP_128',
                          'BIT_TEST','IS_ALIGNED'):
            return 'truth value'
        if token_name in ('BIT_COUNT','BIT_SCAN_FORWARD','BIT_SCAN_REVERSE',
                          'RAW_MEMCMP','FFI_CALL_INT'):
            return 'whole number'
        if token_name in ('RAW_MALLOC','RAW_CALLOC','RAW_REALLOC',
                          'ALIGNED_ALLOC_16','ALIGNED_ALLOC_32','ALIGNED_ALLOC_64',
                          'PUN_INT_TO_PTR','FFI_LOAD','FFI_SYMBOL','FFI_CALL_PTR'):
            return 'raw pointer'
        if token_name in ('PUN_INT_TO_FLOAT','FFI_CALL_FLOAT'):
            return 'fractional number'
        if token_name in ('SWAP_ENDIAN_16','SWAP_ENDIAN_32','SWAP_ENDIAN_64',
                          'HTON_16','HTON_32','HTON_64','NTOH_16','NTOH_32','NTOH_64',
                          'PUN_FLOAT_TO_INT','PUN_PTR_TO_INT',
                          'BIT_REVERSE',
                          'ATOMIC_ADD','ATOMIC_SUB','ATOMIC_AND','ATOMIC_OR',
                          'ATOMIC_XOR','ATOMIC_FAA','ATOMIC_FAS',
                          'ATOMIC_LOAD','PUN_READ_UNALIGNED_16',
                          'PUN_READ_UNALIGNED_32','PUN_READ_UNALIGNED_64'):
            return 'whole number'
        return 'whole number'  # safe default

    def _resolve_dotted_type(self, dotted: str, scope: 'Scope') -> Optional[str]:
        parts = dotted.split('.')
        info = scope.resolve(parts[0])
        if info is None:
            return None
        cur_type = info.type
        for seg in parts[1:]:
            t = cur_type
            for prefix in ['const ref ', 'ref ', 'move ',
                           'unique handle to ', 'shared handle to ',
                           'weak handle to ', 'raw handle to ']:
                if t.startswith(prefix):
                    t = t[len(prefix):].strip(); break
            if '.' in t: t = t.split('.')[-1]
            if t not in self.shapes:
                return None
            shape = self.shapes[t]
            if seg not in shape.fields:
                return None
            cur_type = shape.fields[seg]
        return cur_type

    # ------------------------------------------------------------------
    # Type inference & expression checking
    # ------------------------------------------------------------------
    def check_expression(self, node: Node, scope: Scope) -> Optional[str]:
        if isinstance(node, Literal):
            if isinstance(node.value, bool):   return "truth value"
            if isinstance(node.value, int):    return "whole number"
            if isinstance(node.value, float):  return "fractional number"
            if isinstance(node.value, str):    return "text"
            if isinstance(node.value, list):   return "array"
            return None
        elif isinstance(node, Identifier):
            info = scope.resolve(node.name)
            if info is None:
                if self.current_class and self.current_class in self.shapes:
                    shape = self.shapes[self.current_class]
                    if node.name in shape.fields:
                        return shape.fields[node.name]
                self.error(f"Use of undeclared variable '{node.name}'", node.line)
                return None
            if not info.initialized:
                self.error(f"Use of uninitialized variable '{node.name}'", node.line)
            if info.is_handle and info.released:
                self.error(f"Use-after-free: handle '{node.name}' used after release", node.line)
            return info.type
        elif isinstance(node, FieldAccess):
            base_type = self._resolve_dotted_type(node.obj, scope)
            if base_type is None:
                self.error(f"Access to unknown object '{node.obj}'", node.line)
                return None
            for prefix in ['const ref ', 'ref ', 'move ',
                           'unique handle to ', 'shared handle to ',
                           'weak handle to ', 'raw handle to ']:
                if base_type.startswith(prefix):
                    base_type = base_type[len(prefix):].strip(); break
            if '.' in base_type: base_type = base_type.split('.')[-1]
            if base_type not in self.shapes:
                return None
            shape = self.shapes[base_type]
            if node.field not in shape.fields:
                self.error(f"Shape '{base_type}' has no field '{node.field}'", node.line)
                return None
            return shape.fields[node.field]
        elif isinstance(node, IndexAccess):
            self.check_expression(node.index, scope)
            coll = scope.resolve(node.collection)
            return coll.type if coll else None
        elif isinstance(node, BinaryOp):
            left_type = self.check_expression(node.left, scope)
            right_type = self.check_expression(node.right, scope)
            if node.op in ('==', '!=', '>', '<', '>=', '<='):
                return "truth value"
            return left_type
        elif isinstance(node, UnaryOp):
            if node.op in ('count', 'length'): return "count"
            if node.op in ('tanh', 'sqrt', 'exp', 'sin', 'cos'): return "fractional number"
            if node.op == 'room_for': return "handle to bytes"
            return self.check_expression(node.operand, scope)
        elif isinstance(node, FuncCall):
            for arg in node.args: self.check_expression(arg, scope)
            if node.name in self.actions:
                sig = self.actions[node.name]
                if len(node.args) != len(sig.params):
                    self.error(f"Action '{node.name}' expects {len(sig.params)} args, got {len(node.args)}", node.line)
                else:
                    for (pname, ptype), arg in zip(sig.params, node.args):
                        self._check_handle_type_match(ptype, arg, scope, node)
                return sig.ret_type
            return None
        elif isinstance(node, NewExpr):
            return self.validate_new_expr(node, scope)
        elif isinstance(node, LambdaExpr):
            return self.validate_lambda(node, scope)
        return None

    def infer_type(self, node: Node, scope: Scope) -> Optional[str]:
        if isinstance(node, Literal):
            if isinstance(node.value, bool):  return "truth value"
            if isinstance(node.value, int):   return "whole number"
            if isinstance(node.value, float): return "fractional number"
            if isinstance(node.value, str):   return "text"
        if isinstance(node, Identifier):
            info = scope.resolve(node.name)
            if info: return info.type
        if isinstance(node, FuncCall):
            return self.check_expression(node, scope)
        if isinstance(node, BinaryOp):
            return self.infer_type(node.left, scope)
        if isinstance(node, UnaryOp):
            if node.op == 'room_for': return "handle to bytes"
            return self.infer_type(node.operand, scope)
        if isinstance(node, NewExpr):
            return node.type_name
        return None
