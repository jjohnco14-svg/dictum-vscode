"""
Dictum Stdlib Registry — wires stdlib module calls to real C implementations.
Phase 5: http (libcurl), json, tls (OpenSSL), net, text, file, console, math.

Provides:
  extend_validator(validator)     — registers stdlib types/actions
  extend_emitter(emitter)         — registers type mappings
  detect_stdlib_includes(ast)     — returns (set[header], needs_robotics)
  auto_inject_stdlib_imports(ast) — inserts Use nodes for detected modules
"""

from __future__ import annotations
from typing import Set, Tuple, List, Any, TYPE_CHECKING
from .ast_nodes import Node, FuncCall, Use, Action, VarDecl, Assignment, FieldAccess

if TYPE_CHECKING:
    from .validator import Validator, ActionSig
    from .emit_c import CEmitter
    from .emit_cpp import CppEmitter


# ---------------------------------------------------------------------------
# Type registrations
# ---------------------------------------------------------------------------

DICTUM_STDLIB_TYPES: Set[str] = {
    "http_response", "json_value", "tls_context", "net_socket",
    "file_handle", "timer_handle", "thread_handle", "mutex_handle",
    "channel_handle", "semaphore_handle",
    # Niche / AI
    "tensor_value", "model_handle", "speech_context", "motor_command",
    "sensor_reading", "robot_state",
}

# ---------------------------------------------------------------------------
# Action families — maps Dictum surface syntax to C implementation
# Extending the map in emit_c._MODULE_CALL_MAP at runtime
# ---------------------------------------------------------------------------

