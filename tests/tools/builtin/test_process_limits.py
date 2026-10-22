"""Hardened subprocess running: process-group kill, output cap, and resource limits (POSIX)."""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from agentforge.tools.base import ToolError
from agentforge.tools.builtin import run_process
from agentforge.tools.builtin.process import Limits, ProcessResult

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="resource limits need POSIX")


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def alive(pid: int) -> bool:
    """Is the process running? A zombie that nobody reaped yet does not count."""
    stat = Path(f"/proc/{pid}/stat")
    if Path("/proc/self").exists():  # Linux: read the state
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False
    try:  # macOS: no /proc
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------- process group


@posix_only
async def test_a_timeout_kills_the_children_too(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = await run_process(py(code), cwd=str(tmp_path), timeout_s=1.5)
    assert result.timed_out
    assert time.monotonic() - started < 10
    child = int(pid_file.read_text())
    for _ in range(50):
        if not alive(child):
            break
        await asyncio.sleep(0.1)
    assert not alive(child), "the grandchild outlived the timeout"


async def test_timeout_keeps_the_output_so_far(tmp_path: Path) -> None:
    code = "import sys, time\nprint('before', flush=True)\ntime.sleep(60)"
    result = await run_process(py(code), cwd=str(tmp_path), timeout_s=1.5)
    assert result.timed_out
    assert result.exit_code == -1
    assert "before" in result.stdout
    assert "(timed out)" in result.render()


# --------------------------------------------------------------------------- output cap


async def test_a_flood_of_output_is_cut_and_the_program_stopped(tmp_path: Path) -> None:
    started = time.monotonic()
    result = await run_process(py("while True: print('x' * 1000)"), cwd=str(tmp_path), timeout_s=60)
    assert result.output_truncated
    assert not result.timed_out
    assert len(result.stdout) == 8 * 1024 * 1024  # exactly the default cap, no more
    assert time.monotonic() - started < 30
    assert "too much output" in result.render()


async def test_the_cap_applies_to_stderr_and_can_be_lowered(tmp_path: Path) -> None:
    result = await run_process(
        py("import sys\nwhile True: sys.stderr.write('e' * 1000)"),
        cwd=str(tmp_path),
        timeout_s=60,
        limits=Limits(output_mb=1),
    )
    assert result.output_truncated
    assert len(result.stderr) == 1024 * 1024


async def test_ordinary_output_is_untouched(tmp_path: Path) -> None:
    result = await run_process(
        py("import sys\nprint('out')\nprint('err', file=sys.stderr)"),
        cwd=str(tmp_path),
        timeout_s=30,
    )
    assert (result.exit_code, result.stdout.strip(), result.stderr.strip()) == (0, "out", "err")
    assert not result.output_truncated
    assert result.signal_name is None


# --------------------------------------------------------------------------- limits


@posix_only
async def test_cpu_time_is_limited(tmp_path: Path) -> None:
    started = time.monotonic()
    result = await run_process(
        py("while True: pass"), cwd=str(tmp_path), timeout_s=60, limits=Limits(cpu_s=1)
    )
    assert not result.timed_out
    assert result.signal_name == "SIGXCPU"
    assert time.monotonic() - started < 30
    assert "killed by SIGXCPU" in result.render()


@posix_only
@pytest.mark.skipif(sys.platform == "darwin", reason="macOS does not enforce RLIMIT_AS")
async def test_memory_is_limited(tmp_path: Path) -> None:
    code = "data = bytearray(900 * 1024 * 1024)\nprint('allocated')"
    free = await run_process(py(code), cwd=str(tmp_path), timeout_s=60)
    assert "allocated" in free.stdout  # fine without a limit
    capped = await run_process(
        py(code), cwd=str(tmp_path), timeout_s=60, limits=Limits(memory_mb=300)
    )
    assert capped.exit_code != 0
    assert "MemoryError" in capped.stderr
    assert "allocated" not in capped.stdout


@posix_only
async def test_file_size_is_limited(tmp_path: Path) -> None:
    code = "open('big.bin', 'wb').write(b'x' * 5_000_000)"
    result = await run_process(py(code), cwd=str(tmp_path), timeout_s=60, limits=Limits(file_mb=1))
    assert result.exit_code != 0
    assert (tmp_path / "big.bin").stat().st_size <= 1024 * 1024


@posix_only
async def test_no_core_dumps(tmp_path: Path) -> None:
    code = "import resource\nprint(resource.getrlimit(resource.RLIMIT_CORE))"
    result = await run_process(py(code), cwd=str(tmp_path), timeout_s=30, limits=Limits())
    assert result.stdout.strip() == "(0, 0)"


async def test_limits_do_not_change_what_a_normal_program_sees(tmp_path: Path) -> None:
    result = await run_process(
        py("import os, sys\nprint(os.getcwd() == sys.argv[0] or 'ok', sys.argv)"),
        cwd=str(tmp_path),
        timeout_s=30,
        env={"KEEP": "1"},
        limits=Limits(cpu_s=30, memory_mb=1024, file_mb=10),
    )
    assert result.exit_code == 0
    assert "ok" in result.stdout


async def test_a_missing_program_is_still_a_tool_error_with_limits(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="Program not found: 'no-such-program-xyz'"):
        await run_process(
            ["no-such-program-xyz"], cwd=str(tmp_path), timeout_s=5, limits=Limits(cpu_s=5)
        )


def test_rlimit_table() -> None:
    assert Limits().rlimits() == {"RLIMIT_CORE": 0}
    assert Limits(cpu_s=3, memory_mb=2, file_mb=1).rlimits() == {
        "RLIMIT_CORE": 0,
        "RLIMIT_CPU": 3,
        "RLIMIT_AS": 2 * 1024 * 1024,
        "RLIMIT_FSIZE": 1024 * 1024,
    }


def test_unknown_signals_are_named_plainly() -> None:
    assert ProcessResult(-99, "", "").signal_name == "signal 99"
    assert ProcessResult(-9, "", "").signal_name == "SIGKILL"
    assert ProcessResult(3, "", "").signal_name is None


def test_with_limits_prefixes_the_launcher_only_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentforge.tools.builtin import with_limits

    argv = ["python", "script.py"]
    assert with_limits(argv, None) == argv
    wrapped = with_limits(argv, Limits(cpu_s=2))
    if sys.platform == "win32":
        assert wrapped == argv
    else:
        assert wrapped[:3] == [sys.executable, "-I", "-c"]
        assert wrapped[-2:] == argv
    monkeypatch.setattr(sys, "platform", "win32")
    assert with_limits(argv, Limits(cpu_s=2)) == argv
