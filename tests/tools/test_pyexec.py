"""run_python: results, charts, errors and the limits that keep model-written code contained."""

import os
import sys
from typing import Any

import pytest

from agentforge.core import ToolCall
from agentforge.tools import ToolContext
from agentforge.tools.base import ToolError
from agentforge.tools.builtin import Limits
from agentforge.tools.pyexec import (
    PNG_MAGIC,
    RunPythonTool,
    network_namespace_available,
    run_python,
    safe_relative,
)

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="limits are not applied on Windows")
LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux", reason="address-space limits are only enforced on Linux"
)


async def test_a_script_reads_files_prints_and_emits() -> None:
    code = (
        "import json\n"
        "from agentforge_sandbox import emit\n"
        "rows = json.load(open('data/in.json'))\n"
        "print('rows', len(rows))\n"
        "emit('total', sum(rows))\n"
        "emit('labels', ['a', 'b'])\n"
        "emit('nested', {'x': 1.5, 'y': None})\n"
    )
    run = await run_python({"data/in.json": "[1, 2, 3.5]"}, code)
    assert run.ok
    assert run.process.stdout.strip() == "rows 3"
    assert run.results == {"total": 6.5, "labels": ["a", "b"], "nested": {"x": 1.5, "y": None}}
    assert run.charts == {}


async def test_charts_are_saved_as_png() -> None:
    code = (
        "import matplotlib.pyplot as plt\n"
        "from agentforge_sandbox import save_chart\n"
        "plt.plot([1, 2, 3], [3, 1, 2])\n"
        "save_chart('line')\n"
    )
    run = await run_python({}, code)
    assert run.ok, run.render()
    assert run.charts["line"].startswith(PNG_MAGIC)


async def test_numpy_and_nan_become_plain_json() -> None:
    code = (
        "import numpy as np\n"
        "from agentforge_sandbox import emit\n"
        "emit('n', np.int64(7))\n"
        "emit('f', np.float64(2.5))\n"
        "emit('nan', float('nan'))\n"
        "emit('arr', np.array([1, 2]))\n"
    )
    run = await run_python({}, code)
    assert run.ok, run.render()
    assert run.results == {"n": 7, "f": 2.5, "nan": None, "arr": [1, 2]}


async def test_an_error_shows_the_users_line_not_the_runner() -> None:
    run = await run_python({}, "x = 1\nprint(1 / 0)\n")
    assert not run.ok
    stderr = run.process.stderr
    assert 'File "user_code.py", line 2' in stderr
    assert "ZeroDivisionError" in stderr
    assert "_RUNNER" not in stderr and "runner" not in stderr.lower()


async def test_bad_emit_calls_are_errors() -> None:
    bad_name = await run_python({}, "from agentforge_sandbox import emit\nemit('no spaces', 1)\n")
    assert not bad_name.ok
    assert "name must be letters" in bad_name.process.stderr
    bad_value = await run_python({}, "from agentforge_sandbox import emit\nemit('x', object())\n")
    assert not bad_value.ok
    assert "cannot emit a value of type object" in bad_value.process.stderr
    bad_chart = await run_python(
        {}, "from agentforge_sandbox import save_chart\nsave_chart('Bad Name')\n"
    )
    assert not bad_chart.ok


async def test_a_script_that_never_ends_is_killed() -> None:
    run = await run_python({}, "while True:\n    pass\n", timeout_s=2)
    assert run.process.timed_out
    assert not run.ok
    assert "timed out" in run.render()


@LINUX_ONLY
async def test_memory_is_limited() -> None:
    run = await run_python(
        {}, "data = bytearray(2_000_000_000)\nprint('allocated')\n", limits=Limits(memory_mb=400)
    )
    assert not run.ok
    assert "MemoryError" in run.process.stderr
    assert "allocated" not in run.process.stdout


@POSIX
async def test_cpu_time_is_limited() -> None:
    run = await run_python({}, "while True:\n    pass\n", timeout_s=30, limits=Limits(cpu_s=1))
    assert not run.ok
    assert run.process.signal_name in {"SIGXCPU", "SIGKILL"}


