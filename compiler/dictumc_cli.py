#!/usr/bin/env python3
"""
dictumc — Dictum Compiler CLI v5.0
Usage:
  dictumc <file.dict> [options]

Options:
  --backend c|cpp      Target backend (default: c)
  --cpp-standard 17|20 C++ standard (default: 17)
  --namespace <name>   Wrap output in C++ namespace
  --validate           Validate only, don't emit
  --compile            Transpile then compile with gcc/g++
  --output <file>      Output file (default: stdout)
  --makefile           Also write a Makefile next to the output file
  --summary            Print AST summary
  --grammar            Enable grammar-guided parsing (strict mode)
  --stdlib             Use stdlib-aware transpiler
  --no-validate        Skip validation
"""

import sys
import os
import argparse
import subprocess
import tempfile

def _run_repl(backend: str, cpp_standard: int) -> int:
    """
    Minimal interactive read-transpile-print loop. Each line (or multi-line
    block ending in a blank line) is wrapped in a throwaway `program` block
    if it isn't already a complete program/action/shape declaration, then
    transpiled and the resulting C/C++ is printed. This was previously a
    flag the VS Code extension shelled out to (`--repl`) that didn't exist
    anywhere in this file — cmdRunRepl() would fail with an argparse error
    every time it was invoked.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from dictumc.transpiler import Transpiler
    from dictumc.validator import ValidationError

    print(f"Dictum REPL — backend: {backend}" + (f" (C++{cpp_standard})" if backend == "cpp" else ""))
    print("Type a statement or full program. Blank line submits. Ctrl+D / Ctrl+C to exit.\n")

    TOP_LEVEL_KEYWORDS = ("program", "action", "shape", "use ", "import ")

    while True:
        try:
            lines = []
            prompt = ">>> "
            while True:
                line = input(prompt)
                if line.strip() == "" and lines:
                    break
                if line.strip() == "" and not lines:
                    continue
                lines.append(line)
                prompt = "... "
        except (EOFError, KeyboardInterrupt):
            print("\nExiting Dictum REPL.")
            return 0

        raw = "\n".join(lines)
        is_top_level = raw.strip().startswith(TOP_LEVEL_KEYWORDS)
        source = raw if is_top_level else (
            "program _repl:\n"
            + "\n".join("    " + l for l in lines)
            + "\nend program\n"
        )

        try:
            t = Transpiler(source=source, backend=backend, cpp_standard=cpp_standard, namespace="")
            result = t.run(validate=True, summary=False, grammar_guided=False)
            code = result.get("code") if isinstance(result, dict) else result
            print(code)
        except (SyntaxError, ValidationError) as e:
            print(f"error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"internal error: {e}", file=sys.stderr)
        print()


def main() -> int:
    p = argparse.ArgumentParser(prog="dictumc", description="Dictum Compiler v0.1.30")
    p.add_argument("file", nargs="?", help="Input .dict source file")
    p.add_argument("--backend", choices=["c", "cpp"], default="c")
    p.add_argument("--cpp-standard", type=int, choices=[17, 20, 23], default=17)
    p.add_argument("--namespace", default="")
    p.add_argument("--validate", action="store_true", help="Validate only")
    p.add_argument("--no-validate", action="store_true", help="Skip validation")
    p.add_argument("--compile", action="store_true", help="Compile emitted C/C++")
    p.add_argument("--output", "-o", default="", help="Output path")
    p.add_argument("--makefile", action="store_true", help="Write Makefile alongside output (C backend only)")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--grammar", action="store_true", help="Grammar-constrained parsing")
    p.add_argument("--stdlib", action="store_true", help="Stdlib-aware transpiler")
    p.add_argument("--emit-ast", action="store_true", help="Dump AST repr")
    p.add_argument("--print-ldflags", action="store_true",
                   help="Print the computed linker flags (e.g. '-lm -lpthread') to stdout and exit, without emitting code")
    p.add_argument("--repl", action="store_true", help="Interactive read-transpile-print loop")
    args = p.parse_args()

    if args.repl:
        return _run_repl(args.backend, args.cpp_standard)

    # Read source
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as fh:
                source = fh.read()
        except FileNotFoundError:
            print(f"dictumc: error: file '{args.file}' not found", file=sys.stderr)
            return 1
    else:
        if sys.stdin.isatty():
            p.print_help()
            return 0
        source = sys.stdin.read()

    # Import
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from dictumc.transpiler import Transpiler, StdlibTranspiler
    from dictumc.validator import ValidationError

    TranspilerClass = StdlibTranspiler if args.stdlib else Transpiler

    try:
        t = TranspilerClass(
            source=source,
            backend=args.backend,
            cpp_standard=args.cpp_standard,
            namespace=args.namespace,
        )
        result = t.run(
            validate=not args.no_validate,
            summary=args.summary,
            grammar_guided=args.grammar,
        )
    except (SyntaxError, ValidationError) as e:
        print(f"dictumc: error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"dictumc: internal error: {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        return 1

    # Warnings
    for w in result.get("warnings", []):
        print(f"dictumc: warning: {w}", file=sys.stderr)

    if args.validate:
        print("dictumc: validation passed", file=sys.stderr)
        if args.summary and "summary" in result:
            print(result["summary"])
        return 0

    if args.print_ldflags:
        print(" ".join(result.get("ldflags") or ["-lm"]))
        return 0

    if args.emit_ast:
        import pprint
        pprint.pprint(result["ast"])
        return 0

    if args.summary and "summary" in result:
        print(result["summary"])

    code: str = result["code"]

    # Determine output extension
    ext = ".cpp" if args.backend == "cpp" else ".c"
    out_src = args.output if args.output and not args.compile else ""

    if args.compile:
        # Write to temp file, compile with gcc/g++
        compiler = "g++" if args.backend == "cpp" else "gcc"
        flags = [f"-std=c++{args.cpp_standard}"] if args.backend == "cpp" else ["-std=c11"]
        flags += ["-O2"]
        # compiler/runtime/ ships dictum_core.h / dictum_error.h, needed by
        # any program using `attempt` / `produce failure` / stdlib modules.
        runtime_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime")
        flags += [f"-I{runtime_dir}"]
        # FIX (link-flag plumbing bug): this used to hardcode ["-lm"] no
        # matter what the program actually used, so any program touching
        # Mutex/Thread/Semaphore/Channel/Event (-lpthread), Tls (-lssl
        # -lcrypto), Shm/Timer (-lrt), or a blessed library's #[link "x"]
        # directive (sqlite3, glfw, sdl2, raylib) would compile fine and
        # then fail at the final link step with undefined references,
        # unless the program happened to only need libm.
        link_libs = result.get("ldflags") or ["-lm"]
        binary_out = args.output or (os.path.splitext(args.file)[0] if args.file else "a.out")

        with tempfile.NamedTemporaryFile(suffix=ext, mode='w', delete=False,
                                         encoding='utf-8') as tf:
            tf.write(code)
            src_path = tf.name

        cmd = [compiler] + flags + [src_path, "-o", binary_out] + link_libs
        proc = subprocess.run(cmd, capture_output=True, text=True)
        os.unlink(src_path)

        if proc.returncode != 0:
            print(f"dictumc: compile error:\n{proc.stderr}", file=sys.stderr)
            return 1
        print(f"dictumc: compiled to '{binary_out}'", file=sys.stderr)

        # Also write the .c/.cpp source for inspection
        if args.output:
            src_out = args.output + ext
            with open(src_out, "w", encoding="utf-8") as fh:
                fh.write(code)
        return 0

    # Write code
    if out_src:
        with open(out_src, "w", encoding="utf-8") as fh:
            fh.write(code)
        print(f"dictumc: wrote '{out_src}'", file=sys.stderr)
    else:
        sys.stdout.write(code)

    # Write header if generated
    if "h_code" in result:
        h_path = (os.path.splitext(out_src)[0] + ".h") if out_src else None
        if h_path:
            with open(h_path, "w", encoding="utf-8") as fh:
                fh.write(result["h_code"])
    elif "hpp_code" in result:
        hpp_path = (os.path.splitext(out_src)[0] + ".hpp") if out_src else None
        if hpp_path:
            with open(hpp_path, "w", encoding="utf-8") as fh:
                fh.write(result["hpp_code"])

    # P2.1: write Makefile if requested and backend is C
    if args.makefile and args.backend == "c" and result.get("makefile"):
        mf_dir  = os.path.dirname(out_src) if out_src else "."
        mf_path = os.path.join(mf_dir, "Makefile")
        prog_name = os.path.splitext(os.path.basename(out_src))[0] if out_src else "program"
        # Re-generate with correct program name
        from dictumc.emit_c import CEmitter
        mf_text = result["makefile"].replace("program:", f"{prog_name}:") \
                                     .replace("program.c", f"{prog_name}.c") \
                                     .replace("-o program ", f"-o {prog_name} ")
        with open(mf_path, "w", encoding="utf-8") as fh:
            fh.write(mf_text)
        print(f"dictumc: wrote '{mf_path}'", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
