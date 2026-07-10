"""
Dictum Lexer — tokenises Dictum source text.
Extracted from transpiler.py v3.3 and kept API-identical.
"""

from enum import Enum, auto
from dataclasses import dataclass
from typing import List, Any


class TokenType(Enum):
    WORD    = auto()
    NUMBER  = auto()
    STRING  = auto()
    NEWLINE = auto()
    INDENT  = auto()
    DEDENT  = auto()
    EOF     = auto()


@dataclass
class Token:
    type: TokenType
    value: Any
    line: int = 0


class Lexer:
    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.indent_stack = [0]
        self.tokens: List[Token] = []

    def peek(self, offset: int = 0) -> str:
        idx = self.pos + offset
        if idx >= len(self.source):
            return '\0'
        return self.source[idx]

    def advance(self) -> str:
        ch = self.source[self.pos]
        self.pos += 1
        if ch == '\n':
            self.line += 1
        return ch

    def skip_spaces(self) -> None:
        while self.peek() in ' \t\r':
            self.advance()

    _STRING_ESCAPES = {
        'n': '\n', 't': '\t', 'r': '\r', '0': '\0',
        '"': '"', "'": "'", '\\': '\\',
    }

    def read_string(self) -> Token:
        quote = self.advance()
        start_line = self.line
        buf = []
        while self.peek() not in ('\0', '\n'):
            ch = self.advance()
            if ch == quote:
                return Token(TokenType.STRING, ''.join(buf), start_line)
            if ch == '\\':
                # FIX: this used to just discard the backslash and keep
                # whatever character followed it literally — so `\"`
                # became a bare `"` with no record that it had been
                # escaped, `\n` became the letter 'n' (not a newline),
                # and `\\` became a single `\`. That's silently wrong for
                # any string with an escape in it, and specifically
                # breaks embedding JSON literals in Dictum source (e.g.
                # `"{\"name\":\"Jeff\"}"`), which need `\"` to survive as
                # an actual quote character the C emitter can re-escape.
                nxt = self.advance()
                ch = self._STRING_ESCAPES.get(nxt, nxt)
            buf.append(ch)
        raise SyntaxError(f"Unterminated string at line {start_line}")

    def read_number(self) -> Token:
        start_line = self.line
        buf = []
        while self.peek().isdigit() or self.peek() == '.':
            buf.append(self.advance())
        val = ''.join(buf)
        if '.' in val:
            return Token(TokenType.NUMBER, float(val), start_line)
        return Token(TokenType.NUMBER, int(val), start_line)

    def read_word(self) -> Token:
        start_line = self.line
        buf = []
        while self.peek().isalnum() or self.peek() == '_':
            buf.append(self.advance())
        return Token(TokenType.WORD, ''.join(buf), start_line)

    def tokenize(self) -> List[Token]:
        while True:
            self.skip_spaces()
            ch = self.peek()
            if ch == '\0':
                break
            if ch in ('"', "'"):
                self.tokens.append(self.read_string())
                continue
            if ch.isdigit():
                self.tokens.append(self.read_number())
                continue
            if ch.isalpha() or ch == '_':
                self.tokens.append(self.read_word())
                continue
            if ch == '#':
                if self.peek(1) == '[':
                    self.tokens.append(Token(TokenType.WORD, '#', self.line))
                    self.advance()
                    continue
                else:
                    while self.peek() not in ('\0', '\n'):
                        self.advance()
                    continue
            if ch == '\n':
                self.advance()
                self.tokens.append(Token(TokenType.NEWLINE, None, self.line))
                indent = 0
                while self.peek() in ' \t':
                    if self.advance() == '\t':
                        indent += 4
                    else:
                        indent += 1
                if self.peek() in ('\n', '#', '\0'):
                    continue
                if indent > self.indent_stack[-1]:
                    self.indent_stack.append(indent)
                    self.tokens.append(Token(TokenType.INDENT, None, self.line))
                elif indent < self.indent_stack[-1]:
                    while indent < self.indent_stack[-1]:
                        self.indent_stack.pop()
                        self.tokens.append(Token(TokenType.DEDENT, None, self.line))
                    if indent != self.indent_stack[-1]:
                        raise SyntaxError(f"Invalid dedent at line {self.line}")
                continue
            # Single-char punctuation
            _SINGLE = {'.', '*', '&', '(', ')', '[', ']', ',', ';', '+', '-', '%', '@'}
            if ch in _SINGLE:
                self.tokens.append(Token(TokenType.WORD, ch, self.line))
                self.advance()
                continue
            # Skip unknown characters (e.g. ':', '=', '/')
            self.advance()

        while len(self.indent_stack) > 1:
            self.indent_stack.pop()
            self.tokens.append(Token(TokenType.DEDENT, None, self.line))
        self.tokens.append(Token(TokenType.EOF, None, self.line))
        return self.tokens