@POSIX
async def test_file_size_is_limited() -> None:
    code = "with open('big.bin', 'wb') as f:\n    f.write(b'x' * 5_000_000)\n"
    run = await run_python({}, code, limits=Limits(file_mb=1))
    assert not run.ok


CONNECT = (
    "import socket, _socket, urllib.request\n"
    "for attempt in (\n"
    "    lambda: socket.create_connection(('93.184.215.14', 80), 2),\n"
    "    lambda: socket.socket().connect(('93.184.215.14', 80)),\n"
    "    lambda: _socket.socket().connect(('93.184.215.14', 80)),\n"
    "    lambda: socket.getaddrinfo('example.com', 80),\n"
    "    lambda: urllib.request.urlopen('http://example.com', timeout=2),\n"
    "):\n"
    "    try:\n"
    "        attempt()\n"
    "        print('CONNECTED')\n"
    "    except OSError:\n"
    "        print('blocked')\n"
)


@pytest.mark.parametrize("namespace", [False, True])
async def test_the_network_is_blocked(namespace: bool) -> None:
    if namespace and not network_namespace_available():
        pytest.skip("this machine does not allow unprivileged network namespaces")
    run = await run_python({}, CONNECT, network_namespace=namespace)
    assert run.ok, run.render()
    assert run.process.stdout.split() == ["blocked"] * 5
    assert run.isolation == ("namespace" if namespace else "guard")


async def test_the_default_isolation_matches_the_machine() -> None:
    run = await run_python({}, "print('hi')")
    assert run.isolation == ("namespace" if network_namespace_available() else "guard")


async def test_secrets_in_the_environment_do_not_reach_the_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("AGENTFORGE_API__API_KEY", "also-not-real")
    run = await run_python({}, "import os\nprint(sorted(os.environ))")
    assert run.ok
    assert "ANTHROPIC_API_KEY" not in run.process.stdout
    assert "AGENTFORGE_API__API_KEY" not in run.process.stdout


def _exists(path: str) -> bool:
    return os.path.exists(path)


async def test_the_working_folder_is_removed_afterwards() -> None:
    run = await run_python({}, "import os\nprint(os.getcwd())")
    folder = run.process.stdout.strip()
    assert "agentforge-py-" in folder
    assert not _exists(folder)


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "../x.csv",
        "a/../../x.csv",
        "",
        "user_code.py",
        "results.json",
        ".hidden/x",
        "charts/a.png",
    ],
)
def test_unsafe_file_names_are_refused(name: str) -> None:
    with pytest.raises(ToolError):
        safe_relative(name)


def test_nested_names_are_fine() -> None:
    assert str(safe_relative("data/2026/a.csv")) == "data/2026/a.csv"
    assert str(safe_relative("data\\a.csv")) == "data/a.csv"


async def call(tool: RunPythonTool, args: dict[str, Any]) -> tuple[str, bool]:
    result = await tool.invoke(
        ToolCall(id="c", name="run_python", arguments=args),
        ToolContext(allowed=frozenset({"exec"})),  # type: ignore[arg-type]
    )
    return result.content, result.is_error


async def test_the_tool_reports_output_results_and_charts() -> None:
    tool = RunPythonTool({"a.csv": "x\n1\n3\n"})
    code = (
        "import pandas as pd\n"
        "from agentforge_sandbox import emit\n"
        "df = pd.read_csv('a.csv')\n"
        "print(df.x.sum())\n"
        "emit('mean_x', df.x.mean())\n"
    )
    text, error = await call(tool, {"code": code})
    assert not error
    assert "exit code: 0" in text
    assert "4" in text
    assert '"mean_x": 2.0' in text


async def test_the_tool_needs_exec_permission() -> None:
    tool = RunPythonTool({})
    result = await tool.invoke(
        ToolCall(id="c", name="run_python", arguments={"code": "print(1)"}),
        ToolContext(allowed=frozenset({"read"})),  # type: ignore[arg-type]
    )
    assert result.is_error
    assert "Permission denied" in result.content
