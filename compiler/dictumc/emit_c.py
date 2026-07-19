"""
Dictum C Emitter — emits ANSI C from the AST.
Extracted and heavily fixed from transpiler.py v3.3.

Phase 1-5 fixes applied:
  BUG-01:  `put ... into Z` auto-declares Z with inferred type if undeclared.
  BUG-04:  Module.function calls (Http.get) correctly routed via STDLIB_ACTION_FAMILIES.
  BUG-05:  `use Module` emits #include, never a function call.
  BUG-06:  `decimal number` / `decimal` mapped to `double`.
  BUG-09:  `produce success with X` emits `return X;` without noise comments.
  BUG-10:  Forward declarations emitted for ALL actions before main().
  MISSING-01: Arrays work: declaration, indexed read/write, `for each` emission.
  MISSING-02: `dictum_text` typedef added; Text.* ops wired to string helpers.
  MISSING-03: NewExpr emits malloc/calloc for C heap allocation.
  MISSING-05: `attempt` block emits complete C with setjmp/longjmp pattern.
  MISSING-09: truth value → bool; true/false consistent.
"""

from __future__ import annotations
import re
from typing import List, Dict, Optional, Set, Tuple, Any

from .ast_nodes import (
    Node, Program, Module, Shape, Method, Constructor, Destructor,
    VarDecl, Assignment, Action, FuncCall, Return, If, While, ForEach,
    Repeat, Attempt, Literal, Identifier, BinaryOp, UnaryOp,
    FieldAccess, IndexAccess, Assert, Print, ImportC, ImportCpp, ImportDict,
    UnsafeBlock, UnsafeToken, VerifyToken, ExternFn, Transmute, Use, Bind, NewExpr, LambdaExpr,
    Possibilities, HandleTypeDecl, Break,
)

# BUG-04 FIX: mapping of Module.function surface syntax → C function names
# Populated from STDLIB_ACTION_FAMILIES in the stdlib module.
_MODULE_CALL_MAP: Dict[str, str] = {
    "Text.grapheme_length":  "dictum_text_grapheme_length",
    "Text.grapheme_slice":   "dictum_text_grapheme_slice",
    "Text.grapheme_reverse": "dictum_text_grapheme_reverse",
    "Text.normalize":        "dictum_text_normalize",
    # Http (complete — HTTP + HTTPS auto-routing)
    "Http.get":              "dictum_http_get",
    "Http.post":             "dictum_http_post",
    "Http.post_form":        "dictum_http_post_form",
    "Http.put":              "dictum_http_put",
    "Http.delete":           "dictum_http_delete",
    "Http.patch":            "dictum_http_patch",
    # Console
    "Console.write":     "dictum_console_write",
    "Console.write_line":"dictum_console_write_line",
    "Console.read_line": "dictum_console_read_line",
    # Json
    "Json.parse":           "dictum_json_parse",
    "Json.get":             "dictum_json_get",
    "Json.get_string":      "dictum_json_get_string",
    "Json.get_int":         "dictum_json_get_int",
    "Json.get_float":       "dictum_json_get_float",
    "Json.get_bool":        "dictum_json_get_bool",
    "Json.set":             "dictum_json_set",
    "Json.stringify":       "dictum_json_stringify",
    "Json.destroy":         "dictum_json_destroy",
    "Json.length":          "dictum_json_length",
    "Json.array_length":    "dictum_json_array_length",
    "Json.get_at":          "dictum_json_get_at",
    "Json.get_int_at":      "dictum_json_get_int_at",
    "Json.get_float_at":    "dictum_json_get_float_at",
    "Json.get_object_at":   "dictum_json_get_object_at",
    "Json.get_path":        "dictum_json_get_path",
    # File
    "File.open":            "dictum_file_open",
    "File.read":            "dictum_file_read",
    "File.read_line":       "dictum_file_read_line",
    "File.read_all":        "dictum_file_read_all",
    "File.write":           "dictum_file_write",
    "File.seek":            "dictum_file_seek",
    "File.tell":            "dictum_file_tell",
    "File.flush":           "dictum_file_flush",
    "File.size":            "dictum_file_size",
    "File.exists":          "dictum_file_exists",
    "File.delete":          "dictum_file_delete",
    "File.append":          "dictum_file_append",
    "File.close":           "dictum_file_close",
    # Text module  (MISSING-02 + P1.6)
    "Text.length":          "dictum_text_length",
    "Text.utf8_length":     "dictum_text_utf8_length",
    "Text.find":            "dictum_text_find",
    "Text.find_from":       "dictum_text_find_from",
    "Text.slice":           "dictum_text_slice",
    "Text.join":            "dictum_text_join",
    "Text.split":           "dictum_text_split",
    "Text.trim":            "dictum_text_trim",
    "Text.to_upper":        "dictum_text_to_upper",
    "Text.to_lower":        "dictum_text_to_lower",
    "Text.replace":         "dictum_text_replace",
    "Text.compare":         "dictum_text_compare",
    "Text.starts_with":     "dictum_text_starts_with",
    "Text.ends_with":       "dictum_text_ends_with",
    "Text.contains":        "dictum_text_contains",
    "Text.format":          "dictum_text_format",
    "Text.from_int":        "dictum_text_from_int",
    "Text.from_float":      "dictum_text_from_float",
    # Legacy aliases — only kept for raw C passthrough; use Text.* for Dictum programs
    "Text.copy":            "strcpy",
    "Text.concat":          "strcat",
    # Net
    "Net.connect":       "dictum_net_connect",
    "Net.send":          "dictum_net_send",
    "Net.receive":       "dictum_net_receive",
    "Net.close":         "dictum_net_close",
    # Tls
    "Tls.wrap":          "dictum_tls_wrap",
}

# BUG-05 FIX: `use Module` → #include path mapping
_USE_INCLUDE_MAP: Dict[str, str] = {
    "Http":      "dictum_http.h",
    "Console":   "dictum_console.h",
    "Json":      "dictum_json.h",
    "File":      "dictum_file.h",
    "Net":       "dictum_net.h",
    "Tls":       "dictum_tls.h",
    "Text":      "dictum_text.h",
    "Thread":    "dictum_thread.h",
    "Mutex":     "dictum_mutex.h",
    "Channel":   "dictum_channel.h",
    "Semaphore": "dictum_semaphore.h",
    "Timer":     "dictum_timer.h",
    "Process":   "dictum_process.h",
    "Signal":    "dictum_signal.h",
    "Pipe":      "dictum_pipe.h",
    "Mmap":      "dictum_mmap.h",
    "Shm":       "dictum_shm.h",
    "Path":      "dictum_path.h",
    "Directory": "dictum_directory.h",
    "Device":    "dictum_device.h",
    "Csv":       "dictum_csv.h",
    "Event":     "dictum_event.h",
    "Math":      "math.h",
    "Io":        "stdio.h",
    # ── Sprint 3: blessed library bridges ────────────────────────────────
    "Glfw":      "GLFW/glfw3.h",
    "Sdl":       "SDL2/SDL.h",
    "Raylib":    "raylib.h",
    "Sqlite":    "sqlite3.h",
    "Ssl":       "openssl/evp.h",
}


# BUG-B FIX: C keywords and common stdlib names that cannot be used as function names.
# An action named `double`, `float`, `free`, etc. would generate invalid C.
_C_RESERVED: frozenset = frozenset({
    # C keywords (C11)
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if",
    "inline", "int", "long", "register", "restrict", "return", "short",
    "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
    "unsigned", "void", "volatile", "while",
    "_Alignas", "_Alignof", "_Atomic", "_Bool", "_Complex",
    "_Generic", "_Imaginary", "_Noreturn", "_Static_assert", "_Thread_local",
    # Common POSIX / libc names that cause hard-to-debug link errors
    "free", "exit", "abort", "printf", "fprintf", "sprintf", "snprintf",
    "malloc", "calloc", "realloc", "strlen", "strcpy", "strcat", "strcmp",
    "memcpy", "memset", "memmove", "read", "write", "open", "close",
    "main", "assert", "pow", "sqrt", "exp", "sin", "cos", "log",
})


def _sanitize_action_name(name: str) -> str:
    """Return a C-safe function name, prefixing with dictum_ if it collides."""
    if name in _C_RESERVED:
        return f"dictum_{name}"
    return name


# ── helper: convert ordering string to __ATOMIC_* constant ────────────────────
def _atomic_order(s: str) -> str:
    _MAP = {
        'relaxed':  '__ATOMIC_RELAXED',
        'acquire':  '__ATOMIC_ACQUIRE',
        'release':  '__ATOMIC_RELEASE',
        'acq_rel':  '__ATOMIC_ACQ_REL',
        'seq_cst':  '__ATOMIC_SEQ_CST',
    }
    return _MAP.get(s.lower(), '__ATOMIC_SEQ_CST')


