# Dictum — Vibe-Code C/C++ in Plain English

**Describe what you want. Get a compiled binary. No C knowledge required.**

Dictum is a natural-language programming language that compiles to C and C++. Powered by a constrained LLM pipeline with formal grammar (GBNF) guarantees, every program you describe either compiles correctly or tells you exactly why it doesn't.

```
"build a live terminal dashboard showing CPU, memory, and top processes refreshing every second"
        ↓
Dictum generates valid C  (GBNF makes invalid output impossible)
        ↓
gcc compiles it  (94KB binary, no runtime, no dependencies)
        ↓
./monitor  — it just works
```

---

## Why Dictum

Every other AI coding tool generates code and hopes it works. Dictum generates Dictum — a small, well-defined language — and the **compiler** guarantees the output. The AI cannot produce syntactically invalid programs. The validator catches semantic errors before emit. The C that comes out is deterministic and correct.

| Tool | Generates | Correctness guarantee |
|---|---|---|
| Copilot / ChatGPT | C/Python directly | None — hope it runs |
| Dictum | Dictum → C/C++ | Compiler-verified |

---

## Quick Start

### Requirements
- VS Code 1.85+
- [Ollama](https://ollama.ai) (free, runs locally) **or** OpenAI / Anthropic / Groq API key
- Python 3.10+ (for the compiler)
- `gcc` or `clang` (for compilation)

### Install

1. Install this extension from the VS Code Marketplace
2. Install Ollama: [ollama.ai/download](https://ollama.ai/download)
3. Open the **Dictum** panel in the sidebar
4. Type what you want to build — hit **Generate Plan**

That's it. No config files. No setup scripts.

---

## The Pipeline

Dictum uses a three-pass AI pipeline — each pass gets a different skill injected:

```
Your description
      ↓
[PLAN]   — structured build plan, L0-L5 Merge Architecture layers
      ↓  you approve
[BUILD]  — GBNF-constrained generation, physically cannot produce invalid syntax
      ↓
[REVIEW] — checks plan vs implementation, catches unsafe compositions
      ↓
Dictum source (.dict)
      ↓
Compiler (lexer → parser → validator → emit_c / emit_cpp)
      ↓
C / C++ source
      ↓
gcc / g++ → native binary
```

---

## What You Can Build

Anything that runs on a CPU with no dependencies:

- **System tools** — monitors, scanners, analyzers
- **Games** — terminal games, raycasters, simulations
- **Cryptography** — via OpenSSL blessed library
- **Databases** — via SQLite blessed library
- **Graphics** — via Raylib / SDL2 blessed library
- **Networking** — TCP servers, HTTP clients via libcurl

### Example Programs

**System monitor (94KB binary)**
```
build a live terminal dashboard showing CPU usage, memory usage,
and top 10 processes refreshing every second
```

**Bitcoin miner**
```
build a SHA-256 CPU miner that computes hashes and displays
hash rate in megahashes per second
```

**3D raycaster**
```
build a first-person 3D maze renderer using raycasting
that I can walk through with arrow keys
```

---

## Blessed Libraries

Dictum ships pre-built stubs for common C libraries. No header files needed:

| Library | Coverage |
|---|---|
| SQLite3 | 291 functions |
| OpenSSL | Full crypto + TLS |
| Raylib | 2D/3D game dev |
| SDL2 | Audio + input |

Import any of them in one line:
```
use sqlite3
use raylib
```

---

## Language Overview

Dictum reads like English and compiles to C:

```dictum
program hello:
    keep message as text with value "Hello, world"
    print the text message and newline
end program
```

```dictum
action square takes n as whole number produces whole number:
    produce success with the product of n and n
end action
```

```dictum
use sqlite3

program contacts:
    keep db as opaque pointer
    call db_open with "contacts.db" and db
    ...
end program
```

Full language reference: [LANGUAGE_REFERENCE.md](https://github.com/jjsvg/dictum-vscode/blob/HEAD/LANGUAGE_REFERENCE.md)

---

## Models

Dictum works with any model that supports GBNF grammar constraints:

| Provider | Recommended model | Notes |
|---|---|---|
| Ollama (local) | llama3.1:8b | Free, private, works offline |
| LM Studio | Qwen2.5-7B | Best local quality |
| Anthropic | claude-sonnet-4-6 | Best overall quality |
| OpenAI | gpt-4o | Strong alternative |
| Groq | llama-3.1-8b-instant | Fastest |

Even a **2B parameter model** produces correct Dictum thanks to GBNF constraints — the grammar makes invalid output impossible regardless of model size.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `dictum.provider` | `ollama` | LLM provider |
| `dictum.baseUrl` | `http://localhost:11434` | Ollama / LM Studio URL |
| `dictum.topModel` | `llama3.1:8b` | Model for Plan + Review |
| `dictum.buildModel` | `llama3.1:8b` | Model for Build pass |
| `dictum.backend` | `c` | Output language: `c` or `cpp` |
| `dictum.actMode` | `false` | Auto-transpile on save |

API keys are stored securely in VS Code SecretStorage — never in plain-text settings.

---

## Commands

| Command | Shortcut | Description |
|---|---|---|
| Generate Plan | — | Start a new program from description |
| Approve Plan | — | Confirm plan and trigger Build |
| Build | — | Run the Build pass |
| Apply | — | Write generated code to active file |
| Transpile | `Ctrl+Shift+T` | Transpile active .dict to C/C++ |
| Transpile & Compile | `Ctrl+Shift+B` | Full pipeline to native binary |
| Open REPL | — | Interactive Dictum REPL |

---

## Multi-File Projects

For larger programs, Dictum supports multi-file projects with automatic dependency resolution:

```
my-project/
  main.dict
  database.dict
  networking.dict
  dictum.project.json
```

Run `Dictum: Build Project` to compile the entire workspace — topological sort, shared headers, and a unified Makefile are generated automatically.

---

## Performance

C output from Dictum vs equivalent Python:

| Task | Python | Dictum C | Speedup |
|---|---|---|---|
| SHA-256 hashing | ~1M hash/s | ~4M hash/s | 4× |
| File I/O | interpreter overhead | direct syscalls | 10–50× |
| Binary size | requires Python runtime | 40–200KB standalone | — |

---

## License

MIT — compiler, extension, and blessed libraries are all open source.

Pro features (cloud providers, Act Mode, multi-file projects) available via [Dictum Pro](#).
