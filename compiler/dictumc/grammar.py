"""
Dictum Grammar — constraint engine for LLM token masking and grammar-guided parsing.
Extracted from transpiler.py v3.3, wired to the new modular parser.

New in v4:
  • DictumGrammar can be passed directly to Parser(tokens, grammar=grammar)
    so every parsed token also advances the grammar state machine.
  • resync_from_source() uses the modular Lexer.
  • GrammarConstrainedGenerator.parse_with_grammar() returns a full AST.
"""

from __future__ import annotations
from enum import Enum, auto
from dataclasses import dataclass
from typing import List, Dict, Set, Optional, Tuple, FrozenSet


# ---------------------------------------------------------------------------
# Grammar states
# ---------------------------------------------------------------------------

class GrammarState(Enum):
    TOP_LEVEL        = auto()
    PROGRAM_NAME     = auto()
    MODULE_NAME      = auto()
    BLOCK_BODY       = auto()
    STATEMENT        = auto()
    KEEP_NAME        = auto()
    KEEP_TYPE        = auto()
    KEEP_WITH        = auto()
    KEEP_INIT        = auto()
    PUT_VALUE        = auto()
    PUT_TARGET       = auto()
    SET_TARGET       = auto()    # NEW: BUG-02
    SET_TO           = auto()    # NEW: BUG-02
    IF_COND          = auto()
    IF_THEN          = auto()
    WHILE_COND       = auto()
    WHILE_REPEAT     = auto()
    FOR_EACH_ITEM    = auto()
    FOR_EACH_COLL    = auto()
    FOR_EACH_REPEAT  = auto()
    REPEAT_COUNT     = auto()
    REPEAT_TIMES     = auto()
    REPEAT_USING     = auto()
    ACTION_NAME      = auto()
    ACTION_PARAMS    = auto()
    ACTION_RET       = auto()
    SHAPE_NAME       = auto()
    SHAPE_HOLDS      = auto()
    SHAPE_FIELDS     = auto()
    EXPRESSION       = auto()
    COMPARISON       = auto()
    PREFIX_EXPR      = auto()
    PREFIX_OF        = auto()
    CALL_ARGS        = auto()
    END_BLOCK_TYPE   = auto()
    ATTEMPT_CALL     = auto()
    ATTEMPT_GIVING   = auto()
    ATTEMPT_SUCCESS  = auto()
    ATTEMPT_FAILURE  = auto()
    IMPORT_C         = auto()
    IMPORT_FROM      = auto()
    IMPORT_THE       = auto()
    IMPORT_ACTION    = auto()
    IMPORT_ACTION_NAME = auto()
    IMPORT_TAKES     = auto()
    IMPORT_PRODUCES  = auto()
    IMPORT_AS        = auto()
    DEFER_RELEASE    = auto()
    PRODUCE_FAILURE  = auto()
    PRODUCE_SUCCESS  = auto()
    END_TOP_LEVEL    = auto()
    TYPE_SMART_PTR   = auto()
    TYPE_REF         = auto()
    TYPE_RAW_PTR     = auto()
    METHOD_NAME      = auto()
    METHOD_PARAMS    = auto()
    METHOD_RET       = auto()
    METHOD_BODY      = auto()
    CTOR_PARAMS      = auto()
    CTOR_RET         = auto()
    CTOR_BODY        = auto()
    DTOR_RET         = auto()
    DTOR_BODY        = auto()
    NEW_TYPE_NAME    = auto()
    NEW_WITH         = auto()
    CALL_NAME        = auto()
    CALL_OF          = auto()
    CALL_OBJ         = auto()
    CALL_WITH        = auto()
    CALL_RESULT      = auto()
    LAMBDA_PARAMS    = auto()
    LAMBDA_RET       = auto()
    LAMBDA_BODY      = auto()
    UNSAFE_BODY      = auto()  # NEW: unsafe block body
    UNSAFE_TOKEN     = auto()  # NEW: inside a [TOKEN:...] special token
    TEMPLATE_PARAM   = auto()
    ACCESS_SPEC      = auto()


@dataclass
class GrammarCheckpoint:
    """Immutable snapshot for rollback support (speculative decoding)."""
    stack_snapshot: Tuple[GrammarState, ...]
    indent_level: int
    block_depth: int
    pending_expr_pop: bool


class TrieNode:
    __slots__ = ['children', 'token_ids', 'is_terminal']
    def __init__(self):
        self.children: Dict[str, 'TrieNode'] = {}
        self.token_ids: List[int] = []
        self.is_terminal = False


class GrammarTokenizerBridge:
    """
    Bridges grammar valid-strings to LLM BPE vocabulary via trie prefix matching.
    """

    def __init__(self, vocab: Dict[str, int]):
        self.trie = TrieNode()
        self.vocab = vocab
        self.id_to_str = {tid: ts for ts, tid in vocab.items()}
        self.identifier_ids: Set[int] = set()
        self.number_ids: Set[int] = set()
        self.string_ids: Set[int] = set()

        for token_str, tid in vocab.items():
            node = self.trie
            for char in token_str:
                if char not in node.children:
                    node.children[char] = TrieNode()
                node = node.children[char]
            node.token_ids.append(tid)
            node.is_terminal = True
            if token_str and (token_str[0].isalpha() or token_str[0] == '_'):
                self.identifier_ids.add(tid)
            elif token_str and token_str[0].isdigit():
                self.number_ids.add(tid)
            elif token_str and token_str[0] in ('"', "'"):
                self.string_ids.add(tid)

    def _collect_below(self, node: TrieNode) -> Set[int]:
        result = set(node.token_ids)
        stack = list(node.children.values())
        while stack:
            n = stack.pop()
            result.update(n.token_ids)
            stack.extend(n.children.values())
        return result

    def find_tokens_for_string(self, s: str) -> Set[int]:
        result: Set[int] = set()
        node = self.trie
        for char in s:
            if char not in node.children:
                break
            node = node.children[char]
            result.update(node.token_ids)
        else:
            result.update(self._collect_below(node))
        return result

    def get_valid_token_ids(self, valid_strings: Set[str]) -> Dict[int, float]:
        result: Dict[int, float] = {}
        has_id = '<IDENTIFIER>' in valid_strings
        has_num = '<NUMBER>' in valid_strings
        has_str = '<STRING>' in valid_strings

        for s in valid_strings:
            if s.startswith('<') and s.endswith('>'):
                if s == '<IDENTIFIER>' and has_id:
                    for tid in self.identifier_ids: result[tid] = 0.0
                elif s == '<NUMBER>' and has_num:
                    for tid in self.number_ids: result[tid] = 0.0
                elif s == '<STRING>' and has_str:
                    for tid in self.string_ids: result[tid] = 0.0
                continue
            for tid in self.find_tokens_for_string(s):
                result[tid] = 0.0
        return result


