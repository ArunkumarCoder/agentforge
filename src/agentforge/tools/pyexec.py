"""Run a Python script that a model wrote, in a throw-away folder, with limits and no network.

``run_python(files, code)`` copies ``files`` into a fresh temporary folder, runs ``code`` there
in a child interpreter and returns what it printed, what it ``emit``-ted and which charts it
saved. Inside the script::

    from agentforge_sandbox import emit, save_chart
    emit("average_order_value", df["amount"].mean())   # numbers, text, lists, dicts
    save_chart("revenue_by_month")                      # saves the current matplotlib figure

What is enforced (see ``docs/SECURITY.md`` for what is not):

- **time**: a wall-clock timeout kills the child and everything it started,
- **memory, CPU time, file size**: the ``AGENTFORGE_SANDBOX__*`` limits (Linux and macOS),
- **no network**: on Linux the child runs in an empty network namespace (``unshare -rn``) when
  the machine allows it, so there is no route out at all. Everywhere else, and as a second
  layer, the child's ``socket`` module is replaced by one that refuses to connect or resolve
  names. The second layer stops accidental and naive network use; code that is trying to get
  around it (for example through ``ctypes``) is only stopped by the namespace.
- **where it writes**: the working folder is deleted afterwards. Files elsewhere that the
  AgentForge user can read stay readable: this is not a container.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from agentforge.tools.base import BaseTool, ToolContext, ToolError, ToolPermission
from agentforge.tools.builtin.process import Limits, ProcessResult, run_process

MAX_RESULTS_BYTES = 1_000_000
MAX_CHART_BYTES = 1_500_000
MAX_CHARTS = 6
CHART_NAME = "^[a-z][a-z0-9_-]{0,39}$"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SCRIPT = "user_code.py"

# Runs first in the child: block the network, define ``agentforge_sandbox``, then the user code.
_RUNNER = r'''
import json, math, os, re, socket, sys, traceback, types, _socket

def _deny(*args, **kwargs):
    raise OSError(101, "network access is disabled in the analysis sandbox")

for _mod in (socket, _socket):
    for _fn in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
                "create_connection"):
        if hasattr(_mod, _fn):
            setattr(_mod, _fn, _deny)

class _NoSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        _deny()

socket.socket = _NoSocket
_socket.socket = _NoSocket

_results = {}

def _plain(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if getattr(value, "shape", None) == () and hasattr(value, "item"):
        return _plain(value.item())
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    if hasattr(value, "tolist"):
        return _plain(value.tolist())
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    raise TypeError("cannot emit a value of type " + type(value).__name__)

def emit(name, value):
    """Record a result under ``name`` (a short snake_case string)."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,59}", name):
        raise ValueError("emit: name must be letters, digits and underscores, e.g. 'mean_amount'")
    _results[name] = _plain(value)
    with open("results.json", "w", encoding="utf-8") as handle:
        json.dump(_results, handle)

def save_chart(name, figure=None):
    """Save a matplotlib figure (default: the current one) as charts/<name>.png and close it."""
    if not isinstance(name, str) or not re.fullmatch(__CHART_NAME__, name):
        raise ValueError("save_chart: name must be lower-case, digits, _ or -, e.g. 'sales'")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig = figure if figure is not None else plt.gcf()
    os.makedirs("charts", exist_ok=True)
    fig.savefig("charts/" + name + ".png", dpi=100, bbox_inches="tight")
    plt.close(fig)

_module = types.ModuleType("agentforge_sandbox")
_module.emit = emit
_module.save_chart = save_chart
sys.modules["agentforge_sandbox"] = _module

os.environ["MPLBACKEND"] = "Agg"
os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".mplconfig"))
sys.path.insert(0, os.getcwd())
_source = open(sys.argv[1], encoding="utf-8").read()
try:
    exec(compile(_source, sys.argv[1], "exec"), {"__name__": "__main__"})
except SystemExit:
    raise
except BaseException as exc:
    tb = exc.__traceback__.tb_next if exc.__traceback__ else None
    sys.stderr.write("".join(traceback.format_exception(type(exc), exc, tb)))
    sys.exit(1)
