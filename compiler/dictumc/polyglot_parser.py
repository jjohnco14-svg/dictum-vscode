"""
Dictum Polyglot Parser Extension — v1.1.0

Monkey-patches / subclasses the base Parser to handle:
  - `polyglot module <name> uses <backend> [as safe|unsafe|checked] [via ffi|grpc|http]`
  - `@export [unsafe] [threadsafe] [as "<c_name>"]` annotation
  - `call <Module>.<fn> with ... giving ...` (polyglot cross-module call)
  - `polyglot import <module>` 
  - `unsafe call foreign "<symbol>" with ... giving ... as <type>`
  - `shape <Name> from foreign C holds ...`
  - `#[link "lib"]` / `#[cflags "..."]` / `#[ldflags "..."]` directives
  - `@serializable [json|msgpack|protobuf]` annotation

Usage:
    from dictumc.polyglot_parser import PolyglotParser
    parser = PolyglotParser(tokens, grammar=grammar)
    ast = parser.parse()
"""

from __future__ import annotations
from typing import List, Optional

from .parser import Parser
from .lexer import Token, TokenType
from .ast_nodes import Node, Shape, Action, Use
from .polyglot_ast import (
    PolyglotModule, ExportDecl, PolyglotCall, PolyglotImport,
    UnsafeForeignCall, BuildDirective, ForeignShape, SerializedType,
    SafetyLevel, POLYGLOT_BACKENDS, INTEROP_PATTERNS,
)