# ---------------------------------------------------------------------------
# Core grammar state machine
# ---------------------------------------------------------------------------

class DictumGrammar:
    """
    Grammar constraint engine.

    Usage with new modular parser:
        grammar = DictumGrammar(cpp_mode=True)
        lexer = Lexer(source)
        tokens = lexer.tokenize()
        parser = Parser(tokens, grammar=grammar)   # ← wired here
        ast = parser.parse()

    Token masking for LLM generation:
        mask = grammar.to_mask_dict(vocab)   # {token_id: 0.0} for allowed
    """

    KEYWORDS = {
        'program','module','shape','action','import','from','C',
        'keep','as','with','value','values','all','no','room','for',
        'put','into','set','to',                       # BUG-02: 'set','to'
        'if','then','otherwise','end',
        'while','repeat','for','each','in','times','using',
        'attempt','giving','on','success','failure',
        'return','produce','assert','print','the','text',
        'call','run','defer','release',
        'is','equal','to','not','greater','less','than','or','at','least','most','empty',
        'sum','difference','product','quotient','remainder','divided','by',
        'modulo','times','count','length','bitwise','and','or','left','right',
        'shift','of','tanh','true','false','nothing','newline',
        'holds','takes','produces','alone','holding','plus','minus',
        'unique','shared','weak','raw','handle','const','ref','move',
        'method','constructor','destructor','unsafe',  # NEW
        'public','private','protected',
        'new','virtual','override',
        'any','Type',
        'use','bind','extern','export',  # NEW: cross-module/FFI top-level
                                          # keywords (parse_use/parse_bind/
                                          # parse_extern/parse_export_statement
                                          # in parser.py) -- previously
                                          # absent from this set entirely,
                                          # so every generated GBNF grammar
                                          # silently lacked them even though
                                          # the parser has always accepted
                                          # real programs that use them.
        # FIX (keyword-vocabulary audit, same class of bug as TYPE_WORDS):
        # these 17 words are all live, currently-parseable keywords
        # (verified individually against parser.py, not just grepped) that
        # were entirely absent from this set. Math prefix-expression
        # keywords, `stop repeating`, and several declaration forms.
        'a', 'square', 'root', 'power', 'exponential', 'sine', 'cosine',
        'stop', 'repeating', 'define', 'extends', 'fn', 'item',
        'possibilities', 'syscall', 'taking', 'transmute',
    }

    # SOURCE OF TRUTH: derived from type_registry.py -- see that module's
    # docstring. This set used to require a manual addition here every
    # time a type word was added anywhere else (see the FIX comments
    # below, kept for history) -- exactly the kind of drift this registry
    # exists to prevent.
    # ROOT-CAUSE FIX: this used to be a bare `from .type_registry import
    # all_type_words` with no fallback -- the only import in this whole
    # codebase that skipped the relative-then-absolute pattern every
    # other cross-module import here uses (see chunk_grammar.py's own
    # `try: from . import type_registry / except ImportError: import
    # type_registry`). That mattered because chunkGrammar.js's real
    # spawn contract spawns `python3 <full-path>/chunk_grammar.py`
    # directly -- no `-m`, no package context -- which puts grammar.py
    # on sys.path as a bare top-level module named "grammar", not
    # "dictumc.grammar". A relative import at that point raises
    # "attempted relative import with no known parent package" --
    # confirmed live: `import grammar` (or `from .grammar import ...`)
    # from a standalone script in this same directory hit exactly that
    # exception at class-definition time, meaning DictumGrammar could
    # never fully define itself in the exact invocation mode production
    # actually uses. This silently broke every OTHER module's attempt to
    # introspect grammar.py at runtime too -- e.g. chunk_grammar.py's
    # own `_check_reserved_words_sync()` guard has been silently
    # no-op-ing (caught by its own except-ImportError-and-return) in
    # production this whole time, not just in a standalone test.
    try:
        from .type_registry import all_type_words
    except ImportError:
        from type_registry import all_type_words
    TYPE_WORDS = all_type_words()
    # Historical context (now automatic via type_registry.py):
    #   - 'unique'/'shared'/'weak'/'raw'/'pointer'/'opaque' were once
    #     missing here even though the parser always accepted them,
    #     silently breaking bridge.js's TYPE_WORDS scrape and every
    #     auto-generated .gbnf grammar's recognition of those forms.

    _VALID_TOKENS: Dict[GrammarState, FrozenSet[str]] = {}

    _EXPR_STATES = frozenset({
        GrammarState.KEEP_TYPE, GrammarState.KEEP_WITH,
        GrammarState.PUT_TARGET, GrammarState.EXPRESSION,
        GrammarState.COMPARISON, GrammarState.PREFIX_EXPR,
        GrammarState.CALL_ARGS, GrammarState.ACTION_RET,
        GrammarState.KEEP_INIT, GrammarState.IF_THEN,
        GrammarState.WHILE_REPEAT, GrammarState.FOR_EACH_REPEAT,
        GrammarState.REPEAT_USING, GrammarState.PREFIX_OF,
        GrammarState.ATTEMPT_GIVING, GrammarState.ATTEMPT_SUCCESS,
        GrammarState.ATTEMPT_FAILURE, GrammarState.IMPORT_AS,
        GrammarState.PRODUCE_FAILURE, GrammarState.PRODUCE_SUCCESS,
        GrammarState.SET_TARGET, GrammarState.SET_TO,
        GrammarState.TYPE_SMART_PTR, GrammarState.TYPE_REF,
        GrammarState.TYPE_RAW_PTR, GrammarState.METHOD_RET,
        GrammarState.CTOR_RET, GrammarState.DTOR_RET,
        GrammarState.NEW_WITH, GrammarState.CALL_WITH,
        GrammarState.CALL_RESULT, GrammarState.LAMBDA_RET,
        GrammarState.LAMBDA_BODY,
    })

    _BLOCK_STATES = frozenset({
        GrammarState.BLOCK_BODY, GrammarState.SHAPE_FIELDS,
        GrammarState.SHAPE_HOLDS, GrammarState.ACTION_PARAMS,
        GrammarState.METHOD_BODY, GrammarState.CTOR_BODY,
        GrammarState.DTOR_BODY, GrammarState.LAMBDA_BODY,
        GrammarState.UNSAFE_BODY,  # NEW
    })

    _NEWLINE_BLOCK_STATES = frozenset({
        GrammarState.IF_THEN, GrammarState.WHILE_REPEAT,
        GrammarState.FOR_EACH_REPEAT, GrammarState.REPEAT_USING,
        GrammarState.ATTEMPT_SUCCESS, GrammarState.ATTEMPT_FAILURE,
    })

    def __init__(self, cpp_mode: bool = False, strict: bool = False):
        self.state_stack: List[GrammarState] = [GrammarState.TOP_LEVEL]
        self.indent_stack: List[int] = [0]
        self.indent_level: int = 0
        self.block_depth: int = 0
        self.pending_expr_pop: bool = False
        self.tokenizer_bridge: Optional[GrammarTokenizerBridge] = None
        self.strict = strict
        self.cpp_mode = cpp_mode
        if not DictumGrammar._VALID_TOKENS:
            DictumGrammar._init_valid_tokens()

    @classmethod
    def _init_valid_tokens(cls) -> None:
        EXPR_START = frozenset({
            'the','<IDENTIFIER>','<NUMBER>','<STRING>','true','false',
            'nothing','empty','newline',
        })
        cls._VALID_TOKENS = {
            GrammarState.TOP_LEVEL: frozenset({'program','module','shape','action','import','end'}),
            GrammarState.PROGRAM_NAME: frozenset({'<IDENTIFIER>'}),
            GrammarState.MODULE_NAME: frozenset({'<IDENTIFIER>'}),
            GrammarState.BLOCK_BODY: frozenset({
                'keep','put','set','if','while','for','repeat','attempt',
                'return','produce','assert','print','call','run',
                'defer','release','shape','action','import','unsafe','end',  # NEW: unsafe
                '<IDENTIFIER>','<NUMBER>','<STRING>',
            }),
            GrammarState.KEEP_NAME: frozenset({'<IDENTIFIER>','as'}),
            GrammarState.KEEP_TYPE: frozenset(cls.TYPE_WORDS | {'<IDENTIFIER>','with','<NEWLINE>'}),
            GrammarState.KEEP_WITH: frozenset({'value','values','all','no','room','<NEWLINE>'}),
            GrammarState.KEEP_INIT: frozenset(EXPR_START | {'<NEWLINE>'}),
            GrammarState.PUT_VALUE: frozenset(EXPR_START | {'into'}),
            GrammarState.PUT_TARGET: frozenset({'<IDENTIFIER>','<NEWLINE>'}),
            GrammarState.SET_TARGET: frozenset({'<IDENTIFIER>'}),     # BUG-02
            GrammarState.SET_TO: frozenset(EXPR_START | {'<NEWLINE>'}),  # BUG-02
            GrammarState.IF_COND: frozenset(EXPR_START | {'then'}),
            GrammarState.IF_THEN: frozenset({'<NEWLINE>','<INDENT>'}),
            GrammarState.WHILE_COND: frozenset(EXPR_START | {'repeat'}),
            GrammarState.WHILE_REPEAT: frozenset({'<NEWLINE>','<INDENT>'}),
            GrammarState.FOR_EACH_ITEM: frozenset({'<IDENTIFIER>'}),
            GrammarState.FOR_EACH_COLL: frozenset({'<IDENTIFIER>'}),
            GrammarState.FOR_EACH_REPEAT: frozenset({'repeat','<NEWLINE>'}),
            GrammarState.REPEAT_COUNT: frozenset({'<NUMBER>','<IDENTIFIER>'}),
            GrammarState.REPEAT_TIMES: frozenset({'times'}),
            GrammarState.REPEAT_USING: frozenset({'using'}),
            GrammarState.ACTION_NAME: frozenset({'<IDENTIFIER>','takes','produces'}),
            GrammarState.ACTION_PARAMS: frozenset({'<IDENTIFIER>','as','produces','<NEWLINE>','<DEDENT>'}),
            GrammarState.ACTION_RET: frozenset(cls.TYPE_WORDS | {'<IDENTIFIER>','<NEWLINE>','<INDENT>'}),
            GrammarState.SHAPE_NAME: frozenset({'<IDENTIFIER>','holds'}),
            GrammarState.SHAPE_HOLDS: frozenset({'<NEWLINE>','<INDENT>'}),
            GrammarState.SHAPE_FIELDS: frozenset({'<IDENTIFIER>','as','end','<NEWLINE>','<DEDENT>'}),
            GrammarState.EXPRESSION: frozenset({
                'is','modulo','times','divided','and','or','plus','minus',
                'then','into','with','giving','end','otherwise','on','repeat',
                '<NEWLINE>','<DEDENT>',
            } | EXPR_START),
            GrammarState.COMPARISON: frozenset({
                'equal','not','greater','less','than','or','at','empty',
                'then','otherwise','repeat','<NEWLINE>','<DEDENT>',
            }),
            GrammarState.PREFIX_EXPR: frozenset({
                'sum','difference','product','quotient','remainder',
                'count','length','bitwise','left','right','tanh',
            }),
            GrammarState.PREFIX_OF: frozenset(EXPR_START | {'of','by'}),
            GrammarState.CALL_ARGS: frozenset(EXPR_START | {'and','<NEWLINE>','<DEDENT>'}),
            GrammarState.END_BLOCK_TYPE: frozenset({
                'program','module','shape','action','if','while',
                'for','repeat','attempt','unsafe','<NEWLINE>','<DEDENT>',  # NEW: unsafe
            }),
            GrammarState.ATTEMPT_CALL: frozenset(EXPR_START | {'giving','<NEWLINE>'}),
            GrammarState.ATTEMPT_GIVING: frozenset({'<IDENTIFIER>','<NEWLINE>'}),
            GrammarState.ATTEMPT_SUCCESS: frozenset({'<NEWLINE>','<INDENT>','on'}),
            GrammarState.ATTEMPT_FAILURE: frozenset({'<NEWLINE>','<INDENT>','end'}),
            GrammarState.IMPORT_C: frozenset({'from','<NEWLINE>'}),
            GrammarState.IMPORT_FROM: frozenset({'C','<NEWLINE>'}),
            GrammarState.IMPORT_THE: frozenset({'action','<NEWLINE>'}),
            GrammarState.IMPORT_ACTION: frozenset({'<IDENTIFIER>','<NEWLINE>'}),
            GrammarState.IMPORT_ACTION_NAME: frozenset({'takes','<NEWLINE>'}),
            GrammarState.IMPORT_TAKES: frozenset(cls.TYPE_WORDS | {'<IDENTIFIER>','produces','<NEWLINE>'}),
            GrammarState.IMPORT_PRODUCES: frozenset(cls.TYPE_WORDS | {'<IDENTIFIER>','as','<NEWLINE>'}),
            GrammarState.IMPORT_AS: frozenset({'<IDENTIFIER>','<NEWLINE>'}),
            GrammarState.DEFER_RELEASE: frozenset({'release','<NEWLINE>'}),
            GrammarState.PRODUCE_FAILURE: frozenset({'with','<NEWLINE>'}),
            GrammarState.PRODUCE_SUCCESS: frozenset(EXPR_START | {'<NEWLINE>'}),
            GrammarState.END_TOP_LEVEL: frozenset({
                'program','module','shape','action','if','while',
                'for','repeat','attempt','<NEWLINE>','<DEDENT>',
            }),
            GrammarState.UNSAFE_BODY: frozenset({
                # All normal Dictum statements valid in safe mode
                'keep','put','set','if','while','for','repeat','attempt',
                'return','produce','assert','print','call','run',
                'defer','release','end',
                '<IDENTIFIER>','<NUMBER>','<STRING>',
                # Special token openers — all begin with '['
                '[',
            }),
            GrammarState.UNSAFE_TOKEN: frozenset({
                # Inside [TOKEN:...] — allow anything until closing ]
                '<IDENTIFIER>','<NUMBER>','<STRING>',
                ':','.','-','>','[',']',
                'ATOMIC_LOAD','ATOMIC_STORE','ATOMIC_ADD','ATOMIC_SUB',
                'ATOMIC_AND','ATOMIC_OR','ATOMIC_XOR',
                'ATOMIC_CAS_32','ATOMIC_CAS_64','ATOMIC_CAS_PTR',
                'ATOMIC_FAA','ATOMIC_FAS',
                'BARRIER_ACQUIRE','BARRIER_RELEASE','BARRIER_SEQ_CST',
                'BARRIER_ACQ_REL','BARRIER_RELAXED','COMPILER_BARRIER',
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
                'PUN_INT_TO_FLOAT','PUN_FLOAT_TO_INT','PUN_PTR_TO_INT',
                'PUN_INT_TO_PTR','PUN_READ_UNALIGNED_16',
                'PUN_READ_UNALIGNED_32','PUN_READ_UNALIGNED_64',
                'FFI_LOAD','FFI_SYMBOL','FFI_CALL_VOID','FFI_CALL_INT',
                'FFI_CALL_FLOAT','FFI_CALL_PTR','FFI_CLOSE',
                'ALIGNED_ALLOC_16','ALIGNED_ALLOC_32','ALIGNED_ALLOC_64',
                'ALIGN_UP','ALIGN_DOWN','IS_ALIGNED',
                'BIT_SET','BIT_CLEAR','BIT_TOGGLE','BIT_TEST',
                'BIT_COUNT','BIT_REVERSE','BIT_SCAN_FORWARD','BIT_SCAN_REVERSE',
                'SWAP_ENDIAN_16','SWAP_ENDIAN_32','SWAP_ENDIAN_64',
                'HTON_16','HTON_32','HTON_64','NTOH_16','NTOH_32','NTOH_64',
            }),
            GrammarState.TYPE_SMART_PTR: frozenset({'<IDENTIFIER>'}),
            GrammarState.TYPE_REF: frozenset({'<IDENTIFIER>'}),
            GrammarState.TYPE_RAW_PTR: frozenset({'<IDENTIFIER>'}),
            GrammarState.METHOD_NAME: frozenset({'<IDENTIFIER>','takes','produces'}),
            GrammarState.METHOD_PARAMS: frozenset({'<IDENTIFIER>','as','produces','<NEWLINE>','<DEDENT>'}),
            GrammarState.METHOD_RET: frozenset(cls.TYPE_WORDS | {'<IDENTIFIER>','<NEWLINE>','<INDENT>'}),
            GrammarState.METHOD_BODY: frozenset({
                'keep','put','set','if','while','for','repeat','attempt',
                'return','produce','assert','print','call','run',
                'defer','release','end','<IDENTIFIER>','<NUMBER>','<STRING>',
            }),
            GrammarState.CTOR_PARAMS: frozenset({'<IDENTIFIER>','as','produces','<NEWLINE>','<DEDENT>'}),
            GrammarState.CTOR_RET: frozenset(cls.TYPE_WORDS | {'<IDENTIFIER>','<NEWLINE>','<INDENT>'}),
            GrammarState.CTOR_BODY: frozenset({
                'keep','put','set','if','while','for','repeat','attempt',
                'return','produce','assert','print','call','run',
                'defer','release','end','<IDENTIFIER>','<NUMBER>','<STRING>',
            }),
            GrammarState.DTOR_RET: frozenset(cls.TYPE_WORDS | {'<IDENTIFIER>','<NEWLINE>','<INDENT>'}),
            GrammarState.DTOR_BODY: frozenset({
                'keep','put','set','if','while','for','repeat','attempt',
                'return','produce','assert','print','call','run',
                'defer','release','end','<IDENTIFIER>','<NUMBER>','<STRING>',
            }),
            GrammarState.NEW_TYPE_NAME: frozenset({'<IDENTIFIER>','with','<NEWLINE>'}),
            GrammarState.NEW_WITH: frozenset(EXPR_START | {'<NEWLINE>','<DEDENT>'}),
            GrammarState.CALL_NAME: frozenset({'<IDENTIFIER>','of','with','giving','<NEWLINE>'}),
            GrammarState.CALL_OF: frozenset({'<IDENTIFIER>'}),
            GrammarState.CALL_OBJ: frozenset({'with','giving','<NEWLINE>'}),
            GrammarState.CALL_WITH: frozenset(EXPR_START | {'and','giving','<NEWLINE>','<DEDENT>'}),
            GrammarState.CALL_RESULT: frozenset({'<IDENTIFIER>','<NEWLINE>'}),
            GrammarState.LAMBDA_PARAMS: frozenset({'<IDENTIFIER>','as','produces','<NEWLINE>','<DEDENT>'}),
            GrammarState.LAMBDA_RET: frozenset(cls.TYPE_WORDS | {'<IDENTIFIER>','<NEWLINE>','<INDENT>'}),
            GrammarState.LAMBDA_BODY: frozenset({
                'keep','put','set','if','while','for','repeat','attempt',
                'return','produce','assert','print','call','run',
                'defer','release','end','<IDENTIFIER>','<NUMBER>','<STRING>',
            }),
            GrammarState.TEMPLATE_PARAM: frozenset({'<IDENTIFIER>'}),
            GrammarState.ACCESS_SPEC: frozenset({
                'public','private','protected','method','constructor','destructor',
                '<IDENTIFIER>','end','<NEWLINE>','<DEDENT>',
            }),
        }

    # ------------------------------------------------------------------
    # Stack
    # ------------------------------------------------------------------
    def push(self, state: GrammarState) -> None:
        self.state_stack.append(state)

    def pop(self) -> GrammarState:
        if len(self.state_stack) > 1:
            return self.state_stack.pop()
        return self.state_stack[-1]

    def current(self) -> GrammarState:
        return self.state_stack[-1]

    # ------------------------------------------------------------------
    # Checkpoint / rollback for speculative decoding
    # ------------------------------------------------------------------
    def checkpoint(self) -> GrammarCheckpoint:
        return GrammarCheckpoint(
            stack_snapshot=tuple(self.state_stack),
            indent_level=self.indent_level,
            block_depth=self.block_depth,
            pending_expr_pop=self.pending_expr_pop,
        )

    def rollback(self, cp: GrammarCheckpoint) -> None:
        self.state_stack = list(cp.stack_snapshot)
        self.indent_level = cp.indent_level
        self.block_depth = cp.block_depth
        self.pending_expr_pop = cp.pending_expr_pop

    # ------------------------------------------------------------------
    def get_valid_tokens(self, partial_source: str = "") -> Set[str]:
        state = self.current()
        tokens = set(DictumGrammar._VALID_TOKENS.get(state, frozenset()))
        tokens.update({'<NEWLINE>', '<INDENT>', '<DEDENT>'})
        return tokens

    # ------------------------------------------------------------------
    # Token feeding
    # ------------------------------------------------------------------
    def feed_token(self, token_text: str, token_type: str = "WORD",
                   strict: bool = False) -> bool:
        EXPR_CONTINUATORS = {
            'and','or','of','by','plus','minus','times','divided','modulo',
            'is','equal','to','not','greater','less','than','or','at','least','most',
            'empty','with','giving','as','takes','produces',',','.','to',
        }
        if self.pending_expr_pop and token_type != "INDENT":
            if token_text not in EXPR_CONTINUATORS:
                self._pop_expr_states()
            self.pending_expr_pop = False
        elif self.pending_expr_pop and token_type == "INDENT":
            self.pending_expr_pop = False

        state = self.current()

        # Structural tokens
        if token_type == "DEDENT":
            self._pop_expr_states()
            if state in self._BLOCK_STATES:
                if len(self.indent_stack) > 1:
                    self.indent_stack.pop()
                if self.current() in self._BLOCK_STATES:
                    self.pop()
                self.block_depth = max(0, self.block_depth - 1)
            return True

        if token_type == "INDENT":
            self.indent_level += 1
            self.indent_stack.append(self.indent_level)
            return True

        if token_type == "NEWLINE":
            if self.current() in self._EXPR_STATES:
                self.pending_expr_pop = True
            if state in self._NEWLINE_BLOCK_STATES:
                self.pop()
                self.push(GrammarState.BLOCK_BODY)
                self.block_depth += 1
            return True

        is_generic = token_type in ("NUMBER", "STRING", "IDENTIFIER")

        # Keyword/identifier ambiguity fix
        is_identifier_context = state in (
            GrammarState.KEEP_NAME, GrammarState.PUT_TARGET,
            GrammarState.SET_TARGET, GrammarState.FOR_EACH_ITEM,
            GrammarState.FOR_EACH_COLL, GrammarState.REPEAT_USING,
            GrammarState.ACTION_NAME, GrammarState.SHAPE_NAME,
            GrammarState.SHAPE_FIELDS, GrammarState.ACTION_PARAMS,
            GrammarState.ATTEMPT_GIVING, GrammarState.IMPORT_AS,
            GrammarState.IMPORT_ACTION_NAME, GrammarState.DEFER_RELEASE,
            GrammarState.METHOD_NAME, GrammarState.METHOD_PARAMS,
            GrammarState.CTOR_PARAMS, GrammarState.NEW_TYPE_NAME,
            GrammarState.CALL_NAME, GrammarState.CALL_OF,
            GrammarState.LAMBDA_PARAMS, GrammarState.TEMPLATE_PARAM,
        )
        expected = self._get_expected_keywords(state)
        if is_identifier_context and token_type == "WORD" and token_text in self.KEYWORDS:
            if token_text not in expected:
                is_generic = True

        # ── TOP LEVEL ──
        if state == GrammarState.TOP_LEVEL:
            if token_text == 'program':   self.push(GrammarState.PROGRAM_NAME)
            elif token_text == 'module':  self.push(GrammarState.MODULE_NAME)
            elif token_text == 'shape':   self.push(GrammarState.SHAPE_NAME)
            elif token_text == 'action':  self.push(GrammarState.ACTION_NAME)
            elif token_text == 'import':  self.push(GrammarState.IMPORT_C)
            elif token_text == 'end':     self.push(GrammarState.END_TOP_LEVEL)
            return True

        if state == GrammarState.PROGRAM_NAME:
            self.pop(); self.push(GrammarState.BLOCK_BODY); self.block_depth += 1
            return True

        if state == GrammarState.MODULE_NAME:
            self.pop(); self.push(GrammarState.BLOCK_BODY); self.block_depth += 1
            return True

        if state == GrammarState.END_TOP_LEVEL:
            if token_text in ('program','module','shape','action','if','while','for','repeat','attempt'):
                self.pop()
            return True

        if state == GrammarState.ACTION_NAME:
            if is_generic: return True
            if token_text == 'takes':    self.pop(); self.push(GrammarState.ACTION_PARAMS)
            elif token_text == 'produces': self.pop(); self.push(GrammarState.ACTION_RET)
            return True

        if state == GrammarState.ACTION_PARAMS:
            if token_text == 'produces': self.pop(); self.push(GrammarState.ACTION_RET)
            elif self.cpp_mode and token_text == 'any': self.push(GrammarState.TEMPLATE_PARAM)
            return True

        if state == GrammarState.ACTION_RET:
            if token_text in ('<NEWLINE>','<INDENT>') or token_type in ('NEWLINE','INDENT'):
                self.pop(); self.push(GrammarState.BLOCK_BODY); self.block_depth += 1
                return True
            if token_text == 'end': self.pop()
            return True

        if state == GrammarState.SHAPE_NAME:
            if is_generic: return True
            if token_text == 'holds': self.pop(); self.push(GrammarState.SHAPE_HOLDS)
            return True

        if state == GrammarState.SHAPE_HOLDS:
            if token_text == '<NEWLINE>':
                self.pop(); self.push(GrammarState.SHAPE_FIELDS)
            return True

        if state == GrammarState.SHAPE_FIELDS:
            if token_text == 'end': self.pop()
            elif self.cpp_mode and token_text == 'method':     self.push(GrammarState.METHOD_NAME)
            elif self.cpp_mode and token_text == 'constructor': self.push(GrammarState.CTOR_PARAMS)
            elif self.cpp_mode and token_text == 'destructor':  self.push(GrammarState.DTOR_RET)
            elif self.cpp_mode and token_text in ('public','private','protected'):
                self.push(GrammarState.ACCESS_SPEC)
            return True

        if state == GrammarState.KEEP_NAME:
            if is_generic: return True
            if token_text == 'as': self.pop(); self.push(GrammarState.KEEP_TYPE)
            return True

        if state == GrammarState.KEEP_TYPE:
            if token_text == 'with': self.pop(); self.push(GrammarState.KEEP_WITH)
            return True

        if state == GrammarState.KEEP_WITH:
            if token_text in ('value','values','all','no','room'):
                self.pop(); self.push(GrammarState.KEEP_INIT)
            return True

        if state == GrammarState.KEEP_INIT:
            if token_text == '<NEWLINE>': self.pop()
            return True

        # BUG-02: set X to
        if state == GrammarState.BLOCK_BODY and token_text == 'set':
            self.push(GrammarState.SET_TARGET); return True
        if state == GrammarState.SET_TARGET:
            # Any word that isn't a keyword is the variable name
            if is_generic or (token_type == "WORD" and token_text not in self.KEYWORDS):
                self.pop(); self.push(GrammarState.SET_TO)
            return True
        if state == GrammarState.SET_TO:
            if token_text == 'to': return True
            if token_text in ('<NEWLINE>','<DEDENT>'): self.pop()
            return True

        if state == GrammarState.PUT_VALUE:
            if token_text == 'into': self.pop(); self.push(GrammarState.PUT_TARGET)
            return True

        if state == GrammarState.PUT_TARGET:
            if is_generic: self.pop()
            return True

        if state == GrammarState.IF_COND:
            if token_text == 'then': self.pop(); self.push(GrammarState.IF_THEN)
            elif token_text == 'is': self.push(GrammarState.COMPARISON)
            elif token_text == 'the': self.push(GrammarState.PREFIX_EXPR)
            return True

        if state == GrammarState.WHILE_COND:
            if token_text == 'repeat': self.pop(); self.push(GrammarState.WHILE_REPEAT)
            elif token_text == 'is': self.push(GrammarState.COMPARISON)
            return True

        if state == GrammarState.FOR_EACH_ITEM:
            if is_generic: return True
            if token_text == 'in': self.pop(); self.push(GrammarState.FOR_EACH_COLL)
            return True

        if state == GrammarState.FOR_EACH_COLL:
            if is_generic: return True
            if token_text == 'repeat': self.pop(); self.push(GrammarState.FOR_EACH_REPEAT)
            return True

        if state == GrammarState.REPEAT_COUNT:
            if is_generic: return True
            if token_text == 'times': self.pop(); self.push(GrammarState.REPEAT_TIMES)
            return True

        if state == GrammarState.REPEAT_TIMES:
            if token_text == 'using': self.pop(); self.push(GrammarState.REPEAT_USING)
            return True

        if state == GrammarState.REPEAT_USING:
            if is_generic:
                self.pop(); self.push(GrammarState.BLOCK_BODY); self.block_depth += 1
            return True

        if state == GrammarState.COMPARISON:
            # BUG-03: accept 'or' in `less than or equal to`
            if token_text in ('equal','not','greater','less','than','or','at','empty'): return True
            if token_text in ('then','otherwise'): self.pop()
            return True

        if state == GrammarState.PREFIX_EXPR:
            if token_text in ('of','by','and'):
                self.pop(); self.push(GrammarState.EXPRESSION)
            return True

        if state == GrammarState.PREFIX_OF:
            if token_text == 'and': self.pop(); self.pop()
            return True

        if state == GrammarState.EXPRESSION:
            if token_text in ('then','into','with','giving','end','otherwise','on','repeat'):
                self.pop()
            elif token_text == 'the': self.push(GrammarState.PREFIX_EXPR)
            elif token_text == 'is': self.push(GrammarState.COMPARISON)
            return True

        if state == GrammarState.UNSAFE_BODY:
            if token_text == '[':
                self.push(GrammarState.UNSAFE_TOKEN)
            elif token_text == 'keep':   self.push(GrammarState.KEEP_NAME)
            elif token_text == 'put':    self.push(GrammarState.PUT_VALUE)
            elif token_text == 'set':    self.push(GrammarState.SET_TARGET)
            elif token_text == 'if':     self.push(GrammarState.IF_COND)
            elif token_text == 'while':  self.push(GrammarState.WHILE_COND)
            elif token_text == 'for':    self.push(GrammarState.FOR_EACH_ITEM)
            elif token_text == 'repeat': self.push(GrammarState.REPEAT_COUNT)
            elif token_text == 'attempt': self.push(GrammarState.ATTEMPT_CALL)
            elif token_text == 'end':
                self.pop(); self.push(GrammarState.END_BLOCK_TYPE)
            return True

        if state == GrammarState.UNSAFE_TOKEN:
            # Stay in token state until ']' closes it
            if token_text == ']':
                self.pop()
            return True

        if state == GrammarState.END_BLOCK_TYPE:
            if token_text in ('program','module','shape','action','if','while','for','repeat','attempt','unsafe'):  # NEW: unsafe
                self.pop()
                while self.current() in self._BLOCK_STATES:
                    self.pop()
                self.block_depth = max(0, self.block_depth - 1)
            return True

        if state == GrammarState.BLOCK_BODY:
            if token_text == 'keep':    self.push(GrammarState.KEEP_NAME)
            elif token_text == 'put':   self.push(GrammarState.PUT_VALUE)
            elif token_text == 'if':    self.push(GrammarState.IF_COND)
            elif token_text == 'while': self.push(GrammarState.WHILE_COND)
            elif token_text == 'for':   self.push(GrammarState.FOR_EACH_ITEM)
            elif token_text == 'repeat': self.push(GrammarState.REPEAT_COUNT)
            elif token_text == 'attempt': self.push(GrammarState.ATTEMPT_CALL)
            elif token_text == 'action': self.push(GrammarState.ACTION_NAME)
            elif token_text == 'shape':  self.push(GrammarState.SHAPE_NAME)
            elif token_text == 'end':
                self.pop(); self.push(GrammarState.END_BLOCK_TYPE)
            elif token_text == 'import': self.push(GrammarState.IMPORT_C)
            elif token_text == 'unsafe':  self.push(GrammarState.UNSAFE_BODY)  # NEW
            elif token_text in ('print','assert','return','produce','release','call','run'):
                self.push(GrammarState.EXPRESSION)
            return True

        if state == GrammarState.ATTEMPT_CALL:
            if token_text == 'giving':
                self.pop(); self.push(GrammarState.ATTEMPT_GIVING)
            elif token_text in ('<NEWLINE>','<INDENT>'):
                self.pop(); self.push(GrammarState.BLOCK_BODY); self.block_depth += 1
            return True

        if state == GrammarState.ATTEMPT_GIVING:
            if is_generic: self.pop(); self.push(GrammarState.ATTEMPT_SUCCESS)
            return True

        if state == GrammarState.IMPORT_C:
            if token_text == 'from': self.pop(); self.push(GrammarState.IMPORT_FROM)
            return True

        if state == GrammarState.IMPORT_FROM:
            if token_text == 'C': self.pop(); self.push(GrammarState.IMPORT_THE)
            return True

        if state == GrammarState.IMPORT_THE:
            if token_text == 'action': self.pop(); self.push(GrammarState.IMPORT_ACTION)
            return True

        if state == GrammarState.IMPORT_ACTION:
            if is_generic: self.pop(); self.push(GrammarState.IMPORT_ACTION_NAME)
            return True

        if state == GrammarState.IMPORT_ACTION_NAME:
            if token_text == 'takes': self.pop(); self.push(GrammarState.IMPORT_TAKES)
            return True

        if state == GrammarState.IMPORT_TAKES:
            if token_text == 'produces': self.pop(); self.push(GrammarState.IMPORT_PRODUCES)
            return True

        if state == GrammarState.IMPORT_PRODUCES:
            if token_text == 'as': self.pop(); self.push(GrammarState.IMPORT_AS)
            return True

        if state == GrammarState.IMPORT_AS:
            if is_generic: self.pop()
            return True

        if state == GrammarState.PRODUCE_FAILURE:
            if token_text == 'with': self.pop(); self.push(GrammarState.EXPRESSION)
            return True

        if state == GrammarState.PRODUCE_SUCCESS:
            if token_text in ('<NEWLINE>','<DEDENT>'): self.pop()
            return True

        # C++ states — pass-through; detailed transitions for main states only
        if state in (GrammarState.METHOD_NAME, GrammarState.METHOD_PARAMS,
                     GrammarState.METHOD_RET, GrammarState.METHOD_BODY,
                     GrammarState.CTOR_PARAMS, GrammarState.CTOR_RET, GrammarState.CTOR_BODY,
                     GrammarState.DTOR_RET, GrammarState.DTOR_BODY,
                     GrammarState.LAMBDA_PARAMS, GrammarState.LAMBDA_RET,
                     GrammarState.LAMBDA_BODY, GrammarState.TEMPLATE_PARAM,
                     GrammarState.ACCESS_SPEC, GrammarState.TYPE_SMART_PTR,
                     GrammarState.TYPE_REF, GrammarState.TYPE_RAW_PTR,
                     GrammarState.NEW_TYPE_NAME, GrammarState.NEW_WITH,
                     GrammarState.CALL_NAME, GrammarState.CALL_OF,
                     GrammarState.CALL_OBJ, GrammarState.CALL_WITH,
                     GrammarState.CALL_RESULT):
            return True

        if strict or self.strict:
            return False
        return True

    def _get_expected_keywords(self, state: GrammarState) -> Set[str]:
        mapping = {
            GrammarState.KEEP_NAME: {'as'},
            GrammarState.KEEP_TYPE: {'with'},
            GrammarState.KEEP_WITH: {'value','values','all','no','room'},
            GrammarState.PUT_VALUE: {'into'},
            GrammarState.SET_TARGET: {'to'},
            GrammarState.FOR_EACH_ITEM: {'in'},
            GrammarState.FOR_EACH_COLL: {'repeat'},
            GrammarState.REPEAT_COUNT: {'times'},
            GrammarState.REPEAT_TIMES: {'using'},
            GrammarState.ACTION_NAME: {'takes','produces'},
            GrammarState.ACTION_PARAMS: {'as','produces'},
            GrammarState.SHAPE_NAME: {'holds'},
            GrammarState.ATTEMPT_GIVING: set(),
            GrammarState.IMPORT_AS: set(),
            GrammarState.IMPORT_ACTION_NAME: {'takes'},
            GrammarState.IMPORT_TAKES: {'produces'},
            GrammarState.IMPORT_PRODUCES: {'as'},
        }
        return mapping.get(state, set())

    def _pop_expr_states(self) -> None:
        while self.current() in self._EXPR_STATES:
            self.pop()

    # ------------------------------------------------------------------
    # BPE / vocab integration
    # ------------------------------------------------------------------
    def build_tokenizer_bridge(self, vocab: Dict[str, int]) -> None:
        self.tokenizer_bridge = GrammarTokenizerBridge(vocab)

    def to_mask_dict(self, vocab: Dict[str, int]) -> Dict[int, float]:
        if self.tokenizer_bridge is None or self.tokenizer_bridge.vocab != vocab:
            self.build_tokenizer_bridge(vocab)
        return self.tokenizer_bridge.get_valid_token_ids(self.get_valid_tokens())