STDLIB_ACTION_FAMILIES: dict = {
    # Http (HTTP + HTTPS — auto-routes via dictum_tls for https://)
    "Http.get":              ("dictum_http_get",          ["text"],                    "text"),
    "Http.post":             ("dictum_http_post",         ["text","text"],              "text"),
    "Http.post_form":        ("dictum_http_post_form",    ["text","text"],              "text"),
    "Http.put":              ("dictum_http_put",          ["text","text"],              "text"),
    "Http.delete":           ("dictum_http_delete",       ["text"],                    "text"),
    "Http.patch":            ("dictum_http_patch",        ["text","text"],              "text"),
    "Http.delete":         ("dictum_http_delete",     ["text"],            "text"),
    "Http.headers":        ("dictum_http_headers",    ["text"],            "text"),
    # Json (cJSON wrappers)
    # Json (recursive-descent parser: nested objects, arrays, indexing)
    "Json.parse":            ("dictum_json_parse",        ["text"],                             "whole number"),
    "Json.get":              ("dictum_json_get",           ["whole number","text"],              "text"),
    "Json.get_string":       ("dictum_json_get_string",    ["whole number","text"],              "text"),
    "Json.get_int":          ("dictum_json_get_int",       ["whole number","text"],              "whole number"),
    "Json.get_float":        ("dictum_json_get_float",     ["whole number","text"],              "decimal number"),
    "Json.get_bool":         ("dictum_json_get_bool",      ["whole number","text"],              "truth value"),
    "Json.set":              ("dictum_json_set",           ["whole number","text","text"],       "truth value"),
    "Json.stringify":        ("dictum_json_stringify",     ["whole number"],                     "text"),
    "Json.destroy":          ("dictum_json_destroy",       ["whole number"],                     "nothing"),
    "Json.length":           ("dictum_json_length",        ["whole number"],                     "whole number"),
    "Json.array_length":     ("dictum_json_array_length",  ["whole number","text"],              "whole number"),
    "Json.get_at":           ("dictum_json_get_at",        ["whole number","text","whole number"],"text"),
    "Json.get_int_at":       ("dictum_json_get_int_at",    ["whole number","text","whole number"],"whole number"),
    "Json.get_float_at":     ("dictum_json_get_float_at",  ["whole number","text","whole number"],"decimal number"),
    "Json.get_object_at":    ("dictum_json_get_object_at", ["whole number","text","whole number"],"whole number"),
    "Json.get_path":         ("dictum_json_get_path",      ["whole number","text"],              "text"),
    # File
    "File.read":           ("dictum_file_read",       ["text"],            "text"),
    "File.write":          ("dictum_file_write",      ["text","text"],     "truth value"),
    "File.exists":         ("dictum_file_exists",     ["text"],            "truth value"),
    "File.delete":         ("dictum_file_delete",     ["text"],            "truth value"),
    "File.list":           ("dictum_file_list",       ["text"],            "text"),
    # Console
    "Console.write":       ("dictum_console_write",   ["text"],            "nothing"),
    "Console.write_line":  ("dictum_console_write_line",["text"],          "nothing"),
    "Console.read_line":   ("dictum_console_read_line",[],                 "text"),
    # Net (POSIX sockets)
    "Net.connect":         ("dictum_net_connect",     ["text","whole number"], "net_socket"),
    "Net.send":            ("dictum_net_send",        ["net_socket","text"],   "whole number"),
    "Net.receive":         ("dictum_net_receive",     ["net_socket"],          "text"),
    "Net.close":           ("dictum_net_close",       ["net_socket"],          "nothing"),
    "Net.listen":          ("dictum_net_listen",      ["whole number"],        "net_socket"),
    "Net.accept":          ("dictum_net_accept",      ["net_socket"],          "net_socket"),
    # Tls (OpenSSL)
    "Tls.wrap":            ("dictum_tls_wrap",        ["net_socket"],      "tls_context"),
    "Tls.handshake":       ("dictum_tls_handshake",   ["tls_context"],     "truth value"),
    "Tls.send":            ("dictum_tls_send",        ["tls_context","text"],"whole number"),
    "Tls.receive":         ("dictum_tls_receive",     ["tls_context"],     "text"),
    "Tls.close":           ("dictum_tls_close",       ["tls_context"],     "nothing"),
    # Text (MISSING-02 string ops)
    # Text (P1.6 — dictum_text.c complete implementation)
    "Text.length":         ("dictum_text_length",      ["text"],                    "whole number"),
    "Text.utf8_length":    ("dictum_text_utf8_length", ["text"],                    "whole number"),
    "Text.copy":           ("dictum_text_copy",        ["text","text"],              "text"),
    "Text.concat":         ("dictum_text_concat",      ["text","text"],              "text"),
    "Text.compare":        ("dictum_text_compare",     ["text","text"],              "whole number"),
    "Text.format":         ("dictum_text_format",      ["text","text"],              "text"),
    "Text.slice":          ("dictum_text_slice",       ["text","whole number","whole number"], "text"),
    "Text.find":           ("dictum_text_find",        ["text","text"],              "whole number"),
    "Text.find_from":      ("dictum_text_find_from",   ["text","text","whole number"],"whole number"),
    "Text.to_number":      ("dictum_text_to_int",      ["text"],                    "whole number"),
    "Text.to_float":       ("dictum_text_to_float",    ["text"],                    "decimal number"),
    "Text.from_int":       ("dictum_text_from_int",    ["whole number"],             "text"),
    "Text.from_float":     ("dictum_text_from_float",  ["decimal number"],           "text"),
    "Text.from_number":    ("dictum_text_from_int",    ["whole number"],             "text"),
    "Text.join":           ("dictum_text_join",        ["text","text"],              "text"),
    "Text.split":          ("dictum_text_split",       ["text","text"],              "text list"),
    "Text.trim":           ("dictum_text_trim",        ["text"],                    "text"),
    "Text.to_upper":       ("dictum_text_to_upper",    ["text"],                    "text"),
    "Text.to_lower":       ("dictum_text_to_lower",    ["text"],                    "text"),
    "Text.replace":        ("dictum_text_replace",     ["text","text","text"],       "text"),
    "Text.starts_with":    ("dictum_text_starts_with", ["text","text"],              "truth value"),
    "Text.ends_with":      ("dictum_text_ends_with",   ["text","text"],              "truth value"),
    "Text.contains":       ("dictum_text_contains",    ["text","text"],              "truth value"),
    "Text.grapheme_length":  ("dictum_text_grapheme_length",  ["text"],                                  "whole number"),
    "Text.grapheme_slice":   ("dictum_text_grapheme_slice",   ["text","whole number","whole number"],     "text"),
    "Text.grapheme_reverse": ("dictum_text_grapheme_reverse", ["text"],                                  "text"),
    "Text.normalize":        ("dictum_text_normalize",        ["text"],                                  "text"),
    # Math
    "Math.sqrt":           ("sqrt",                   ["fractional number"],   "fractional number"),
    "Math.pow":            ("pow",                    ["fractional number","fractional number"],"fractional number"),
    "Math.abs":            ("fabs",                   ["fractional number"],   "fractional number"),
    "Math.floor":          ("floor",                  ["fractional number"],   "fractional number"),
    "Math.ceil":           ("ceil",                   ["fractional number"],   "fractional number"),
    "Math.round":          ("round",                  ["fractional number"],   "fractional number"),
    "Math.sin":            ("sin",                    ["fractional number"],   "fractional number"),
    "Math.cos":            ("cos",                    ["fractional number"],   "fractional number"),
    "Math.log":            ("log",                    ["fractional number"],   "fractional number"),
    "Math.exp":            ("exp",                    ["fractional number"],   "fractional number"),
    # Thread/concurrency stubs
    "Thread.start":        ("dictum_thread_start",    ["action taking nothing produces nothing"],"thread_handle"),
    "Thread.join":         ("dictum_thread_join",     ["thread_handle"],   "nothing"),
    "Mutex.create":        ("dictum_mutex_create",    [],                  "mutex_handle"),
    "Mutex.lock":          ("dictum_mutex_lock",      ["mutex_handle"],    "nothing"),
    "Mutex.unlock":        ("dictum_mutex_unlock",    ["mutex_handle"],    "nothing"),
    # AI / LLM stubs
    "LLM.load":            ("dictum_llm_load",        ["text"],            "model_handle"),
    "LLM.infer":           ("dictum_llm_infer",       ["model_handle","text"],"text"),
    "LLM.unload":          ("dictum_llm_unload",      ["model_handle"],    "nothing"),
    # Speech stubs
    "Speech.tts":          ("dictum_speech_tts",      ["text"],            "nothing"),
    "Speech.stt":          ("dictum_speech_stt",      [],                  "text"),
    # Robotics stubs
    "Robot.move":          ("dictum_robot_move",      ["motor_command"],   "nothing"),
    "Robot.sensor":        ("dictum_robot_sensor",    [],                  "sensor_reading"),
    "Robot.state":         ("dictum_robot_state",     [],                  "robot_state"),
}