class PolyglotParser(Parser):
    """
    Extends Parser with polyglot-specific syntax.
    All base-parser functionality is preserved unchanged.
    """

    def __init__(self, tokens, grammar=None):
        super().__init__(tokens, grammar)
        self._pending_export: Optional[ExportDecl] = None
        self._pending_serializable: Optional[str] = None   # format string
        self._pending_build_directives: List[BuildDirective] = []

    # ------------------------------------------------------------------
    # Override parse_top_level to handle polyglot keywords
    # ------------------------------------------------------------------
    def parse_top_level(self) -> Node:
        self.consume_newlines()
        tok = self.cur()
        if tok.type != TokenType.WORD:
            return super().parse_top_level()

        # @export / @serializable / @unsafe annotation
        if tok.value == '@':
            return self._parse_annotation()

        # polyglot module / polyglot import
        if tok.value == 'polyglot':
            self.advance()
            nxt = self.cur()
            if nxt.type == TokenType.WORD and nxt.value == 'module':
                return self._parse_polyglot_module()
            elif nxt.type == TokenType.WORD and nxt.value == 'import':
                return self._parse_polyglot_import()
            raise SyntaxError(f"Expected 'module' or 'import' after 'polyglot' at line {tok.line}")

        # #[directive]
        if tok.value == '#':
            node = self._try_parse_build_directive()
            if node is not None:
                return node

        return super().parse_top_level()

    # ------------------------------------------------------------------
    # Override parse_statement for inline polyglot constructs
    # ------------------------------------------------------------------
    def parse_statement(self) -> Node:
        tok = self.cur()
        if tok.type != TokenType.WORD:
            return super().parse_statement()

        # @export / @serializable inline
        if tok.value == '@':
            return self._parse_annotation()

        # polyglot import inside a block
        if tok.value == 'polyglot':
            save = self.pos
            self.advance()
            nxt = self.cur()
            if nxt.type == TokenType.WORD and nxt.value == 'import':
                return self._parse_polyglot_import()
            # Not a polyglot keyword — restore and fall through
            self.pos = save

        # unsafe call foreign "<symbol>" ...
        if tok.value == 'unsafe':
            save = self.pos
            self.advance()
            if (self.cur().type == TokenType.WORD and self.cur().value == 'call'):
                self.advance()
                if (self.cur().type == TokenType.WORD and self.cur().value == 'foreign'):
                    self.advance()   # consume 'foreign'
                    return self._parse_unsafe_foreign_call(tok.line)
            # Not the unsafe foreign call form — restore
            self.pos = save

        # #[directive] inside a block
        if tok.value == '#':
            node = self._try_parse_build_directive()
            if node is not None:
                return node

        return super().parse_statement()

    # ------------------------------------------------------------------
    # @annotation parsing
    # ------------------------------------------------------------------
    def _parse_annotation(self) -> Node:
        """Parse @export, @serializable, @unsafe — then consume the decorated node."""
        line = self.cur().line
        self.advance()  # consume '@'
        if self.cur().type != TokenType.WORD:
            raise SyntaxError(f"Expected annotation name after '@' at line {line}")

        ann = self.advance().value  # 'export' | 'serializable' | 'unsafe' | 'threadsafe'

        if ann == 'export':
            return self._parse_export_annotation(line)
        elif ann == 'serializable':
            return self._parse_serializable_annotation(line)
        elif ann in ('unsafe', 'checked'):
            # @unsafe action ... or @unsafe shape ...
            self._pending_export = ExportDecl(
                safety=SafetyLevel.UNSAFE if ann == 'unsafe' else SafetyLevel.CHECKED,
                line=line
            )
            self.consume_newlines()
            return self._consume_annotated_decl()
        else:
            raise SyntaxError(f"Unknown annotation '@{ann}' at line {line}")

    def _parse_export_annotation(self, line: int) -> Node:
        """
        @export [unsafe|checked] [threadsafe] [as "<c_name>"] [calling <conv>]
        """
        safety = SafetyLevel.SAFE
        thread_safe = False
        c_name = ''
        calling_conv = 'cdecl'

        while self.cur().type == TokenType.WORD and self.cur().value in (
                'unsafe', 'checked', 'threadsafe', 'as', 'calling'):
            kw = self.advance().value
            if kw == 'unsafe':
                safety = SafetyLevel.UNSAFE
            elif kw == 'checked':
                safety = SafetyLevel.CHECKED
            elif kw == 'threadsafe':
                thread_safe = True
            elif kw == 'as':
                if self.cur().type == TokenType.STRING:
                    c_name = self.advance().value
                else:
                    c_name = self.expect_word().value
            elif kw == 'calling':
                calling_conv = self.expect_word().value

        self._pending_export = ExportDecl(
            c_name=c_name, safety=safety, calling_conv=calling_conv,
            thread_safe=thread_safe, line=line,
        )
        self.consume_newlines()
        return self._consume_annotated_decl()

    def _parse_serializable_annotation(self, line: int) -> Node:
        """@serializable [json|msgpack|protobuf]"""
        fmt = 'json'
        if self.cur().type == TokenType.WORD and self.cur().value in ('json', 'msgpack', 'protobuf'):
            fmt = self.advance().value
        self._pending_serializable = fmt
        self.consume_newlines()
        return self._consume_annotated_decl()

    def _consume_annotated_decl(self) -> Node:
        """Consume the declaration that follows an annotation, wrap with metadata.
        Handles stacked annotations: @export @serializable shape ..."""
        self.consume_newlines()
        tok = self.cur()
        if tok.type != TokenType.WORD:
            raise SyntaxError(f"Expected declaration after annotation at line {tok.line}")

        # Handle stacked annotations (e.g. @export then @serializable)
        if tok.value == '@':
            additional = self._parse_annotation()
            # The additional annotation already consumed its decorated node
            # Transfer any pending export from this annotation to the already-consumed node
            if self._pending_export and hasattr(additional, '_serializable'):
                exp = self._pending_export
                exp.name = getattr(additional, 'name', '')
                if not exp.c_name:
                    exp.c_name = exp.name
                additional._polyglot_export = exp
                self._pending_export = None
            return additional

        if tok.value == 'action':
            node = self.parse_action()
            if self._pending_export:
                exp = self._pending_export
                exp.name = node.name
                if not exp.c_name:
                    exp.c_name = node.name
                node._polyglot_export = exp
                self._pending_export = None
            if self._pending_serializable:
                self._pending_serializable = None  # not applicable to actions
        elif tok.value == 'shape':
            node = self.parse_shape()
            if self._pending_export:
                exp = self._pending_export
                exp.name = node.name
                if not exp.c_name:
                    exp.c_name = node.name
                node._polyglot_export = exp
                self._pending_export = None
            if self._pending_serializable:
                node._serializable = self._pending_serializable
                self._pending_serializable = None
        else:
            raise SyntaxError(
                f"@export/@serializable can only decorate 'action' or 'shape', "
                f"got '{tok.value}' at line {tok.line}"
            )

        return node

    # ------------------------------------------------------------------
    # polyglot module
    # ------------------------------------------------------------------
    def _parse_polyglot_module(self) -> PolyglotModule:
        """
        polyglot module <name> uses <backend>
            [as safe|unsafe|checked]
            [via ffi|grpc|http|msgqueue|wasm]
        <body>
        end module
        """
        line = self.advance().line   # consume 'module'
        name = self.expect_word().value
        self.expect_word('uses')
        backend = self.expect_word().value
        if backend not in POLYGLOT_BACKENDS:
            raise SyntaxError(
                f"Unknown backend '{backend}' at line {line}. "
                f"Valid backends: {sorted(POLYGLOT_BACKENDS)}"
            )

        safety = SafetyLevel.SAFE
        interop = 'ffi'

        while self.cur().type == TokenType.WORD and self.cur().value in ('as', 'via'):
            kw = self.advance().value
            if kw == 'as':
                s = self.expect_word().value
                if s in ('safe', 'unsafe', 'checked'):
                    safety = s
                else:
                    raise SyntaxError(f"Expected safe|unsafe|checked, got '{s}' at line {line}")
            elif kw == 'via':
                p = self.expect_word().value
                if p in INTEROP_PATTERNS:
                    interop = p
                else:
                    raise SyntaxError(
                        f"Unknown interop pattern '{p}' at line {line}. "
                        f"Valid: {sorted(INTEROP_PATTERNS)}"
                    )

        self.consume_newlines()
        body = self.parse_block()
        if self.cur().type == TokenType.WORD and self.cur().value == 'end':
            self.advance()
            if self.cur().type == TokenType.WORD and self.cur().value == 'module':
                self.advance()

        return PolyglotModule(
            name=name, backend=backend, safety=safety,
            interop=interop, body=body, line=line,
        )

    # ------------------------------------------------------------------
    # polyglot import
    # ------------------------------------------------------------------
    def _parse_polyglot_import(self) -> PolyglotImport:
        """polyglot import <module_name> [as <alias>] [via ffi|grpc|http]"""
        line = self.advance().line   # consume 'import'
        module_name = self.expect_word().value
        alias = module_name
        pattern = 'ffi'

        while self.cur().type == TokenType.WORD and self.cur().value in ('as', 'via'):
            kw = self.advance().value
            if kw == 'as':
                alias = self.expect_word().value
            elif kw == 'via':
                p = self.expect_word().value
                if p in INTEROP_PATTERNS:
                    pattern = p
        return PolyglotImport(module_name=module_name, alias=alias, pattern=pattern, line=line)

    # ------------------------------------------------------------------
    # unsafe call foreign
    # ------------------------------------------------------------------
    def _parse_unsafe_foreign_call(self, line: int) -> UnsafeForeignCall:
        """unsafe call foreign "<symbol>" with <args> giving <result> as <type>"""
        # 'unsafe call foreign' already consumed
        if self.cur().type != TokenType.STRING:
            raise SyntaxError(f"Expected string symbol name after 'foreign' at line {line}")
        symbol = self.advance().value

        args = []
        if self.match_word('with'):
            while not self._is_end_of_statement():
                args.append(self.parse_expression())
                if not self.match_word('and') and not self.match_word(','):
                    break

        result_name = ''
        result_type = ''
        if self.match_word('giving'):
            result_name = self.expect_word().value
            if self.match_word('as'):
                result_type = self.parse_type()

        return UnsafeForeignCall(
            symbol=symbol, args=args,
            result_name=result_name, result_type=result_type,
            line=line,
        )

    # ------------------------------------------------------------------
    # #[directive]
    # ------------------------------------------------------------------
    def _try_parse_build_directive(self) -> Optional[BuildDirective]:
        """
        #[link "libcurl"]
        #[cflags "-O3 -march=native"]
        #[ldflags "-lpthread -lssl"]
        #[include_path "/usr/local/include"]
        """
        save = self.pos
        try:
            line = self.advance().line  # consume '#'
            if self.cur().type != TokenType.WORD or self.cur().value != '[':
                self.pos = save
                return None
            self.advance()  # consume '['
            if self.cur().type != TokenType.WORD:
                self.pos = save
                return None
            kind = self.advance().value
            if kind not in ('link', 'cflags', 'ldflags', 'include_path', 'packed'):
                self.pos = save
                return None
            value = ''
            if self.cur().type == TokenType.STRING:
                value = self.advance().value
            if self.cur().type == TokenType.WORD and self.cur().value == ']':
                self.advance()
            return BuildDirective(kind=kind, value=value, line=line)
        except Exception:
            self.pos = save
            return None

    # ------------------------------------------------------------------
    # shape ... from foreign C holds ...
    # ------------------------------------------------------------------
    def _parse_foreign_shape(self) -> ForeignShape:
        """
        shape <Name> from foreign C [packed] holds
            <field> as <type>
            ...
        end shape
        """
        line = self.advance().line  # consume 'shape'
        name = self.expect_word().value
        self.expect_word('from')
        self.expect_word('foreign')
        lang = self.expect_word().value
        packed = self.match_word('packed')
        self.expect_word('holds')
        self.consume_newlines()

        fields = []
        if self.cur().type == TokenType.INDENT:
            self.advance()
        while not (self.cur().type == TokenType.WORD and self.cur().value == 'end'):
            self.consume_newlines()
            if self.cur().type in (TokenType.DEDENT, TokenType.EOF):
                break
            fname = self.expect_word().value
            self.expect_word('as')
            ftype = self.parse_type()
            fields.append((fname, ftype))
            self.consume_newlines()
        if self.cur().type == TokenType.WORD and self.cur().value == 'end':
            self.advance()
            if self.cur().type == TokenType.WORD and self.cur().value == 'shape':
                self.advance()
        if self.cur().type == TokenType.DEDENT:
            self.advance()

        return ForeignShape(name=name, source_language=lang.lower(),
                            fields=fields, packed=packed, line=line)
