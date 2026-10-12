"""Refactoring agent: verification against the tests, the public API and the smell count.

FakeProvider plays the model; the tests inside each case really run (pytest in a subprocess).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agentforge.agents.refactor import RefactorAgent, RefactorInput, RefactorOutput
from agentforge.agents.refactor.scoring import load_cases, score
from agentforge.core import ErrorKind
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools.pytest_runner import PytestRun
from tests.agents.refactor_refs import REFERENCE_SOURCES

CASES = {c.name: c for c in load_cases()}
NESTING = CASES["deep_nesting"]
NESTING_SOURCE: str = NESTING.input["files"]["shipping/rates.py"]
NESTING_FIX = REFERENCE_SOURCES["deep_nesting"]


def answer(source: str = NESTING_FIX, **overrides: Any) -> str:
    base = {
        "summary": "Flattened the nested ifs with guard clauses.",
        "changes": ["replaced nested ifs with early raises"],
        "refactored_source": source,
    }
    return json.dumps({**base, **overrides})


async def run(llm: FakeProvider, **data: Any) -> RefactorOutput:
    result = await RefactorAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("refactor") is RefactorAgent
    assert "refactoring" in RefactorAgent.card().tags


def test_input_rules() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        RefactorInput(target="a.py")
    with pytest.raises(ValueError, match="not in files"):
        RefactorInput(target="a.py", files={"b.py": "x = 1"})
    with pytest.raises(ValueError, match=r"\.py file"):
        RefactorInput(target="../a.py", repo_path=".")


async def test_refactors_and_proves_behaviour_is_unchanged() -> None:
    llm = FakeProvider(script=[answer()])
    output = await run(llm, **NESTING.input)
    assert output.behaviour_verified
    assert output.api_preserved
    assert [s.kind.value for s in output.smells_before] == ["deep_nesting"]
    assert output.smells_after == []
    assert output.tests_before is not None
    assert output.tests_before.passed
    assert output.tests_after is not None
    assert output.tests_after.passed
    assert output.diff.startswith("--- a/shipping/rates.py\n+++ b/shipping/rates.py\n")
    assert "+    if weight_kg <= 0:" in output.diff
    request = llm.requests[0]
    user = request.messages[1].content
    assert "shipping_cost: 5 levels of nesting (limit 3)" in user
    assert "### tests/test_rates.py" in user  # visible tests are shown
    assert "test_hidden" not in user
    assert request.tools == []


async def test_clean_code_skips_the_model() -> None:
    files = {
        "tidy.py": "def add(a, b):\n    return a + b\n",
        "tests/test_tidy.py": (
            "from tidy import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        ),
    }
    llm = FakeProvider(script=[])
    output = await run(llm, target="tidy.py", files=files)
    assert llm.requests == []
    assert output.refactored_source == files["tidy.py"]
    assert output.changes == []
    assert not output.behaviour_verified


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (answer("def shipping_cost(:\n"), "not valid Python"),
        (
            answer(NESTING_FIX.replace("(weight_kg,", "(weight,").replace("weight_kg", "weight")),
            "The public API changed",
        ),
        (answer(NESTING_FIX.replace("def shipping_cost", "def cost")), "shipping_cost was removed"),
        (answer(NESTING_SOURCE), "The source is unchanged"),
        (
            answer(NESTING_SOURCE.replace("cost *= 1.5", "cost = cost * 1.5")),
            "The code is not simpler: 1 smell(s) now, 1 before",
        ),
        (answer(NESTING_FIX.replace("cost *= 1.5", "cost *= 2")), "The tests fail"),
    ],
    ids=["syntax", "renamed-param", "removed", "unchanged", "not-simpler", "behaviour"],
)
async def test_bad_refactorings_go_back_with_the_reason(bad: str, message: str) -> None:
    llm = FakeProvider(script=[bad, answer()])
    output = await run(llm, **NESTING.input)
    assert output.behaviour_verified
    assert message in llm.requests[1].messages[-1].content


async def test_failing_tests_are_quoted_back() -> None:
    llm = FakeProvider(script=[answer(NESTING_FIX.replace("cost *= 1.5", "cost *= 2")), answer()])
    await run(llm, **NESTING.input)
    feedback = llm.requests[1].messages[-1].content
    assert "behaviour changed" in feedback
    assert "7.5" in feedback  # pytest's assertion message is quoted


async def test_gives_up_after_two_corrections() -> None:
    llm = FakeProvider(script=[answer(NESTING_SOURCE)] * 3)
    result = await RefactorAgent(llm).arun(NESTING.input)
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "after 2 repair attempts" in result.error.message


# --------------------------------------------------------------------------- start conditions


async def test_no_tests_means_no_refactoring_unless_allowed() -> None:
    files = {"shipping/rates.py": NESTING_SOURCE}
    result = await RefactorAgent(FakeProvider(script=[])).arun(
        {"target": "shipping/rates.py", "files": files}
    )
    assert result.error is not None
    assert "no tests found" in result.error.message
    assert "allow_untested" in result.error.message

    llm = FakeProvider(script=[answer()])
    output = await run(llm, target="shipping/rates.py", files=files, allow_untested=True)
    assert not output.behaviour_verified
    assert output.tests_after is None
    assert "no tests, so be especially careful" in llm.requests[0].messages[1].content


async def test_tests_that_already_fail_are_reported_not_ignored() -> None:
    files = dict(NESTING.input["files"])
    files["tests/test_rates.py"] += (
        "\n\ndef test_broken():\n    assert shipping_cost(1, 'NL') == 0\n"
    )
    result = await RefactorAgent(FakeProvider(script=[])).arun(
        {"target": "shipping/rates.py", "files": files}
    )
    assert result.error is not None
    assert "already fail on the original code" in result.error.message
    assert "test_broken" in result.error.message


# --------------------------------------------------------------------------- repo mode


def _write_repo(root: Path) -> None:
    files = {**NESTING.input["files"], "shipping/__init__.py": ""}
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")
    (root / "tests/test_unrelated.py").write_text("def test_x():\n    assert True\n", "utf-8")


def _snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


async def test_repo_mode_finds_the_tests_and_changes_nothing_on_disk(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    before = _snapshot(tmp_path)
    llm = FakeProvider(script=[answer()])
    output = await run(llm, target="shipping/rates.py", repo_path=str(tmp_path))
    assert output.behaviour_verified
    assert output.tests_after is not None
    assert output.tests_after.tests == 2  # test_rates.py only, not test_unrelated.py
    assert sorted(t.name for t in llm.requests[0].tools) == [
        "list_files",
        "read_file",
        "search_text",
    ]
    assert _snapshot(tmp_path) == before


async def test_repo_tests_run_against_the_new_source_not_the_repos_copy(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    data = RefactorInput(target="shipping/rates.py", repo_path=str(tmp_path))
    tests = RefactorAgent._test_files(data)
    assert sorted(tests) == ["tests/test_rates.py"]
    broken = NESTING_FIX.replace("cost *= 1.5", "cost *= 2")
    run_result: PytestRun = await RefactorAgent._run_tests(data, broken, tests)
    assert not run_result.passed  # the broken copy was imported, so the shadowing works
    assert (await RefactorAgent._run_tests(data, NESTING_FIX, tests)).passed


async def test_repo_mode_explicit_tests_and_missing_files(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    ok = await run(
        FakeProvider(script=[answer()]),
        target="shipping/rates.py",
        repo_path=str(tmp_path),
        tests=["tests/test_rates.py"],
    )
    assert ok.behaviour_verified
    for payload, message in (
        ({"target": "shipping/rates.py", "tests": ["tests/nope.py"]}, "test file 'tests/nope.py'"),
        ({"target": "nope.py"}, "not found"),
    ):
        result = await RefactorAgent(FakeProvider(script=[])).arun(
            {**payload, "repo_path": str(tmp_path)}
        )
        assert result.error is not None
        assert message in result.error.message


# --------------------------------------------------------------------------- golden cases


def test_five_golden_cases_load() -> None:
    assert sorted(CASES) == sorted(REFERENCE_SOURCES)
    for case in CASES.values():
        data = case.refactor_input()
        assert case.expect.hidden_tests
        assert any(p.startswith("tests/") for p in data.files)


@pytest.mark.parametrize("name", sorted(REFERENCE_SOURCES))
async def test_reference_refactorings_score_full_marks(name: str) -> None:
    output = RefactorOutput.model_validate_json(answer(REFERENCE_SOURCES[name]))
    card = await score(CASES[name], output)
    assert card.passed, [c for c in card.checks if not c.passed]


@pytest.mark.parametrize("name", sorted(REFERENCE_SOURCES))
async def test_the_original_code_fails_its_own_case(name: str) -> None:
    original = CASES[name].input["files"][CASES[name].refactor_input().target]
    card = await score(CASES[name], RefactorOutput.model_validate_json(answer(original)))
    failed = {c.name for c in card.checks if not c.passed}
    assert "fewer smells, none of a new kind" in failed
    assert any(n.startswith("fixes ") for n in failed)
    assert "tests pass" not in failed  # the original is correct, just smelly


async def test_hidden_tests_catch_a_refactoring_that_only_passes_the_visible_ones() -> None:
    cheat = NESTING_FIX.replace(
        "            cost += 10", "            cost += 10\n    elif insured:\n        cost += 10"
    )
    card = await score(NESTING, RefactorOutput.model_validate_json(answer(cheat)))
    failed = [c.name for c in card.checks if not c.passed]
    assert failed == ["hidden tests pass"]


async def test_scoring_reports_syntax_errors_and_api_changes() -> None:
    bad = await score(NESTING, RefactorOutput.model_validate_json(answer("def (:\n")))
    assert [c.name for c in bad.checks] == ["valid Python"]
    renamed = NESTING_FIX.replace("def shipping_cost", "def cost")
    card = await score(NESTING, RefactorOutput.model_validate_json(answer(renamed)))
    api = next(c for c in card.checks if c.name == "public API kept")
    assert not api.passed
    assert "shipping_cost was removed" in api.detail