# ---------------------------------------------------------------------------
# Header mappings (module → .h file needed)
# ---------------------------------------------------------------------------

_MODULE_HEADER_MAP: dict = {
    "Http":     "dictum_http.h",
    "Json":     "dictum_json.h",
    "File":     "dictum_file.h",
    "Console":  "dictum_console.h",
    "Net":      "dictum_net.h",
    "Tls":      "dictum_tls.h",
    "Text":     "dictum_text.h",
    "Math":     "math.h",
    "Thread":   "dictum_thread.h",
    "Mutex":    "dictum_mutex.h",
    "LLM":      "dictum_llm.h",
    "Speech":   "dictum_speech.h",
    "Robot":    "dictum_robotics.h",
}

_ROBOTICS_MODULES = {"Robot", "Speech", "LLM"}


def extend_validator(validator: Any) -> None:
    """Register all stdlib types and action signatures in the validator."""
    from .validator import ActionSig, VarInfo
    for t in DICTUM_STDLIB_TYPES:
        validator.PRIMITIVE_TYPES.add(t)
    for key, (c_name, params, ret) in STDLIB_ACTION_FAMILIES.items():
        # Register both Module.fn and c_name forms
        sig = ActionSig(
            name=key,
            params=[(f"arg{i}", pt) for i, pt in enumerate(params)],
            ret_type=ret,
        )
        validator.actions[key] = sig
        validator.actions[c_name] = sig


