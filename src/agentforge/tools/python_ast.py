"""Facts about Python source from its syntax tree: definitions, docstrings, complexity.

Shared by the Documentation agent (which docstrings are missing, did anything
but docstrings change?) and the Refactoring agent (which functions are too
long, too complex or too deeply nested?). Everything here is static: the code
is parsed, never imported or run.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from agentforge.core.errors import AgentForgeError

type FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
type DefinitionNode = ast.Module | ast.ClassDef | FunctionNode
type Kind = Literal["module", "class", "function", "method"]

MODULE = "<module>"


class PythonSourceError(AgentForgeError):
    """The source is not valid Python."""


@dataclass(frozen=True)
class Definition:
    """A module, class, function or method, by qualified name (``Cart.add``)."""

    qualname: str
    kind: Kind
    node: DefinitionNode

    @property
    def public(self) -> bool:
        return self.qualname == MODULE or not any(
            part.startswith("_") for part in self.qualname.split(".")
        )

    @property
    def line(self) -> int:
        return 1 if isinstance(self.node, ast.Module) else self.node.lineno

    @property
    def docstring(self) -> str | None:
        return ast.get_docstring(self.node, clean=True)


def parse(source: str, filename: str = "<source>") -> ast.Module:
    try:
        return ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise PythonSourceError(f"{filename}: line {exc.lineno}: {exc.msg}") from exc


def definitions(tree: ast.Module) -> list[Definition]:
    """The module, then its classes, functions and methods in source order.

    Functions nested inside functions are skipped: they are implementation
    details, not API. So are ``@overload`` stubs and property setters/deleters:
    the implementation and the getter are what get documented. If a name is
    defined twice, the last definition wins, as it does at runtime.
    """
    found = [Definition(MODULE, "module", tree)]

    def visit(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qualname = f"{prefix}{node.name}"
                found.append(Definition(qualname, "class", node))
                visit(node.body, f"{qualname}.", in_class=True)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if _is_overload_or_accessor(node):
                    continue
                kind: Kind = "method" if in_class else "function"
                found.append(Definition(f"{prefix}{node.name}", kind, node))

    visit(tree.body, "", in_class=False)
    last = {d.qualname: i for i, d in enumerate(found)}
    return [d for i, d in enumerate(found) if last[d.qualname] == i]


def _is_overload_or_accessor(node: FunctionNode) -> bool:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "overload":
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr in {
            "overload",
            "setter",
            "deleter",
        }:
            return True
    return False


def has_docstring(node: DefinitionNode) -> bool:
    return ast.get_docstring(node, clean=False) is not None


@dataclass(frozen=True)
class DocCoverage:
    documented: int
    total: int
    missing: tuple[str, ...]

    @property
    def percent(self) -> float:
        return round(100.0 * self.documented / self.total, 1) if self.total else 100.0


def docstring_coverage(tree: ast.Module) -> DocCoverage:
    """How many public definitions (the module included) have a docstring."""
    public = [d for d in definitions(tree) if d.public]
    missing = tuple(d.qualname for d in public if not has_docstring(d.node))
    return DocCoverage(documented=len(public) - len(missing), total=len(public), missing=missing)


def without_docstrings(tree: ast.Module) -> str:
    """A dump of the tree with every docstring removed, for "only docs changed?" checks."""
    clone = ast.parse(ast.unparse(tree))
    for node in ast.walk(clone):
        documentable = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        if isinstance(node, documentable) and has_docstring(node):
            empty: list[ast.stmt] = [] if isinstance(node, ast.Module) else [ast.Pass()]
            node.body = node.body[1:] or empty
    return ast.dump(clone, include_attributes=False)


def same_code(before: str, after: str) -> bool:
    """True if ``after`` differs from ``before`` only in docstrings, comments and layout."""
    return without_docstrings(parse(before)) == without_docstrings(parse(after))


# --------------------------------------------------------------------------- function facts


def own_nodes(func: FunctionNode) -> Iterator[ast.AST]:
    """Nodes in a function's body, not descending into nested functions or classes."""
    stack: list[ast.AST] = list(reversed(func.body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def parameters(func: FunctionNode, *, is_method: bool) -> list[str]:
    """Parameter names, without ``self``/``cls`` for methods (unless it is a staticmethod)."""
    args = func.args
    names = [a.arg for a in (*args.posonlyargs, *args.args)]
    static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in func.decorator_list)
    if is_method and names and not static:
        names = names[1:]
    if args.vararg:
        names.append(args.vararg.arg)
    names += [a.arg for a in args.kwonlyargs]
    if args.kwarg:
        names.append(args.kwarg.arg)
    return names


def raised_exceptions(func: FunctionNode) -> list[str]:
    """Names of exception classes raised directly in the function (``raise X(...)``)."""
    names: list[str] = []
    for node in own_nodes(func):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if name and name[0].isupper() and name not in names:
            names.append(name)
    return names


def returns_value(func: FunctionNode) -> bool:
    return any(
        isinstance(n, ast.Return)
        and n.value is not None
        and not (isinstance(n.value, ast.Constant) and n.value.value is None)
        for n in own_nodes(func)
    )


def is_generator(func: FunctionNode) -> bool:
    return any(isinstance(n, ast.Yield | ast.YieldFrom) for n in own_nodes(func))


_BRANCHES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler,
    ast.comprehension, ast.match_case,
)  # fmt: skip


def cyclomatic_complexity(func: FunctionNode) -> int:
    """McCabe-style complexity: 1 + decision points.

    Decision points are ``if``/``elif``, conditional expressions, loops,
    comprehension clauses, ``except`` handlers, ``match`` cases, and each extra
    operand of ``and``/``or``. Nested functions are scored on their own.
    """
    score = 1
    for node in own_nodes(func):
        if isinstance(node, _BRANCHES):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
    return score


_NESTING = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)


def max_nesting(func: FunctionNode) -> int:
    """Deepest level of nested blocks (if/for/while/with/try/match) in the body."""

    def depth(nodes: list[ast.stmt], level: int) -> int:
        deepest = level
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            inner = level + 1 if isinstance(node, _NESTING) else level
            for field in ("body", "orelse", "finalbody"):
                deepest = max(deepest, depth(getattr(node, field, []) or [], inner))
            for handler in getattr(node, "handlers", []) or []:
                deepest = max(deepest, depth(handler.body, inner))
            for case in getattr(node, "cases", []) or []:
                deepest = max(deepest, depth(case.body, inner))
            if inner > deepest:
                deepest = inner
        return deepest

    return depth(func.body, 0)


def length(func: FunctionNode) -> int:
    """Lines from ``def`` to the last line of the body (decorators and docstring excluded)."""
    end = func.end_lineno or func.lineno
    start = func.lineno
    if has_docstring(func):
        doc = func.body[0]
        if len(func.body) > 1:
            return end - start + 1 - ((doc.end_lineno or doc.lineno) - doc.lineno + 1)
    return end - start + 1
