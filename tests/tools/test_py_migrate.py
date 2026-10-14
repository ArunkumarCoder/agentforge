"""The migration scanner: which old spellings it finds, and which modern code it leaves alone."""

import pytest

from agentforge.tools.py_migrate import HINTS, RULES, scan


def found(source: str, rules: list[str] | None = None) -> list[tuple[str, int]]:
    return [(s.rule, s.line) for s in scan({"m.py": source}, rules)]


def test_every_rule_has_a_hint() -> None:
    assert set(HINTS) == set(RULES)


def test_typing_generics_imports_and_uses() -> None:
    source = "from typing import List, Dict\n\n\ndef f(a: List[int]) -> Dict[str, int]:\n    pass\n"
    assert found(source) == [("typing-generics", 1), ("typing-generics", 4)]
    assert scan({"m.py": source})[0].detail == "List, Dict"


def test_typing_module_attributes_and_aliases() -> None:
    source = "import typing as t\n\n\ndef f(a: t.Optional[int], b: t.Tuple[int]):\n    pass\n"
    assert found(source) == [("optional-union", 4), ("typing-generics", 4)]


def test_optional_and_union_are_their_own_rule() -> None:
    source = "from typing import Optional, Union\nx: Optional[int] = None\ny: Union[int, str] = 1\n"
    assert found(source) == [("optional-union", 1), ("optional-union", 2), ("optional-union", 3)]
    assert found(source, ["typing-generics"]) == []


def test_modern_annotations_and_other_typing_names_are_fine() -> None:
    source = (
        "from typing import Any, Callable, Protocol\n\n\n"
        "def f(a: list[int], b: int | None, c: Any): ...\n"
    )
    assert found(source) == []


def test_utcnow_and_utcfromtimestamp() -> None:
    source = (
        "from datetime import datetime\na = datetime.utcnow()\n"
        "b = datetime.utcfromtimestamp(0)\nc = datetime.now()\n"
    )
    assert found(source) == [("utcnow", 2), ("utcnow", 3)]


def test_collections_abcs_but_not_collections_abc() -> None:
    assert found("from collections import Mapping, OrderedDict\n") == [("collections-abc", 1)]
    assert found("import collections\nx = collections.Iterable\n") == [("collections-abc", 2)]
    assert found("from collections.abc import Mapping\nfrom collections import OrderedDict\n") == []


def test_unittest_aliases() -> None:
    source = (
        "class T:\n    def t(self):\n"
        "        self.assertEquals(1, 1)\n        self.assertEqual(1, 1)\n"
    )
    assert found(source) == [("unittest-aliases", 3)]


def test_removed_modules_and_functions() -> None:
    source = (
        "import imp\nimport distutils.core\nfrom asyncore import dispatcher\n"
        "import inspect, base64, time\nfrom inspect import getargspec\n"
        "a = inspect.getargspec(f)\nb = base64.encodestring(b'')\nc = time.clock()\n"
    )
    assert found(source) == [
        ("removed-modules", 1),
        ("removed-modules", 2),
        ("removed-modules", 3),
        ("removed-functions", 5),
        ("removed-functions", 6),
        ("removed-functions", 7),
        ("removed-functions", 8),
    ]


def test_relative_imports_are_not_stdlib() -> None:
    assert found("from . import imp\nfrom .typing import List\n") == []


def test_rule_filter_and_ignored_files() -> None:
    source = "import imp\nfrom typing import List\n"
    assert found(source, ["removed-modules"]) == [("removed-modules", 1)]
    assert scan({"a.txt": source, "b.py": "def (:"}) == []


def test_code_is_the_stripped_source_line() -> None:
    (site,) = scan({"m.py": "if True:\n    import imp\n"})
    assert (site.file, site.line, site.code) == ("m.py", 2, "import imp")


@pytest.mark.parametrize("rule", RULES)
def test_each_rule_can_be_selected(rule: str) -> None:
    assert scan({"m.py": "x = 1\n"}, [rule]) == []
