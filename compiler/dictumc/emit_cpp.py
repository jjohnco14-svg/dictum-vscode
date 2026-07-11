"""
Dictum C++ Emitter — emits C++17/20/23 from the AST.
Extracted and fixed from transpiler.py v3.3.

Fixes applied (same set as CEmitter where applicable):
  BUG-01:  auto-declare undeclared assignment targets.
  BUG-04:  Module.function call resolution via _MODULE_CALL_MAP.
  BUG-05:  `use Module` → #include, never a function call.
  BUG-06:  decimal number → double.
  BUG-09:  produce success emits clean return.
  BUG-10:  forward declarations for all actions before main().
  MISSING-02: std::string support alongside const char*.
  MISSING-04: `otherwise` plain else branch (parser handles; emitter unchanged).
  MISSING-05: attempt → try/catch.
  MISSING-09: truth value → bool consistently.
"""

from __future__ import annotations
import re
from typing import List, Dict, Optional, Set, Tuple, Any

from .ast_nodes import (
    Node, Program, Module, Shape, Method, Constructor, Destructor,
    VarDecl, Assignment, Action, FuncCall, Return, If, While, ForEach,
    Repeat, Attempt, Literal, Identifier, BinaryOp, UnaryOp,
    FieldAccess, IndexAccess, Assert, Print, ImportC, ImportCpp,
    UnsafeBlock, UnsafeToken, VerifyToken, ExternFn, Transmute, Use, Bind, NewExpr, LambdaExpr,
    Possibilities, HandleTypeDecl, Break,
)

# BUG-04/BUG-05 — reuse same maps from C emitter
from .emit_c import _MODULE_CALL_MAP, _USE_INCLUDE_MAP, _sanitize_action_name


# ── helper: convert ordering string to std::memory_order_* ──────────────────
def _cpp_order(s: str) -> str:
    _MAP = {
        'relaxed':  'std::memory_order_relaxed',
        'acquire':  'std::memory_order_acquire',
        'release':  'std::memory_order_release',
        'acq_rel':  'std::memory_order_acq_rel',
        'seq_cst':  'std::memory_order_seq_cst',
    }
    return _MAP.get(s.lower(), 'std::memory_order_seq_cst')


