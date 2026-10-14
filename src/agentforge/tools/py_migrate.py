"""Find code that needs a Python upgrade: deprecated spellings and removed standard-library APIs.

A small catalogue of rules, each reported as a :class:`Site` (rule, file, line). It reads
syntax trees only and never runs the code. The Migration agent uses it twice: to list what
must change, and to prove that after the rewrite those sites are gone and no new ones appeared.

Rules (all verified against Python 3.12):

- ``typing-generics``: ``List``, ``Dict``, ``Set``, ``Tuple``, ``Type`` ... from ``typing``,
- ``optional-union``: ``Optional[X]`` and ``Union[A, B]`` instead of ``X | None`` and ``A | B``,
- ``utcnow``: ``datetime.utcnow()`` and ``utcfromtimestamp()`` (deprecated in 3.12),
- ``collections-abc``: ``collections.Mapping`` and friends (removed in 3.10),
- ``unittest-aliases``: ``assertEquals``, ``assertRaisesRegexp`` ... (removed in 3.12),
- ``removed-modules``: ``imp``, ``distutils``, ``asyncore``, ``asynchat``, ``smtpd`` (3.12),
- ``removed-functions``: ``inspect.getargspec``, ``asyncio.coroutine``, ``time.clock``,
  ``base64.encodestring`` and ``decodestring``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

HINTS: dict[str, str] = {
    "typing-generics": (
        "use the built-in generics (list[int], dict[str, int], tuple[int, ...], type[X], "
        "str for Text) and remove the now unused typing imports"
    ),
    "optional-union": (
        "write X | None and A | B instead of Optional[X] and Union[A, B]; remove unused "
        "typing imports"
    ),
    "utcnow": (
        "datetime.utcnow() is naive; keep that behaviour with "
        "datetime.now(UTC).replace(tzinfo=None) (from datetime import UTC), and likewise "
        "datetime.fromtimestamp(ts, UTC).replace(tzinfo=None) for utcfromtimestamp(ts)"
    ),
    "collections-abc": "import the abstract base classes from collections.abc",
    "unittest-aliases": (
        "use assertEqual, assertNotEqual, assertTrue, assertRegex, assertNotRegex, "
        "assertRaisesRegex, assertAlmostEqual"
    ),
    "removed-modules": (
        "imp -> importlib; distutils -> setuptools, shutil or sysconfig; "
        "asyncore, asynchat, smtpd -> asyncio or a third-party library"
    ),
    "removed-functions": (
        "inspect.getargspec -> inspect.getfullargspec; asyncio.coroutine -> async def; "
        "time.clock -> time.perf_counter; base64.encodestring/decodestring -> "
        "encodebytes/decodebytes"
    ),
}
RULES = tuple(HINTS)

_TYPING_BUILTIN = {
    "List", "Dict", "Set", "FrozenSet", "Tuple", "Type", "DefaultDict", "Deque", "Text",
}  # fmt: skip
_TYPING_UNION = {"Optional", "Union"}
_COLLECTIONS_ABC = {
    "Mapping", "MutableMapping", "Sequence", "MutableSequence", "Iterable", "Iterator",
    "Callable", "Set", "MutableSet", "Hashable", "Sized", "Container", "Generator",
    "Awaitable", "Coroutine", "KeysView", "ItemsView", "ValuesView",
}  # fmt: skip
_UNITTEST_ALIASES = {
    "assertEquals", "assertNotEquals", "assert_", "assertRegexpMatches",
    "assertNotRegexpMatches", "assertRaisesRegexp", "assertAlmostEquals",
    "assertNotAlmostEquals", "failUnless", "failIf", "failUnlessEqual", "assertDictContainsSubset",
}  # fmt: skip
_REMOVED_MODULES = {"imp", "distutils", "asyncore", "asynchat", "smtpd"}
_REMOVED_FUNCTIONS = {
    "inspect": {"getargspec"},
    "asyncio": {"coroutine"},
    "time": {"clock"},
    "base64": {"encodestring", "decodestring"},
}


@dataclass(frozen=True)
class Site:
    """One place that needs migrating."""

    rule: str
    file: str
    line: int
    code: str  # the source line, stripped
    detail: str  # which names or calls triggered it


def scan(files: Mapping[str, str], rules: Iterable[str] | None = None) -> list[Site]:
    """Sites in every parsable ``.py`` file, one per (rule, line), sorted by file and line."""
    wanted = set(rules) if rules is not None else set(RULES)
    found: dict[tuple[str, str, int], Site] = {}
    for path, source in files.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            continue
        lines = source.splitlines()
        for rule, line, detail in _file_sites(tree):
            if rule not in wanted:
                continue
            code = lines[line - 1].strip()[:200] if 0 < line <= len(lines) else ""
            key = (rule, path, line)
            if key in found:
                found[key] = Site(rule, path, line, code, f"{found[key].detail}, {detail}")
            else:
                found[key] = Site(rule, path, line, code, detail)
    return sorted(found.values(), key=lambda s: (s.file, s.line, s.rule))


def _typing_rule(name: str) -> str | None:
    if name in _TYPING_BUILTIN:
        return "typing-generics"
    return "optional-union" if name in _TYPING_UNION else None


def _file_sites(tree: ast.Module) -> list[tuple[str, int, str]]:
    typing_names: dict[str, str] = {}  # local name -> name in typing
    typing_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            for alias in node.names:
                typing_names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing":
                    typing_modules.add(alias.asname or "typing")
    out: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        out.extend(_node_sites(node, typing_names, typing_modules))
    return out


def _node_sites(
    node: ast.AST, typing_names: dict[str, str], typing_modules: set[str]
) -> list[tuple[str, int, str]]:
    line = getattr(node, "lineno", 0)
    sites: list[tuple[str, int, str]] = []
    if isinstance(node, ast.ImportFrom):
        module = "" if node.level else (node.module or "")
        for alias in node.names:
            if module == "typing" and (rule := _typing_rule(alias.name)):
                sites.append((rule, line, alias.name))
            elif module == "collections" and alias.name in _COLLECTIONS_ABC:
                sites.append(("collections-abc", line, alias.name))
            elif alias.name in _REMOVED_FUNCTIONS.get(module, set()):
                sites.append(("removed-functions", line, f"{module}.{alias.name}"))
        if module.split(".")[0] in _REMOVED_MODULES:
            sites.append(("removed-modules", line, module))
    elif isinstance(node, ast.Import):
        sites.extend(
            ("removed-modules", line, alias.name)
            for alias in node.names
            if alias.name.split(".")[0] in _REMOVED_MODULES
        )
    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        original = typing_names.get(node.id)
        if original and (rule := _typing_rule(original)):
            sites.append((rule, line, original))
    elif isinstance(node, ast.Attribute):
        sites.extend(_attribute_sites(node, line, typing_modules))
    return sites


def _attribute_sites(
    node: ast.Attribute, line: int, typing_modules: set[str]
) -> list[tuple[str, int, str]]:
    owner = node.value.id if isinstance(node.value, ast.Name) else ""
    attr = node.attr
    sites: list[tuple[str, int, str]] = []
    if owner in typing_modules and (rule := _typing_rule(attr)):
        sites.append((rule, line, attr))
    if owner == "collections" and attr in _COLLECTIONS_ABC:
        sites.append(("collections-abc", line, f"collections.{attr}"))
    if attr in _UNITTEST_ALIASES:
        sites.append(("unittest-aliases", line, attr))
    if attr in _REMOVED_FUNCTIONS.get(owner, set()):
        sites.append(("removed-functions", line, f"{owner}.{attr}"))
    if attr in {"utcnow", "utcfromtimestamp"}:
        sites.append(("utcnow", line, attr))
    return sites
