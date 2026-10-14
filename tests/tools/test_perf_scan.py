"""Static slow-pattern scan and the cProfile runner."""

import pytest

from agentforge.tools import perf_scan
from agentforge.tools.perf_scan import (
    Candidate,
    PerfScanError,
    attach_shares,
    profile,
    scan_python,
)


def kinds(source: str) -> list[tuple[str, int]]:
    return [(c.kind, c.line) for c in scan_python({"m.py": source})]


def test_list_membership_in_a_loop_but_not_a_set() -> None:
    slow = (
        "def f(rows):\n    seen = []\n    for r in rows:\n        if r in seen:\n            pass\n"
    )
    assert kinds(slow) == [("list_membership", 4)]
    fast = slow.replace("seen = []", "seen = set()")
    assert kinds(fast) == []


def test_membership_in_a_comprehension_counts_as_a_loop() -> None:
    source = "def f(a):\n    b = list(a)\n    return [x for x in a if x in b]\n"
    assert kinds(source) == [("list_membership", 3)]


def test_list_parameter_annotation_is_enough() -> None:
    source = (
        "def f(items: list[int], wanted):\n"
        "    for w in wanted:\n        if w in items:\n            pass\n"
    )
    assert kinds(source) == [("list_membership", 3)]


def test_string_building_in_a_loop() -> None:
    both = "def f(rows):\n    s = ''\n    for r in rows:\n        s += r\n        s = s + r\n"
    assert kinds(both) == [("string_concat", 4), ("string_concat", 5)]
    assert kinds("def f(n):\n    n = 0\n    for r in range(9):\n        n += r\n") == []


def test_io_in_a_loop() -> None:
    source = (
        "def f(db, ids):\n    for i in ids:\n        db.execute('x', i)\n"
        "        requests.get(i)\n        open(i)\n"
    )
    assert [k for k, _ in kinds(source)] == ["io_in_loop"] * 3


def test_front_of_list_sort_and_regex_in_a_loop() -> None:
    source = (
        "import re\n\ndef f(q, items):\n    while q:\n        q.pop(0)\n        q.insert(0, 1)\n"
        "        items.sort()\n        sorted(items)\n        re.compile('x')\n"
    )
    assert [k for k, _ in kinds(source)] == [
        "list_front_ops", "list_front_ops", "sort_in_loop", "sort_in_loop", "regex_in_loop",
    ]  # fmt: skip


def test_the_iterable_of_a_loop_is_evaluated_once_not_per_iteration() -> None:
    assert kinds("def f(xs):\n    for x in sorted(xs):\n        pass\n") == []


def test_nested_loops_ignore_small_constant_ranges() -> None:
    big = "def f(a, b):\n    for x in a:\n        for y in b:\n            pass\n"
    assert kinds(big) == [("nested_loop", 3)]
    small = "def f(a):\n    for x in a:\n        for y in range(3):\n            pass\n"
    assert kinds(small) == []
    assert (
        kinds("def f():\n    for x in (1, 2):\n        for y in [1, 2]:\n            pass\n") == []
    )


def test_complexity_is_reported_on_the_def_line() -> None:
    body = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(15))
    found = scan_python({"m.py": f"def f(x):\n{body}"})
    assert [(c.kind, c.line, c.function) for c in found] == [("complexity", 1, "f")]
    assert "16" in found[0].title


def test_module_level_code_methods_and_nested_functions() -> None:
    source = (
        "rows = []\nfor r in rows:\n    db.execute(r)\n\n"
        "class C:\n    def m(self, xs):\n        for x in xs:\n            db.fetchone()\n"
    )
    found = scan_python({"m.py": source})
    assert [(c.function, c.line) for c in found] == [("<module>", 3), ("C.m", 8)]


def test_non_python_and_broken_files_are_skipped() -> None:
    assert scan_python({"a.txt": "for x in y: db.execute()", "b.py": "def (:"}) == []


def test_attach_shares_marks_candidates_in_measured_functions() -> None:
    c = Candidate("io_in_loop", "m.py", 3, "f", "t", "code", "high")
    spot = perf_scan.Hotspot("m.py", 1, "f", 1, 0.5, 0.5, 0.8)
    (marked,) = attach_shares([c], [spot])
    assert marked.share == 0.8
    assert attach_shares([c], [])[0].share is None


SCRIPT = {
    "app.py": (
        "def slow(n):\n    t = 0\n    for i in range(n):\n        t += i * i\n    return t\n\n"
        "def fast():\n    return 1\n\nslow(400000)\nfast()\n"
    )
}


async def test_profile_finds_the_function_that_takes_the_time() -> None:
    spots = await profile(SCRIPT, "app.py")
    assert spots[0].function == "slow"
    assert spots[0].file == "app.py"
    assert spots[0].line == 1
    assert spots[0].share > 0.5
    assert all(s.function != "<module>" for s in spots)


async def test_profile_script_must_exist_and_stay_inside() -> None:
    with pytest.raises(PerfScanError, match="not among the files"):
        await profile(SCRIPT, "other.py")
    with pytest.raises(PerfScanError, match="outside the profile directory"):
        await profile({"../evil.py": "x = 1", "m.py": ""}, "m.py")


async def test_profile_reports_a_failing_script() -> None:
    with pytest.raises(PerfScanError, match="ZeroDivisionError"):
        await profile({"m.py": "1 / 0\n"}, "m.py")


async def test_profile_script_exit_is_not_a_failure() -> None:
    spots = await profile({"m.py": "import sys\nsys.exit(0)\n"}, "m.py")
    assert spots == []


async def test_profile_timeout() -> None:
    with pytest.raises(PerfScanError, match="longer than"):
        await profile({"m.py": "while True:\n    pass\n"}, "m.py", timeout_s=1)
