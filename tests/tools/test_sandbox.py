"""Sandbox settings and the limits that model-written code runs under."""

import sys
from collections.abc import Iterator

import pytest

from agentforge.config import SandboxSettings, Settings, get_settings
from agentforge.tools import perf_scan, sandbox
from agentforge.tools.builtin.process import Limits
from agentforge.tools.pytest_runner import run_pytest
from agentforge.tools.sandbox import limits_from, untrusted_limits

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="resource limits need POSIX")


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_are_on_and_sensible() -> None:
    box = Settings(_env_file=None).sandbox
    assert (box.enabled, box.cpu_s, box.memory_mb, box.file_mb, box.output_mb) == (
        True,
        120,
        2048,
        100,
        8,
    )
    assert limits_from(Settings(_env_file=None)) == Limits(
        cpu_s=120, memory_mb=2048, file_mb=100, output_mb=8
    )


def test_disabling_keeps_the_output_cap_but_drops_the_rest() -> None:
    off = Settings(_env_file=None, sandbox=SandboxSettings(enabled=False, output_mb=3))
    assert limits_from(off) == Limits(output_mb=3)


def test_limits_follow_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_SANDBOX__MEMORY_MB", "512")
    monkeypatch.setenv("AGENTFORGE_SANDBOX__CPU_S", "7")
    assert untrusted_limits() == Limits(cpu_s=7, memory_mb=512, file_mb=100, output_mb=8)


def test_nonsense_values_are_refused() -> None:
    for bad in ({"cpu_s": 0}, {"memory_mb": 8}, {"file_mb": 0}, {"output_mb": 0}):
        with pytest.raises(ValueError):
            SandboxSettings(**bad)


def test_a_broken_environment_falls_back_to_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_SANDBOX__CPU_S", "not-a-number")
    assert untrusted_limits() == Limits(cpu_s=120, memory_mb=2048, file_mb=100, output_mb=8)


@posix_only
async def test_a_runaway_generated_test_is_stopped_by_the_memory_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFORGE_SANDBOX__MEMORY_MB", "400")
    hog = "def test_hog() -> None:\n    data = bytearray(1500 * 1024 * 1024)\n    assert data\n"
    result = await run_pytest(
        files={"tests/test_hog.py": hog}, tests=["tests/test_hog.py"], measure=None
    )
    assert not result.passed
    assert "MemoryError" in result.output


@posix_only
async def test_a_fine_generated_test_still_passes_under_the_default_limits() -> None:
    ok = "def test_ok() -> None:\n    assert sum(range(10)) == 45\n"
    result = await run_pytest(
        files={"tests/test_ok.py": ok}, tests=["tests/test_ok.py"], measure=None
    )
    assert result.passed
    assert result.tests == 1


@posix_only
async def test_a_profile_script_that_spins_is_stopped_by_the_cpu_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFORGE_SANDBOX__CPU_S", "1")
    with pytest.raises(perf_scan.PerfScanError):
        await perf_scan.profile({"main.py": "while True:\n    pass\n"}, "main.py", timeout_s=60)


def test_the_module_reexports_what_the_tools_need() -> None:
    assert sandbox.Limits is Limits