def extend_emitter(emitter: Any) -> None:
    """Register all stdlib types in the emitter type table and action map."""
    from .emit_c import _MODULE_CALL_MAP
    for key, (c_name, params, ret) in STDLIB_ACTION_FAMILIES.items():
        _MODULE_CALL_MAP[key] = c_name
        # FIX (silent handle-truncation bug): both the dotted key
        # ("Mutex.create") and the raw C name are registered, since a
        # FuncCall's .name can appear as either depending on how it was
        # parsed/rewritten by the time _infer_type_from_expr sees it.
        if hasattr(emitter, 'action_return_types'):
            emitter.action_return_types[key] = ret
            emitter.action_return_types[c_name] = ret
    # stdlib types in C map to void* or specific typedefs
    for t in DICTUM_STDLIB_TYPES:
        if t not in emitter.types:
            emitter.types[t] = f"dictum_{t}_t"


def detect_stdlib_includes(ast: List[Node]) -> Tuple[Set[str], bool]:
    """Walk AST and find which stdlib headers are needed."""
    headers: Set[str] = set()
    needs_robotics = False

    def _walk(node: Node) -> None:
        nonlocal needs_robotics
        if isinstance(node, Use):
            h = _MODULE_HEADER_MAP.get(node.path)
            if h:
                headers.add(h)
            if node.path in _ROBOTICS_MODULES:
                needs_robotics = True
        elif isinstance(node, FuncCall):
            # Detect Module.fn style calls
            if '.' in node.name:
                mod = node.name.split('.')[0]
                h = _MODULE_HEADER_MAP.get(mod)
                if h:
                    headers.add(h)
                if mod in _ROBOTICS_MODULES:
                    needs_robotics = True
            for arg in node.args:
                _walk(arg)
        elif isinstance(node, FieldAccess):
            h = _MODULE_HEADER_MAP.get(node.obj)
            if h:
                headers.add(h)
            if node.obj in _ROBOTICS_MODULES:
                needs_robotics = True
        elif hasattr(node, 'body'):
            for child in getattr(node, 'body', []):
                _walk(child)
        for attr in ('then_body', 'else_body', 'success_body', 'failure_body'):
            for child in getattr(node, attr, []):
                _walk(child)
        if hasattr(node, 'value') and isinstance(node.value, Node):
            _walk(node.value)

    for n in ast:
        _walk(n)
    return headers, needs_robotics


def auto_inject_stdlib_imports(ast: List[Node]) -> List[Node]:
    """
    Pre-process AST: for any FuncCall whose name is Module.fn where
    the module is a known stdlib module, inject a Use(path=module) node
    at the top of the AST if not already present.
    """
    existing_uses: Set[str] = set()

    def _collect_uses(nodes: List[Node]) -> None:
        for n in nodes:
            if isinstance(n, Use):
                existing_uses.add(n.path)
            elif hasattr(n, 'body'):
                _collect_uses(getattr(n, 'body', []))

    def _collect_module_refs(nodes: List[Node]) -> Set[str]:
        refs: Set[str] = set()
        for n in nodes:
            if isinstance(n, FuncCall) and '.' in n.name:
                mod = n.name.split('.')[0]
                if mod in _MODULE_HEADER_MAP:
                    refs.add(mod)
            if hasattr(n, 'body'):
                refs.update(_collect_module_refs(getattr(n, 'body', [])))
            for attr in ('then_body', 'else_body', 'success_body', 'failure_body'):
                refs.update(_collect_module_refs(getattr(n, attr, [])))
        return refs

    _collect_uses(ast)
    needed = _collect_module_refs(ast) - existing_uses
    injected = [Use(path=mod) for mod in sorted(needed)]
    return injected + ast
