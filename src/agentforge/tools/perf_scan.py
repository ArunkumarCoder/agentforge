"""Performance evidence: static patterns in Python source, and a real profile of a script.

Two sources, both local:

- :func:`scan_python` reads syntax trees (never runs the code) and reports patterns that are
  usually slow: a list searched with ``in`` inside a loop, ``s += ...`` on a string in a loop,
  a query or HTTP request per loop iteration, ``list.pop(0)``, nested loops over big
  collections, sorting inside a loop, and very complex functions. They are *candidates*:
  whether each one matters is for the caller to decide.
- :func:`profile` runs one script under ``cProfile`` in a separate ``python -I`` process
  (temporary directory, minimal environment, timeout) and returns the functions that took
  a real share of the time. This is measurement, not a guess.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from agentforge.tools.builtin.process import run_process
from agentforge.tools.pytest_runner import safe_env
from agentforge.tools.python_ast import cyclomatic_complexity, definitions

PROFILE_TIMEOUT_S = 60.0
MIN_SHARE = 0.05
MAX_HOTSPOTS = 10
COMPLEXITY_LIMIT = 15
SMALL_RANGE = 16
KINDS = (
    "nested_loop",
    "list_membership",
    "string_concat",
    "io_in_loop",
    "list_front_ops",
    "sort_in_loop",
    "regex_in_loop",
    "complexity",
    "hotspot",
    "algorithm",
    "caching",
    "memory",
    "other",
)
IMPACT_RANK = {"low": 1, "medium": 2, "high": 3}

_IO_ATTRS = {"execute", "executemany", "fetchone", "fetchall", "urlopen"}
_HTTP_RECEIVERS = {"requests", "httpx", "session", "client"}
_HTTP_ATTRS = {"get", "post", "put", "patch", "delete", "request"}

_PROFILE_RUNNER = """
import cProfile, json, os, runpy, sys
root, entry, out = os.getcwd(), sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
profiler = cProfile.Profile()
status, message = "ok", ""
profiler.enable()
try:
    runpy.run_path(os.path.join(root, entry), run_name="__main__")
except SystemExit:
    pass
except BaseException as exc:
    status, message = "error", f"{type(exc).__name__}: {exc}"
finally:
    profiler.disable()
rows, total = [], 0.0
for e in profiler.getstats():
    total += e.inlinetime
    code = e.code
    if isinstance(code, str) or code.co_filename.startswith("<"):
        continue
    path = os.path.abspath(code.co_filename)
    if not path.startswith(root + os.sep):
        continue
    rows.append({
        "file": os.path.relpath(path, root).replace(os.sep, "/"),
        "line": code.co_firstlineno,
        "function": code.co_qualname,
        "calls": e.callcount,
        "tottime": e.inlinetime,
        "cumtime": e.totaltime,
    })
with open(out, "w", encoding="utf-8") as handle:
    json.dump({"status": status, "message": message, "total": total, "rows": rows}, handle)
