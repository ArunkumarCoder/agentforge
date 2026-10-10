"""Refactoring groundwork: function metrics and code smells."""

from agentforge.agents.refactor import SmellKind, Thresholds, find_smells, function_metrics

TANGLED = """
def report(orders, customers, products, start, end, currency, verbose=False):
    total = 0
    for order in orders:
        if order.ok:
            for line in order.lines:
                if line.qty > 0:
                    if line.price and not line.void:
                        total += line.qty * line.price
    print("summary")
    count = len(orders)
    average = total / count
    print(average)
    return total


class Shop:
    def audit(self, orders):
        print("summary")
        count = len(orders)
        average = total / count
        print(average)
        return count
"""

CLEAN = '''
def add(a, b):
    """Add."""
    return a + b
'''


def test_function_metrics() -> None:
    report, audit = function_metrics(TANGLED)
    assert report.model_dump() == {
        "name": "report",
        "line": 2,
        "lines": 13,
        "complexity": 7,  # 1 + for + if + for + if + if + "and"
        "parameters": 7,
        "nesting": 5,
    }
    assert (audit.name, audit.parameters, audit.complexity) == ("Shop.audit", 1, 1)


def test_finds_each_kind_of_smell() -> None:
    smells = find_smells(TANGLED, Thresholds(max_lines=10, max_complexity=6))
    assert [(s.kind, s.name) for s in smells] == [
        (SmellKind.COMPLEX_FUNCTION, "report"),
        (SmellKind.DEEP_NESTING, "report"),
        (SmellKind.LONG_FUNCTION, "report"),
        (SmellKind.TOO_MANY_PARAMETERS, "report"),
        (SmellKind.DUPLICATE_CODE, "Shop.audit"),
    ]
    assert smells[1].message == "report: 5 levels of nesting (limit 3)"
    assert smells[-1].message == "Shop.audit: lines 19-22 repeat lines 10-13 of report"


def test_default_thresholds_and_clean_code() -> None:
    kinds = {s.kind for s in find_smells(TANGLED)}
    assert kinds == {
        SmellKind.DEEP_NESTING,
        SmellKind.TOO_MANY_PARAMETERS,
        SmellKind.DUPLICATE_CODE,
    }
    assert find_smells(CLEAN) == []


def test_a_longer_copy_is_reported_once() -> None:
    body = "".join(f"    x{i} = {i}\n" for i in range(8))
    source = f"def a():\n{body}\n\ndef b():\n{body}"
    smells = find_smells(source)
    assert [s.kind for s in smells] == [SmellKind.DUPLICATE_CODE]
    assert smells[0].name == "b"
    assert smells[0].value == 8
    assert smells[0].message == "b: lines 13-20 repeat lines 2-9 of a"


def test_repeats_inside_one_function_count_but_overlaps_do_not() -> None:
    block = "    a = 1\n    b = 2\n    c = 3\n    d = 4\n"
    assert len(find_smells(f"def f():\n{block}{block}")) == 1
    same = "".join("    x = 1\n" for _ in range(5))  # windows overlap: not a copy
    assert find_smells(f"def g():\n{same}") == []
