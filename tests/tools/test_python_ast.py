"""Static facts about Python source: definitions, docstring coverage, complexity."""

import ast

import pytest

from agentforge.tools.python_ast import (
    PythonSourceError,
    api_changes,
    cyclomatic_complexity,
    definitions,
    docstring_coverage,
    is_generator,
    length,
    max_nesting,
    parameters,
    parse,
    public_api,
    raised_exceptions,
    returns_value,
    same_code,
)

SOURCE = '''"""Module doc."""


class Cart:
    """A cart."""

    def add(self, item, *extra, qty=1, **options):
        if qty < 1 or not item:
            raise ValueError("bad")
        for thing in extra:
            try:
                pass
            except KeyError:
                raise errors.CartError("x") from None

        def helper():
            raise TypeError("nested functions are not counted")

        return qty

    @staticmethod
    def make(name):
        yield name

    def _private(self):
        return None


def build():
    pass


def _hidden():
    pass
'''

TREE = parse(SOURCE)
DEFS = {d.qualname: d for d in definitions(TREE)}


def _func(name: str) -> ast.FunctionDef:
    node = DEFS[name].node
    assert isinstance(node, ast.FunctionDef)
    return node


def test_definitions_kinds_and_visibility() -> None:
    assert [(d.qualname, d.kind, d.public) for d in definitions(TREE)] == [
        ("<module>", "module", True),
        ("Cart", "class", True),
        ("Cart.add", "method", True),
        ("Cart.make", "method", True),
        ("Cart._private", "method", False),
        ("build", "function", True),
        ("_hidden", "function", False),
    ]
    assert DEFS["Cart.add"].line == 7
    assert DEFS["<module>"].docstring == "Module doc."


def test_overloads_accessors_and_redefinitions_are_not_separate_definitions() -> None:
    source = (
        "import typing\n\n\n@typing.overload\ndef f(x: int) -> int: ...\n\n\n"
        "def f(x):\n    return x\n\n\nclass C:\n    @property\n    def v(self):\n"
        "        return 1\n\n    @v.setter\n    def v(self, value):\n        pass\n\n\n"
        "def g():\n    pass\n\n\ndef g():\n    return 2\n"
    )
    found = definitions(parse(source))
    assert [d.qualname for d in found] == ["<module>", "f", "C", "C.v", "g"]
    assert found[1].line == 8  # the implementation, not the overload
    assert found[3].line == 14  # the getter
    assert found[4].line == 26  # the last g


def test_docstring_coverage_counts_public_definitions() -> None:
    coverage = docstring_coverage(TREE)
    assert (coverage.documented, coverage.total) == (2, 5)
    assert coverage.missing == ("Cart.add", "Cart.make", "build")
    assert coverage.percent == 40.0
    assert docstring_coverage(parse("")).percent == 0.0


def test_function_facts() -> None:
    add = _func("Cart.add")
    assert parameters(add, is_method=True) == ["item", "extra", "qty", "options"]
    assert parameters(_func("Cart.make"), is_method=True) == ["name"]  # staticmethod
    assert raised_exceptions(add) == ["ValueError", "CartError"]
    assert returns_value(add)
    assert not returns_value(_func("Cart._private"))  # return None
    assert is_generator(_func("Cart.make"))
    assert not is_generator(add)


def test_complexity_nesting_and_length() -> None:
    add = _func("Cart.add")
    # 1 + if + "or" + for + except = 5 (the nested helper is not counted)
    assert cyclomatic_complexity(add) == 5
    assert max_nesting(add) == 2  # for -> try
    assert length(add) == 13
    assert cyclomatic_complexity(_func("build")) == 1
    assert max_nesting(_func("build")) == 0
    documented = parse('def f():\n    """Doc\n    more."""\n    return 1\n').body[0]
    assert isinstance(documented, ast.FunctionDef)
    assert length(documented) == 2  # the docstring lines are not counted


def test_same_code_ignores_docstrings_comments_and_layout() -> None:
    before = "def f(x):\n    return x+1\n"
    assert same_code(before, '"""Mod."""\n\n\ndef f(x):  # c\n    """Doc."""\n    return x + 1\n')
    assert not same_code(before, "def f(x):\n    return x + 2\n")
    assert not same_code(before, 'def f(x):\n    """Doc."""\n')  # body replaced by docstring


def test_syntax_errors_are_reported_with_the_line() -> None:
    with pytest.raises(PythonSourceError, match=r"mod\.py: line 1"):
        parse("def (:\n", "mod.py")


API = """
LIMIT = 3
_private = 1


class Cart(Base):
    def add(self, item, qty=1, *, note=None): ...
    def _hidden(self): ...


async def fetch(url, *args, **kw): ...
def _helper(): ...
"""


def test_public_api_describes_shapes_not_bodies() -> None:
    api = public_api(parse(API))
    assert api["LIMIT"] == "variable"
    assert api["Cart"] == "class(Base)"
    assert api["Cart.add"] == "(self, item, qty=..., *, note=...)"
    assert api["fetch"].startswith("async (url")
    assert not any(name.startswith("_") or "._" in name for name in api)


def test_api_changes_ignore_bodies_defaults_and_additions() -> None:
    before = public_api(parse("def f(a, b=1):\n    return a\n"))
    after = public_api(parse("def f(a, b=2):\n    return b\n\n\ndef g():\n    pass\n"))
    assert api_changes(before, after) == []


def test_api_changes_report_removals_and_new_shapes() -> None:
    before = public_api(parse("def f(a):\n    pass\n\n\ndef g():\n    pass\n"))
    after = public_api(parse("def f(a, b):\n    pass\n"))
    changes = api_changes(before, after)
    assert any(c.startswith("f changed") for c in changes)
    assert "g was removed" in changes
