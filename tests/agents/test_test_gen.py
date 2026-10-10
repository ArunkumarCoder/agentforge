"""Test Generation agent: running generated tests, retrying on failure, repo mode, scoring.

FakeProvider plays the model; the generated tests really run (pytest in a
subprocess), so these tests take a few seconds.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agentforge.agents.test_gen import GenerateTestsInput, GenerateTestsOutput, TestGenAgent
from agentforge.agents.test_gen.schema import import_name
from agentforge.agents.test_gen.scoring import load_cases, score
from agentforge.core import ErrorKind
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools import pytest_runner as runner_module
from tests.agents.testgen_refs import REFERENCE_TESTS

CASES = {c.name: c for c in load_cases()}
LRU = CASES["lru_cache"]
GOOD = REFERENCE_TESTS["lru_cache"]
WRONG = GOOD.replace('assert cache.get("a") == 10', 'assert cache.get("a") == 11')


def answer(code: str = GOOD, **overrides: Any) -> str:
    base = {
        "test_path": "tests/test_lru.py",
        "test_code": code,
        "summary": "Covers get/put, recency, eviction and the capacity check.",
        "cases": ["get with default", "evicts least recently used", "capacity must be >= 1"],
    }
    return json.dumps({**base, **overrides})


async def run(llm: FakeProvider, **data: Any) -> GenerateTestsOutput:
    result = await TestGenAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


# --------------------------------------------------------------------------- schema


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("shop/cart.py", "shop.cart"),
        ("src/shop/cart.py", "shop.cart"),
        ("shop/__init__.py", "shop"),
        ("calc.py", "calc"),
    ],
)
def test_import_name(path: str, name: str) -> None:
    assert import_name(path) == name


def test_input_rules() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        GenerateTestsInput(target="a.py")
    with pytest.raises(ValueError, match="not in files"):
        GenerateTestsInput(target="a.py", files={"b.py": "x = 1"})
    with pytest.raises(ValueError, match=r"\.py file"):
        GenerateTestsInput(target="../a.py", repo_path=".")
    assert GenerateTestsInput(target=".\\pkg\\a.py", repo_path=".").target == "pkg/a.py"


@pytest.mark.parametrize(
    "path", ["lru.py", "tests/lru_test.txt", "../tests/test_x.py", "/abs/test_x.py"]
)
def test_output_test_path_must_be_a_new_test_file(path: str) -> None:
    with pytest.raises(ValueError, match="test_path"):
        GenerateTestsOutput.model_validate_json(answer(test_path=path))


# --------------------------------------------------------------------------- agent


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("test-gen") is TestGenAgent
    assert "testing" in TestGenAgent.card().tags


async def test_passing_tests_on_the_first_try() -> None:
    llm = FakeProvider(script=[answer()])
    output = await run(llm, **LRU.input)
    assert output.run is not None
    assert output.run.passed
    assert output.run.tests == 4
    assert output.run.coverage == 100.0
    assert output.coverage_before == 0.0
    assert output.attempts == 1
    request = llm.requests[0]
    assert "import it as `cachekit.lru`" in request.messages[1].content
    assert "### cachekit/lru.py" in request.messages[1].content
    assert '"test_code"' in request.messages[0].content  # output schema in the prompt
    assert request.tools == []


async def test_failing_tests_go_back_with_pytest_output() -> None:
    llm = FakeProvider(script=[answer(WRONG), answer()])
    output = await run(llm, **LRU.input)
    assert output.attempts == 2
    assert output.run is not None
    assert output.run.passed
    feedback = llm.requests[1].messages[-1].content
    assert "1 of 4 tests failed or errored" in feedback
    assert "assert 10 == 11" in feedback
    assert "Fix the tests, not the module" in feedback


async def test_gives_up_after_max_attempts() -> None:
    llm = FakeProvider(script=[answer(WRONG)] * 3)
    result = await TestGenAgent(llm).arun({**LRU.input, "max_attempts": 3})
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "after 2 repair attempts" in result.error.message
    assert len(llm.requests) == 3


async def test_one_attempt_means_no_retry() -> None:
    llm = FakeProvider(script=[answer(WRONG)])
    result = await TestGenAgent(llm).arun({**LRU.input, "max_attempts": 1})
    assert result.error is not None
    assert "after 0 repair attempts" in result.error.message
    assert len(llm.requests) == 1


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (
            answer("from cachekit.lru import LRUCache\n\nCACHE = LRUCache(1)\n"),
            "collected no tests",
        ),
        (answer("import cachekit.nope\n\n\ndef test_x() -> None:\n    pass\n"), "No module named"),
        (answer(test_path="tests/test_inventory_add.py"), "already exists"),
    ],
    ids=["no-tests", "import-error", "path-exists"],
)
async def test_unusable_tests_are_explained(bad: str, message: str) -> None:
    files = {
        **LRU.input["files"],
        "tests/test_inventory_add.py": "def test_x() -> None:\n    pass\n",
    }
    llm = FakeProvider(script=[bad, answer()])
    await run(llm, target=LRU.input["target"], files=files)
    assert message in llm.requests[1].messages[-1].content


async def test_coverage_before_comes_from_existing_tests() -> None:
    case = CASES["inventory"]
    llm = FakeProvider(
        script=[answer(REFERENCE_TESTS["inventory"], test_path="tests/test_inventory.py")]
    )
    output = await run(llm, **case.input)
    assert output.coverage_before == 50.0
    assert output.run is not None
    assert output.run.coverage == 100.0
    assert "Existing tests" in llm.requests[0].messages[1].content


def _snapshot(root: Path) -> list[Path]:
    return sorted(p.relative_to(root) for p in root.rglob("*"))


def _write_repo(root: Path) -> None:
    for rel, content in LRU.input["files"].items():
        (root / "src" / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / "src" / rel).write_text(content, encoding="utf-8")


async def test_repo_mode_runs_against_the_repo_without_writing_to_it(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    before = _snapshot(tmp_path)
    llm = FakeProvider(script=[answer()])
    output = await run(llm, target="src/cachekit/lru.py", repo_path=str(tmp_path))
    assert output.run is not None
    assert output.run.passed
    assert output.run.coverage == 100.0
    assert output.coverage_before is None  # existing repo tests are not run
    request = llm.requests[0]
    assert "class LRUCache" in request.messages[1].content  # target is inlined
    assert sorted(t.name for t in request.tools) == ["list_files", "read_file", "search_text"]
    assert _snapshot(tmp_path) == before


async def test_repo_mode_missing_target(tmp_path: Path) -> None:
    result = await TestGenAgent(FakeProvider(script=[])).arun(
        {"target": "nope.py", "repo_path": str(tmp_path)}
    )
    assert result.error is not None
    assert result.error.kind is ErrorKind.EXECUTION
    assert "not found" in result.error.message


async def test_needs_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "pytest_available", lambda: False)
    result = await TestGenAgent(FakeProvider(script=[])).arun(LRU.input)
    assert result.error is not None
    assert "pytest is not installed" in result.error.message


# --------------------------------------------------------------------------- golden cases


def test_five_golden_cases_load() -> None:
    assert sorted(CASES) == sorted(REFERENCE_TESTS)
    for case in CASES.values():
        assert case.gen_input().target in case.input["files"]
        assert len(case.expect.mutants) >= 3


@pytest.mark.parametrize("name", sorted(REFERENCE_TESTS))
async def test_reference_tests_score_full_marks(name: str) -> None:
    output = GenerateTestsOutput.model_validate_json(
        answer(REFERENCE_TESTS[name], test_path="tests/test_reference.py")
    )
    card = await score(CASES[name], output)
    assert card.passed, [c for c in card.checks if not c.passed]
    assert card.score == 1.0


async def test_tests_without_assertions_do_not_catch_mutants() -> None:
    smoke = (
        "from cachekit.lru import LRUCache\n\n\n"
        "def test_smoke() -> None:\n"
        "    cache = LRUCache(1)\n    cache.put('a', 1)\n    cache.put('b', 2)\n"
        "    cache.get('a')\n    cache.get('b')\n    len(cache)\n"
    )
    card = await score(LRU, GenerateTestsOutput.model_validate_json(answer(smoke)))
    failed = [c.name for c in card.checks if not c.passed]
    assert "tests pass" not in failed
    assert failed[0].startswith("coverage")  # the capacity check is never run
    assert [f for f in failed if f.startswith("catches")] == [
        "catches: get does not refresh recency",
        "catches: evicts the newest item",
        "catches: holds one item too many",
    ]
