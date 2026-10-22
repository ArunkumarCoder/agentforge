# Security notes

AgentForge runs code written by language models and code you send it. This page says what is protected, what is not, and what to do about it. Last reviewed in Week 5 (Day 25).

## The short version

- **Treat every agent run as running someone else's code.** The Python tools execute generated tests, profile scripts and SQLAlchemy models. They are guarded, but they are **not a sandbox**.
- Run AgentForge as a user that owns nothing valuable, or inside a container or VM, and never expose `agentforge serve` to people you do not trust.
- There is no container in v0.1.0. That was a decision, not an omission: limits and process cleanup remove the common accidents (loops, memory hogs, floods) cheaply on every platform, and real isolation is better delegated to the container or VM you already deploy in. See "Decision" below.

## What runs model-written code

| Where | What runs | Guarded by |
|---|---|---|
| `tools/pytest_runner.py` (Test Generation, Documentation, Refactoring) | generated tests and doctests, with `python -I` in a temp folder | minimal environment, timeout, sandbox limits |
| `tools/perf_scan.py` `profile()` (Performance) | a profile script under cProfile | minimal environment, timeout, sandbox limits |
| `tools/sa_models.py` (Database Schema) | generated SQLAlchemy models, imported in a child process | minimal environment, timeout, sandbox limits |
| `agents/bug_fix/scoring.py` (Bug Fix evals) | the patched code plus the case's check script | timeout, sandbox limits (CPU, memory, file size) |
| `run_command` tool | only commands from an allow-list the developer chose; no shell | allow-list, argument pattern, timeout |
| Bandit, `pglast`, `ast` | read the code, never run it | not executed |

## The sandbox limits (Linux and macOS)

`run_process` (the one place AgentForge starts programs) does the following:

- **Process group kill.** The child starts in its own process group. On timeout, everything it started is killed, not just the child.
- **Output cap.** Captured stdout and stderr are capped (8 MB each by default). A program that prints more is killed and the result says `too much output`.
- **Resource limits** for untrusted code, set by a small launcher just before the real program starts: CPU seconds (`SIGXCPU`), address space (allocations beyond it fail with `MemoryError`), the largest file it may write, and no core dumps.

| Setting | Default | Meaning |
|---|---|---|
| `AGENTFORGE_SANDBOX__ENABLED` | `true` | `false` removes the CPU, memory and file limits (the output cap and group kill stay) |
| `AGENTFORGE_SANDBOX__CPU_S` | `120` | CPU seconds per run |
| `AGENTFORGE_SANDBOX__MEMORY_MB` | `2048` | address space per run |
| `AGENTFORGE_SANDBOX__FILE_MB` | `100` | largest file the code may write |
| `AGENTFORGE_SANDBOX__OUTPUT_MB` | `8` | captured stdout / stderr, each |

Known limits of the limits:

- **Windows:** CPU, memory and file limits are not applied (there is no `resource` module). The output cap and timeout still are; the process-tree kill is the child only.
- **macOS:** the kernel does not enforce the address-space limit; CPU and file limits work.
- **No process-count limit.** `RLIMIT_NPROC` counts all of the user's processes, so a low value would break normal use. A fork bomb is stopped only by the timeout and the group kill.
- **Not isolated from the machine.** The code can read any file the AgentForge user can read, can use the network, and can see the same machine. Do not run it where that matters.

## Decision: no container yet

We considered running each tool call in a Docker container (or gVisor, nsjail, a microVM). Not for v0.1.0, because it would make Docker a hard requirement of a command-line tool, behave differently on Windows, macOS and Linux, and slow every test run by seconds. Revisit when the API is used by people other than its owner: the seam is `run_process`, so adding an executor that runs the same argument list in a container does not change any agent.

## The HTTP API

`agentforge serve` is private by default; see the README. In short: loopback only, an API key (Bearer) is required for any other host, `repo_path` is refused unless a root folder is configured and paths stay inside it, bodies over 5 MB are refused and at most 4 runs execute at once. The run history routes need the key too, because stored inputs can contain source code.

## Secrets

- Child processes for untrusted code get a **minimal environment**: `PATH`, temp and locale variables. Provider API keys and `AGENTFORGE_*` settings are not passed on.
- Logs redact secret-looking keys (`api_key`, `token`, `password`, ...). `config show` prints keys and database passwords as `set` / masked.
- The Security Audit agent redacts secrets it finds before they reach the report.
- Run history stores each run's **input** by default. Set `AGENTFORGE_STORAGE__STORE_INPUTS=false` if inputs may hold secrets, and use `agentforge runs prune` to expire old rows.
- The model provider receives whatever the agent puts in its prompts: code, diffs, schemas. Do not point AgentForge at code you may not send to the provider you configured.

## Reporting a problem

Open an issue without exploit details, or email the maintainer.