'''.replace("__CHART_NAME__", repr(CHART_NAME))


@dataclass
class PyRun:
    """What one run produced."""

    process: ProcessResult
    results: dict[str, object] = field(default_factory=dict)
    charts: dict[str, bytes] = field(default_factory=dict)
    isolation: str = "guard"  # "namespace": no network at all; "guard": socket module blocked

    @property
    def ok(self) -> bool:
        return self.process.exit_code == 0 and not self.process.timed_out

    def render(self, *, max_chars: int = 6000) -> str:
        """The run as text for a model: outcome, output, emitted results, charts."""
        parts = [self.process.render()]
        if self.results:
            shown = json.dumps(self.results, default=str)
            if len(shown) > max_chars:
                shown = shown[:max_chars] + "...[cut]"
            parts.append(f"--- emitted results ---\n{shown}")
        if self.charts:
            parts.append("--- charts saved ---\n" + ", ".join(sorted(self.charts)))
        return "\n".join(parts)


@functools.cache
def network_namespace_available() -> bool:
    """True when ``unshare -rn`` works here (Linux with unprivileged user namespaces)."""
    return _is_linux() and _probe_unshare()


def _is_linux() -> bool:
    return str(sys.platform).startswith("linux")


def _probe_unshare() -> bool:
    unshare = shutil.which("unshare")
    if unshare is None:
        return False
    try:
        done = subprocess.run(  # noqa: S603 - fixed command
            [unshare, "-rn", "true"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _child_env() -> dict[str, str]:
    """A small environment: no API keys, no AgentForge settings."""
    if sys.platform == "win32":
        secret = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        kept = {
            k: v
            for k, v in os.environ.items()
            if not k.upper().startswith("AGENTFORGE_") and not any(w in k.upper() for w in secret)
        }
        return {**kept, "PYTHONHASHSEED": "0"}
    else:
        return {"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0", "LC_ALL": "C.UTF-8"}


def safe_relative(name: str) -> PurePosixPath:
    """A file name that stays inside the working folder; raises ``ToolError`` otherwise."""
    path = PurePosixPath(name.replace("\\", "/"))
    if not name or path.is_absolute() or ".." in path.parts or path.parts[0] in {"charts"}:
        raise ToolError(f"'{name}' is not an allowed file name")
    if path.name in {SCRIPT, "results.json"} or path.parts[0].startswith("."):
        raise ToolError(f"'{name}' is a reserved name")
    return path


async def run_python(
    files: Mapping[str, str],
    code: str,
    *,
    timeout_s: float = 60.0,
    limits: Limits | None = None,
    network_namespace: bool | None = None,
) -> PyRun:
    """Run ``code`` with ``files`` in a temporary folder (deleted afterwards)."""
    use_namespace = (
        network_namespace_available() if network_namespace is None else network_namespace
    )
    with tempfile.TemporaryDirectory(prefix="agentforge-py-") as tmp:
        work = Path(tmp)
        for name, text in files.items():
            target = work / safe_relative(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        (work / SCRIPT).write_text(code, encoding="utf-8")
        argv = [sys.executable, "-I", "-c", _RUNNER, SCRIPT]
        if use_namespace:
            argv = ["unshare", "-rn", *argv]
        process = await run_process(
            argv,
            cwd=str(work),
            timeout_s=timeout_s,
            env=_child_env(),
            inherit_env=False,
            limits=limits,
        )
        return PyRun(
            process=process,
            results=_read_results(work / "results.json"),
            charts=_read_charts(work / "charts"),
            isolation="namespace" if use_namespace else "guard",
        )


def _read_results(path: Path) -> dict[str, object]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_RESULTS_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_charts(folder: Path) -> dict[str, bytes]:
    charts: dict[str, bytes] = {}
    if not folder.is_dir():
        return charts
    for path in sorted(folder.glob("*.png"))[:MAX_CHARTS]:
        if path.is_symlink() or path.stat().st_size > MAX_CHART_BYTES:
            continue
        data = path.read_bytes()
        if data.startswith(PNG_MAGIC):
            charts[path.stem] = data
    return charts


class RunPythonArgs(BaseModel):
    code: str = Field(
        min_length=1,
        max_length=20_000,
        description="A complete Python script. It runs from scratch in a folder with the data "
        "files. Use print() to look at data and emit(name, value) to record results.",
    )


class RunPythonTool(BaseTool[RunPythonArgs]):
    """``run_python`` for one set of data files. Every call starts from a clean folder."""

    name = "run_python"
    description = (
        "Run a Python script (pandas, numpy and matplotlib are installed) on the data files and "
        "return its output. Each call is independent: nothing carries over, so write a complete "
        "script. No network. `from agentforge_sandbox import emit, save_chart` records results "
        "and charts."
    )
    args_model = RunPythonArgs
    permission = ToolPermission.EXEC
    timeout_s = 90.0

    def __init__(
        self,
        files: Mapping[str, str],
        *,
        limits: Limits | None = None,
        run_timeout_s: float = 60.0,
    ) -> None:
        super().__init__()
        self._files = dict(files)
        self._limits = limits
        self._run_timeout_s = run_timeout_s

    async def run(self, args: RunPythonArgs, ctx: ToolContext) -> str:
        result = await run_python(
            self._files, args.code, timeout_s=self._run_timeout_s, limits=self._limits
        )
        return result.render()