class CEmitter:
    def __init__(self) -> None:
        self.output: List[str] = []
        self.indent: int = 0
        self._struct_buffer: List[str] = []
        self._action_buffer: List[str] = []
        self._extra_includes: List[str] = []     # BUG-05: `use` directives
        self._includes_emitted: bool = False
        self._fwd_sigs: List[str] = []           # BUG-10: forward declarations
        self._main_inits: List[str] = []          # room_for globals init in main
        # BUG-01 / MISSING-01: track declared variable types for auto-decl
        self.declared_vars: Dict[str, str] = {}
        self.shapes: Dict[str, Any] = {}         # name → fields list/dict
        # P2.1: track which stdlib modules are `use`d for Makefile generation
        self._used_modules: Set[str] = set()
        # FIX (link-flag plumbing bug): #[link "x"] / #[cflags ...] /
        # #[ldflags ...] / #[include_path ...] directives used to be emitted
        # ONLY as a human-readable comment in the generated C — nothing ever
        # read them back out to actually add -lx to the build. Collected
        # here as (kind, value) pairs so get_ldflags()/get_cflags() can
        # turn them into real flags for the Makefile / compile-check gate.
        self._build_directives: List[Tuple[str, str]] = []
        # Nominal handle types (`define handle Name`) collected from
        # anywhere in the source — top-level or inside a Program/Module
        # body — and spliced in right after the includes at output time,
        # so they're always declared before any code that references
        # them regardless of where `define handle` appears in source.
        self._handle_typedefs: Set[str] = set()

        # SOURCE OF TRUTH: derived from type_registry.py -- see that
        # module's docstring. Was previously an independent hand-typed
        # dict that had to be kept in sync with parser.py/validator.py/
        # emit_cpp.py/grammar.py by hand across 5 files; it wasn't.
        from .type_registry import c_type_map
        self.types: Dict[str, str] = c_type_map()
        self.actions: Set[str] = set()
        # FIX (silent handle-truncation bug): _infer_type_from_expr used to
        # return None for every FuncCall, no matter what the action's real
        # return type was, so `keep x with value call Mutex.create` (or the
        # `call ... giving x` form) always fell back to the "int32_t"
        # default at every call site that does `_infer_type_from_expr(...)
        # or "int32_t"`. That's silent pointer truncation on 64-bit
        # platforms for every handle-returning stdlib call (Mutex/Thread/
        # Net/...), and silently wrong for every text-returning one too.
        # Populated for user actions below, and for stdlib actions by
        # stdlib_registry.extend_emitter().
        self.action_return_types: Dict[str, str] = {}
        # BUGFIX (struct-return + `produce failure` compile error): `return 0;`
        # was hard-coded for every `produce failure` regardless of the
        # enclosing action's real C return type, which is a hard gcc type
        # error for any action that `produces` a shape (struct return type
        # can't be initialized from a bare `0`). Track the current action's
        # C return type here so the failure-return path can emit a value of
        # the correct type instead. See _zero_value_for_c_type() below and
        # its use in the Return/failure branch of emit_node().
        self.current_action_ret_c_type: str = "int32_t"
        # BUGFIX (missing dictum_error.h include for a top-level action):
        # _has_attempt_nodes/_has_produce_failure only ever inspected the
        # SINGLE node passed to emit_node -- but the driver loop
        # (transpiler.py) calls emit_node once per TOP-LEVEL AST node, so
        # a `produce failure` inside a top-level `action` (a sibling node,
        # not nested under the `program` node) was invisible to the gate
        # that decides whether to #include "dictum_error.h". Callers set
        # these two flags once, up front, from a scan across the WHOLE
        # top-level node list (see transpiler.py) -- default False so
        # anything that doesn't set them explicitly keeps prior behavior.
        self._file_has_produce_failure: bool = False
        self._file_has_attempt_nodes: bool = False
        self.current_module: Optional[str] = None
        self._module_actions: Dict[str, set] = {}   # FIX: module_name -> {action names declared in it}
        self._active_local_modules: set = set()     # FIX: modules brought into scope via `use <local module>`
        # Names of Module nodes defined at top level in this same source file.
        # `use <Name>` for one of these must NOT emit an #include — the module
        # is emitted inline in this translation unit. Populated by the
        # transpiler before emission begins.
        self.local_modules: Set[str] = set()

    # ------------------------------------------------------------------
    def _ptr_alloc_stmt(self, out: str, rhs_expr: str) -> str:
        if out in self.declared_vars:
            return f"{out} = {rhs_expr};"
        self.declared_vars[out] = "void*"
        return f"void *{out} = {rhs_expr};"

    def emit(self, line: str) -> None:
        self.output.append("    " * self.indent + line)


    # ═══════════════════════════════════════════════════════════════════════
    # _emit_unsafe_token
    # Expands [TOKEN_NAME: p1 : p2 : result] to verified C intrinsics.
    # All expansions are minimal, correct, and header-free (caller must
    # #include <stdatomic.h> / <immintrin.h> etc via `use` declarations).
    # ═══════════════════════════════════════════════════════════════════════
    def _emit_unsafe_token(self, node: "UnsafeToken") -> None:
        n   = node.name
        p   = node.params      # list of raw param strings
        res = node.result      # last param (result var, if any)

        def pa(i, default='0'):
            return p[i] if i < len(p) else default

        # ── ATOMIC OPS ──────────────────────────────────────────────────────
        if n == 'ATOMIC_LOAD':
            # [ATOMIC_LOAD: ptr : ordering : result]
            ptr, order, out = pa(0), pa(1,'seq_cst'), pa(2)
            mo = _atomic_order(order)
            self.emit(f"__atomic_load({ptr}, &{out}, {mo});")

        elif n == 'ATOMIC_STORE':
            # [ATOMIC_STORE: ptr : ordering : value]
            ptr, order, val = pa(0), pa(1,'seq_cst'), pa(2)
            mo = _atomic_order(order)
            self.emit(f"__atomic_store({ptr}, &{val}, {mo});")

        elif n == 'ATOMIC_ADD':
            # [ATOMIC_ADD: ptr : val : result]
            ptr, val, out = pa(0), pa(1,'1'), pa(2)
            self.emit(f"{out} = __atomic_fetch_add({ptr}, {val}, __ATOMIC_SEQ_CST);")

        elif n == 'ATOMIC_SUB':
            ptr, val, out = pa(0), pa(1,'1'), pa(2)
            self.emit(f"{out} = __atomic_fetch_sub({ptr}, {val}, __ATOMIC_SEQ_CST);")

        elif n == 'ATOMIC_AND':
            ptr, val, out = pa(0), pa(1), pa(2)
            self.emit(f"{out} = __atomic_fetch_and({ptr}, {val}, __ATOMIC_SEQ_CST);")

        elif n == 'ATOMIC_OR':
            ptr, val, out = pa(0), pa(1), pa(2)
            self.emit(f"{out} = __atomic_fetch_or({ptr}, {val}, __ATOMIC_SEQ_CST);")

        elif n == 'ATOMIC_XOR':
            ptr, val, out = pa(0), pa(1), pa(2)
            self.emit(f"{out} = __atomic_fetch_xor({ptr}, {val}, __ATOMIC_SEQ_CST);")

        elif n == 'ATOMIC_FAA':
            # [ATOMIC_FAA: ptr : addend : result]  — fetch-and-add
            ptr, val, out = pa(0), pa(1,'1'), pa(2)
            self.emit(f"{out} = __atomic_fetch_add({ptr}, {val}, __ATOMIC_RELAXED);")

        elif n == 'ATOMIC_FAS':
            # [ATOMIC_FAS: ptr : new_val : result]  — fetch-and-store
            ptr, val, out = pa(0), pa(1), pa(2)
            self.emit(f"__atomic_exchange({ptr}, &{val}, &{out}, __ATOMIC_SEQ_CST);")

        elif n in ('ATOMIC_CAS_32', 'ATOMIC_CAS_64', 'ATOMIC_CAS_PTR'):
            # [ATOMIC_CAS_64: ptr : expected : desired : success_var]
            ptr, exp, des, out = pa(0), pa(1), pa(2), pa(3)
            self.emit(f"{out} = __atomic_compare_exchange_n({ptr}, &{exp}, {des},")
            self.emit(f"    0, __ATOMIC_SEQ_CST, __ATOMIC_RELAXED);")

        # ── BARRIERS ────────────────────────────────────────────────────────
        elif n == 'BARRIER_ACQUIRE':
            self.emit("__atomic_thread_fence(__ATOMIC_ACQUIRE);")
        elif n == 'BARRIER_RELEASE':
            self.emit("__atomic_thread_fence(__ATOMIC_RELEASE);")
        elif n == 'BARRIER_SEQ_CST':
            self.emit("__atomic_thread_fence(__ATOMIC_SEQ_CST);")
        elif n == 'BARRIER_ACQ_REL':
            self.emit("__atomic_thread_fence(__ATOMIC_ACQ_REL);")
        elif n == 'BARRIER_RELAXED':
            self.emit("__atomic_thread_fence(__ATOMIC_RELAXED);")
        elif n == 'COMPILER_BARRIER':
            self.emit('__asm__ __volatile__("" ::: "memory");')

        # ── CAS LOOPS ───────────────────────────────────────────────────────
        elif n in ('CAS_LOOP_32', 'CAS_LOOP_64', 'CAS_LOOP_PTR'):
            # [CAS_LOOP_64: ptr : expected : desired : success_var]
            ptr, exp, des, out = pa(0), pa(1), pa(2), pa(3)
            self.emit(f"{{")
            self.emit(f"  __typeof__(*{ptr}) _cas_exp = {exp};")
            self.emit(f"  {out} = 0;")
            self.emit(f"  while (!{out}) {{")
            self.emit(f"    {out} = __atomic_compare_exchange_n({ptr}, &_cas_exp, {des},")
            self.emit(f"        0, __ATOMIC_SEQ_CST, __ATOMIC_RELAXED);")
            self.emit(f"  }}")
            self.emit(f"}}")

        elif n == 'DCAS_LOOP_128':
            # Double-width CAS (x86 CMPXCHG16B) — needs -mcx16
            ptr, exp, des, out = pa(0), pa(1), pa(2), pa(3)
            self.emit(f"/* DCAS_LOOP_128: requires -mcx16 and __int128 support */")
            self.emit(f"{{")
            self.emit(f"  __int128 _dcas_exp = (__int128){exp};")
            self.emit(f"  {out} = __atomic_compare_exchange_n((__int128 *){ptr}, &_dcas_exp,")
            self.emit(f"      (__int128){des}, 0, __ATOMIC_SEQ_CST, __ATOMIC_RELAXED);")
            self.emit(f"}}")

        # ── HAZARD POINTERS ─────────────────────────────────────────────────
        elif n == 'HP_PROTECT':
            # [HP_PROTECT: hp_slot_ptr : protected_ptr]
            hp, ptr = pa(0), pa(1)
            self.emit(f"__atomic_store_n(&{hp}, {ptr}, __ATOMIC_RELEASE);")

        elif n == 'HP_READ':
            # [HP_READ: hp_slot_ptr : src_ptr : result]
            hp, src, out = pa(0), pa(1), pa(2)
            self.emit(f"{out} = __atomic_load_n({src}, __ATOMIC_ACQUIRE);")
            self.emit(f"__atomic_store_n(&{hp}, {out}, __ATOMIC_RELEASE);")

        elif n == 'HP_CLEAR':
            # [HP_CLEAR: hp_slot_ptr : ptr]
            hp = pa(0)
            self.emit(f"__atomic_store_n(&{hp}, NULL, __ATOMIC_RELEASE);")

        elif n == 'HP_RETIRE':
            # [HP_RETIRE: hp_table : ptr]  — add ptr to retire list
            table, ptr = pa(0), pa(1)
            self.emit(f"/* HP_RETIRE: add {ptr} to {table}.retired[] */")
            self.emit(f"if ({table}.retired_count < HP_MAX_RETIRED)")
            self.emit(f"    {table}.retired[{table}.retired_count++] = {ptr};")

        elif n == 'HP_SCAN':
            # [HP_SCAN: hp_table]  — reclaim non-protected retired pointers
            table = pa(0)
            self.emit(f"/* HP_SCAN: reclaim safe retired pointers in {table} */")
            self.emit(f"for (int _i = 0; _i < {table}.retired_count; ) {{")
            self.emit(f"    int _protected = 0;")
            self.emit(f"    for (int _j = 0; _j < HP_MAX_THREADS && !_protected; _j++)")
            self.emit(f"        if ({table}.slots[_j] == {table}.retired[_i]) _protected = 1;")
            self.emit(f"    if (!_protected) {{ free({table}.retired[_i]);")
            self.emit(f"        {table}.retired[_i] = {table}.retired[--{table}.retired_count]; }}")
            self.emit(f"    else _i++;")
            self.emit(f"}}")

        # ── RCU ─────────────────────────────────────────────────────────────
        elif n == 'RCU_READ_LOCK':
            self.emit("/* rcu_read_lock() */")
            self.emit("__atomic_thread_fence(__ATOMIC_ACQUIRE);")
        elif n == 'RCU_READ_UNLOCK':
            self.emit("/* rcu_read_unlock() */")
            self.emit("__atomic_thread_fence(__ATOMIC_RELEASE);")
        elif n == 'RCU_SYNCHRONIZE':
            self.emit("/* synchronize_rcu() — full barrier, all readers must finish */")
            self.emit("__atomic_thread_fence(__ATOMIC_SEQ_CST);")
        elif n == 'RCU_ASSIGN_POINTER':
            # [RCU_ASSIGN_POINTER: ptr : new_val]
            ptr, val = pa(0), pa(1)
            self.emit(f"__atomic_store_n(&{ptr}, {val}, __ATOMIC_RELEASE);")
        elif n == 'RCU_DEREFERENCE':
            # [RCU_DEREFERENCE: src_ptr : result]
            src, out = pa(0), pa(1)
            self.emit(f"{out} = __atomic_load_n(&{src}, __ATOMIC_ACQUIRE);")

        # ── SIMD (AVX2 / SSE2) ──────────────────────────────────────────────
        elif n == 'SIMD_LOAD_F32':
            # [SIMD_LOAD_F32: ptr : reg]  — aligned load
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256 {reg} = _mm256_load_ps((float *){ptr});")
        elif n == 'SIMD_LOADU_F32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256 {reg} = _mm256_loadu_ps((float *){ptr});")
        elif n == 'SIMD_LOAD_I32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256i {reg} = _mm256_load_si256((__m256i *){ptr});")
        elif n == 'SIMD_LOADU_I32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256i {reg} = _mm256_loadu_si256((__m256i *){ptr});")
        elif n == 'SIMD_LOAD_F64':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256d {reg} = _mm256_load_pd((double *){ptr});")
        elif n == 'SIMD_LOADU_F64':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256d {reg} = _mm256_loadu_pd((double *){ptr});")
        elif n == 'SIMD_LOAD_I64':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256i {reg} = _mm256_load_si256((__m256i *){ptr});")
        elif n == 'SIMD_LOADU_I64':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256i {reg} = _mm256_loadu_si256((__m256i *){ptr});")

        elif n == 'SIMD_STORE_F32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"_mm256_store_ps((float *){ptr}, {reg});")
        elif n == 'SIMD_STOREU_F32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"_mm256_storeu_ps((float *){ptr}, {reg});")
        elif n == 'SIMD_STORE_I32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"_mm256_store_si256((__m256i *){ptr}, {reg});")
        elif n == 'SIMD_STOREU_I32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"_mm256_storeu_si256((__m256i *){ptr}, {reg});")

        elif n == 'SIMD_ADD_F32':
            a, b, out = pa(0), pa(1), pa(2)
            self.emit(f"__m256 {out} = _mm256_add_ps({a}, {b});")
        elif n == 'SIMD_SUB_F32':
            a, b, out = pa(0), pa(1), pa(2)
            self.emit(f"__m256 {out} = _mm256_sub_ps({a}, {b});")
        elif n == 'SIMD_MUL_F32':
            a, b, out = pa(0), pa(1), pa(2)
            self.emit(f"__m256 {out} = _mm256_mul_ps({a}, {b});")
        elif n == 'SIMD_DIV_F32':
            a, b, out = pa(0), pa(1), pa(2)
            self.emit(f"__m256 {out} = _mm256_div_ps({a}, {b});")
        elif n == 'SIMD_SQRT_F32':
            a, out = pa(0), pa(1)
            self.emit(f"__m256 {out} = _mm256_sqrt_ps({a});")
        elif n == 'SIMD_FMA_F32':
            # [SIMD_FMA_F32: a : b : c : result]  — (a*b)+c
            a, b, c, out = pa(0), pa(1), pa(2), pa(3)
            self.emit(f"__m256 {out} = _mm256_fmadd_ps({a}, {b}, {c});")
        elif n == 'SIMD_MIN_F32':
            a, b, out = pa(0), pa(1), pa(2)
            self.emit(f"__m256 {out} = _mm256_min_ps({a}, {b});")
        elif n == 'SIMD_MAX_F32':
            a, b, out = pa(0), pa(1), pa(2)
            self.emit(f"__m256 {out} = _mm256_max_ps({a}, {b});")
        elif n == 'SIMD_BROADCAST_F32':
            val, out = pa(0), pa(1)
            self.emit(f"__m256 {out} = _mm256_set1_ps({val});")
        elif n == 'SIMD_SHUFFLE_F32':
            a, imm, out = pa(0), pa(1), pa(2)
            self.emit(f"__m256 {out} = _mm256_permute_ps({a}, {imm});")
        elif n == 'SIMD_BLEND_F32':
            a, b, mask, out = pa(0), pa(1), pa(2), pa(3)
            self.emit(f"__m256 {out} = _mm256_blend_ps({a}, {b}, {mask});")

        # ── ALIGNMENT ───────────────────────────────────────────────────────
        elif n == 'IS_ALIGNED':
            # [IS_ALIGNED: ptr : alignment : result]
            ptr, align, out = pa(0), pa(1), pa(2)
            self.emit(f"int {out} = (((uintptr_t){ptr}) & ({align} - 1)) == 0;")
        elif n == 'ALIGNED_ALLOC_16':
            size, out = pa(0), pa(1)
            self.emit(self._ptr_alloc_stmt(out, f"aligned_alloc(16, {size})"))
        elif n == 'ALIGNED_ALLOC_32':
            size, out = pa(0), pa(1)
            self.emit(self._ptr_alloc_stmt(out, f"aligned_alloc(32, {size})"))
        elif n == 'ALIGNED_ALLOC_64':
            size, out = pa(0), pa(1)
            self.emit(self._ptr_alloc_stmt(out, f"aligned_alloc(64, {size})"))
        elif n == 'ALIGN_UP':
            val, align, out = pa(0), pa(1), pa(2)
            self.emit(f"uintptr_t {out} = ({val} + {align} - 1) & ~((uintptr_t){align} - 1);")
        elif n == 'ALIGN_DOWN':
            val, align, out = pa(0), pa(1), pa(2)
            self.emit(f"uintptr_t {out} = {val} & ~((uintptr_t){align} - 1);")

        # ── RAW MEMORY ──────────────────────────────────────────────────────
        elif n == 'RAW_MALLOC':
            size, out = pa(0), pa(1)
            self.emit(self._ptr_alloc_stmt(out, f"malloc({size})"))
        elif n == 'RAW_FREE':
            ptr = pa(0)
            self.emit(f"free({ptr});")
        elif n == 'RAW_CALLOC':
            nmemb, size, out = pa(0), pa(1), pa(2)
            self.emit(self._ptr_alloc_stmt(out, f"calloc({nmemb}, {size})"))
        elif n == 'RAW_REALLOC':
            ptr, size, out = pa(0), pa(1), pa(2)
            self.emit(self._ptr_alloc_stmt(out, f"realloc({ptr}, {size})"))
        elif n == 'RAW_MEMCPY':
            dst, src, size = pa(0), pa(1), pa(2)
            self.emit(f"memcpy({dst}, {src}, {size});")
        elif n == 'RAW_MEMSET':
            dst, val, size = pa(0), pa(1), pa(2)
            self.emit(f"memset({dst}, {val}, {size});")
        elif n == 'RAW_MEMCMP':
            a, b, size, out = pa(0), pa(1), pa(2), pa(3)
            self.emit(f"int {out} = memcmp({a}, {b}, {size});")
        elif n == 'RAW_MEMMOVE':
            dst, src, size = pa(0), pa(1), pa(2)
            self.emit(f"memmove({dst}, {src}, {size});")

        # ── FFI ─────────────────────────────────────────────────────────────
        elif n == 'FFI_LOAD':
            path, handle = pa(0), pa(1)
            self.emit(f"void *{handle} = dlopen({path}, RTLD_LAZY);")
        elif n == 'FFI_SYMBOL':
            handle, sym, fptr = pa(0), pa(1), pa(2)
            self.emit(f"void *{fptr} = dlsym({handle}, {sym});")
        elif n == 'FFI_CALL_VOID':
            fptr, args = pa(0), ', '.join(p[1:])
            self.emit(f"((void (*)())(uintptr_t){fptr})({args});")
        elif n == 'FFI_CALL_INT':
            fptr = pa(0); args = ', '.join(p[1:-1]); out = pa(-1)
            self.emit(f"int {out} = ((int (*)())(uintptr_t){fptr})({args});")
        elif n == 'FFI_CALL_FLOAT':
            fptr = pa(0); args = ', '.join(p[1:-1]); out = pa(-1)
            self.emit(f"double {out} = ((double (*)())(uintptr_t){fptr})({args});")
        elif n == 'FFI_CALL_PTR':
            fptr = pa(0); args = ', '.join(p[1:-1]); out = pa(-1)
            self.emit(f"void *{out} = ((void *(*)())(uintptr_t){fptr})({args});")
        elif n == 'FFI_CLOSE':
            handle = pa(0)
            self.emit(f"dlclose({handle});")

        # ── BIT MANIPULATION ────────────────────────────────────────────────
        elif n == 'BIT_SET':
            val, bit = pa(0), pa(1)
            self.emit(f"{val} |= (1ULL << {bit});")
        elif n == 'BIT_CLEAR':
            val, bit = pa(0), pa(1)
            self.emit(f"{val} &= ~(1ULL << {bit});")
        elif n == 'BIT_TOGGLE':
            val, bit = pa(0), pa(1)
            self.emit(f"{val} ^= (1ULL << {bit});")
        elif n == 'BIT_TEST':
            val, bit, out = pa(0), pa(1), pa(2)
            self.emit(f"int {out} = ({val} >> {bit}) & 1;")
        elif n == 'BIT_COUNT':
            val, out = pa(0), pa(1)
            self.emit(f"int {out} = __builtin_popcountll({val});")
        elif n == 'BIT_REVERSE':
            val, out = pa(0), pa(1)
            self.emit(f"uint64_t {out} = __builtin_bitreverse64({val});")
        elif n == 'BIT_SCAN_FORWARD':
            val, out = pa(0), pa(1)
            self.emit(f"int {out} = __builtin_ctzll({val});")
        elif n == 'BIT_SCAN_REVERSE':
            val, out = pa(0), pa(1)
            self.emit(f"int {out} = 63 - __builtin_clzll({val});")

        # ── ENDIANNESS ──────────────────────────────────────────────────────
        elif n == 'SWAP_ENDIAN_16':
            val, out = pa(0), pa(1)
            self.emit(f"uint16_t {out} = __builtin_bswap16({val});")
        elif n == 'SWAP_ENDIAN_32':
            val, out = pa(0), pa(1)
            self.emit(f"uint32_t {out} = __builtin_bswap32({val});")
        elif n == 'SWAP_ENDIAN_64':
            val, out = pa(0), pa(1)
            self.emit(f"uint64_t {out} = __builtin_bswap64({val});")
        elif n in ('HTON_16', 'NTOH_16'):
            val, out = pa(0), pa(1)
            self.emit(f"uint16_t {out} = __builtin_bswap16({val});")
        elif n in ('HTON_32', 'NTOH_32'):
            val, out = pa(0), pa(1)
            self.emit(f"uint32_t {out} = __builtin_bswap32({val});")
        elif n in ('HTON_64', 'NTOH_64'):
            val, out = pa(0), pa(1)
            self.emit(f"uint64_t {out} = __builtin_bswap64({val});")

        # ── TYPE PUNNING ────────────────────────────────────────────────────
        elif n == 'PUN_INT_TO_FLOAT':
            val, out = pa(0), pa(1)
            self.emit(f"float {out}; memcpy(&{out}, &{val}, sizeof(float));")
        elif n == 'PUN_FLOAT_TO_INT':
            val, out = pa(0), pa(1)
            self.emit(f"uint32_t {out}; memcpy(&{out}, &{val}, sizeof(uint32_t));")
        elif n == 'PUN_PTR_TO_INT':
            ptr, out = pa(0), pa(1)
            self.emit(f"uintptr_t {out} = (uintptr_t){ptr};")
        elif n == 'PUN_INT_TO_PTR':
            val, out = pa(0), pa(1)
            self.emit(f"void *{out} = (void *)(uintptr_t){val};")
        elif n == 'PUN_READ_UNALIGNED_16':
            ptr, out = pa(0), pa(1)
            self.emit(f"uint16_t {out}; memcpy(&{out}, {ptr}, 2);")
        elif n == 'PUN_READ_UNALIGNED_32':
            ptr, out = pa(0), pa(1)
            self.emit(f"uint32_t {out}; memcpy(&{out}, {ptr}, 4);")
        elif n == 'PUN_READ_UNALIGNED_64':
            ptr, out = pa(0), pa(1)
            self.emit(f"uint64_t {out}; memcpy(&{out}, {ptr}, 8);")

        # ── UNKNOWN TOKEN — emit as comment so compiler catches it ──────────
        else:
            params_str = " : ".join(node.params)
            self.emit(f"/* UNKNOWN_UNSAFE_TOKEN: [{n}: {params_str}] */")


    def _is_list_type(self, t: str) -> bool:
        # BUG-FIX: the suffix form ('TYPE list'/'TYPE array') was previously
        # the only form detected as an array type. The prefix form
        # ('list of TYPE'/'array of TYPE'), which is what Dictum source
        # actually uses throughout (shape fields, `keep X as list of ...`),
        # was falling through to a plain scalar C type — silently truncating
        # every list-typed field/variable to a scalar and breaking any
        # `item N of X` indexing on it.
        return (t.startswith('list of ') or t.startswith('array of ')
                or t.endswith(' list') or t.endswith(' array'))

    # Wrapper-pointer prefixes that the parser can produce (see parser.py
    # smart-pointer wrapper handling). C has no smart-pointer types, so all
    # of these lower to a plain C pointer to the inner type — that's the
    # correct C-backend codegen for them (ownership semantics are a
    # C++-only / source-level concept and are enforced earlier, not by the
    # C type system).
    _WRAPPER_PREFIXES = ('unique', 'shared', 'weak', 'raw')

    def type_to_c(self, t: str) -> str:
        if t.startswith('*'):
            rest = t[1:].strip()
            if rest.startswith('volatile'):
                inner = rest[8:].strip()
                return f"volatile {self.types.get(inner, inner.replace(' ', '_'))}*"
            return f"{self.types.get(rest, rest.replace(' ', '_'))}*"

        # `<wrapper> handle to <inner>` / `<wrapper> pointer to <inner>` /
        # bare `<wrapper> pointer` (no inner type given, MISSING-01-style)
        for prefix in self._WRAPPER_PREFIXES:
            handle_pfx = f"{prefix} handle to "
            ptr_pfx = f"{prefix} pointer to "
            if t.startswith(handle_pfx):
                inner = t[len(handle_pfx):].strip()
                return f"{self.type_to_c(inner)}*"
            if t.startswith(ptr_pfx):
                inner = t[len(ptr_pfx):].strip()
                return f"{self.type_to_c(inner)}*"
            if t == f"{prefix} pointer" or t == f"{prefix} handle":
                # No inner type specified — fall back to an opaque pointer
                # rather than emitting a mangled identifier as a type name.
                return "void*"

        # Strip 'list of <type>' prefix form (MISSING-01 FIX)
        if t.startswith('list of '):
            t = t[len('list of '):].strip()
        elif t.startswith('array of '):
            t = t[len('array of '):].strip()
        # Strip trailing ' list' / ' array' suffix form
        for suffix in (' list', ' array'):
            if t.endswith(suffix):
                t = t[:-len(suffix)].strip()
                break
        return self.types.get(t, t.replace(" ", "_"))

    # ------------------------------------------------------------------
    # BUGFIX (struct-return + `produce failure` compile error): given a C
    # type string (as returned by type_to_c), produce a syntactically valid
    # "nothing happened, this is the failure path" return expression for
    # that exact type -- 0 for numeric types, NULL for pointer/text types,
    # false for bool, no value at all for void, and a zero-initialized
    # compound literal for anything else (struct/shape return types).
    # Previously every failure path hard-coded `return 0;`, which is a
    # hard gcc type error for any action that `produces` a shape.
    # ------------------------------------------------------------------
    def _zero_value_for_c_type(self, c_type: str) -> Optional[str]:
        c_type = c_type.strip()
        if c_type == "void":
            return None  # caller must emit a bare `return;`
        if c_type == "bool":
            return "false"
        if c_type.endswith("*"):
            return "NULL"
        if c_type in ("dictum_text", "const char*", "char*"):
            return "NULL"
        numeric_types = {
            "int8_t", "uint8_t", "int16_t", "uint16_t", "int32_t", "uint32_t",
            "int64_t", "uint64_t", "size_t", "float", "double",
        }
        if c_type in numeric_types:
            return "0"
        # Anything else is assumed to be a user-defined shape/struct type --
        # zero-initialize every member via a compound literal.
        return f"({c_type}){{0}}"

    # ------------------------------------------------------------------
    # BUG-04 FIX: resolve Module.function names
    # ------------------------------------------------------------------
    def _resolve_call_name(self, name: str) -> str:
        if '.' in name:
            return _MODULE_CALL_MAP.get(name, name.replace('.', '_'))
        # BUG-B FIX: sanitize user-defined action names at call sites too
        safe_name = _sanitize_action_name(name)
        if self.current_module and name in self._module_actions.get(self.current_module, ()):
            # FIX: unqualified sibling-action call within the same module —
            # match the Module_action mangling used at the definition site.
            return f"{self.current_module}_{safe_name}"
        for mod in self._active_local_modules:
            if name in self._module_actions.get(mod, ()):
                # FIX: unqualified call to an action exported by a module
                # brought into scope via `use <module>` (from OUTSIDE that
                # module, e.g. from `program Main:`) -- same mangling.
                return f"{mod}_{safe_name}"
        return safe_name

    # ------------------------------------------------------------------
    # Expression → C string
    # ------------------------------------------------------------------
    @staticmethod
    def _c_string_escape(s: str) -> str:
        """Escape a Python string for safe embedding in a C string literal.
        FIX: expr_to_c's Literal case used to do `f'"{node.value}"'` with
        no escaping at all (only a special-cased whole-string check for
        exactly a lone newline) — so any string containing a literal `"`,
        `\\`, or a real newline/tab (which the lexer now decodes properly
        instead of mangling) would either break the C string literal
        outright or silently change its meaning. Order matters: backslash
        must be escaped first, before the escapes introduced for the
        other characters get a chance to be double-escaped."""
        out = s.replace("\\", "\\\\")
        out = out.replace('"', '\\"')
        out = out.replace("\n", "\\n")
        out = out.replace("\t", "\\t")
        out = out.replace("\r", "\\r")
        return out

    def expr_to_c(self, node: Node) -> str:
        if isinstance(node, Literal):
            if isinstance(node.value, bool):
                return "true" if node.value else "false"
            if isinstance(node.value, str):
                if node.value in ("nothing", "null", "NULL"):
                    return "NULL"   # P7.2: nothing → NULL
                return f'"{self._c_string_escape(node.value)}"'
            if isinstance(node.value, list):
                return "{" + ", ".join(str(v) for v in node.value) + "}"
            if node.value is None:
                return "NULL"
            return str(node.value)
        elif isinstance(node, Identifier):
            return node.name
        elif isinstance(node, FieldAccess):
            # BUGFIX: a variable holding `new Shape` is a pointer (see the
            # VarDecl/NewExpr fix above) and pointer field access in C uses
            # `->`, not `.`. Only the immediate base is checked here (not
            # nested chains flattened into node.obj, e.g. "a.b") -- those
            # aren't in declared_vars by construction and keep using `.`,
            # matching prior (working) behavior for value-typed structs.
            op = "->" if self.declared_vars.get(node.obj, "").rstrip().endswith("*") else "."
            return f"{node.obj}{op}{node.field}"
        elif isinstance(node, IndexAccess):
            idx = self.expr_to_c(node.index)
            return f"{node.collection}[{idx}]"
        elif isinstance(node, BinaryOp):
            left = self.expr_to_c(node.left)
            right = self.expr_to_c(node.right)
            if right in ('"empty"', "'empty'"):
                right = 'NULL'
            if node.op == 'pow':
                return f"pow({left}, {right})"
            return f"({left} {node.op} {right})"
        elif isinstance(node, UnaryOp):
            op = node.op
            operand = self.expr_to_c(node.operand)
            if op == "count":   return f"sizeof({operand}) / sizeof({operand}[0])"
            if op == "length":  return f"strlen({operand})"
            if op == "tanh":    return f"tanh({operand})"
            if op == "sqrt":    return f"sqrt({operand})"
            if op == "exp":     return f"exp({operand})"
            if op == "sin":     return f"sin({operand})"
            if op == "cos":     return f"cos({operand})"
            if op == "room_for":return f"(void*)malloc({operand})"  # MISSING-03
            if op == "addrof":  return f"(&{operand})"
            if op == "deref":   return f"(*{operand})"
            if op == "neg":     return f"(-{operand})"
            return f"({op}{operand})"
        elif isinstance(node, Transmute):
            expr = self.expr_to_c(node.expr)
            type_ = self.type_to_c(node.type)
            return f"(({type_}){expr})"
        elif isinstance(node, NewExpr):
            # MISSING-03 FIX: NewExpr → calloc in C
            type_c = self.type_to_c(node.type_name.replace('.', '_'))
            if node.args:
                args = ", ".join(self.expr_to_c(a) for a in node.args)
                return f"calloc(1, sizeof({type_c})) /* new {node.type_name}({args}) */"
            return f"calloc(1, sizeof({type_c}))"
        elif isinstance(node, FuncCall):
            c_name = self._resolve_call_name(node.name)  # BUG-04
            args = ", ".join(self.expr_to_c(a) for a in node.args)
            if c_name == '__produce_success':
                return args
            if c_name == 'failure':
                return f"/* failure: {args} */ 0"
            return f"{c_name}({args})"
        return f"/* expr: {type(node).__name__} */"

    def lvalue_to_c(self, target: str) -> str:
        return target

    # ------------------------------------------------------------------
    # Infer C type from expression (BUG-01 helper)
    # ------------------------------------------------------------------
    def _infer_type_from_expr(self, node: Node) -> Optional[str]:
        if isinstance(node, Literal):
            if isinstance(node.value, bool):  return "bool"
            if isinstance(node.value, int):   return "int32_t"
            if isinstance(node.value, float): return "double"
            if isinstance(node.value, str):   return "dictum_text"
        if isinstance(node, Identifier):
            return self.declared_vars.get(node.name)
        if isinstance(node, BinaryOp):
            lt = self._infer_type_from_expr(node.left)
            rt = self._infer_type_from_expr(node.right)
            if node.op in ('==', '!=', '>', '<', '>=', '<='):
                return "bool"
            return lt or rt
        if isinstance(node, UnaryOp):
            if node.op == 'room_for': return "void*"
            return self._infer_type_from_expr(node.operand)
        if isinstance(node, FuncCall):
            # FIX: used to unconditionally `return None` here, discarding
            # any known return type and forcing every caller's fallback
            # ("... or int32_t") to kick in — silently wrong/truncating
            # for handle- and text-returning calls (stdlib or user-defined).
            dictum_ret = self.action_return_types.get(node.name)
            return self.type_to_c(dictum_ret) if dictum_ret is not None else None
        if isinstance(node, NewExpr):
            return self.type_to_c(node.type_name.replace('.', '_')) + "*"
        return None

    # ------------------------------------------------------------------
    # BUG-A FIX: detect whether an AST expression is a C compile-time constant.
    # C only allows literal constants (integers, floats, string literals, NULL,
    # sizeof, and address-of static objects) as global initializers.
    # Any expression referencing another variable, calling a function, or
    # performing arithmetic on non-literal operands is NOT a constant.
    # ------------------------------------------------------------------
    def _is_c_constant_expr(self, node: Node) -> bool:
        if node is None:
            return True
        if isinstance(node, Literal):
            return True   # integer, float, string, bool, None — all fine
        if isinstance(node, UnaryOp):
            if node.op == 'room_for':
                return False   # malloc — already handled separately
            if node.op == 'addrof':
                return True    # &global is a constant address
            if node.op == 'sizeof':
                return True
            # Arithmetic on a literal operand is constant; on a variable is not
            return self._is_c_constant_expr(node.operand)
        if isinstance(node, BinaryOp):
            return (self._is_c_constant_expr(node.left) and
                    self._is_c_constant_expr(node.right))
        if isinstance(node, Transmute):
            return self._is_c_constant_expr(node.expr)
        # Identifiers, FuncCalls, FieldAccess, etc. → not constant
        return False

    # ------------------------------------------------------------------
    # BUG-10 FIX: collect forward declaration signatures
    # ------------------------------------------------------------------
    def _collect_fwd_sig(self, node: Action) -> Optional[str]:
        params = ", ".join(f"{self.type_to_c(ptype)} {pname}" for pname, ptype in node.params)
        if not params:
            params = "void"
        ret = self.type_to_c(node.ret_type) if node.ret_type != 'result' else 'void*'
        safe_name = _sanitize_action_name(node.name)  # BUG-B FIX
        return f"{ret} {safe_name}({params})"

    # ------------------------------------------------------------------
    # Node emission
    # ------------------------------------------------------------------
    def emit_node(self, node: Node) -> None:
        # ----------------------------------------------------------------
        if isinstance(node, Program):
            # Collect `use` and `polyglot import` directives BEFORE emitting (BUG-05)
            for stmt in node.body:
                if isinstance(stmt, Use):
                    if stmt.path in self.local_modules:
                        # Defined in this same file as a `module ... end module`
                        # block — emitted inline, no header to include.
                        continue
                    # P2.1: track for Makefile generation
                    self._used_modules.add(stmt.path)
                    inc_path = _USE_INCLUDE_MAP.get(stmt.path,
                                                     f"dictum_{stmt.path.lower()}.h")
                    if stmt.is_system or not inc_path.startswith('dictum_'):
                        inc_line = f"#include <{inc_path}>"
                    else:
                        inc_line = f'#include "{inc_path}"'
                    if inc_line not in self._extra_includes:
                        self._extra_includes.append(inc_line)
                else:
                    # Polyglot import and build directives
                    try:
                        from .polyglot_ast import PolyglotImport, BuildDirective
                        if isinstance(stmt, PolyglotImport):
                            inc = f'#include "{stmt.module_name}_polyglot.h"  /* polyglot import {stmt.module_name} via {stmt.pattern} */'
                            if inc not in self._extra_includes:
                                self._extra_includes.append(inc)
                        elif isinstance(stmt, BuildDirective):
                            self._build_directives.append((stmt.kind, stmt.value))
                            if stmt.kind == 'link':
                                self._extra_includes.append(f'/* #[link "{stmt.value}"] — adds -l{stmt.value} to LDFLAGS */')
                            elif stmt.kind in ('cflags', 'ldflags', 'include_path'):
                                self._extra_includes.append(f'/* #[{stmt.kind} "{stmt.value}"] */')
                    except ImportError:
                        pass
            # PHASE 0: system includes
            # FIX (silent-corruption bug): stdlib runtime headers
            # (dictum_text/json/file/net/http.h) use POSIX functions —
            # strdup, strcasecmp, getaddrinfo, opendir/readdir — that
            # glibc only declares under a POSIX/GNU feature-test macro.
            # Compiling with strict `-std=c11` (as both dictumc_cli.py
            # --compile and the VS Code extension's compile-check gate
            # do) previously left these as *implicit* declarations —
            # which for a pointer-returning function like strdup means
            # the compiler assumes `int`, truncating the real 64-bit
            # pointer and reinterpreting the garbage as a pointer on
            # every affected call. That's undefined behavior, not just a
            # warning, and it must be defined before the FIRST system
            # header of the translation unit — setting it inside a
            # dictum_*.h runtime header is too late, since <string.h>
            # etc. are already included (and their guards already
            # locked in) by the time those headers are reached.
            if not self._includes_emitted:
                self.emit("#define _DEFAULT_SOURCE")
                self.emit("#include <stdint.h>")
                self.emit("#include <stdbool.h>")
                self.emit("#include <stdio.h>")
                self.emit("#include <stdlib.h>")
                self.emit("#include <string.h>")
                self.emit("#include <assert.h>")
                self.emit("#include <math.h>")
                self.emit("#include <setjmp.h>")   # MISSING-05: attempt support
            # P4.1: error handling for attempt blocks and stdlib modules.
            # Only include when stdlib modules are used OR attempt blocks are present
            # (avoids breaking old tests that compile without -I stdlib/).
            # BUGFIX: `produce failure with ...` used directly inside an
            # ordinary action (no enclosing `attempt` block at all) also
            # emits a call to dictum_error_set() -- but this gate used to
            # check ONLY _has_attempt_nodes, so that call went out with no
            # #include for it and no way to link it: implicit-declaration
            # warning at compile time, then "undefined reference to
            # `dictum_error_set`" at link time. Every `produce failure`
            # example needs this include, not just ones inside `attempt`.
            _needs_core = (bool(self._extra_includes) or self._has_attempt_nodes(node)
                           or self._has_produce_failure(node)
                           or self._file_has_attempt_nodes or self._file_has_produce_failure)
            if _needs_core:
                self.emit('#include "dictum_core.h"')
                self.emit('#include "dictum_error.h"')
            # BUG-05: stdlib includes
            for inc in self._extra_includes:
                self.emit(inc)
            self._extra_includes.clear()
            if not self._includes_emitted:
                # MISSING-02: dictum_text typedef
                self.emit("")
                self.emit("typedef const char* dictum_text;")
                self.emit("")
            # Nominal handle types: `define handle Db` -> typedef void* Db;
            # Collected here (Program body) and also for any top-level
            # HandleTypeDecl outside the Program; actual typedef lines are
            # spliced in at get_output() time so ordering is always correct.
            for stmt in node.body:
                if isinstance(stmt, HandleTypeDecl):
                    self._handle_typedefs.add(stmt.name)
            self._includes_emitted = True
            # Flush buffered structs from pre-program shapes
            for line in self._struct_buffer:
                self.emit(line)
            self._struct_buffer.clear()
            # PHASE 1: shapes & enums
            for stmt in node.body:
                if isinstance(stmt, Shape):
                    self.emit_node(stmt)
                elif isinstance(stmt, Possibilities):
                    self.emit_node(stmt)
            self.emit("")
            # BUG-10: forward declarations for ALL actions/externs
            for stmt in node.body:
                if isinstance(stmt, Action):
                    sig = self._collect_fwd_sig(stmt)
                    if sig:
                        self.emit(f"{sig};")
                elif isinstance(stmt, (ImportC, ExternFn)):
                    self.emit_node(stmt)
            # Flush buffered action definitions (defined before the program block)
            if self._action_buffer:
                self.emit("")
                for line in self._action_buffer:
                    self.emit(line)
                self._action_buffer.clear()
            self.emit("")
            # PHASE 2: global variables
            for stmt in node.body:
                if isinstance(stmt, VarDecl):
                    self.declared_vars[stmt.name] = self.type_to_c(stmt.type)
                    ct = self.type_to_c(stmt.type)
                    raw_type = stmt.type
                    is_array = raw_type.endswith(' list') or raw_type.endswith(' array')
                    if isinstance(stmt.value, Literal) and isinstance(stmt.value.value, list):
                        # MISSING-01: array literal — emit typed array with count
                        elem_type = ct
                        raw_vals = []
                        for v in stmt.value.value:
                            if isinstance(v, (int, float, bool)):
                                raw_vals.append(str(v).lower() if isinstance(v, bool) else str(v))
                            else:
                                raw_vals.append(self.expr_to_c(v))
                        vals = ", ".join(raw_vals)
                        size = len(stmt.value.value)
                        self.emit(f"{elem_type} {stmt.name}[{size}] = {{{vals}}};")
                        self.emit(f"const size_t {stmt.name}_count = {size};")
                        self.emit(f"const size_t {stmt.name}_size = sizeof({stmt.name});")
                        self.declared_vars[f"{stmt.name}_count"] = "size_t"
                    elif isinstance(stmt.value, UnaryOp) and stmt.value.op == "room_for":
                        # MISSING-03: malloc at file scope not valid; defer to main
                        operand = self.expr_to_c(stmt.value.operand)
                        self.emit(f"{ct} {stmt.name} = NULL;  /* allocated in main() */")
                        self._main_inits.append(f"{stmt.name} = (void*)malloc({operand});")
                    elif isinstance(stmt.value, NewExpr):
                        # BUGFIX: `keep p as Person with value new Person` heap-
                        # allocates via calloc(), which returns a pointer. The
                        # declared C type must be `Person*`, not `Person` --
                        # otherwise this is `Person p = calloc(...)`, an
                        # incompatible-types compile error (previously this fell
                        # through to the generic "not constant" branch below,
                        # which kept `ct` as the bare struct type). calloc() is
                        # also a function call, so -- like room_for -- it's not
                        # a legal global initializer and must be deferred to
                        # main() regardless.
                        # (Guard against double-pointer: a declared type like
                        # `unique handle to Person` already resolves to
                        # `Person*` via type_to_c, so only append `*` if `ct`
                        # isn't already a pointer type.)
                        ptr_ct = ct if ct.rstrip().endswith('*') else f"{ct}*"
                        self.declared_vars[stmt.name] = ptr_ct
                        val = self.expr_to_c(stmt.value)
                        self.emit(f"{ptr_ct} {stmt.name} = NULL;  /* init deferred to main() */")
                        self._main_inits.append(f"{stmt.name} = {val};")
                    elif stmt.value is None:
                        if is_array:
                            self.emit(f"{ct} {stmt.name}[1];  /* uninitialized array */")
                        else:
                            self.emit(f"{ct} {stmt.name};")
                    elif not self._is_c_constant_expr(stmt.value):
                        # BUG-A FIX: non-constant expression (references other vars,
                        # calls a function, etc.) is illegal as a C global initializer.
                        # Declare the global as zero/NULL and assign in main().
                        if ct in ('double', 'float'):
                            self.emit(f"{ct} {stmt.name} = 0.0;  /* init deferred to main() */")
                        elif ct == 'dictum_text':
                            self.emit(f"{ct} {stmt.name} = NULL;  /* init deferred to main() */")
                        elif ct == 'bool':
                            self.emit(f"{ct} {stmt.name} = false;  /* init deferred to main() */")
                        else:
                            self.emit(f"{ct} {stmt.name} = 0;  /* init deferred to main() */")
                        val = self.expr_to_c(stmt.value)
                        self._main_inits.append(f"{stmt.name} = {val};")
                    else:
                        val = self.expr_to_c(stmt.value)
                        self.emit(f"{ct} {stmt.name} = {val};")
            self.emit("")
            # PHASE 3: module bodies
            for stmt in node.body:
                if isinstance(stmt, Module):
                    self.emit_node(stmt)
            self.emit("")
            # PHASE 4: action definitions
            for stmt in node.body:
                if isinstance(stmt, Action):
                    self.emit_node(stmt)
            self.emit("")
            # PHASE 5: main()
            self.emit("int main(void) {")
            self.indent += 1
            # Emit deferred room_for allocations
            for init_line in self._main_inits:
                self.emit(init_line)
            if self._main_inits:
                self.emit("")
            for stmt in node.body:
                try:
                    from .polyglot_ast import (
                        PolyglotModule, PolyglotImport, PolyglotCall,
                        UnsafeForeignCall, BuildDirective, ForeignShape,
                    )
                    _poly_stmt_types = (
                        PolyglotModule, PolyglotCall, UnsafeForeignCall,
                    )
                    _poly_skip_types = (PolyglotImport, BuildDirective, ForeignShape)
                except ImportError:
                    _poly_stmt_types = ()
                    _poly_skip_types = ()

                if isinstance(stmt, (If, While, ForEach, Repeat, Assignment,
                                     Print, Assert, FuncCall, UnsafeBlock, Attempt, Use)):
                    self.emit_node(stmt)
                elif _poly_stmt_types and isinstance(stmt, _poly_stmt_types):
                    self.emit_node(stmt)
            self.indent -= 1
            self.emit("    return 0;")
            self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Module):
            if not self._includes_emitted:
                self.emit("#define _DEFAULT_SOURCE")
                self.emit("#include <stdint.h>")
                self.emit("#include <stdbool.h>")
                self.emit("#include <stdio.h>")
                self.emit("#include <stdlib.h>")
                self.emit("#include <string.h>")
                self.emit("#include <assert.h>")
                self.emit("#include <math.h>")
                if self._has_produce_failure(node):
                    self.emit('#include "dictum_core.h"')
                    self.emit('#include "dictum_error.h"')
                self.emit("")
                self.emit("typedef const char* dictum_text;")
                self.emit("")
                self._includes_emitted = True
                # FIX: flush any shapes declared earlier in the file before
                # this module's own action bodies are emitted, or their
                # signatures/bodies would reference an as-yet-undefined
                # struct type (see comment above).
                for line in self._struct_buffer:
                    self.emit(line)
                self._struct_buffer.clear()
            prev_module = self.current_module
            self.current_module = node.name
            self._module_actions[node.name] = {
                stmt.name for stmt in node.body if isinstance(stmt, Action)
            }
            for stmt in node.body:
                self.emit_node(stmt)
            self.current_module = prev_module
            return

        # ----------------------------------------------------------------
        if isinstance(node, Possibilities):
            lines = [f"typedef enum {{"]
            for v in node.variants:
                lines.append(f"    {v},")
            lines.append(f"}} {node.name};")
            lines.append("")
            if self._includes_emitted:
                for line in lines: self.emit(line)
            else:
                self._struct_buffer.extend(lines)
            return

        # ----------------------------------------------------------------
        if isinstance(node, Shape):
            self.shapes[node.name] = {f: t for f, t in node.fields}
            lines = []
            if node.is_packed:
                lines.append(f"typedef struct __attribute__((packed)) {{")
            else:
                lines.append(f"typedef struct {{")
            for fname, ftype in node.fields:
                fct = self.type_to_c(ftype)
                if self._is_list_type(ftype):
                    fct = f"{fct}*"
                lines.append(f"    {fct} {fname};")
            lines.append(f"}} {node.name};")
            lines.append("")
            if self._includes_emitted:
                for line in lines: self.emit(line)
            else:
                self._struct_buffer.extend(lines)
            return

        # ----------------------------------------------------------------
        if isinstance(node, VarDecl):
            ct = self.type_to_c(node.type)
            raw_type = node.type
            is_array = self._is_list_type(raw_type)
            self.declared_vars[node.name] = ct
            if node.value is None:
                if is_array:
                    self.emit(f"{ct} {node.name}[1];  /* uninitialized array */")
                else:
                    self.emit(f"{ct} {node.name};  /* uninitialized */")
            elif isinstance(node.value, Literal) and isinstance(node.value.value, list):
                # MISSING-01: array literal init
                raw_vals = []
                for v in node.value.value:
                    if isinstance(v, (int, float, bool)):
                        raw_vals.append(str(v).lower() if isinstance(v, bool) else str(v))
                    else:
                        raw_vals.append(self.expr_to_c(v))
                vals = ", ".join(raw_vals)
                size = len(node.value.value)
                self.emit(f"{ct} {node.name}[{size}] = {{{vals}}};")
                self.emit(f"const size_t {node.name}_count = {size};")
                self.emit(f"const size_t {node.name}_size = sizeof({node.name});")
            elif isinstance(node.value, UnaryOp) and node.value.op == "all_values":
                self.emit(f"/* all_values init: {node.name} */")
            elif isinstance(node.value, UnaryOp) and node.value.op == "room_for":
                operand = self.expr_to_c(node.value.operand)
                ptr_ct = f"{ct}*" if is_array else ct
                self.declared_vars[node.name] = ptr_ct
                self.emit(f"{ptr_ct} {node.name} = ({ptr_ct})malloc(sizeof({ct}) * ({operand}));  /* room_for */")
            elif isinstance(node.value, NewExpr):
                # BUGFIX (same root cause as the global-VarDecl case above):
                # `new Shape` allocates via calloc() and returns a pointer,
                # so the declared C type must be `Shape*`, not `Shape`, or
                # this is `Shape p = calloc(...)` -- incompatible types.
                # Guard against double-pointer for an already-pointer
                # declared type (e.g. `unique handle to Shape` -> `Shape*`).
                ptr_ct = ct if ct.rstrip().endswith('*') else f"{ct}*"
                self.declared_vars[node.name] = ptr_ct
                val = self.expr_to_c(node.value)
                self.emit(f"{ptr_ct} {node.name} = {val};")
            else:
                val = self.expr_to_c(node.value)
                if is_array:
                    ptr_ct = f"{ct}*"
                    self.declared_vars[node.name] = ptr_ct
                    self.emit(f"{ptr_ct} {node.name} = {val};")
                    return
                self.emit(f"{ct} {node.name} = {val};")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Assignment):
            target = self.lvalue_to_c(node.target)
            val = self.expr_to_c(node.value)
            # BUG-01 FIX: auto-declare undeclared variable
            base_name = target.split('[')[0].split('.')[0]
            if (base_name not in self.declared_vars and
                    '[' not in target and '.' not in target):
                inferred_ct = self._infer_type_from_expr(node.value) or "int32_t"
                self.declared_vars[base_name] = inferred_ct
                self.emit(f"{inferred_ct} {target} = {val};")
            else:
                # BUGFIX: `set p.field to X` where p holds `new Shape` (a
                # pointer, per the VarDecl/NewExpr fix) needs `p->field`,
                # not `p.field` -- C rejects `.` on a pointer. Only the
                # first segment is rewritten, matching the read-side
                # FieldAccess fix, since nested chains aren't tracked here.
                if '.' in target and self.declared_vars.get(base_name, "").rstrip().endswith("*"):
                    target = target.replace(f"{base_name}.", f"{base_name}->", 1)
                self.emit(f"{target} = {val};")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Action):
            self.actions.add(node.name)
            self.action_return_types[node.name] = node.ret_type
            safe_name = _sanitize_action_name(node.name)  # BUG-B FIX
            if self.current_module:
                # Module-scoped actions are called as Module.action, which
                # _resolve_call_name turns into Module_action — match that here.
                safe_name = f"{self.current_module}_{safe_name}"
            params_str = ", ".join(f"{self.type_to_c(ptype)} {pname}" for pname, ptype in node.params)
            if not params_str:
                params_str = "void"
            ret = self.type_to_c(node.ret_type) if node.ret_type != 'result' else 'void*'
            # BUGFIX (struct-return + `produce failure`): remember this
            # action's real C return type for the duration of its body so
            # a `produce failure` inside it returns a value of the right
            # type instead of a hard-coded `0`. Saved/restored rather than
            # just set, in case of any future nested-emission path.
            saved_ret_c_type = self.current_action_ret_c_type
            self.current_action_ret_c_type = ret
            # BUGFIX (companion to the FieldAccess format-spec fix below):
            # parameters were never added to declared_vars, so a field
            # access on a PARAMETER (not just a local `keep` variable)
            # couldn't be type-resolved either. Register them here.
            #
            # BUGFIX (cross-action declared_vars leakage): declared_vars
            # is a single CEmitter-instance-level dict, but each Action's
            # locals (params AND anything the body declares via `keep` or
            # the BUG-01 Assignment auto-declare path) live in a fresh C
            # scope per function -- they must not stay visible to the
            # NEXT action emitted from the same file. The previous fix
            # only snapshotted/restored PARAM names, so a local like
            # `Result` from `call c_sqrt with X giving Result` (auto-
            # declared by the Assignment branch, not a param) stayed in
            # declared_vars forever. A second, later action that also
            # does `call c_other with Y giving Result` then saw `Result`
            # as "already declared" and skipped emitting its type,
            # producing `Result = c_other(Y);` with no declaration at all
            # -- a real, confirmed "'Result' undeclared" compile error
            # whenever two sibling actions in the same file both use the
            # same `giving <name>` result-variable name (e.g. two
            # IMPORT_C-calling actions both writing `giving Result`).
            # Snapshotting the WHOLE dict and restoring it after the body
            # is emitted fixes this for params AND locals alike, and
            # subsumes the old params-only save/restore.
            saved_declared_vars = dict(self.declared_vars)
            for pname, ptype in node.params:
                self.declared_vars[pname] = self.type_to_c(ptype)

            if not self._includes_emitted:
                # Buffer for after includes
                saved, saved_indent = self.output, self.indent
                self.output = self._action_buffer
                self.indent = 0
                self.emit(f"{ret} {safe_name}({params_str}) {{")
                self.indent += 1
                for stmt in node.body:
                    self.emit_node(stmt)
                self.indent -= 1
                self.emit("}")
                self.emit("")
                self.output = saved; self.indent = saved_indent
            else:
                self.emit(f"{ret} {safe_name}({params_str}) {{")
                self.indent += 1
                for stmt in node.body:
                    self.emit_node(stmt)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            self.current_action_ret_c_type = saved_ret_c_type
            self.declared_vars = saved_declared_vars
            return

        # ----------------------------------------------------------------
        if isinstance(node, If):
            cond = self.expr_to_c(node.cond)
            cond = cond.replace("== 'empty'", "== NULL").replace('== "empty"', "== NULL")
            self.emit(f"if ({cond}) {{")
            self.indent += 1
            for stmt in node.then_body:
                self.emit_node(stmt)
            self.indent -= 1
            if node.else_body:
                self.emit("} else {")
                self.indent += 1
                for stmt in node.else_body:
                    self.emit_node(stmt)
                self.indent -= 1
            self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, While):
            cond = self.expr_to_c(node.cond)
            self.emit(f"while ({cond}) {{")
            self.indent += 1
            for stmt in node.body:
                self.emit_node(stmt)
            self.indent -= 1
            self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, ForEach):
            # MISSING-01 / MISSING-07 FIX: for each over array
            self.emit(f"for (size_t __i = 0; __i < {node.collection}_count; __i++) {{")
            self.indent += 1
            elem_type = self.declared_vars.get(node.collection, "int32_t")
            # Strip array brackets from type if present
            elem_type = elem_type.replace("*", "").strip()
            self.emit(f"{elem_type} {node.item} = {node.collection}[__i];")
            for stmt in node.body:
                self.emit_node(stmt)
            self.indent -= 1
            self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Repeat):
            count = self.expr_to_c(node.count)
            self.emit(f"for (int32_t {node.counter} = 0; {node.counter} < {count}; {node.counter}++) {{")
            self.indent += 1
            for stmt in node.body:
                self.emit_node(stmt)
            self.indent -= 1
            self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Attempt):
            # P4.1: emit attempt block using dictum_last_error for real error propagation.
            result    = node.result_name or "__attempt_result"
            fail_name = node.failure_name or "__err"

            if node.call is not None:
                call_expr = self.expr_to_c(node.call)
                inferred  = self._infer_type_from_expr(node.call) or "int32_t"
                self.emit("/* attempt */")
                self.emit("dictum_error_clear();")
                if result in self.declared_vars:
                    # Already declared (e.g. via `keep result as ... with value ...`
                    # before the attempt) — assign rather than redeclare.
                    self.emit(f"{result} = {call_expr};")
                else:
                    self.emit(f"{inferred} {result} = {call_expr};")
                    self.declared_vars[result] = inferred
                self.emit("if (!DICTUM_HAS_ERROR()) {")
                self.indent += 1
                for stmt in node.success_body:
                    self.emit_node(stmt)
                self.indent -= 1
                if node.failure_body:
                    self.emit("} else {")
                    self.indent += 1
                    if node.failure_name:
                        self.emit(f'const char* {node.failure_name} = dictum_error_last();')
                        self.declared_vars[node.failure_name] = "const char*"
                    for stmt in node.failure_body:
                        self.emit_node(stmt)
                    self.indent -= 1
                self.emit("}")
            else:
                # Block form: use do { ... } while(0) + goto pattern so that
                # failure body can be reached via goto when dictum_error_set()
                # is called inside the success body.
                lbl_fail = f"__attempt_fail_{node.line}"
                lbl_end  = f"__attempt_end_{node.line}"
                self.emit("/* attempt block */")
                self.emit("dictum_error_clear();")
                self.emit("do {")
                self.indent += 1
                for stmt in node.success_body:
                    self.emit_node(stmt)
                # After success body, skip over failure block
                if node.failure_body:
                    self.emit(f"if (DICTUM_HAS_ERROR()) {{ goto {lbl_fail}; }}")
                self.indent -= 1
                self.emit(f"}} while (0);")

                if node.failure_body:
                    self.emit(f"if (!DICTUM_HAS_ERROR()) {{ goto {lbl_end}; }}")
                    self.emit(f"{lbl_fail}:")
                    self.emit("{")
                    self.indent += 1
                    if node.failure_name:
                        self.emit(f'const char* {node.failure_name} = dictum_error_last();')
                        self.declared_vars[node.failure_name] = "const char*"
                    for stmt in node.failure_body:
                        self.emit_node(stmt)
                    self.indent -= 1
                    self.emit("}")
                    self.emit(f"{lbl_end}: ;")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Return):
            # BUG-09 FIX: clean return without noise
            if isinstance(node.value, FuncCall):
                if node.value.name in ('__produce_success', 'success'):
                    inner = self.expr_to_c(node.value.args[0]) if node.value.args else ""
                    self.emit(f"return {inner};")
                    return
                if node.value.name == 'failure':
                    msg = self.expr_to_c(node.value.args[0]) if node.value.args else '"error"'
                    self.emit(f"dictum_error_set({msg});")
                    # BUGFIX (struct-return + `produce failure` compile
                    # error): was hard-coded `return 0;` regardless of the
                    # enclosing action's real C return type -- a hard gcc
                    # type error for any action that `produces` a shape.
                    zero = self._zero_value_for_c_type(self.current_action_ret_c_type)
                    self.emit("return;" if zero is None else f"return {zero};")
                    return
            val = self.expr_to_c(node.value)
            self.emit(f"return {val};")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Assert):
            self.emit(f"assert({self.expr_to_c(node.cond)});")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Print):
            fmt_parts, args = [], []
            for p in node.parts:
                if isinstance(p, Literal) and isinstance(p.value, str):
                    escaped = self._c_string_escape(p.value)
                    fmt_parts.append(escaped)
                else:
                    # Type-aware format specifier
                    spec = self._format_spec(p)
                    fmt_parts.append(spec)
                    args.append(self.expr_to_c(p))
            fmt = "".join(fmt_parts)
            if args:
                self.emit(f'printf("{fmt}", {", ".join(args)});')
            else:
                self.emit(f'printf("{fmt}");')
            return

        # ----------------------------------------------------------------
        if isinstance(node, FuncCall):
            c_name = self._resolve_call_name(node.name)   # BUG-04
            if c_name == "__defer_release":
                self.emit(f"/* defer release: {self.expr_to_c(node.args[0])} */")
            elif c_name == "release":
                arg = self.expr_to_c(node.args[0])
                self.emit(f"free({arg});")
            else:
                args = ", ".join(self.expr_to_c(a) for a in node.args)
                self.emit(f"{c_name}({args});")
            return

        # ----------------------------------------------------------------
        if isinstance(node, ImportDict):
            # MISSING-08: resolved at Transpiler level; emitter just emits the #include.
            stem = node.module_name.lower()
            self._extra_includes.append(f'#include "{stem}.h"')
            return

        if isinstance(node, ImportC):
            c_params = [self.type_to_c(p) for p in node.params]
            ret_c = self.type_to_c(node.ret_type)
            params_decl = ", ".join(c_params) if c_params else "void"
            # `action_name` is the real C symbol (e.g. "sqrt"); `alias` is the
            # Dictum-side name used at call sites. They commonly differ
            # ("import from C the action sqrt ... as c_sqrt"), and `sqrt` is
            # provided by libm/libc — declaring `extern <ret> c_sqrt(...)`
            # produces an undefined-reference at link time. Instead, declare
            # the real symbol and emit a thin inline wrapper under the alias
            # so both names resolve and the alias is always linkable.
            self.emit(f"extern {ret_c} {node.action_name}({params_decl});")
            if node.alias and node.alias != node.action_name:
                arg_names = [f"a{i}" for i in range(len(c_params))]
                wrapper_params = ", ".join(f"{t} {n}" for t, n in zip(c_params, arg_names)) or "void"
                call_args = ", ".join(arg_names)
                self.emit(f"static inline {ret_c} {node.alias}({wrapper_params}) {{ return {node.action_name}({call_args}); }}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, HandleTypeDecl):
            # Register for deferred typedef emission (see get_output);
            # covers both Program-body and bare top-level occurrences.
            self._handle_typedefs.add(node.name)
            return

        # ----------------------------------------------------------------
        if isinstance(node, Break):
            self.emit("break;")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Use):
            if node.path in self.local_modules:
                self._active_local_modules.add(node.path)
                return
            # BUG-05 FIX: `use Module` → #include
            # P2.1: track module for Makefile generation
            self._used_modules.add(node.path)
            inc_path = _USE_INCLUDE_MAP.get(node.path, f"dictum_{node.path.lower()}.h")
            if node.is_system or inc_path.endswith('.h') and not inc_path.startswith('dictum_'):
                inc_line = f"#include <{inc_path}>"
            else:
                inc_line = f'#include "{inc_path}"'
            if self._includes_emitted:
                self.emit(inc_line)
            else:
                self._extra_includes.append(inc_line)
            return

        # ----------------------------------------------------------------
        if isinstance(node, Bind):
            params = ", ".join(f"{self.type_to_c(ptype)} {pname}" for pname, ptype in node.params)
            ret = self.type_to_c(node.ret_type)
            self.emit(f"extern {ret} {node.name}({params});")
            self.actions.add(node.alias)
            self.action_return_types[node.alias] = node.ret_type
            return

        # ----------------------------------------------------------------
        if isinstance(node, ExternFn):
            params = ", ".join(f"{self.type_to_c(ptype)} {pname}" for pname, ptype in node.params)
            ret = self.type_to_c(node.ret_type)
            if node.syscall_name:
                self.emit(f"/* @syscall: {node.syscall_name} */")
            self.emit(f"extern {ret} {node.name}({params});")
            return

        # ────────────────────────────────────────────────────────────────
        # VerifyToken — emits a C comment, consumed by review engine (L2)
        # ────────────────────────────────────────────────────────────────
        if isinstance(node, VerifyToken):
            self.emit(f"/* [VERIFY:{node.key}] */")
            return

        # ────────────────────────────────────────────────────────────────
        # UnsafeToken — expands pre-verified special tokens to C intrinsics
        # ────────────────────────────────────────────────────────────────
        if isinstance(node, UnsafeToken):
            self._emit_unsafe_token(node)
            return

        # ────────────────────────────────────────────────────────────────
        # UnsafeBlock — walk body, emitting each node (includes UnsafeToken)
        # ────────────────────────────────────────────────────────────────
        if isinstance(node, UnsafeBlock):
            self.emit("/* unsafe block — L3 token expansion */")
            for stmt in node.body:
                self.emit_node(stmt)
            return

        # ----------------------------------------------------------------
        if isinstance(node, VarDecl):
            # VarDecl inside a block (already handled above, fallback)
            self.emit_node(node)
            return

        # ----------------------------------------------------------------
        # Polyglot nodes — C emitter treatment
        # ----------------------------------------------------------------
        try:
            from .polyglot_ast import (
                PolyglotModule, PolyglotImport, PolyglotCall,
                UnsafeForeignCall, BuildDirective, ForeignShape,
            )
        except ImportError:
            pass
        else:
            if isinstance(node, PolyglotModule):
                # Emit the module body inline; the binding glue is generated by the linker
                self.emit(f"/* polyglot module '{node.name}' backend={node.backend} safety={node.safety} */")
                for stmt in node.body:
                    self.emit_node(stmt)
                return

            if isinstance(node, PolyglotImport):
                # BUG-05 style: emit an extern include comment
                inc = f"{node.module_name}_polyglot.h"
                self.emit(f'#include "{inc}"  /* polyglot import {node.module_name} via {node.pattern} */')
                return

            if isinstance(node, PolyglotCall):
                # Cross-module call: resolve via binding header
                c_fn = f"dictum_safe_{node.function}" if node.safety != 'unsafe' else node.function
                args = ", ".join(self.expr_to_c(a) for a in node.args)
                if node.result_name:
                    inferred = "int32_t"
                    self.emit(f"{inferred} {node.result_name} = {c_fn}({args});")
                else:
                    self.emit(f"{c_fn}({args});")
                return

            if isinstance(node, UnsafeForeignCall):
                # Raw dlsym / direct symbol call
                args = ", ".join(self.expr_to_c(a) for a in node.args)
                ret_type = self.type_to_c(node.result_type) if node.result_type else "void*"
                if node.result_name:
                    self.emit(f"/* unsafe foreign call */")
                    self.emit(f"{ret_type} {node.result_name} = "
                               f"(({ret_type}(*)())(uintptr_t)\"{node.symbol}\")({args});")
                else:
                    self.emit(f"/* unsafe foreign call: {node.symbol}({args}) */")
                return

            if isinstance(node, BuildDirective):
                # Now ALSO recorded for get_ldflags()/get_cflags() — the
                # comment alone used to be the only trace of this directive
                # anywhere, so nothing downstream could act on it.
                self._build_directives.append((node.kind, node.value))
                if node.kind == 'link':
                    self.emit(f"/* #[link \"{node.value}\"] — adds -l{node.value} to LDFLAGS */")
                elif node.kind in ('cflags', 'ldflags', 'include_path'):
                    self.emit(f"/* #[{node.kind} \"{node.value}\"] */")
                return

            if isinstance(node, ForeignShape):
                # Emit as a C struct with a matching layout
                lines = []
                if node.packed:
                    lines.append("#pragma pack(push, 1)")
                lines.append(f"/* foreign {node.source_language} struct: {node.name} */")
                lines.append(f"typedef struct {{")
                for fname, ftype in node.fields:
                    ct = self.type_to_c(ftype)
                    lines.append(f"    {ct} {fname};")
                lines.append(f"}} {node.name};")
                if node.packed:
                    lines.append("#pragma pack(pop)")
                for ln in lines:
                    self.emit(ln)
                return

        self.emit(f"/* unhandled: {type(node).__name__} */")

    # ------------------------------------------------------------------
    # Format specifier helper for printf
    # ------------------------------------------------------------------
    def _format_spec(self, p: Node) -> str:
        if isinstance(p, Literal):
            if isinstance(p.value, float): return "%f"
            if isinstance(p.value, str):   return "%s"
            return "%d"
        if isinstance(p, Identifier):
            t = self.declared_vars.get(p.name, '')
            if t in ('double', 'float'):   return "%f"
            if t in ('dictum_text', 'const char*', 'char*'): return "%s"
            if t == 'bool':                return "%d"
            if t == 'size_t':              return "%zu"
            if t in ('int64_t', 'uint64_t'): return "%lld"
            # heuristic from variable name
            n = p.name.lower()
            if any(h in n for h in ('frac', 'dist', 'price', 'rate', 'double', 'float')): return "%f"
            if any(h in n for h in ('name', 'msg', 'text', 'str')):  return "%s"
            return "%d"
        if isinstance(p, FieldAccess):
            # BUGFIX (silent wrong printf format): this used to look up
            # self.shapes keyed by p.obj directly -- but p.obj is the
            # VARIABLE name (e.g. "updated"), while self.shapes is keyed by
            # the shape/TYPE name (e.g. "Account"). The lookup always
            # missed, silently falling through to "%d" even for a
            # `decimal number` field -- no error, just a wrong printed
            # value. Resolve the variable's declared C type first, then
            # use THAT as the key into self.shapes.
            shape_name = self.declared_vars.get(p.obj, '')
            # BUGFIX: a `new Shape` variable is now declared as "Shape*"
            # (see VarDecl/NewExpr fix), but self.shapes is keyed by the
            # bare shape name "Shape" -- strip the pointer suffix so the
            # lookup still hits instead of silently falling back to "%d".
            shape_name = shape_name.rstrip('*').strip()
            if shape_name in self.shapes:
                field_type = self.shapes[shape_name].get(p.field, '')
                if 'fractional' in field_type or 'decimal' in field_type: return "%f"
                if field_type == 'text': return "%s"
                if field_type == 'truth value': return "%d"
                if field_type in ('count',): return "%zu"
            return "%d"
        if isinstance(p, BinaryOp):
            if p.op in ('==', '!=', '>', '<', '>=', '<='): return "%d"
            return self._format_spec(p.left)
        return "%d"

    # ------------------------------------------------------------------
    def get_output(self) -> str:
        # Flush any residual action buffer (module-only files)
        if not self._includes_emitted and (self._struct_buffer or self._action_buffer):
            prelude = [
                "#define _DEFAULT_SOURCE",
                "#include <stdint.h>", "#include <stdbool.h>", "#include <stdio.h>",
                "#include <stdlib.h>", "#include <string.h>", "#include <assert.h>",
                "#include <math.h>", "#include <setjmp.h>", "",
                "typedef const char* dictum_text;", "",
            ]
            # BUGFIX (missing dictum_error.h for module-only / no-Program
            # files): this hard-coded prelude never checked whether any
            # buffered action uses `produce failure` -- same class of bug
            # as the Program-node gate above, for files that never reach
            # that branch at all (no `program` block in the file).
            if self._file_has_produce_failure or self._file_has_attempt_nodes:
                prelude.append('#include "dictum_core.h"')
                prelude.append('#include "dictum_error.h"')
                prelude.append("")
            for hname in sorted(self._handle_typedefs):
                prelude.append(f"typedef void* {hname};")
            if self._handle_typedefs:
                prelude.append("")
            prelude.extend(self._struct_buffer)
            # BUGFIX (IMPORT_C forward-declaration ordering, module-only /
            # no-Program files): action bodies used to be spliced into the
            # prelude BEFORE self.output, but self.output is where ImportC/
            # ExternFn emit their `extern ...;` + `static inline` alias
            # wrapper (see emit_node's ImportC branch) -- those lines are
            # appended directly, never buffered, since only Action bodies
            # defer via _action_buffer. That ordering put every action body
            # ahead of the C import declarations it calls into, producing an
            # implicit-declaration warning at the call site and then a hard
            # "conflicting types" error once the late `static inline`
            # definition landed on top of the compiler's implicit int()
            # guess. Actions must come AFTER self.output (imports/externs),
            # not before, so any `call c_sqrt with ...` in an action body
            # always sees c_sqrt's real double-typed declaration first.
            self.output = prelude + self.output + self._action_buffer
            self._action_buffer = []
        elif self._action_buffer:
            # Inject buffered actions after last #include line
            last_inc = -1
            for i, ln in enumerate(self.output):
                if ln.strip().startswith('#include') or ln.strip().startswith('typedef'):
                    last_inc = i
            if last_inc >= 0:
                inject = [''] + self._action_buffer
                self.output = (self.output[:last_inc + 1]
                               + inject
                               + self.output[last_inc + 1:])
            self._action_buffer = []
        self._fix_preamble_ordering()
        if self._handle_typedefs:
            self._splice_handle_typedefs()
        return "\n".join(self.output)

    def _splice_handle_typedefs(self) -> None:
        """Ensure `typedef void* Name;` exists for every declared nominal
        handle type, inserted right after the includes/dictum_text typedef
        block — *before* any extern/struct/var that references it — no
        matter where in the source `define handle Name` actually appeared.
        Idempotent: skips names already emitted (e.g. by the Program
        preamble path) to avoid duplicate typedefs.

        Also guards against a separate pre-existing ordering issue: a
        top-level `import from C` (outside any `program` block) emits its
        `extern`/wrapper lines in raw source order, which can land *before*
        the `#include`/typedef preamble block emitted later when the
        `program` node itself is visited. If that's happened, the
        first-contiguous-preamble block (starting at #include) is not at
        the very top, the handle typedefs are still inserted right after
        the start of that block, which is sufficient for the
        typedef-before-use requirement we're responsible for here."""
        already = set()
        for ln in self.output:
            s = ln.strip()
            if s.startswith('typedef void* ') and s.endswith(';'):
                already.add(s[len('typedef void* '):-1].strip())
        missing = sorted(self._handle_typedefs - already)
        if not missing:
            return
        last_preamble = -1
        for i, ln in enumerate(self.output):
            s = ln.strip()
            if s.startswith('#include') or s.startswith('typedef'):
                last_preamble = i
        insert_at = last_preamble + 1 if last_preamble >= 0 else 0
        new_lines = [f"typedef void* {hname};" for hname in missing]
        self.output = self.output[:insert_at] + new_lines + [''] + self.output[insert_at:]

    def _fix_preamble_ordering(self) -> None:
        """Pre-existing bug guard: if a top-level `import from C` (outside
        any program block) emitted extern/wrapper lines before the
        #include/typedef preamble block (which is only emitted once the
        Program node is visited), move that leading chunk to *after* the
        preamble instead of before it, so int32_t/etc. are always declared
        before use. No-op if the preamble is already at (or near) the top.

        FIX: this used to treat ANY non-blank line before the first
        #include as "leading garbage" to relocate — which silently ate
        the `#define _DEFAULT_SOURCE` line (added to fix the POSIX
        strdup/strcasecmp/getaddrinfo implicit-declaration bug) by moving
        it to just before `main()`, i.e. AFTER <string.h>/<stdlib.h>/etc.
        had already been included without the feature-test macro in
        effect — defeating the fix entirely while looking like it worked
        (the line was still present in the output, just too late to do
        anything). `#define` lines immediately preceding the preamble are
        now left in place; only genuinely different content (extern
        declarations, wrapper functions) is still treated as misplaced."""
        first_include = -1
        for i, ln in enumerate(self.output):
            if ln.strip().startswith('#include'):
                first_include = i
                break
        if first_include <= 0:
            return  # already at top, or no includes at all
        leading = self.output[:first_include]
        if not any(ln.strip() and not ln.strip().startswith('#define') for ln in leading):
            return  # only blank lines / #define lines ahead — nothing to fix
        # Keep #define lines pinned immediately before the include block —
        # they must stay ahead of #include for feature-test macros like
        # _DEFAULT_SOURCE to have any effect — and only relocate the
        # remaining genuine leading garbage (e.g. a misplaced extern).
        pinned_defines = [ln for ln in leading if ln.strip().startswith('#define')]
        leading = [ln for ln in leading if not ln.strip().startswith('#define')]
        rest = self.output[first_include:]
        # Find end of the contiguous preamble block (includes + typedefs +
        # the blank line that follows it) so we splice the leading chunk
        # in right after it, not in the middle of it.
        end = 0
        while end < len(rest):
            s = rest[end].strip()
            if s.startswith('#include') or s == '':
                end += 1
                continue
            if s.startswith('typedef') and s.endswith(';'):
                # Complete single-line typedef (e.g. `typedef const char*
                # dictum_text;` / `typedef void* Foo;`).
                end += 1
                continue
            if (s.startswith('typedef struct') or s.startswith('typedef enum')) and not s.endswith(';'):
                # Multi-line struct/enum typedef — consume through its
                # closing `} Name;` line so anything relocated after this
                # block (e.g. an extern referencing the struct type) lands
                # after the full definition, not before or inside it.
                j = end + 1
                while j < len(rest) and not rest[j].strip().startswith('}'):
                    j += 1
                if j < len(rest):
                    j += 1  # include the closing "} Name;" line itself
                end = j
                continue
            break
        self.output = pinned_defines + rest[:end] + leading + rest[end:]

    # ------------------------------------------------------------------
    # P2.1: Generate a Makefile for the transpiled program
    # ------------------------------------------------------------------
    # Module → linker flags mapping
    _MODULE_LDFLAGS: Dict[str, List[str]] = {
        "Http":      [],  # dictum_http.h is pure sockets now — no libcurl needed
        "Tls":       ["-lssl", "-lcrypto"],
        "Net":       [],
        "Thread":    ["-lpthread"],
        "Mutex":     ["-lpthread"],
        "Channel":   ["-lpthread"],
        "Semaphore": ["-lpthread"],
        "Event":     ["-lpthread"],
        "Math":      ["-lm"],
        "Shm":       ["-lrt"],
        "Timer":     ["-lrt"],
        "Process":   [],
        "Signal":    [],
        "Pipe":      [],
        "Mmap":      [],
        "Path":      [],
        "Directory": [],
        "Device":    [],
        "Csv":       [],
        "File":      [],
        "Text":      [],
        "Json":      ["-lm"],   # atof uses libm
        "Console":   [],
        # ── Sprint 3: blessed library bridges ──────────────────────────────
        "Glfw":      ["-lglfw", "-lGL", "-lm"],
        "Sdl":       ["-lSDL2"],
        "Raylib":    ["-lraylib", "-lGL", "-lm", "-lpthread"],
        "Sqlite":    ["-lsqlite3"],
        "Ssl":       ["-lssl", "-lcrypto"],
    }

    def _has_attempt_nodes(self, root: Node) -> bool:
        """Return True if any Attempt node exists under root."""
        from .ast_nodes import Attempt as AttemptNode
        def _walk(n: Node) -> bool:
            if isinstance(n, AttemptNode):
                return True
            for attr in ('body', 'success_body', 'failure_body', 'then_body',
                         'else_body', 'actions', 'cases'):
                val = getattr(n, attr, None)
                if isinstance(val, list):
                    if any(_walk(child) for child in val if isinstance(child, Node)):
                        return True
            return False
        return _walk(root)

    def _has_produce_failure(self, root: Node) -> bool:
        """Return True if `produce failure with ...` appears anywhere under
        root — i.e. emitted code will call dictum_error_set(), which needs
        dictum_error.h even outside of an `attempt` block (e.g. inside a
        module's actions)."""
        def _walk(n: Node) -> bool:
            if isinstance(n, Return) and isinstance(n.value, FuncCall) and n.value.name == 'failure':
                return True
            for attr in ('body', 'success_body', 'failure_body', 'then_body',
                         'else_body', 'actions', 'cases'):
                val = getattr(n, attr, None)
                if isinstance(val, list):
                    if any(_walk(child) for child in val if isinstance(child, Node)):
                        return True
            return False
        return _walk(root)

    def get_ldflags(self) -> List[str]:
        """Compute the real set of linker flags this program needs: always
        -lm, plus whatever `_MODULE_LDFLAGS` says for each `use`d stdlib
        module, plus -l<value> for every #[link "value"] BuildDirective
        (the mechanism blessed libraries like sqlite3/glfw/sdl2/raylib/
        openssl declare their link requirement with). Centralizing this
        here means the CLI's --compile mode, project_builder.py's
        multi-file Makefile, and the VS Code extension's compile-check
        gate can all ask for the same, correct answer instead of each
        hardcoding "-lm" independently (three places used to do exactly
        that, silently dropping -lpthread/-lsqlite3/etc.)."""
        ldflags: List[str] = ["-lm"]
        seen: set = set(ldflags)
        for mod in sorted(self._used_modules):
            for flag in self._MODULE_LDFLAGS.get(mod, []):
                if flag not in seen:
                    seen.add(flag)
                    ldflags.append(flag)
        for kind, value in self._build_directives:
            if kind == 'link':
                flag = f"-l{value}"
                if flag not in seen:
                    seen.add(flag)
                    ldflags.append(flag)
            elif kind == 'ldflags':
                if value not in seen:
                    seen.add(value)
                    ldflags.append(value)
        return ldflags

    def get_cflags(self) -> List[str]:
        """Companion to get_ldflags(): -I/-D/other compile-time flags from
        #[cflags ...] / #[include_path ...] BuildDirectives."""
        cflags: List[str] = []
        for kind, value in self._build_directives:
            if kind == 'include_path':
                cflags.append(f"-I{value}")
            elif kind == 'cflags':
                cflags.append(value)
        return cflags

    def get_makefile(self, program_name: str = "program",
                     stdlib_dir: str = "stdlib") -> str:
        """Return an auto-generated Makefile string for the transpiled program."""
        ldflags_str = " ".join(self.get_ldflags())
        lines = [
            f"# Auto-generated by dictumc — Dictum v5",
            f"# Rebuild with: make",
            f"",
            f"CC      = gcc",
            f"AR      = ar",
            f"CFLAGS  = -std=c11 -Wall -O2 -I{stdlib_dir}",
            f"LDFLAGS = {ldflags_str}",
            f"STDLIB  = {stdlib_dir}/libdictum_stdlib.a",
            f"",
            f"all: {program_name}",
            f"",
            f"{program_name}: {program_name}.c $(STDLIB)",
            f"\t$(CC) $(CFLAGS) {program_name}.c $(STDLIB) -o {program_name} $(LDFLAGS)",
            f"",
            f"$(STDLIB):",
            f"\t$(MAKE) -C {stdlib_dir} lib",
            f"",
            f"clean:",
            f"\trm -f {program_name}",
            f"",
            f".PHONY: all clean",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Header generation for exports
    # ------------------------------------------------------------------
    def get_header_output(self, ast: List[Node]) -> str:
        lines = ["#pragma once", "#include <stdint.h>", "#include <stdbool.h>",
                 "#include <stddef.h>", "typedef const char* dictum_text;", ""]
        def _emit(nodes):
            for node in nodes:
                if isinstance(node, Shape) and node.export:
                    if node.is_packed:
                        lines.append(f"typedef struct __attribute__((packed)) {{")
                    else:
                        lines.append(f"typedef struct {{")
                    for fname, ftype in node.fields:
                        lines.append(f"    {self.type_to_c(ftype)} {fname};")
                    lines.append(f"}} {node.name};")
                    lines.append("")
                elif isinstance(node, VarDecl) and node.export:
                    lines.append(f"extern {self.type_to_c(node.type)} {node.name};")
                elif isinstance(node, Action) and node.export:
                    params = ", ".join(f"{self.type_to_c(pt)} {pn}" for pn, pt in node.params) or "void"
                    ret = self.type_to_c(node.ret_type)
                    lines.append(f"extern {ret} {node.name}({params});")
                elif isinstance(node, (Program, Module)):
                    _emit(node.body)
        _emit(ast)
        return "\n".join(lines)
