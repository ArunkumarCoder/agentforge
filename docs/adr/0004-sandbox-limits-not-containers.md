# ADR-0004: Resource limits and process cleanup now, containers later

- **Status:** Accepted
- **Date:** 2026-10-20
- **Deciders:** Arunkumar

## Context

Several agents run code that a model wrote: generated tests and doctests, profile scripts, SQLAlchemy models, patched code in the Bug Fix evals. Until Day 25 that code ran with a timeout and a minimal environment, which stops secrets leaking and infinite loops, but not a runaway allocation, a program that prints gigabytes, or children that outlive their timeout. The open question in `docs/ARCHITECTURE.md` was whether to use "subprocess with resource limits, or a Docker container per run".

Forces:

- **Portability.** AgentForge is a command-line tool used on Windows, macOS and Linux. Requiring Docker for `agentforge agents run` would be a heavy new prerequisite.
- **Speed.** Every test run would pay a container start (seconds), and the eval harness runs fifty cases.
- **What goes wrong in practice.** The realistic failures are accidents (a loop, a huge list, an unbounded print), not attacks.
- **Where the real boundary belongs.** Whoever deploys AgentForge for other people already has a container or VM story.

## Decision

1. All program starts go through `run_process`. It puts the child in its own process group and kills the whole group on timeout, caps captured output (8 MB per stream by default) and stops a program that exceeds it.
2. For untrusted code `run_process` takes `Limits`: CPU seconds, address space and largest file, plus no core dumps. They are set on POSIX by a small `python -I -c` launcher that then `exec`s the program (no `preexec_fn`, which is unsafe in a threaded process).
3. The values come from `AGENTFORGE_SANDBOX__*`, on by default, and can be turned off.
4. No container ships in v0.1.0. `docs/SECURITY.md` says what the limits do not do. If a container executor is added later, it implements the same `run_process` contract and no agent changes.

## Alternatives considered

- **A container per run (Docker, gVisor, nsjail, microVM):** real isolation, but a hard dependency, slower, and different on every platform. Not worth it before the API has users other than its owner.
- **`RLIMIT_NPROC` for fork bombs:** it counts every process of the user, so a low value breaks ordinary use and a high one protects nothing. Left out; the timeout and group kill are the defence.
- **`preexec_fn` to set limits:** fewer moving parts, but documented as unsafe with threads, and AgentForge uses threads (run history, the API).
- **Doing nothing new:** leaves the common accidents unhandled.

## Consequences

- **Positive:** the common accidents are contained on Linux and macOS at almost no cost, on every code path, with one place to maintain. The behaviour is tested (grandchild killed, flood cut, CPU/memory/file limits enforced).
- **Negative:** not a security boundary. The code can still read files and use the network. On Windows only the timeout and output cap apply, and macOS ignores the address-space limit.
- **Follow-ups:** a container executor behind the same interface if the API is exposed beyond its owner; consider Windows Job Objects for limits there.