class CppEmitter:
    def __init__(self, cpp_standard: int = 17) -> None:
        self.output: List[str] = []
        self.indent: int = 0
        self.cpp_standard = cpp_standard
        self.declared_vars: Dict[str, str] = {}
        self._includes: List[str] = []
        self._action_buffer: List[str] = []
        self._extra_includes: List[str] = []
        self._includes_emitted: bool = False
        self.namespace: str = ""
        self.shapes: Dict[str, Any] = {}
        self.actions: Set[str] = set()
        self.imported_containers: Dict[str, str] = {}
        self.imported_actions: Dict[str, Tuple] = {}
        # Nominal handle types (`define handle Name`) collected from
        # anywhere in the source so the typedef can be emitted in the
        # preamble pass regardless of where the declaration appeared.
        self._handle_typedefs: Set[str] = set()
        # Tracks the enclosing `module Name:` block (if any) so nested
        # action definitions can be mangled to Name_action, matching the
        # Module_function call-site convention shared with the C backend
        # (see _MODULE_CALL_MAP / _resolve_call_name fallback).
        self.current_module: Optional[str] = None
        self._module_actions: Dict[str, set] = {}
        self._active_local_modules: set = set()
        self.local_modules: set = set()  # populated by transpiler.py via hasattr, mirrors emit_c.py

        # SOURCE OF TRUTH: derived from type_registry.py -- see that
        # module's docstring.
        from .type_registry import cpp_type_map
        self.types: Dict[str, str] = cpp_type_map()

    # ------------------------------------------------------------------
    def _ptr_alloc_stmt(self, out: str, rhs_expr: str) -> str:
        if out in self.declared_vars:
            known_type = self.declared_vars[out]
            if known_type and known_type not in ("void*", "void *"):
                cpp_type = self.type_to_cpp(known_type)
                return f"{out} = static_cast<{cpp_type}>({rhs_expr});"
            return f"{out} = {rhs_expr};"
        self.declared_vars[out] = "void*"
        return f"void* {out} = {rhs_expr};"

    def emit(self, line: str) -> None:
        self.output.append("    " * self.indent + line)


    # ═══════════════════════════════════════════════════════════════════════
    # _emit_unsafe_token_cpp
    # C++ variant of the special token expansion table.
    # Atomics use std::atomic<T> member functions.
    # SIMD, FFI, memory, bits, endian, pun — identical to C emitter.
    # ═══════════════════════════════════════════════════════════════════════
    def _emit_unsafe_token_cpp(self, node: "UnsafeToken") -> None:
        n   = node.name
        p   = node.params
        res = node.result

        def pa(i, default='0'):
            return p[i] if i < len(p) else default

        # ── C++ ATOMIC OPS (std::atomic<T>) ─────────────────────────────
        if n == 'ATOMIC_LOAD':
            ptr, order, out = pa(0), pa(1,'seq_cst'), pa(2)
            mo = _cpp_order(order)
            self.emit(f"auto {out} = {ptr}->load({mo});")

        elif n == 'ATOMIC_STORE':
            ptr, order, val = pa(0), pa(1,'seq_cst'), pa(2)
            mo = _cpp_order(order)
            self.emit(f"{ptr}->store({val}, {mo});")

        elif n == 'ATOMIC_ADD':
            ptr, val, out = pa(0), pa(1,'1'), pa(2)
            self.emit(f"auto {out} = {ptr}->fetch_add({val}, std::memory_order_seq_cst);")

        elif n == 'ATOMIC_SUB':
            ptr, val, out = pa(0), pa(1,'1'), pa(2)
            self.emit(f"auto {out} = {ptr}->fetch_sub({val}, std::memory_order_seq_cst);")

        elif n == 'ATOMIC_AND':
            ptr, val, out = pa(0), pa(1), pa(2)
            self.emit(f"auto {out} = {ptr}->fetch_and({val}, std::memory_order_seq_cst);")

        elif n == 'ATOMIC_OR':
            ptr, val, out = pa(0), pa(1), pa(2)
            self.emit(f"auto {out} = {ptr}->fetch_or({val}, std::memory_order_seq_cst);")

        elif n == 'ATOMIC_XOR':
            ptr, val, out = pa(0), pa(1), pa(2)
            self.emit(f"auto {out} = {ptr}->fetch_xor({val}, std::memory_order_seq_cst);")

        elif n == 'ATOMIC_FAA':
            ptr, val, out = pa(0), pa(1,'1'), pa(2)
            self.emit(f"auto {out} = {ptr}->fetch_add({val}, std::memory_order_relaxed);")

        elif n == 'ATOMIC_FAS':
            ptr, val, out = pa(0), pa(1), pa(2)
            self.emit(f"auto {out} = {ptr}->exchange({val}, std::memory_order_seq_cst);")

        elif n in ('ATOMIC_CAS_32', 'ATOMIC_CAS_64', 'ATOMIC_CAS_PTR'):
            ptr, exp, des, out = pa(0), pa(1), pa(2), pa(3)
            self.emit(f"auto _exp_{out} = {exp};")
            self.emit(f"bool {out} = {ptr}->compare_exchange_strong(")
            self.emit(f"    _exp_{out}, {des},")
            self.emit(f"    std::memory_order_seq_cst, std::memory_order_relaxed);")

        # ── C++ BARRIERS (std::atomic_thread_fence) ──────────────────────
        elif n == 'BARRIER_ACQUIRE':
            self.emit("std::atomic_thread_fence(std::memory_order_acquire);")
        elif n == 'BARRIER_RELEASE':
            self.emit("std::atomic_thread_fence(std::memory_order_release);")
        elif n == 'BARRIER_SEQ_CST':
            self.emit("std::atomic_thread_fence(std::memory_order_seq_cst);")
        elif n == 'BARRIER_ACQ_REL':
            self.emit("std::atomic_thread_fence(std::memory_order_acq_rel);")
        elif n == 'BARRIER_RELAXED':
            self.emit("std::atomic_thread_fence(std::memory_order_relaxed);")
        elif n == 'COMPILER_BARRIER':
            self.emit('__asm__ __volatile__("" ::: "memory");')

        # ── C++ CAS LOOPS ─────────────────────────────────────────────────
        elif n in ('CAS_LOOP_32', 'CAS_LOOP_64', 'CAS_LOOP_PTR'):
            ptr, exp, des, out = pa(0), pa(1), pa(2), pa(3)
            self.emit(f"{{")
            self.emit(f"  auto _cas_exp = {exp};")
            self.emit(f"  bool {out} = false;")
            self.emit(f"  while (!{out}) {{")
            self.emit(f"    {out} = {ptr}->compare_exchange_weak(")
            self.emit(f"        _cas_exp, {des},")
            self.emit(f"        std::memory_order_seq_cst, std::memory_order_relaxed);")
            self.emit(f"  }}")
            self.emit(f"}}")

        elif n == 'DCAS_LOOP_128':
            ptr, exp, des, out = pa(0), pa(1), pa(2), pa(3)
            self.emit(f"/* DCAS_LOOP_128: requires __int128 + std::atomic<__int128> */")
            self.emit(f"{{")
            self.emit(f"  __int128 _dexp = static_cast<__int128>({exp});")
            self.emit(f"  bool {out} = false;")
            self.emit(f"  while (!{out}) {{")
            self.emit(f"    std::atomic<__int128>* _ap = reinterpret_cast<std::atomic<__int128>*>({ptr});")
            self.emit(f"    {out} = _ap->compare_exchange_weak(")
            self.emit(f"        _dexp, static_cast<__int128>({des}),")
            self.emit(f"        std::memory_order_seq_cst, std::memory_order_relaxed);")
            self.emit(f"  }}")
            self.emit(f"}}")

        # ── HAZARD POINTERS (same semantics as C, std::atomic API) ───────
        elif n == 'HP_PROTECT':
            hp, ptr = pa(0), pa(1)
            self.emit(f"{hp}.store({ptr}, std::memory_order_release);")
        elif n == 'HP_READ':
            hp, src_p, out = pa(0), pa(1), pa(2)
            self.emit(f"auto {out} = {src_p}.load(std::memory_order_acquire);")
            self.emit(f"{hp}.store({out}, std::memory_order_release);")
        elif n == 'HP_CLEAR':
            hp = pa(0)
            self.emit(f"{hp}.store(nullptr, std::memory_order_release);")
        elif n == 'HP_RETIRE':
            table, ptr = pa(0), pa(1)
            self.emit(f"if ({table}.retired_count < HP_MAX_RETIRED)")
            self.emit(f"    {table}.retired[{table}.retired_count++] = {ptr};")
        elif n == 'HP_SCAN':
            table = pa(0)
            self.emit(f"for (int _i = 0; _i < {table}.retired_count; ) {{")
            self.emit(f"  bool _protected = false;")
            self.emit(f"  for (int _j = 0; _j < HP_MAX_THREADS && !_protected; _j++)")
            self.emit(f"    if ({table}.slots[_j].load() == {table}.retired[_i]) _protected = true;")
            self.emit(f"  if (!_protected) {{ delete {table}.retired[_i];")
            self.emit(f"    {table}.retired[_i] = {table}.retired[--{table}.retired_count]; }}")
            self.emit(f"  else ++_i;")
            self.emit(f"}}")

        # ── RCU (std::atomic) ─────────────────────────────────────────────
        elif n == 'RCU_READ_LOCK':
            self.emit("std::atomic_thread_fence(std::memory_order_acquire);")
        elif n == 'RCU_READ_UNLOCK':
            self.emit("std::atomic_thread_fence(std::memory_order_release);")
        elif n == 'RCU_SYNCHRONIZE':
            self.emit("std::atomic_thread_fence(std::memory_order_seq_cst);")
        elif n == 'RCU_ASSIGN_POINTER':
            ptr, val = pa(0), pa(1)
            self.emit(f"{ptr}.store({val}, std::memory_order_release);")
        elif n == 'RCU_DEREFERENCE':
            src_p, out = pa(0), pa(1)
            self.emit(f"auto {out} = {src_p}.load(std::memory_order_acquire);")

        # ── SIMD (identical to C — AVX2 intrinsics don't change) ─────────
        elif n == 'SIMD_LOAD_F32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256 {reg} = _mm256_load_ps(reinterpret_cast<const float*>({ptr}));")
        elif n == 'SIMD_LOADU_F32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256 {reg} = _mm256_loadu_ps(reinterpret_cast<const float*>({ptr}));")
        elif n == 'SIMD_LOAD_I32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256i {reg} = _mm256_load_si256(reinterpret_cast<const __m256i*>({ptr}));")
        elif n == 'SIMD_LOADU_I32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"__m256i {reg} = _mm256_loadu_si256(reinterpret_cast<const __m256i*>({ptr}));")
        elif n == 'SIMD_STORE_F32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"_mm256_store_ps(reinterpret_cast<float*>({ptr}), {reg});")
        elif n == 'SIMD_STOREU_F32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"_mm256_storeu_ps(reinterpret_cast<float*>({ptr}), {reg});")
        elif n == 'SIMD_STORE_I32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"_mm256_store_si256(reinterpret_cast<__m256i*>({ptr}), {reg});")
        elif n == 'SIMD_STOREU_I32':
            ptr, reg = pa(0), pa(1)
            self.emit(f"_mm256_storeu_si256(reinterpret_cast<__m256i*>({ptr}), {reg});")
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

        # ── ALIGNMENT ─────────────────────────────────────────────────────
        elif n == 'IS_ALIGNED':
            ptr, align, out = pa(0), pa(1), pa(2)
            self.emit(f"bool {out} = (reinterpret_cast<uintptr_t>({ptr}) & ({align} - 1)) == 0;")
        elif n == 'ALIGNED_ALLOC_16':
            size, out = pa(0), pa(1)
            self.emit(self._ptr_alloc_stmt(out, f"std::aligned_alloc(16, {size})"))
        elif n == 'ALIGNED_ALLOC_32':
            size, out = pa(0), pa(1)
            self.emit(self._ptr_alloc_stmt(out, f"std::aligned_alloc(32, {size})"))
        elif n == 'ALIGNED_ALLOC_64':
            size, out = pa(0), pa(1)
            self.emit(self._ptr_alloc_stmt(out, f"std::aligned_alloc(64, {size})"))
        elif n == 'ALIGN_UP':
            val, align, out = pa(0), pa(1), pa(2)
            self.emit(f"uintptr_t {out} = ({val} + {align} - 1) & ~(static_cast<uintptr_t>({align}) - 1);")
        elif n == 'ALIGN_DOWN':
            val, align, out = pa(0), pa(1), pa(2)
            self.emit(f"uintptr_t {out} = {val} & ~(static_cast<uintptr_t>({align}) - 1);")

        # ── RAW MEMORY (C++ uses delete[] for array, free for malloc) ─────
        elif n == 'RAW_MALLOC':
            size, out = pa(0), pa(1)
            self.emit(self._ptr_alloc_stmt(out, f"std::malloc({size})"))
        elif n == 'RAW_FREE':
            ptr = pa(0)
            self.emit(f"std::free({ptr});")
        elif n == 'RAW_CALLOC':
            nmemb, size, out = pa(0), pa(1), pa(2)
            self.emit(self._ptr_alloc_stmt(out, f"std::calloc({nmemb}, {size})"))
        elif n == 'RAW_REALLOC':
            ptr, size, out = pa(0), pa(1), pa(2)
            self.emit(self._ptr_alloc_stmt(out, f"std::realloc({ptr}, {size})"))
        elif n == 'RAW_MEMCPY':
            dst, src_p, size = pa(0), pa(1), pa(2)
            self.emit(f"std::memcpy({dst}, {src_p}, {size});")
        elif n == 'RAW_MEMSET':
            dst, val, size = pa(0), pa(1), pa(2)
            self.emit(f"std::memset({dst}, {val}, {size});")
        elif n == 'RAW_MEMCMP':
            a, b, size, out = pa(0), pa(1), pa(2), pa(3)
            self.emit(f"int {out} = std::memcmp({a}, {b}, {size});")
        elif n == 'RAW_MEMMOVE':
            dst, src_p, size = pa(0), pa(1), pa(2)
            self.emit(f"std::memmove({dst}, {src_p}, {size});")

        # ── FFI (identical to C — dlopen/dlsym unchanged) ─────────────────
        elif n == 'FFI_LOAD':
            path, handle = pa(0), pa(1)
            self.emit(f"void* {handle} = dlopen({path}, RTLD_LAZY);")
        elif n == 'FFI_SYMBOL':
            handle, sym, fptr = pa(0), pa(1), pa(2)
            self.emit(f"void* {fptr} = dlsym({handle}, {sym});")
        elif n == 'FFI_CALL_VOID':
            fptr = pa(0); args = ', '.join(p[1:])
            self.emit(f"reinterpret_cast<void(*)()>({fptr})({args});")
        elif n == 'FFI_CALL_INT':
            fptr = pa(0); args = ', '.join(p[1:-1]); out = p[-1] if p else 'result'
            self.emit(f"int {out} = reinterpret_cast<int(*)()>({fptr})({args});")
        elif n == 'FFI_CALL_FLOAT':
            fptr = pa(0); args = ', '.join(p[1:-1]); out = p[-1] if p else 'result'
            self.emit(f"double {out} = reinterpret_cast<double(*)()>({fptr})({args});")
        elif n == 'FFI_CALL_PTR':
            fptr = pa(0); args = ', '.join(p[1:-1]); out = p[-1] if p else 'result'
            self.emit(f"void* {out} = reinterpret_cast<void*(*)()>({fptr})({args});")
        elif n == 'FFI_CLOSE':
            handle = pa(0)
            self.emit(f"dlclose({handle});")

        # ── BITS (identical to C) ─────────────────────────────────────────
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
            self.emit(f"bool {out} = ({val} >> {bit}) & 1;")
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

        # ── ENDIAN (identical to C) ───────────────────────────────────────
        elif n == 'SWAP_ENDIAN_16':
            val, out = pa(0), pa(1)
            self.emit(f"uint16_t {out} = __builtin_bswap16({val});")
        elif n == 'SWAP_ENDIAN_32':
            val, out = pa(0), pa(1)
            self.emit(f"uint32_t {out} = __builtin_bswap32({val});")
        elif n == 'SWAP_ENDIAN_64':
            val, out = pa(0), pa(1)
            self.emit(f"uint64_t {out} = __builtin_bswap64({val});")
        elif n in ('HTON_16','NTOH_16'):
            val, out = pa(0), pa(1)
            self.emit(f"uint16_t {out} = __builtin_bswap16({val});")
        elif n in ('HTON_32','NTOH_32'):
            val, out = pa(0), pa(1)
            self.emit(f"uint32_t {out} = __builtin_bswap32({val});")
        elif n in ('HTON_64','NTOH_64'):
            val, out = pa(0), pa(1)
            self.emit(f"uint64_t {out} = __builtin_bswap64({val});")

        # ── TYPE PUNNING (C++ memcpy — undefined behaviour safe) ──────────
        elif n == 'PUN_INT_TO_FLOAT':
            val, out = pa(0), pa(1)
            self.emit(f"float {out}; std::memcpy(&{out}, &{val}, sizeof(float));")
        elif n == 'PUN_FLOAT_TO_INT':
            val, out = pa(0), pa(1)
            self.emit(f"uint32_t {out}; std::memcpy(&{out}, &{val}, sizeof(uint32_t));")
        elif n == 'PUN_PTR_TO_INT':
            ptr, out = pa(0), pa(1)
            self.emit(f"uintptr_t {out} = reinterpret_cast<uintptr_t>({ptr});")
        elif n == 'PUN_INT_TO_PTR':
            val, out = pa(0), pa(1)
            self.emit(f"void* {out} = reinterpret_cast<void*>(static_cast<uintptr_t>({val}));")
        elif n == 'PUN_READ_UNALIGNED_16':
            ptr, out = pa(0), pa(1)
            self.emit(f"uint16_t {out}; std::memcpy(&{out}, {ptr}, 2);")
        elif n == 'PUN_READ_UNALIGNED_32':
            ptr, out = pa(0), pa(1)
            self.emit(f"uint32_t {out}; std::memcpy(&{out}, {ptr}, 4);")
        elif n == 'PUN_READ_UNALIGNED_64':
            ptr, out = pa(0), pa(1)
            self.emit(f"uint64_t {out}; std::memcpy(&{out}, {ptr}, 8);")

        else:
            params_str = " : ".join(node.params)
            self.emit(f"/* UNKNOWN_UNSAFE_TOKEN: [{n}: {params_str}] */")


    def type_to_cpp(self, t: str) -> str:
        if t.startswith('unique handle to '):
            return f"std::unique_ptr<{self.type_to_cpp(t[len('unique handle to '):].strip())}>"
        if t.startswith('shared handle to '):
            return f"std::shared_ptr<{self.type_to_cpp(t[len('shared handle to '):].strip())}>"
        if t.startswith('weak handle to '):
            return f"std::weak_ptr<{self.type_to_cpp(t[len('weak handle to '):].strip())}>"
        if t.startswith('raw handle to '):
            return f"{self.type_to_cpp(t[len('raw handle to '):].strip())}*"
        # 'pointer to' forms (bare-pointer parser output, see parser.py) —
        # same mapping as the corresponding '... handle to' forms above.
        if t.startswith('unique pointer to '):
            return f"std::unique_ptr<{self.type_to_cpp(t[len('unique pointer to '):].strip())}>"
        if t.startswith('shared pointer to '):
            return f"std::shared_ptr<{self.type_to_cpp(t[len('shared pointer to '):].strip())}>"
        if t.startswith('weak pointer to '):
            return f"std::weak_ptr<{self.type_to_cpp(t[len('weak pointer to '):].strip())}>"
        if t.startswith('raw pointer to '):
            return f"{self.type_to_cpp(t[len('raw pointer to '):].strip())}*"
        # Bare wrapper pointer with no inner type given — no way to know the
        # pointee type, so fall back to an opaque pointer rather than
        # mangling the identifier.
        if t in ('unique pointer', 'shared pointer', 'weak pointer', 'raw pointer',
                 'unique handle', 'shared handle', 'weak handle', 'raw handle'):
            return "void*"
        if t.startswith('const ref '):
            return f"const {self.type_to_cpp(t[len('const ref '):].strip())}&"
        if t.startswith('ref '):
            return f"{self.type_to_cpp(t[len('ref '):].strip())}&"
        if t.startswith('move '):
            return f"{self.type_to_cpp(t[len('move '):].strip())}&&"
        if t.startswith('action taking '):
            return "std::function<bool(int32_t)>"
        if t.startswith('*'):
            rest = t[1:].strip()
            if rest.startswith('volatile'):
                inner = rest[8:].strip()
                return f"volatile {self.types.get(inner, inner.replace(' ', '_'))}*"
            return f"{self.types.get(rest, rest.replace(' ', '_'))}*"
        if t.endswith(' list') or t.endswith(' array'):
            # Return vector type for list/array in C++
            elem = t.rsplit(' ', 1)[0].strip()
            return f"std::vector<{self.type_to_cpp(elem)}>"
        if t in self.imported_containers:
            return self.imported_containers[t]
        base = self.types.get(t, t.replace(" ", "_"))
        if '.' in base:
            base = base.replace('.', '::')
        return base

    def _resolve_call_name(self, name: str) -> str:
        if '.' in name:
            return _MODULE_CALL_MAP.get(name, name.replace('.', '_'))
        if self.current_module and name in self._module_actions.get(self.current_module, ()):
            return f"{self.current_module}_{name}"
        for mod in self._active_local_modules:
            if name in self._module_actions.get(mod, ()):
                return f"{mod}_{name}"
        return name

    # ------------------------------------------------------------------
    def expr_to_cpp(self, node: Node) -> str:
        if isinstance(node, Literal):
            if isinstance(node.value, bool):
                return "true" if node.value else "false"
            if isinstance(node.value, str):
                if node.value in ("nothing", "null", "NULL"):
                    return "nullptr"   # P7.2: nothing → nullptr in C++
                if node.value == "\n": return '"\\n"'
                return f'"{node.value}"'
            if isinstance(node.value, list):
                return "{" + ", ".join(str(v) for v in node.value) + "}"
            if node.value is None:
                return "nullptr"
            return str(node.value)
        elif isinstance(node, Identifier):
            return node.name
        elif isinstance(node, FieldAccess):
            # Smart pointer field access → ->
            base_type = self.declared_vars.get(node.obj, '')
            if any(base_type.startswith(p) for p in
                   ['unique handle to ', 'shared handle to ', 'weak handle to ', 'raw handle to ']):
                return f"{node.obj}->{node.field}"
            return f"{node.obj}.{node.field}"
        elif isinstance(node, IndexAccess):
            return f"{node.collection}[{self.expr_to_cpp(node.index)}]"
        elif isinstance(node, BinaryOp):
            left = self.expr_to_cpp(node.left)
            right = self.expr_to_cpp(node.right)
            if right in ('"empty"', "'empty'"):
                right = 'nullptr'
            if node.op == 'pow':
                return f"std::pow({left}, {right})"
            return f"({left} {node.op} {right})"
        elif isinstance(node, UnaryOp):
            op = node.op; operand = self.expr_to_cpp(node.operand)
            if op == "count":   return f"sizeof({operand}) / sizeof({operand}[0])"
            if op == "length":  return f"std::strlen({operand})"
            if op == "tanh":    return f"std::tanh({operand})"
            if op == "sqrt":    return f"std::sqrt({operand})"
            if op == "exp":     return f"std::exp({operand})"
            if op == "sin":     return f"std::sin({operand})"
            if op == "cos":     return f"std::cos({operand})"
            if op == "room_for":
                return f"std::make_unique<int32_t[]>({operand})"
            if op == "addrof":  return f"(&{operand})"
            if op == "deref":   return f"(*{operand})"
            if op == "neg":     return f"(-{operand})"
            return f"({op}{operand})"
        elif isinstance(node, Transmute):
            return f"static_cast<{self.type_to_cpp(node.type)}>({self.expr_to_cpp(node.expr)})"
        elif isinstance(node, NewExpr):
            type_name = node.type_name.replace('.', '::')
            args = ", ".join(self.expr_to_cpp(a) for a in node.args)
            if args:
                return f"std::make_unique<{type_name}>({args})"
            return f"std::make_unique<{type_name}>()"
        elif isinstance(node, LambdaExpr):
            params = ", ".join(f"{self.type_to_cpp(pt)} {pn}" for pn, pt in node.params)
            ret = self.type_to_cpp(node.ret_type)
            body_lines: List[str] = []
            saved, saved_indent = self.output, self.indent
            self.output = body_lines; self.indent = 0
            for stmt in node.body:
                self.emit_node(stmt)
            self.output = saved; self.indent = saved_indent
            body_str = " ".join(l.strip() for l in body_lines)
            captures = self._analyze_captures(node.body, {p[0] for p in node.params})
            cap_str = ", ".join(f"&{c}" for c in sorted(captures))
            return f"[{cap_str}]({params}) -> {ret} {{ {body_str} }}"
        elif isinstance(node, FuncCall):
            c_name = self._resolve_call_name(node.name)
            processed = []
            for a in node.args:
                arg_str = self.expr_to_cpp(a)
                if isinstance(a, Identifier) and a.name in self.declared_vars:
                    vt = self.declared_vars[a.name]
                    if any(vt.startswith(p) for p in
                           ['unique handle to ', 'shared handle to ', 'weak handle to ']):
                        arg_str = f"(*{arg_str})"
                processed.append(arg_str)
            args = ", ".join(processed)
            if c_name in ('success', '__produce_success'):
                return args
            if c_name == 'failure':
                return f"/* failure: {args} */ 0"
            if '->' in c_name:
                parts = c_name.split('->')
                return f"{parts[0]}->{parts[1]}({args})"
            return f"{c_name}({args})"
        return f"/* expr: {type(node).__name__} */"

    def lvalue_to_cpp(self, target: str) -> str:
        if '.' in target:
            parts = target.split('.')
            base = parts[0]
            if base in self.declared_vars:
                bt = self.declared_vars[base]
                if any(bt.startswith(p) for p in
                       ['unique handle to ', 'shared handle to ', 'weak handle to ', 'raw handle to ']):
                    dot_path = '.'.join(parts[1:])
                    return f"{base}->{dot_path}"
        return target

    def _infer_type_from_expr(self, node: Node) -> Optional[str]:
        if isinstance(node, Literal):
            if isinstance(node.value, bool):  return "bool"
            if isinstance(node.value, int):   return "int32_t"
            if isinstance(node.value, float): return "double"
            if isinstance(node.value, str):   return "const char*"
        if isinstance(node, Identifier):
            return self.declared_vars.get(node.name)
        if isinstance(node, BinaryOp):
            lt = self._infer_type_from_expr(node.left)
            if node.op in ('==', '!=', '>', '<', '>=', '<='): return "bool"
            return lt or self._infer_type_from_expr(node.right)
        if isinstance(node, NewExpr):
            return f"std::unique_ptr<{node.type_name.replace('.', '::')}>"
        return None

    # ------------------------------------------------------------------
    def emit_node(self, node: Node) -> None:

        # ----------------------------------------------------------------
        if isinstance(node, Program):
            # Pre-scan for polyglot imports and build directives (add to extra_includes)
            try:
                from .polyglot_ast import PolyglotImport, BuildDirective
                for stmt in node.body:
                    if isinstance(stmt, PolyglotImport):
                        inc = f'#include "{stmt.module_name}_cxx.hpp"  /* polyglot import {stmt.module_name} */'
                        if inc not in self._extra_includes:
                            self._extra_includes.append(inc)
                    elif isinstance(stmt, BuildDirective):
                        if stmt.kind in ('cflags', 'ldflags', 'link', 'include_path'):
                            self._extra_includes.append(f'/* #[{stmt.kind} "{stmt.value}"] */')
            except ImportError:
                pass
            self._emit_includes()
            self.emit("")
            ns = self.namespace
            if ns:
                self.emit(f"namespace {ns} {{")
                self.indent += 1
            # Pre-pass: register global var types
            for stmt in node.body:
                if isinstance(stmt, VarDecl):
                    self.declared_vars[stmt.name] = self.type_to_cpp(stmt.type)
            # Shapes, imports, externs, forward decls
            for stmt in node.body:
                if isinstance(stmt, (Shape, Possibilities)):
                    self.emit_node(stmt)
            self.emit("")
            # Nominal handle types: `define handle Db` -> typedef void* Db;
            for stmt in node.body:
                if isinstance(stmt, HandleTypeDecl):
                    self._handle_typedefs.add(stmt.name)
            if self._handle_typedefs:
                for hname in sorted(self._handle_typedefs):
                    self.emit(f"typedef void* {hname};")
                self.emit("")
            # BUG-10: forward declarations
            for stmt in node.body:
                if isinstance(stmt, Action):
                    self._emit_fwd_decl(stmt)
                elif isinstance(stmt, (ImportC, ExternFn, ImportCpp)):
                    self.emit_node(stmt)
            # Flush buffered actions
            if self._action_buffer:
                self.emit("")
                for line in self._action_buffer:
                    self.output.append(line)
                self._action_buffer.clear()
            self.emit("")
            # Global variables
            for stmt in node.body:
                if isinstance(stmt, VarDecl):
                    self.emit_node(stmt)
            self.emit("")
            # Module bodies
            for stmt in node.body:
                if isinstance(stmt, Module):
                    self.emit_node(stmt)
            self.emit("")
            # Action definitions
            for stmt in node.body:
                if isinstance(stmt, Action):
                    self.emit_node(stmt)
            self.emit("")
            # main()
            self.emit("int main() {")
            self.indent += 1
            for stmt in node.body:
                if isinstance(stmt, (If, While, ForEach, Repeat, Assignment,
                                     Print, Assert, FuncCall, UnsafeBlock, Attempt, Use)):
                    self.emit_node(stmt)
                else:
                    try:
                        from .polyglot_ast import (
                            PolyglotModule, PolyglotCall, UnsafeForeignCall,
                        )
                        if isinstance(stmt, (PolyglotModule, PolyglotCall, UnsafeForeignCall)):
                            self.emit_node(stmt)
                    except ImportError:
                        pass
            self.indent -= 1
            self.emit("    return 0;")
            self.emit("}")
            if ns:
                self.indent -= 1
                self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Module):
            # NOTE: previously wrapped nested actions in a C++ namespace
            # (`namespace Name { ... }`), but call sites resolve
            # `Module.action` to the flat mangled name `Module_action`
            # (see _resolve_call_name / _MODULE_CALL_MAP, shared with the
            # C backend) — so an unmangled namespace member never matched
            # what callers actually invoke. Worse, when this Module node
            # is visited before the Program's preamble (raw top-level
            # source order), nested Action emission silently redirected
            # into _action_buffer via the include-buffering branch,
            # bypassing the namespace `{` that had just been written and
            # landing the function body at global scope with its
            # unmangled name. Mangling here instead — matching the C
            # backend's `current_module`-prefixed approach — fixes both
            # problems at once and needs no namespace wrapper at all.
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
            self.emit(f"enum class {node.name} {{")
            self.indent += 1
            for v in node.variants:
                self.emit(f"{v},")
            self.indent -= 1
            self.emit("};")
            self.emit("")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Shape):
            self.shapes[node.name] = {f: t for f, t in node.fields}
            is_class = bool(node.methods or node.constructors or node.destructor or node.parent)
            if is_class:
                base = f"class {node.name}"
                if node.is_packed:
                    base = f"class __attribute__((packed)) {node.name}"
                if node.parent:
                    base += f" : public {node.parent}"
                self.emit(f"{base} {{")
                self.indent += 1
                self.emit("public:")
                for fname, ftype in node.fields:
                    self.emit(f"{self.type_to_cpp(ftype)} {fname};")
                if not any(len(c.params) == 0 for c in node.constructors):
                    self.emit(f"{node.name}() = default;")
                for ctor in node.constructors:
                    params = ", ".join(f"{self.type_to_cpp(pt)} {pn}" for pn, pt in ctor.params)
                    self.emit(f"{node.name}({params}) {{")
                    self.indent += 1
                    for stmt in ctor.body:
                        self.emit_node(stmt)
                    self.indent -= 1
                    self.emit("}")
                if node.destructor:
                    self.emit(f"~{node.name}() {{")
                    self.indent += 1
                    for stmt in node.destructor.body:
                        self.emit_node(stmt)
                    self.indent -= 1
                    self.emit("}")
                for method in node.methods:
                    params = ", ".join(f"{self.type_to_cpp(pt)} {pn}" for pn, pt in method.params)
                    ret = self.type_to_cpp(method.ret_type)
                    virt = "virtual " if (method.is_virtual or method.is_override or node.parent) else ""
                    override = " override" if method.is_override else ""
                    self.emit(f"{virt}{ret} {method.name}({params}){override} {{")
                    self.indent += 1
                    for stmt in method.body:
                        self.emit_node(stmt)
                    self.indent -= 1
                    self.emit("}")
                self.indent -= 1
                self.emit("};")
            else:
                pfx = "struct __attribute__((packed))" if node.is_packed else "struct"
                self.emit(f"{pfx} {node.name} {{")
                self.indent += 1
                for fname, ftype in node.fields:
                    self.emit(f"{self.type_to_cpp(ftype)} {fname};")
                self.indent -= 1
                self.emit("};")
            self.emit("")
            return

        # ----------------------------------------------------------------
        if isinstance(node, VarDecl):
            raw_type = node.type
            is_array = raw_type.endswith(' list') or raw_type.endswith(' array')
            ct = self.type_to_cpp(raw_type)
            self.declared_vars[node.name] = raw_type
            if node.value is None:
                self.emit(f"{ct} {node.name};  /* uninitialized */")
            elif isinstance(node.value, Literal) and isinstance(node.value.value, list):
                vals = ", ".join(
                    str(v).lower() if isinstance(v, bool) else str(v)
                    for v in node.value.value
                )
                size = len(node.value.value)
                elem_t = self.type_to_cpp(raw_type.rsplit(' ', 1)[0].strip()) if is_array else ct
                self.emit(f"{elem_t} {node.name}[{size}] = {{{vals}}};")
                self.emit(f"const size_t {node.name}_count = {size};")
            elif isinstance(node.value, UnaryOp) and node.value.op == "room_for":
                operand = self.expr_to_cpp(node.value.operand)
                if raw_type.startswith('unique handle to '):
                    inner = raw_type[len('unique handle to '):].strip()
                    self.emit(f"{ct} {node.name} = std::make_unique<{self.type_to_cpp(inner)}[]>({operand});")
                elif raw_type.startswith('shared handle to '):
                    inner = raw_type[len('shared handle to '):].strip()
                    self.emit(f"{ct} {node.name} = std::make_shared<{self.type_to_cpp(inner)}[]>({operand});")
                else:
                    self.emit(f"{ct} {node.name} = std::make_unique<int32_t[]>({operand});")
            else:
                # Smart pointer assignment from NewExpr
                if isinstance(node.value, NewExpr):
                    type_name = node.value.type_name.replace('.', '::')
                    args = ", ".join(self.expr_to_cpp(a) for a in node.value.args)
                    if raw_type.startswith('unique handle to '):
                        inner = self.type_to_cpp(raw_type[len('unique handle to '):].strip())
                        if args:
                            self.emit(f"{ct} {node.name} = std::make_unique<{inner}>({args});")
                        else:
                            self.emit(f"{ct} {node.name} = std::make_unique<{inner}>();")
                        return
                    elif raw_type.startswith('shared handle to '):
                        inner = self.type_to_cpp(raw_type[len('shared handle to '):].strip())
                        if args:
                            self.emit(f"{ct} {node.name} = std::make_shared<{inner}>({args});")
                        else:
                            self.emit(f"{ct} {node.name} = std::make_shared<{inner}>();")
                        return
                    else:
                        # BUGFIX: `keep p as Person with value new Person` (no
                        # explicit "unique handle to" prefix) previously fell
                        # through to the generic path below, emitting
                        # `Person p = std::make_unique<Person>();` -- a
                        # type mismatch (unique_ptr assigned to a by-value
                        # struct). Treat this the same as an explicit
                        # `unique handle to <Type>` declaration: mark it as
                        # such in declared_vars so the existing FieldAccess /
                        # lvalue_to_cpp arrow-detection (below) applies
                        # automatically, and emit a real unique_ptr.
                        inner = self.type_to_cpp(raw_type)
                        self.declared_vars[node.name] = f"unique handle to {raw_type}"
                        if args:
                            self.emit(f"auto {node.name} = std::make_unique<{inner}>({args});")
                        else:
                            self.emit(f"auto {node.name} = std::make_unique<{inner}>();")
                        return
                val = self.expr_to_cpp(node.value)
                self.emit(f"{ct} {node.name} = {val};")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Assignment):
            target = self.lvalue_to_cpp(node.target)
            val = self.expr_to_cpp(node.value)
            # Smart pointer reset from NewExpr
            target_type = self.declared_vars.get(node.target, '')
            if isinstance(node.value, NewExpr):
                type_name = node.value.type_name.replace('.', '::')
                args = ", ".join(self.expr_to_cpp(a) for a in node.value.args)
                if target_type.startswith('unique handle to '):
                    inner = self.type_to_cpp(target_type[len('unique handle to '):].strip())
                    if args:
                        self.emit(f"{target} = std::make_unique<{inner}>({args});")
                    else:
                        self.emit(f"{target} = std::make_unique<{inner}>();")
                    return
                elif target_type.startswith('shared handle to '):
                    inner = self.type_to_cpp(target_type[len('shared handle to '):].strip())
                    if args:
                        self.emit(f"{target} = std::make_shared<{inner}>({args});")
                    else:
                        self.emit(f"{target} = std::make_shared<{inner}>();")
                    return
            # BUG-01 FIX: auto-declare
            base_name = node.target.split('[')[0].split('.')[0]
            if (base_name not in self.declared_vars and
                    '[' not in node.target and '.' not in node.target):
                inferred = self._infer_type_from_expr(node.value) or "int32_t"
                self.declared_vars[base_name] = inferred
                self.emit(f"{inferred} {target} = {val};")
            else:
                self.emit(f"{target} = {val};")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Action):
            self.actions.add(node.name)
            safe_name = _sanitize_action_name(node.name)
            if self.current_module:
                # Module-scoped actions are called as Module.action, which
                # _resolve_call_name turns into Module_action — match that
                # here (same convention as the C backend).
                safe_name = f"{self.current_module}_{safe_name}"
            template_decl = ""
            if node.template_params:
                if self.cpp_standard >= 20:
                    tparams = ", ".join(f"typename {tp[0]}" for tp in node.template_params)
                else:
                    tparams = ", ".join(f"typename {tp[0]}" for tp in node.template_params)
                template_decl = f"template <{tparams}>"

            params = ", ".join(f"{self.type_to_cpp(pt)} {pn}" for pn, pt in node.params)
            ret = self.type_to_cpp(node.ret_type)
            if node.ret_type == 'result':
                ret = 'std::optional<int32_t>'

            # Buffer if includes not yet emitted
            if not self._includes_emitted:
                saved, saved_indent = self.output, self.indent
                self.output = self._action_buffer; self.indent = 0
                if template_decl: self.emit(template_decl)
                self.emit(f"{ret} {safe_name}({params}) {{")
                self.indent += 1
                for stmt in node.body: self.emit_node(stmt)
                self.indent -= 1
                self.emit("}")
                self.emit("")
                self.output = saved; self.indent = saved_indent
            else:
                if template_decl: self.emit(template_decl)
                self.emit(f"{ret} {safe_name}({params}) {{")
                self.indent += 1
                for stmt in node.body: self.emit_node(stmt)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            return

        # ----------------------------------------------------------------
        if isinstance(node, If):
            cond = self.expr_to_cpp(node.cond)
            cond = cond.replace('== "empty"', "== nullptr").replace("== 'empty'", "== nullptr")
            self.emit(f"if ({cond}) {{")
            self.indent += 1
            for stmt in node.then_body: self.emit_node(stmt)
            self.indent -= 1
            if node.else_body:
                self.emit("} else {")
                self.indent += 1
                for stmt in node.else_body: self.emit_node(stmt)
                self.indent -= 1
            self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, While):
            self.emit(f"while ({self.expr_to_cpp(node.cond)}) {{")
            self.indent += 1
            for stmt in node.body: self.emit_node(stmt)
            self.indent -= 1
            self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, ForEach):
            self.emit(f"for (auto& {node.item} : {node.collection}) {{")
            self.indent += 1
            for stmt in node.body: self.emit_node(stmt)
            self.indent -= 1
            self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Repeat):
            count = self.expr_to_cpp(node.count)
            self.emit(f"for (int32_t {node.counter} = 0; {node.counter} < {count}; {node.counter}++) {{")
            self.indent += 1
            for stmt in node.body: self.emit_node(stmt)
            self.indent -= 1
            self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Attempt):
            # MISSING-05 FIX: complete try/catch
            fail_name = node.failure_name or "e"
            self.emit("try {")
            self.indent += 1
            if node.call is not None:
                call_expr = self.expr_to_cpp(node.call)
                result = node.result_name or "__result"
                inferred = self._infer_type_from_expr(node.call) or "auto"
                self.emit(f"auto {result} = {call_expr};")
                self.declared_vars[result] = inferred
            for stmt in node.success_body:
                self.emit_node(stmt)
            self.indent -= 1
            self.emit(f"}} catch (const std::exception& {fail_name}) {{")
            self.indent += 1
            if node.failure_body:
                for stmt in node.failure_body:
                    self.emit_node(stmt)
            else:
                self.emit(f"/* unhandled exception: {fail_name}.what() */")
            self.indent -= 1
            self.emit("}")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Return):
            # BUG-09 FIX
            if isinstance(node.value, FuncCall):
                if node.value.name in ('__produce_success', 'success'):
                    inner = self.expr_to_cpp(node.value.args[0]) if node.value.args else ""
                    self.emit(f"return {inner};")
                    return
                if node.value.name == 'failure':
                    msg = self.expr_to_cpp(node.value.args[0]) if node.value.args else '"error"'
                    self.emit(f"throw std::runtime_error({msg});")
                    return
            self.emit(f"return {self.expr_to_cpp(node.value)};")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Assert):
            self.emit(f"assert({self.expr_to_cpp(node.cond)});")
            return

        # ----------------------------------------------------------------
        if isinstance(node, Print):
            fmt_parts, args = [], []
            for p in node.parts:
                if isinstance(p, Literal) and isinstance(p.value, str):
                    escaped = p.value.replace("\\", "\\\\").replace("\n", "\\n")
                    fmt_parts.append(escaped)
                else:
                    spec = self._format_spec(p)
                    fmt_parts.append(spec)
                    expr = self.expr_to_cpp(p)
                    if isinstance(p, Identifier) and p.name in self.declared_vars:
                        vt = self.declared_vars[p.name]
                        if any(vt.startswith(px) for px in
                               ['unique handle to ', 'shared handle to ', 'weak handle to ']):
                            expr = f"*{expr}"
                    args.append(expr)
            fmt = "".join(fmt_parts)
            if args:
                self.emit(f'std::printf("{fmt}", {", ".join(args)});')
            else:
                self.emit(f'std::printf("{fmt}");')
            return

        # ----------------------------------------------------------------
        if isinstance(node, FuncCall):
            c_name = self._resolve_call_name(node.name)
            if c_name == "__defer_release":
                self.emit(f"/* defer: {self.expr_to_cpp(node.args[0])} */")
            elif c_name == "release":
                arg = self.expr_to_cpp(node.args[0])
                arg_type = self.declared_vars.get(getattr(node.args[0], 'name', ''), '')
                if any(arg_type.startswith(p) for p in ('unique handle to ', 'shared handle to ')):
                    self.emit(f"{arg}.reset();")
                else:
                    self.emit(f"delete {arg};")
            else:
                args = ", ".join(self.expr_to_cpp(a) for a in node.args)
                if '->' in c_name:
                    parts = c_name.split('->')
                    self.emit(f"{parts[0]}->{parts[1]}({args});")
                else:
                    self.emit(f"{c_name}({args});")
            return

        # ----------------------------------------------------------------
        if isinstance(node, ImportC):
            # `action_name` is the real C symbol (e.g. "free", "sqrt");
            # `alias` is the Dictum-side call name, which commonly
            # differs ("import from C the action free ... as
            # release_buffer"). Previously this declared `extern <ret>
            # {alias}(...)` directly -- i.e. treated the alias as if it
            # were the real external symbol -- which left the actual
            # alias name with no definition anywhere, producing an
            # undefined-reference at link time whenever alias !=
            # action_name. Mirrors the C backend's fix: declare the real
            # symbol, then provide an inline wrapper under the alias.
            cpp_params = [self.type_to_cpp(p) for p in node.params]
            ret_cpp = self.type_to_cpp(node.ret_type)
            params_decl = ", ".join(cpp_params)
            self.emit(f"extern {ret_cpp} {node.action_name}({params_decl});")
            if node.alias and node.alias != node.action_name:
                arg_names = [f"a{i}" for i in range(len(cpp_params))]
                wrapper_params = ", ".join(f"{t} {n}" for t, n in zip(cpp_params, arg_names))
                call_args = ", ".join(arg_names)
                self.emit(f"static inline {ret_cpp} {node.alias}({wrapper_params}) "
                          f"{{ return {node.action_name}({call_args}); }}")
            return

        if isinstance(node, ImportCpp):
            if node.item_type == 'action':
                self.imported_actions[node.alias] = (node.params, node.ret_type)
            elif node.item_type == 'container':
                self.imported_containers[node.alias] = self._map_container(node.item_name)
            return

        if isinstance(node, ExternFn):
            params = ", ".join(f"{self.type_to_cpp(pt)} {pn}" for pn, pt in node.params)
            ret = self.type_to_cpp(node.ret_type)
            if node.syscall_name:
                self.emit(f"/* @syscall: {node.syscall_name} */")
            self.emit(f"extern {ret} {node.name}({params});")
            return

        if isinstance(node, Use):
            if node.path in self.local_modules:
                self._active_local_modules.add(node.path)
                return
            # BUG-05 FIX
            inc_path = _USE_INCLUDE_MAP.get(node.path, f"dictum_{node.path.lower()}.h")
            if node.is_system or not inc_path.startswith('dictum_'):
                inc_line = f"#include <{inc_path}>"
            else:
                inc_line = f'#include "{inc_path}"'
            if self._includes_emitted:
                self.emit(inc_line)
            else:
                self._extra_includes.append(inc_line)
            return

        # ── HandleTypeDecl — nominal handle type (`define handle Name`) ──
        if isinstance(node, HandleTypeDecl):
            # Typedef itself is emitted in the Program preamble pass (see
            # the `Shapes, imports, externs, forward decls` block above);
            # this just covers a bare top-level occurrence outside any
            # Program/Module body so it doesn't fall through to the
            # generic "unhandled" comment.
            self._handle_typedefs.add(node.name)
            return

        # ── Break — `stop repeating` exits the nearest enclosing loop ──
        if isinstance(node, Break):
            self.emit("break;")
            return

        # ── VerifyToken (L2) → C comment ────────────────────────────────
        if isinstance(node, VerifyToken):
            self.emit(f"/* [VERIFY:{node.key}] */")
            return

        # ── UnsafeToken (L3) → C++ intrinsics ───────────────────────────
        if isinstance(node, UnsafeToken):
            self._emit_unsafe_token_cpp(node)
            return

        # ── UnsafeBlock — walk body ──────────────────────────────────────
        if isinstance(node, UnsafeBlock):
            self.emit("/* unsafe block — L3 token expansion */")
            for stmt in node.body:
                self.emit_node(stmt)
            return

        # ----------------------------------------------------------------
        # Polyglot nodes — C++ emitter treatment
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
                ns = node.name
                self.emit(f"namespace {ns} {{  /* polyglot module backend={node.backend} safety={node.safety} */")
                self.indent += 1
                for stmt in node.body:
                    self.emit_node(stmt)
                self.indent -= 1
                self.emit(f"}}  /* namespace {ns} */")
                return

            if isinstance(node, PolyglotImport):
                inc = f"{node.module_name}_cxx.hpp"
                self.emit(f'#include "{inc}"  /* polyglot import {node.module_name} */')
                return

            if isinstance(node, PolyglotCall):
                # C++ calls go through the namespace wrapper
                ns_fn = f"dictum::{node.module}::{node.function}"
                args = ", ".join(self.expr_to_cpp(a) for a in node.args)
                if node.result_name:
                    self.emit(f"auto {node.result_name} = {ns_fn}({args});")
                else:
                    self.emit(f"{ns_fn}({args});")
                return

            if isinstance(node, UnsafeForeignCall):
                args = ", ".join(self.expr_to_cpp(a) for a in node.args)
                ret_type = self.type_to_cpp(node.result_type) if node.result_type else "void*"
                if node.result_name:
                    self.emit(f"/* unsafe foreign call */")
                    self.emit(f"auto {node.result_name} = reinterpret_cast<{ret_type}(*)()>"
                               f"(\"{node.symbol}\")();  /* {args} */")
                else:
                    self.emit(f"/* unsafe foreign: {node.symbol}({args}) */")
                return

            if isinstance(node, BuildDirective):
                if node.kind == 'link':
                    self.emit(f"/* #[link \"{node.value}\"] — add to target_link_libraries() */")
                else:
                    self.emit(f"/* #[{node.kind} \"{node.value}\"] */")
                return

            if isinstance(node, ForeignShape):
                pfx = "struct __attribute__((packed))" if node.packed else "struct"
                self.emit(f"/* foreign {node.source_language} struct: {node.name} */")
                self.emit(f"extern \"C\" {pfx} {node.name} {{")
                self.indent += 1
                for fname, ftype in node.fields:
                    self.emit(f"{self.type_to_cpp(ftype)} {fname};")
                self.indent -= 1
                self.emit("};")
                return

        self.emit(f"/* unhandled: {type(node).__name__} */")

    # ------------------------------------------------------------------
    def _emit_fwd_decl(self, node: Action) -> None:
        """BUG-10 FIX: emit forward declaration."""
        params = ", ".join(f"{self.type_to_cpp(pt)} {pn}" for pn, pt in node.params)
        ret = self.type_to_cpp(node.ret_type)
        if node.ret_type == 'result':
            ret = 'std::optional<int32_t>'
        if node.template_params:
            tparams = ", ".join(f"typename {tp[0]}" for tp in node.template_params)
            self.emit(f"template <{tparams}>")
        self.emit(f"{ret} {node.name}({params});")

    def _emit_includes(self) -> None:
        includes = [
            "#include <cstdint>", "#include <cstdbool>", "#include <cstdio>",
            "#include <cstdlib>", "#include <cstring>", "#include <cassert>",
            "#include <cmath>", "#include <memory>", "#include <vector>",
            "#include <map>", "#include <string>", "#include <functional>",
            "#include <optional>", "#include <stdexcept>",
        ]
        if self.cpp_standard >= 20:
            includes.append("#include <concepts>")
        for inc in includes:
            self.emit(inc)
        for inc in self._extra_includes:
            self.emit(inc)
        self._includes = includes
        self._includes_emitted = True

    def _format_spec(self, p: Node) -> str:
        if isinstance(p, Literal):
            if isinstance(p.value, float): return "%f"
            if isinstance(p.value, str):   return "%s"
            return "%d"
        if isinstance(p, Identifier):
            t = self.declared_vars.get(p.name, '')
            if 'double' in t or 'float' in t: return "%f"
            if 'char' in t:                   return "%s"
            if 'size_t' in t:                 return "%zu"
            n = p.name.lower()
            if any(h in n for h in ('frac','dist','price','rate','double','float')): return "%f"
            if any(h in n for h in ('name','msg','text','str')): return "%s"
            return "%d"
        if isinstance(p, FieldAccess):
            # BUGFIX (silent wrong printf format, same root cause the C
            # backend already fixes): p.obj is the VARIABLE name, but
            # self.shapes is keyed by the shape/TYPE name -- looking shapes
            # up by p.obj directly always misses, silently falling back to
            # "%d" even for text/decimal fields. Resolve the variable's
            # declared Dictum type first (stripping any smart-pointer
            # "unique/shared handle to " prefix), then use THAT as the key.
            declared = self.declared_vars.get(p.obj, '')
            for prefix in ('unique handle to ', 'shared handle to ',
                           'weak handle to ', 'raw handle to '):
                if declared.startswith(prefix):
                    declared = declared[len(prefix):].strip()
                    break
            shape_name = declared or p.obj
            if shape_name in self.shapes:
                ft = self.shapes[shape_name].get(p.field, '')
                if 'fractional' in ft or 'decimal' in ft: return "%f"
                if ft == 'text': return "%s"
            return "%d"
        return "%d"

    def _map_container(self, item_name: str) -> str:
        parts = item_name.split()
        if parts[0] == 'vector' and 'of' in parts:
            idx = parts.index('of')
            inner = ' '.join(parts[idx+1:])
            return f"std::vector<{self.type_to_cpp(inner)}>"
        if parts[0] == 'map' and 'of' in parts and 'to' in parts:
            oi = parts.index('of'); ti = parts.index('to')
            k = ' '.join(parts[oi+1:ti]); v = ' '.join(parts[ti+1:])
            return f"std::map<{self.type_to_cpp(k)}, {self.type_to_cpp(v)}>"
        return item_name

    def _analyze_captures(self, body: List[Node], param_names: Set[str]) -> Set[str]:
        captures: Set[str] = set()
        def _walk(n: Node) -> None:
            if isinstance(n, Identifier):
                if n.name not in param_names and n.name in self.declared_vars:
                    captures.add(n.name)
            elif isinstance(n, (BinaryOp,)):
                _walk(n.left); _walk(n.right)
            elif isinstance(n, UnaryOp):
                _walk(n.operand)
            elif isinstance(n, FuncCall):
                for a in n.args: _walk(a)
            elif isinstance(n, FieldAccess):
                if n.obj in self.declared_vars and n.obj not in param_names:
                    captures.add(n.obj)
            elif isinstance(n, IndexAccess):
                if n.collection in self.declared_vars and n.collection not in param_names:
                    captures.add(n.collection)
                _walk(n.index)
            elif isinstance(n, (Assignment,)):
                if isinstance(n.target, str) and n.target in self.declared_vars:
                    captures.add(n.target)
                _walk(n.value)
            elif isinstance(n, Return):
                _walk(n.value)
            elif isinstance(n, If):
                _walk(n.cond)
                for s in n.then_body: _walk(s)
                for s in n.else_body: _walk(s)
            elif isinstance(n, While):
                _walk(n.cond)
                for s in n.body: _walk(s)
            elif isinstance(n, (ForEach, Repeat)):
                for s in n.body: _walk(s)
            elif isinstance(n, Print):
                for part in n.parts: _walk(part)
        for stmt in body: _walk(stmt)
        return captures

    # ------------------------------------------------------------------
    def get_output(self) -> str:
        if not self._includes_emitted:
            # FIX: fragment-only file (no Program/Module ever encountered
            # to trigger preamble emission) -- see comment above. Prepend
            # the same include set _emit_includes() would normally emit,
            # rather than calling it directly (which would append at the
            # current end of self.output, i.e. AFTER content that already
            # depends on these includes).
            includes = [
                "#include <cstdint>", "#include <cstdbool>", "#include <cstdio>",
                "#include <cstdlib>", "#include <cstring>", "#include <cassert>",
                "#include <cmath>", "#include <memory>", "#include <vector>",
                "#include <map>", "#include <string>", "#include <functional>",
                "#include <optional>", "#include <stdexcept>",
            ]
            if self.cpp_standard >= 20:
                includes.append("#include <concepts>")
            includes += self._extra_includes
            self.output = includes + [""] + self.output
            self._includes_emitted = True
        if self._action_buffer:
            # Inject after includes block
            last_inc = -1
            for i, ln in enumerate(self.output):
                if ln.strip().startswith('#include'):
                    last_inc = i
            if last_inc >= 0:
                inject = [''] + self._action_buffer
                self.output = (self.output[:last_inc + 1]
                               + inject
                               + self.output[last_inc + 1:])
            self._action_buffer.clear()
        self._fix_preamble_ordering()
        if self._handle_typedefs:
            self._splice_handle_typedefs()
        return "\n".join(self.output)

    def _splice_handle_typedefs(self) -> None:
        """Ensure `typedef void* Name;` exists for every declared nominal
        handle type, inserted right after the includes block — before any
        extern/struct/var that references it — regardless of where in the
        source `define handle Name` appeared. Idempotent."""
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
        #include preamble block (only emitted once the Program node is
        visited), move that leading chunk to *after* the preamble instead
        of before it. No-op if the preamble is already at (or near) the
        top."""
        first_include = -1
        for i, ln in enumerate(self.output):
            if ln.strip().startswith('#include'):
                first_include = i
                break
        if first_include <= 0:
            return
        leading = self.output[:first_include]
        if not any(ln.strip() for ln in leading):
            return
        rest = self.output[first_include:]
        end = 0
        while end < len(rest):
            s = rest[end].strip()
            if s.startswith('#include') or s == '':
                end += 1
                continue
            if s.startswith('typedef') and s.endswith(';'):
                end += 1
                continue
            if (s.startswith('typedef struct') or s.startswith('typedef enum')) and not s.endswith(';'):
                j = end + 1
                while j < len(rest) and not rest[j].strip().startswith('}'):
                    j += 1
                if j < len(rest):
                    j += 1
                end = j
                continue
            break
        self.output = rest[:end] + leading + rest[end:]

    def get_header_output(self, ast: List[Node]) -> str:
        lines = ["#pragma once", "#include <cstdint>", "#include <cstdbool>",
                 "#include <cstddef>", "#include <memory>", "#include <string>",
                 "#include <vector>", "#include <map>", "#include <functional>", ""]
        def _emit(nodes: List[Node]) -> None:
            for node in nodes:
                if isinstance(node, Shape) and node.export:
                    is_class = bool(node.methods or node.constructors or node.destructor or node.parent)
                    keyword = "class" if is_class else "struct"
                    pfx = f"{keyword} __attribute__((packed))" if node.is_packed else keyword
                    inh = f" : public {node.parent}" if node.parent else ""
                    lines.append(f"{pfx} {node.name}{inh} {{")
                    lines.append("public:")
                    for fname, ftype in node.fields:
                        lines.append(f"    {self.type_to_cpp(ftype)} {fname};")
                    for m in node.methods:
                        params = ", ".join(f"{self.type_to_cpp(pt)} {pn}" for pn, pt in m.params)
                        virt = "virtual " if (m.is_virtual or is_class) else ""
                        override = " override" if m.is_override else ""
                        lines.append(f"    {virt}{self.type_to_cpp(m.ret_type)} {m.name}({params}){override};")
                    lines.append("};")
                    lines.append("")
                elif isinstance(node, VarDecl) and node.export:
                    lines.append(f"extern {self.type_to_cpp(node.type)} {node.name};")
                elif isinstance(node, Action) and node.export:
                    params = ", ".join(f"{self.type_to_cpp(pt)} {pn}" for pn, pt in node.params) or ""
                    lines.append(f"extern {self.type_to_cpp(node.ret_type)} {node.name}({params});")
                elif isinstance(node, (Program, Module)):
                    _emit(node.body)
        _emit(ast)
        return "\n".join(lines)
