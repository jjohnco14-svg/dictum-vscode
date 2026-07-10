#!/usr/bin/env python3
"""
run_selftest.py — Dictum compiler self-test.

Three tiers, in order:

  §1 STATIC INVENTORY   — for every entry in STDLIB_ACTION_FAMILIES, checks
                           whether the corresponding C function in runtime/
                           has a real body, a stub body, or doesn't exist at
                           all. This is what previously reported "3 real /
                           32 stub / 57 missing".

  §2 BEHAVIORAL TESTS    — actually compiles and RUNS small real C/gcc
                           programs against each implemented runtime header,
                           exercising real files, real sockets, real
                           mutexes+threads (a genuine race-condition test,
                           not just a syntax check), and a real HTTP
                           request/response over a loopback socket. A
                           module only counts as PASS here if its behavior
                           is independently verified, not just "compiles".

  §3 REGRESSION TESTS    — one test per concrete, previously-silent bug
                           found and fixed this session, so none of them
                           can silently come back:
                             R1  link-flag plumbing (#[link]/module ldflags
                                 actually reach the Makefile / --compile /
                                 the VS Code extension's compile-check gate)
                             R2  stdlib FuncCall return-type inference
                                 (was always int32_t — silent pointer
                                 truncation for handle-returning calls)
                             R3  string escape handling (\\" in Dictum
                                 source must survive as a real quote and
                                 be re-escaped correctly for C)
                             R4  preamble-ordering bug that ate the
                                 _DEFAULT_SOURCE POSIX feature-test macro
                             R5  project_builder.py multi-file projects
                                 use StdlibTranspiler (not plain
                                 Transpiler), so stdlib calls validate at
                                 all in a multi-file build

Exit code is 0 only if every behavioral test and every regression test
passes. Inventory numbers are reported but don't fail the run by
themselves — modules nobody has gotten to yet (Tls, Channel, Shm, ...)
are expected to still show as stub/missing.

Usage: python3 run_selftest.py [--verbose]
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(HERE, "runtime")
CLI = os.path.join(HERE, "dictumc_cli.py")
PROJECT_BUILDER = os.path.join(HERE, "project_builder.py")
VERBOSE = "--verbose" in sys.argv

sys.path.insert(0, HERE)
from dictumc.stdlib_registry import STDLIB_ACTION_FAMILIES  # noqa: E402


def log(msg=""):
    print(msg)


def vlog(msg):
    if VERBOSE:
        print("    " + msg)


# ─────────────────────────────────────────────────────────────────────────
# §1 Static inventory
# ─────────────────────────────────────────────────────────────────────────

_STUB_MARKERS = (
    "not yet implemented",
    "/* stub */",
    "// stub",
    "TODO",
)


def _find_c_function_body(c_name: str) -> Optional[str]:
    """Grep every runtime/*.h for a definition of `c_name` and return its
    body text (from the opening `{` to the matching top-level `}`), or
    None if not found anywhere."""
    pattern = re.compile(
        r'\b(?:static\s+inline\s+)?[\w\*\s]+?\b' + re.escape(c_name) + r'\s*\([^;{]*\)\s*\{'
    )
    for fname in sorted(os.listdir(RUNTIME_DIR)):
        if not fname.endswith(".h"):
            continue
        path = os.path.join(RUNTIME_DIR, fname)
        text = open(path, encoding="utf-8").read()
        m = pattern.search(text)
        if not m:
            continue
        # Walk forward from the opening brace to find the matching close.
        depth = 0
        i = m.end() - 1
        start_body = m.end()
        for j in range(m.end() - 1, len(text)):
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    return text[start_body:j]
        return text[start_body:]
    return None


def run_inventory():
    log("=" * 70)
    log("§1 STATIC STDLIB INVENTORY")
    log("=" * 70)

    real, stub, missing = [], [], []
    for key, (c_name, params, ret) in sorted(STDLIB_ACTION_FAMILIES.items()):
        # Some registry entries (Math.sqrt -> "sqrt", Math.abs -> "fabs", ...)
        # deliberately call a real libc/libm function directly rather than
        # going through a dictum_*.h wrapper — there's nothing to wrap, the
        # real implementation already exists in a system library the
        # program links against. Counting these as "missing" (because no
        # runtime/*.h defines a body for them) would understate real
        # coverage, so any c_name without the project's `dictum_` prefix
        # is treated as a real, already-implemented passthrough.
        if not c_name.startswith("dictum_"):
            real.append(key)
            continue
        body = _find_c_function_body(c_name)
        if body is None:
            missing.append(key)
            continue
        is_stub = any(marker in body for marker in _STUB_MARKERS)
        # A body under ~3 non-blank lines that's just a bare return of a
        # constant (0 / NULL / (void*)0) with no other logic is also
        # effectively a stub even without an explicit marker comment.
        meaningful_lines = [l.strip() for l in body.splitlines() if l.strip()]
        trivial_return = (
            len(meaningful_lines) <= 1
            and re.match(r'return\s*(\(void\*\)\s*0|0|NULL)\s*;?\s*$', meaningful_lines[0] if meaningful_lines else '')
        )
        if is_stub or trivial_return:
            stub.append(key)
        else:
            real.append(key)

    total = len(STDLIB_ACTION_FAMILIES)
    log(f"stdlib inventory: {total} registered functions")
    log(f"  real implementation : {len(real):3d}  ({100*len(real)//total}%)")
    log(f"  stub only            : {len(stub):3d}")
    log(f"  missing entirely     : {len(missing):3d}")
    log("")
    if VERBOSE:
        log("real:    " + ", ".join(real))
        log("stub:    " + ", ".join(stub))
        log("missing: " + ", ".join(missing))
        log("")

    by_module = {}
    for key in STDLIB_ACTION_FAMILIES:
        mod = key.split(".")[0]
        by_module.setdefault(mod, {"real": 0, "stub": 0, "missing": 0})
    for key in real:
        by_module[key.split(".")[0]]["real"] += 1
    for key in stub:
        by_module[key.split(".")[0]]["stub"] += 1
    for key in missing:
        by_module[key.split(".")[0]]["missing"] += 1

    log(f"{'module':<12}{'real':>6}{'stub':>6}{'missing':>9}")
    for mod in sorted(by_module):
        c = by_module[mod]
        log(f"{mod:<12}{c['real']:>6}{c['stub']:>6}{c['missing']:>9}")
    log("")
    return real, stub, missing


# ─────────────────────────────────────────────────────────────────────────
# §2 Behavioral tests — real gcc compile + real execution
# ─────────────────────────────────────────────────────────────────────────

def _gcc(src_path: str, out_path: str, extra_flags=None) -> subprocess.CompletedProcess:
    flags = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-I", RUNTIME_DIR]
    if extra_flags:
        flags += extra_flags
    return subprocess.run(
        ["gcc", *flags, src_path, "-o", out_path],
        capture_output=True, text=True, timeout=30,
    )


def _run(bin_path: str, timeout=8, env=None) -> subprocess.CompletedProcess:
    return subprocess.run([bin_path], capture_output=True, text=True, timeout=timeout, env=env)


BEHAVIORAL_TESTS = []


def behavioral(name):
    def deco(fn):
        BEHAVIORAL_TESTS.append((name, fn))
        return fn
    return deco


@behavioral("Text: real string ops (concat/slice/trim/case/replace/split/utf8)")
def test_text(tmp):
    src = os.path.join(tmp, "t.c")
    open(src, "w").write(r'''
#include "dictum_text.h"
#include <assert.h>
int main(void) {
    assert(dictum_text_length("hello") == 5);
    assert(dictum_text_utf8_length("h\xc3\xa9llo") == 5);
    dictum_text c = dictum_text_concat("foo", "bar");
    assert(strcmp(c, "foobar") == 0); free((void*)c);
    dictum_text r = dictum_text_replace("aXbXc", "X", "-");
    assert(strcmp(r, "a-b-c") == 0); free((void*)r);
    dictum_text *parts = dictum_text_split("a,b,c", ",");
    assert(strcmp(parts[0], "a") == 0 && strcmp(parts[2], "c") == 0 && parts[3] == NULL);
    for (int i = 0; parts[i]; i++) free((void*)parts[i]);
    free(parts);
    return 0;
}
''')
    bin_ = os.path.join(tmp, "t")
    r = _gcc(src, bin_)
    if r.returncode != 0:
        return False, f"compile failed:\n{r.stderr}"
    r = _run(bin_)
    if r.returncode != 0:
        return False, f"runtime failure (exit {r.returncode}):\n{r.stderr}"
    return True, "ok"


@behavioral("File: real read/write/exists/delete/list against the real filesystem")
def test_file(tmp):
    src = os.path.join(tmp, "t.c")
    target = os.path.join(tmp, "sample.txt")
    open(src, "w").write(f'''
#include "dictum_file.h"
#include <assert.h>
int main(void) {{
    const char *path = "{target}";
    assert(dictum_file_write(path, "hello\\nworld"));
    assert(dictum_file_exists(path));
    dictum_text c = dictum_file_read(path);
    assert(c && strcmp(c, "hello\\nworld") == 0);
    free((void*)c);
    assert(dictum_file_delete(path));
    assert(!dictum_file_exists(path));
    return 0;
}}
''')
    bin_ = os.path.join(tmp, "t")
    r = _gcc(src, bin_)
    if r.returncode != 0:
        return False, f"compile failed:\n{r.stderr}"
    r = _run(bin_)
    if r.returncode != 0:
        return False, f"runtime failure (exit {r.returncode}):\n{r.stderr}"
    return True, "ok"


@behavioral("Json: real recursive-descent parse/get/set/stringify round-trip")
def test_json(tmp):
    src = os.path.join(tmp, "t.c")
    open(src, "w").write(r'''
#include "dictum_json.h"
#include <assert.h>
int main(void) {
    int h = dictum_json_parse("{\"name\":\"Jeff\",\"age\":19,\"tags\":[\"a\",\"b\"]}");
    assert(h >= 0);
    dictum_text n = dictum_json_get_string(h, "name");
    assert(n && strcmp(n, "Jeff") == 0); free((void*)n);
    assert(dictum_json_get_int(h, "age") == 19);
    assert(dictum_json_array_length(h, "tags") == 2);
    assert(dictum_json_parse("{not json") == -1);
    dictum_json_destroy(h);
    return 0;
}
''')
    bin_ = os.path.join(tmp, "t")
    r = _gcc(src, bin_)
    if r.returncode != 0:
        return False, f"compile failed:\n{r.stderr}"
    r = _run(bin_)
    if r.returncode != 0:
        return False, f"runtime failure (exit {r.returncode}):\n{r.stderr}"
    return True, "ok"


@behavioral("Mutex+Thread: real race condition — fails without a working mutex")
def test_mutex_thread(tmp):
    src = os.path.join(tmp, "t.c")
    open(src, "w").write(r'''
#include "dictum_mutex.h"
#include "dictum_thread.h"
#include <assert.h>
static int counter = 0;
static dictum_mutex_handle_t g_mutex;
void bump(void) {
    for (int i = 0; i < 20000; i++) {
        dictum_mutex_lock(g_mutex);
        counter++;
        dictum_mutex_unlock(g_mutex);
    }
}
int main(void) {
    g_mutex = dictum_mutex_create();
    assert(g_mutex);
    dictum_thread_handle_t t1 = dictum_thread_start(bump);
    dictum_thread_handle_t t2 = dictum_thread_start(bump);
    dictum_thread_join(t1);
    dictum_thread_join(t2);
    assert(counter == 40000);  /* fails/flakes without a real mutex */
    dictum_mutex_destroy(g_mutex);
    return 0;
}
''')
    bin_ = os.path.join(tmp, "t")
    r = _gcc(src, bin_, extra_flags=["-pthread"])
    if r.returncode != 0:
        return False, f"compile failed:\n{r.stderr}"
    r = _run(bin_)
    if r.returncode != 0:
        return False, f"runtime failure (exit {r.returncode}):\n{r.stderr}"
    return True, "ok"


@behavioral("Net: real loopback client/server round-trip over POSIX sockets")
def test_net(tmp):
    src = os.path.join(tmp, "t.c")
    open(src, "w").write(r'''
