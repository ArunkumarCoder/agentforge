"""Putting docstrings into source code, and checking them against the code they describe.

``apply_docstrings`` edits only docstring lines, so the code itself cannot
change (``same_code`` proves it afterwards). ``lint_docstring`` is a small,
deterministic rubric: every parameter is described, every exception the
function raises is named, and return values / yields have a section.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass

from agentforge.agents.doc_gen.schema import DocStyle
from agentforge.core.errors import AgentForgeError
from agentforge.tools.python_ast import (
    MODULE,
    Definition,
    definitions,
    has_docstring,
    is_generator,
    parameters,
    parse,
    raised_exceptions,
    returns_value,
)


class DocstringError(AgentForgeError):
    """A docstring cannot be placed (unknown name, bad text, one-line definition)."""


SECTIONS: dict[DocStyle, dict[str, tuple[str, ...]]] = {
    DocStyle.GOOGLE: {
        "params": ("Args:", "Arguments:"),
        "returns": ("Returns:",),
        "yields": ("Yields:",),
        "raises": ("Raises:",),
    },
    DocStyle.NUMPY: {
        "params": ("Parameters\n----",),
        "returns": ("Returns\n----",),
        "yields": ("Yields\n----",),
        "raises": ("Raises\n----",),
    },
    DocStyle.SPHINX: {
        "params": (":param ",),
        "returns": (":return", ":rtype"),
        "yields": (":yield",),
        "raises": (":raises ",),
    },
}


def render(text: str, indent: str) -> list[str]:
    """The docstring as source lines, indented, with ``r`` prefix if it has backslashes."""
    text = text.replace("\r\n", "\n").strip()
    if '"""' in text:
        raise DocstringError('docstrings must not contain """')
    first, _, rest = text.partition("\n")
    body = textwrap.dedent(rest).rstrip().split("\n") if rest.strip() else []
    prefix = "r" if "\\" in text else ""
    if not body and not first.endswith('"'):
        return [f'{indent}{prefix}"""{first.strip()}"""']
    lines = [f'{indent}{prefix}"""{first.strip()}']
    lines += [f"{indent}{line}" if line.strip() else "" for line in body]
    lines.append(f'{indent}"""')
    return lines


@dataclass(frozen=True)
class _Edit:
    start: int  # 0-based line index
    end: int  # exclusive; == start for an insertion
    lines: list[str]


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _first_line(node: ast.stmt) -> int:
    decorators: list[ast.expr] = getattr(node, "decorator_list", [])
    return min([node.lineno, *(d.lineno for d in decorators)])


def _edit_for(definition: Definition, text: str, lines: list[str], replace: bool) -> _Edit:
    node = definition.node
    if not node.body:  # an empty module (or only comments): add before the final newline
        at = len(lines) - 1 if lines[-1] == "" else len(lines)
        return _Edit(at, at, render(text, ""))
    first = node.body[0]
    if isinstance(node, ast.Module):
        indent = ""
    else:
        if not placeable(definition, lines):
            raise DocstringError(
                f"'{definition.qualname}' is a one-line definition; it cannot take a docstring"
            )
        indent = _indent_of(lines[first.lineno - 1])
    if replace:
        return _Edit(first.lineno - 1, first.end_lineno or first.lineno, render(text, indent))
    at = _first_line(first) - 1
    new = render(text, indent)
    if isinstance(node, ast.Module | ast.ClassDef) and lines[at].strip():
        new.append("")  # a blank line after module and class docstrings (PEP 257)
    return _Edit(at, at, new)


def placeable(definition: Definition, lines: list[str]) -> bool:
    """False when the body starts on the signature's line, e.g. ``def f(): return 1``.

    ``lines`` are the source lines; the body must begin its own line for a
    docstring to go above it.
    """
    node = definition.node
    if isinstance(node, ast.Module) or not node.body:
        return True
    first = node.body[0]
    line = lines[first.lineno - 1].encode("utf-8")
    return not line[: first.col_offset].decode("utf-8", "ignore").strip()