"""


class PerfScanError(RuntimeError):
    """The profiler could not run the script."""


@dataclass(frozen=True)
class Candidate:
    """A pattern that is often slow, found in the source."""

    kind: str
    file: str
    line: int
    function: str
    title: str
    code: str
    impact: str  # a first guess: low | medium | high
    share: float | None = None  # filled in from a profile when the function was measured


@dataclass(frozen=True)
class Hotspot:
    """A function that took a measurable share of a profiled run."""

    file: str
    line: int
    function: str
    calls: int
    tottime_s: float
    cumtime_s: float
    share: float  # of the total time spent in functions, 0..1


# --------------------------------------------------------------------------- static


def scan_python(files: Mapping[str, str]) -> list[Candidate]:
    """Slow patterns in every parsable ``.py`` file, sorted by file and line."""
    found: list[Candidate] = []
    for path, source in sorted(files.items()):
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            continue
        lines = source.splitlines()
        for definition in definitions(tree):
            node = definition.node
            if isinstance(node, ast.ClassDef):
                continue
            name = "<module>" if isinstance(node, ast.Module) else definition.qualname
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                score = cyclomatic_complexity(node)
                if score >= COMPLEXITY_LIMIT:
                    found.append(
                        Candidate(
                            "complexity",
                            path,
                            node.lineno,
                            name,
                            f"Cyclomatic complexity {score} (limit {COMPLEXITY_LIMIT})",
                            _line(lines, node.lineno),
                            "low",
                        )
                    )
            scanner = _Scanner(path, name, lines, node)
            for statement in node.body:
                scanner.visit(statement)
            found.extend(scanner.found)
    unique = {(c.kind, c.file, c.line): c for c in found}
    return sorted(unique.values(), key=lambda c: (c.file, c.line, c.kind))


def _line(lines: list[str], number: int) -> str:
    return lines[number - 1].strip()[:200] if 0 < number <= len(lines) else ""


def _own(body: list[ast.stmt]) -> Iterator[ast.AST]:
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _annotation_has(node: ast.expr | None, word: str) -> bool:
    return node is not None and word in ast.unparse(node).lower()


def _types(node: ast.AST) -> tuple[set[str], set[str]]:
    """Names that are certainly lists, and names that are certainly text, in this function."""
    lists: set[str] = set()
    texts: set[str] = set()
    other: set[str] = set()
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        for arg in args:
            if _annotation_has(arg.annotation, "list"):
                lists.add(arg.arg)
            elif _annotation_has(arg.annotation, "str"):
                texts.add(arg.arg)
    body = node.body if hasattr(node, "body") else []
    for item in _own(body):
        if isinstance(item, ast.Assign):
            names = [t.id for t in item.targets if isinstance(t, ast.Name)]
            value: ast.expr | None = item.value
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            names, value = [item.target.id], item.value
        else:
            continue
        if (
            isinstance(value, ast.BinOp)
            and isinstance(value.op, ast.Add)
            and isinstance(value.left, ast.Name)
            and value.left.id in names
        ):
            continue  # s = s + x keeps the type of s
        if _is_list(value):
            lists.update(names)
        elif isinstance(value, ast.JoinedStr) or (
            isinstance(value, ast.Constant) and isinstance(value.value, str)
        ):
            texts.update(names)
        else:
            other.update(names)
    return lists - other, texts - other


def _is_list(value: ast.expr | None) -> bool:
    if isinstance(value, ast.List | ast.ListComp):
        return True
    if isinstance(value, ast.Call):
        func = value.func
        if isinstance(func, ast.Name) and func.id in {"list", "sorted"}:
            return True
        return isinstance(func, ast.Attribute) and func.attr in {"split", "splitlines"}
    return False


def _small(iterable: ast.expr) -> bool:
    """A literal or a ``range`` of small constants: a loop that cannot grow."""
    if isinstance(iterable, ast.Constant | ast.Tuple | ast.List | ast.Set):
        return True
    if (
        isinstance(iterable, ast.Call)
        and isinstance(iterable.func, ast.Name)
        and iterable.func.id == "range"
    ):
        nums = [
            a.value
            for a in iterable.args
            if isinstance(a, ast.Constant) and isinstance(a.value, int)
        ]
        return len(nums) == len(iterable.args) and max(nums, default=0) <= SMALL_RANGE
    return False


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, function: str, lines: list[str], node: ast.AST) -> None:
        self.path, self.function, self.lines = path, function, lines
        self.found: list[Candidate] = []
        self.lists, self.texts = _types(node)
        self.loops = 0  # loops and comprehensions we are inside
        self.for_iters: list[ast.expr] = []

    def _add(self, kind: str, node: ast.AST, title: str, impact: str) -> None:
        line = getattr(node, "lineno", 1)
        self.found.append(
            Candidate(kind, self.path, line, self.function, title, _line(self.lines, line), impact)
        )

    # nested functions and classes are scanned as their own definitions
    def _skip(self, node: ast.AST) -> None:
        return

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = visit_Lambda = _skip  # noqa: N815

    def _for(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        if self.for_iters and not _small(node.iter) and not _small(self.for_iters[-1]):
            self._add("nested_loop", node, "Loop inside a loop over growing collections", "medium")
        self.loops += 1
        self.for_iters.append(node.iter)
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)
        self.for_iters.pop()
        self.loops -= 1

    visit_For = visit_AsyncFor = _for  # noqa: N815

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self.loops += 1
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)
        self.loops -= 1

    def _comprehension(self, node: ast.AST) -> None:
        self.loops += 1
        self.generic_visit(node)
        self.loops -= 1

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comprehension  # noqa: N815

    def visit_Compare(self, node: ast.Compare) -> None:
        if self.loops:
            for op, right in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, ast.In | ast.NotIn) and self._is(right, self.lists):
                    self._add(
                        "list_membership",
                        node,
                        "Searching a list with 'in' inside a loop (linear scan per item)",
                        "medium",
                    )
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if self.loops and isinstance(node.op, ast.Add) and self._is(node.target, self.texts):
            self._add("string_concat", node, "Building a string with += inside a loop", "low")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        value = node.value
        if (
            self.loops
            and isinstance(value, ast.BinOp)
            and isinstance(value.op, ast.Add)
            and len(node.targets) == 1
            and self._is(node.targets[0], self.texts)
            and isinstance(value.left, ast.Name)
            and isinstance(node.targets[0], ast.Name)
            and value.left.id == node.targets[0].id
        ):
            self._add("string_concat", node, "Building a string with + inside a loop", "low")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.loops:
            self._call_in_loop(node)
        self.generic_visit(node)

    def _call_in_loop(self, node: ast.Call) -> None:
        func = node.func
        first = node.args[0] if node.args else None
        zero = isinstance(first, ast.Constant) and first.value == 0
        if isinstance(func, ast.Attribute):
            owner = func.value.id if isinstance(func.value, ast.Name) else ""
            if func.attr in _IO_ATTRS or (owner in _HTTP_RECEIVERS and func.attr in _HTTP_ATTRS):
                self._add("io_in_loop", node, "A query or request on every loop iteration", "high")
            elif func.attr in {"insert", "pop"} and zero:
                self._add(
                    "list_front_ops", node, f"list.{func.attr}(0) shifts every item", "medium"
                )
            elif func.attr == "sort":
                self._add("sort_in_loop", node, "Sorting inside a loop", "medium")
            elif owner == "re" and func.attr == "compile":
                self._add("regex_in_loop", node, "Compiling a regex inside a loop", "low")
        elif isinstance(func, ast.Name):
            if func.id == "sorted":
                self._add("sort_in_loop", node, "Sorting inside a loop", "medium")
            elif func.id == "open":
                self._add("io_in_loop", node, "Opening a file on every loop iteration", "medium")

    @staticmethod
    def _is(node: ast.AST, names: set[str]) -> bool:
        return isinstance(node, ast.Name) and node.id in names


# --------------------------------------------------------------------------- profile


async def profile(
    files: Mapping[str, str], entry: str, *, timeout_s: float = PROFILE_TIMEOUT_S
) -> list[Hotspot]:
    """Run ``entry`` under cProfile; the functions with at least 5% of the time, slowest first."""
    if entry not in files:
        raise PerfScanError(f"profile script '{entry}' is not among the files")
    with tempfile.TemporaryDirectory(prefix="agentforge-profile-") as tmp:
        root = _write(tmp, files)
        out = root.parent / f"{root.name}.profile.json"
        result = await run_process(
            [sys.executable, "-I", "-c", _PROFILE_RUNNER, entry, str(out)],
            cwd=str(root),
            timeout_s=timeout_s,
            env=safe_env(),
            inherit_env=False,
        )
        if result.timed_out:
            raise PerfScanError(f"the profile script ran longer than {timeout_s:.0f}s")
        try:
            report = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            tail = (result.stderr or result.stdout).strip().splitlines()
            raise PerfScanError(f"no profile produced: {tail[-1] if tail else 'unknown'}") from exc
    if report["status"] != "ok":
        raise PerfScanError(f"the profile script failed: {report['message']}")
    total = float(report["total"]) or 1e-9
    spots = [
        Hotspot(
            file=r["file"],
            line=r["line"],
            function=r["function"],
            calls=r["calls"],
            tottime_s=r["tottime"],
            cumtime_s=r["cumtime"],
            share=r["tottime"] / total,
        )
        for r in report["rows"]
        if r["function"] != "<module>" and r["tottime"] / total >= MIN_SHARE
    ]
    return sorted(spots, key=lambda h: -h.share)[:MAX_HOTSPOTS]


def _write(tmp: str, files: Mapping[str, str]) -> Path:
    base = Path(tmp).resolve()
    root = base / "src"
    for rel, content in files.items():
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise PerfScanError(f"refusing to write outside the profile directory: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def attach_shares(candidates: list[Candidate], hotspots: list[Hotspot]) -> list[Candidate]:
    """Copy each hotspot's share onto the static candidates inside that function."""
    shares = {(h.file, h.function): h.share for h in hotspots}
    return [
        Candidate(
            c.kind,
            c.file,
            c.line,
            c.function,
            c.title,
            c.code,
            c.impact,
            shares.get((c.file, c.function)),
        )
        for c in candidates
    ]