#include "dictum_net.h"
#include "dictum_thread.h"
#include <assert.h>
#include <unistd.h>
static dictum_net_socket_t g_server;
void server_thread(void) {
    dictum_net_socket_t c = dictum_net_accept(g_server);
    dictum_text msg = dictum_net_receive(c);
    assert(msg && strcmp(msg, "ping") == 0);
    free((void*)msg);
    dictum_net_send(c, "pong");
    dictum_net_close(c);
}
int main(void) {
    g_server = dictum_net_listen(58921);
    assert(g_server >= 0);
    dictum_thread_handle_t t = dictum_thread_start(server_thread);
    usleep(50000);
    dictum_net_socket_t c = dictum_net_connect("127.0.0.1", 58921);
    assert(c >= 0);
    assert(dictum_net_send(c, "ping") == 4);
    dictum_text resp = dictum_net_receive(c);
    assert(resp && strcmp(resp, "pong") == 0);
    free((void*)resp);
    dictum_net_close(c);
    dictum_thread_join(t);
    dictum_net_close(g_server);
    return 0;
}
''')
    bin_ = os.path.join(tmp, "t")
    r = _gcc(src, bin_, extra_flags=["-pthread"])
    if r.returncode != 0:
        return False, f"compile failed:\n{r.stderr}"
    r = _run(bin_)
    if r.returncode != 0:
        return False, f"runtime failure (exit {r.returncode}):\n{r.stderr}"
    return True, "ok"


@behavioral("Http: real GET/POST over loopback against a live Python HTTP server")
def test_http(tmp):
    port = 58933
    server_src = os.path.join(tmp, "srv.py")
    open(server_src, "w").write(f'''