def source_lines(source: str) -> list[str]:
    return source.replace("\r\n", "\n").split("\n")


@dataclass(frozen=True)
class Applied:
    source: str
    written: tuple[str, ...]  # qualnames that got a new docstring
    kept: tuple[str, ...]  # qualnames skipped because they already had one


def apply_docstrings(
    source: str, docstrings: Mapping[str, str], *, rewrite_existing: bool = False
) -> Applied:
    """Insert (or replace) docstrings by qualified name. Only docstring lines change."""
    tree = parse(source)
    known = {d.qualname: d for d in definitions(tree)}
    unknown = sorted(set(docstrings) - set(known))
    if unknown:
        raise DocstringError(
            f"no such definition: {', '.join(unknown)}. Valid names: {', '.join(known)}"
        )
    lines = source_lines(source)
    edits: list[_Edit] = []
    written: list[str] = []
    kept: list[str] = []
    for name, text in docstrings.items():
        definition = known[name]
        exists = has_docstring(definition.node)
        if exists and not rewrite_existing:
            kept.append(name)
            continue
        edits.append(_edit_for(definition, text, lines, replace=exists))
        written.append(name)
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        lines[edit.start : edit.end] = edit.lines
    return Applied("\n".join(lines), tuple(written), tuple(kept))


def _mentions(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", text) is not None


def _is_property(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Properties are documented like attributes: no Returns section needed."""
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id in {"property", "cached_property"}:
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr in {"setter", "deleter"}:
            return True
    return False


def lint_docstring(definition: Definition, style: DocStyle) -> list[str]:
    """Problems with a definition's docstring, as short sentences naming the definition."""
    doc = definition.docstring
    name = definition.qualname
    if doc is None:
        return [f"{name}: missing docstring"]
    problems: list[str] = []
    summary = doc.strip().split("\n", 1)[0].strip()
    if not summary.endswith((".", "!", "?", ":")):
        problems.append(f"{name}: the first line should be a one-sentence summary ending in '.'")
    node = definition.node
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return problems

    sections = SECTIONS[style]
    params = parameters(node, is_method=definition.kind == "method")
    if params and not any(marker in doc for marker in sections["params"]):
        problems.append(f"{name}: add a '{sections['params'][0].strip()}' section")
    problems += [
        f"{name}: parameter '{p}' is not described" for p in params if not _mentions(doc, p)
    ]
    for exc in raised_exceptions(node):
        if exc != "AssertionError" and not _mentions(doc, exc):  # "unreachable" guards
            problems.append(f"{name}: say when it raises {exc} (a Raises section)")
    if is_generator(node):
        needed = "yields" if not any(m in doc for m in sections["yields"]) else None
    elif returns_value(node) and node.name != "__init__" and not _is_property(node):
        needed = "returns" if not any(m in doc for m in sections["returns"]) else None
    else:
        needed = None
    if needed == "yields":
        problems.append(f"{name}: it is a generator; add a Yields section")
    elif needed == "returns":
        problems.append(f"{name}: it returns a value; add a Returns section")
    return problems


def lint(source: str, names: list[str], style: DocStyle) -> list[str]:
    """Rubric problems for the given definitions (by qualified name) in ``source``."""
    by_name = {d.qualname: d for d in definitions(parse(source))}
    problems: list[str] = []
    for name in names:
        if name in by_name:
            problems += lint_docstring(by_name[name], style)
    return problems


def python_blocks(markdown: str) -> list[str]:
    """Code from ```python fenced blocks (``>>>`` session blocks are skipped)."""
    blocks = re.findall(r"```(?:python|py)\s*\n(.*?)```", markdown, flags=re.DOTALL)
    return [b for b in blocks if not b.lstrip().startswith(">>>")]


__all__ = [
    "MODULE",
    "Applied",
    "DocstringError",
    "apply_docstrings",
    "lint",
    "lint_docstring",
    "placeable",
    "python_blocks",
    "render",
    "source_lines",
]