# ---------------------------------------------------------------------------
# Grammar-constrained generator
# ---------------------------------------------------------------------------

class GrammarConstrainedGenerator:
    """
    Wraps an LLM generation loop with grammar constraints AND can parse
    complete source files with grammar validation simultaneously.

    New in v4:
        parse_with_grammar(source) → List[Node]
            Tokenises, wires grammar to parser, returns full AST while
            keeping the grammar state machine in sync throughout.
    """

    def __init__(self, grammar: DictumGrammar, vocab: Dict[str, int]):
        self.grammar = grammar
        self.vocab = vocab
        self.bridge = GrammarTokenizerBridge(vocab)

    def get_next_token_mask(self) -> Dict[int, float]:
        valid = self.grammar.get_valid_tokens()
        return self.bridge.get_valid_token_ids(valid)

    def step(self, token_text: str, token_type: str) -> bool:
        return self.grammar.feed_token(token_text, token_type, strict=True)

    def speculative_branch(self) -> GrammarCheckpoint:
        return self.grammar.checkpoint()

    def revert(self, cp: GrammarCheckpoint) -> None:
        self.grammar.rollback(cp)

    def parse_with_grammar(self, source: str):
        """
        Parse source with grammar constraint checking wired in.
        Returns the AST (List[Node]).  Any grammar rejection in strict mode
        raises SyntaxError.
        """
        from .lexer import Lexer
        from .parser import Parser

        # Reset grammar state
        self.grammar.state_stack = [GrammarState.TOP_LEVEL]
        self.grammar.indent_stack = [0]
        self.grammar.indent_level = 0
        self.grammar.block_depth = 0
        self.grammar.pending_expr_pop = False

        lexer = Lexer(source)
        tokens = lexer.tokenize()
        parser = Parser(tokens, grammar=self.grammar)
        return parser.parse()


# ---------------------------------------------------------------------------
# Convenience: resync grammar from partial source
# ---------------------------------------------------------------------------

def resync_from_source(grammar: DictumGrammar, source: str) -> None:
    """Reset grammar and re-feed all tokens from partial source."""
    from .lexer import Lexer

    grammar.state_stack = [GrammarState.TOP_LEVEL]
    grammar.indent_stack = [0]
    grammar.indent_level = 0
    grammar.block_depth = 0
    grammar.pending_expr_pop = False

    lexer = Lexer(source)
    tokens = lexer.tokenize()
    from .lexer import TokenType
    for tok in tokens:
        ttype = tok.type.name if tok.type != TokenType.WORD else "WORD"
        grammar.feed_token(str(tok.value), ttype, strict=False)