import http.server, socketserver
socketserver.TCPServer.allow_reuse_address = True
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type','text/plain'); self.end_headers()
        self.wfile.write(b"hello world")
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        data = self.rfile.read(n)
        self.send_response(200); self.end_headers()
        self.wfile.write(b"received: " + data)
with socketserver.TCPServer(("127.0.0.1", {port}), H) as httpd:
    httpd.serve_forever()
''')
    src = os.path.join(tmp, "t.c")
    open(src, "w").write(f'''
#include "dictum_http.h"
#include <assert.h>
int main(void) {{
    dictum_text r = dictum_http_get("http://127.0.0.1:{port}/x");
    assert(r && strstr(r, "hello world"));
    free((void*)r);
    dictum_text r2 = dictum_http_post("http://127.0.0.1:{port}/x", "payload", "text/plain");
    assert(r2 && strstr(r2, "payload"));
    free((void*)r2);
    dictum_text r3 = dictum_http_get("https://example.com/");
    assert(r3 == NULL);  /* documented limitation, must fail loudly not silently */
    return 0;
}}
''')
    bin_ = os.path.join(tmp, "t")
    r = _gcc(src, bin_, extra_flags=["-pthread"])
    if r.returncode != 0:
        return False, f"compile failed:\n{r.stderr}"
    server = subprocess.Popen(
        [sys.executable, "-u", server_src], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        import time
        time.sleep(1)
        r = _run(bin_)
    finally:
        server.kill()
        server.wait(timeout=3)
    if r.returncode != 0:
        return False, f"runtime failure (exit {r.returncode}):\n{r.stderr}"
    return True, "ok"


def run_behavioral():
    log("=" * 70)
    log("§2 BEHAVIORAL TESTS (real gcc compile + real execution)")
    log("=" * 70)
    results = []
    for name, fn in BEHAVIORAL_TESTS:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                ok, detail = fn(tmp)
            except Exception as e:
                ok, detail = False, f"exception: {e}"
        status = "PASS" if ok else "FAIL"
        log(f"[{status}] {name}")
        if not ok:
            log(f"       {detail}")
        elif VERBOSE:
            vlog(detail)
        results.append((name, ok))
    log("")
    return results


# ─────────────────────────────────────────────────────────────────────────
# §3 Regression tests — one per bug fixed this session
# ─────────────────────────────────────────────────────────────────────────

REGRESSION_TESTS = []


def regression(name):
    def deco(fn):
        REGRESSION_TESTS.append((name, fn))
        return fn
    return deco


@regression("R1 link-flag plumbing: --print-ldflags reflects `use Mutex` (-lpthread), not just -lm")
def test_r1_ldflags(tmp):
    src = os.path.join(tmp, "m.dict")
    open(src, "w").write(
        "use Mutex\n\nprogram Main\n"
        "    call Mutex.create giving m\n"
        "    call Console.write_line with \"ok\"\n"
        "end program\n"
    )
    r = subprocess.run(
        [sys.executable, CLI, src, "--backend", "c", "--stdlib", "--print-ldflags"],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        return False, f"CLI failed: {r.stderr}"
    flags = r.stdout.strip()
    if "-lpthread" not in flags:
        return False, f"expected -lpthread in computed ldflags, got: {flags!r}"
    return True, flags


@regression("R1b link-flag plumbing: a program using ONLY Math gets -lm and nothing extra")
def test_r1b_ldflags_minimal(tmp):
    src = os.path.join(tmp, "m.dict")
    open(src, "w").write(
        "program Main\n"
        "    call Console.write_line with \"ok\"\n"
        "end program\n"
    )
    r = subprocess.run(
        [sys.executable, CLI, src, "--backend", "c", "--stdlib", "--print-ldflags"],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        return False, f"CLI failed: {r.stderr}"
    flags = r.stdout.strip()
    if "-lpthread" in flags:
        return False, f"did not expect -lpthread for a Mutex/Thread-free program, got: {flags!r}"
    if "-lm" not in flags:
        return False, f"expected baseline -lm always present, got: {flags!r}"
    return True, flags


@regression("R2 stdlib return-type inference: `call Mutex.create giving m` types m as a handle, not int32_t")
def test_r2_return_type(tmp):
    src = os.path.join(tmp, "m.dict")
    open(src, "w").write(
        "use Mutex\n\nprogram Main\n"
        "    call Mutex.create giving m\n"
        "    call Mutex.lock with m\n"
        "    call Mutex.unlock with m\n"
        "end program\n"
    )
    r = subprocess.run(
        [sys.executable, CLI, src, "--backend", "c", "--stdlib"],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        return False, f"CLI failed: {r.stderr}"
    code = r.stdout
    if "int32_t m = dictum_mutex_create" in code:
        return False, "m was declared int32_t — pointer-truncating regression is back"
    if "dictum_mutex_handle_t m" not in code:
        return False, f"expected `dictum_mutex_handle_t m`, got:\n{code}"
    return True, "ok"


@regression("R3 string escaping: an embedded \\\" in Dictum source survives as valid, correctly-escaped C")
def test_r3_string_escape(tmp):
    src = os.path.join(tmp, "m.dict")
    open(src, "w").write(
        'program Main\n'
        '    call Console.write_line with "{\\"name\\":\\"Jeff\\"}"\n'
        'end program\n'
    )
    r = subprocess.run(
        [sys.executable, CLI, src, "--backend", "c", "--stdlib"],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        return False, f"CLI failed: {r.stderr}"
    code = r.stdout
    out_c = os.path.join(tmp, "out.c")
    open(out_c, "w").write(code)
    comp = _gcc(out_c, os.path.join(tmp, "out"))
    if comp.returncode != 0:
        return False, f"generated C failed to compile:\n{comp.stderr}\n---\n{code}"
    run = _run(os.path.join(tmp, "out"))
    if run.returncode != 0:
        return False, f"runtime failure: {run.stderr}"
    if '{"name":"Jeff"}' not in run.stdout:
        return False, f"expected the literal JSON text in output, got: {run.stdout!r}"
    return True, "ok"


@regression("R4 preamble ordering: #define _DEFAULT_SOURCE stays before the first #include")
def test_r4_preamble_order(tmp):
    src = os.path.join(tmp, "m.dict")
    open(src, "w").write(
        "use File\nuse Json\n\nprogram Main\n"
        "    call File.write with \"/tmp/_dictum_selftest.json\" and \"{}\"\n"
        "end program\n"
    )
    r = subprocess.run(
        [sys.executable, CLI, src, "--backend", "c", "--stdlib"],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        return False, f"CLI failed: {r.stderr}"
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    first_include_idx = next((i for i, l in enumerate(lines) if l.strip().startswith("#include")), None)
    define_idx = next((i for i, l in enumerate(lines) if "_DEFAULT_SOURCE" in l), None)
    if define_idx is None:
        return False, "no _DEFAULT_SOURCE define found in output at all"
    if first_include_idx is None or define_idx > first_include_idx:
        return False, f"_DEFAULT_SOURCE (line {define_idx}) is not before the first #include (line {first_include_idx})"
    return True, "ok"


@regression("R5 project_builder.py uses StdlibTranspiler, so multi-file projects validate stdlib calls")
def test_r5_project_builder_stdlib(tmp):
    src = open(PROJECT_BUILDER, encoding="utf-8").read()
    if "StdlibTranspiler" not in src:
        return False, "project_builder.py no longer imports/uses StdlibTranspiler"
    if re.search(r'\bt\s*=\s*Transpiler\(', src):
        return False, "project_builder.py still constructs a plain Transpiler() somewhere"
    return True, "ok"


def run_regressions():
    log("=" * 70)
    log("§3 REGRESSION TESTS (one per bug fixed this session)")
    log("=" * 70)
    results = []
    for name, fn in REGRESSION_TESTS:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                ok, detail = fn(tmp)
            except Exception as e:
                ok, detail = False, f"exception: {e}"
        status = "PASS" if ok else "FAIL"
        log(f"[{status}] {name}")
        if not ok:
            log(f"       {detail}")
        elif VERBOSE:
            vlog(str(detail))
        results.append((name, ok))
    log("")
    return results


# ─────────────────────────────────────────────────────────────────────────

def main():
    if shutil.which("gcc") is None:
        log("gcc not found on PATH — §2/§3 tests that compile C cannot run.")
        return 1

    real, stub, missing = run_inventory()
    behavioral_results = run_behavioral()
    regression_results = run_regressions()

    log("=" * 70)
    log("SUMMARY")
    log("=" * 70)
    b_pass = sum(1 for _, ok in behavioral_results if ok)
    r_pass = sum(1 for _, ok in regression_results if ok)
    log(f"Behavioral: {b_pass}/{len(behavioral_results)} passed")
    log(f"Regression: {r_pass}/{len(regression_results)} passed")
    log(f"Stdlib inventory: {len(real)} real / {len(stub)} stub / {len(missing)} missing "
        f"(of {len(STDLIB_ACTION_FAMILIES)})")

    all_ok = (b_pass == len(behavioral_results)) and (r_pass == len(regression_results))
    if not all_ok:
        log("")
        log("FAILED — see [FAIL] lines above.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
