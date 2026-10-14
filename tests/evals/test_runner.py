"""The eval harness: statuses, usage, concurrency, saved reports and suite wiring."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from agentforge.agents.echo import EchoAgent, EchoInput, EchoOutput
from agentforge.agents.refactor import RefactorAgent
from agentforge.core.registry import registry
from agentforge.evals import (
    CaseScore,
    CaseStatus,
    Check,
    Suite,
    UnknownCaseError,
    all_suites,
    run_suite,
)
from agentforge.llm import FakeProvider
from tests.agents.refactor_refs import REFERENCE_SOURCES

# --------------------------------------------------------------------------- a tiny suite


class EchoCase(BaseModel):
    name: str
    text: str
    expect: str


def echo_cases() -> list[EchoCase]:
    return [
        EchoCase(name="shout", text="hi", expect="hi"),  # echo does not shout: scores 0
        EchoCase(name="same", text="ok", expect="ok"),
        EchoCase(name="longer", text="abc", expect="abc"),
    ]


def echo_score(case: EchoCase, output: EchoOutput) -> CaseScore:
    return CaseScore(
        case=case.name,
        checks=[
            Check(name="text", passed=output.text == case.expect),
            Check(name="length", passed=output.length == len(case.expect)),
        ],
    )


async def echo_score_async(case: EchoCase, output: EchoOutput) -> CaseScore:
    return echo_score(case, output)


def echo_suite(score: Any = echo_score) -> Suite:
    return Suite(
        agent="echo",
        load_cases=echo_cases,
        make_input=lambda c: EchoInput(text=c.text, upper=c.name == "shout"),
        score=score,
    )


async def test_statuses_scores_and_summary() -> None:
    report = await run_suite(echo_suite(), EchoAgent(), provider="none", model="none")
    by_name = {r.case: r for r in report.results}
    assert by_name["same"].status is CaseStatus.PASSED
    assert by_name["same"].score == 1.0
    assert by_name["shout"].status is CaseStatus.FAILED  # "HI" != "hi"
    assert by_name["shout"].score == 0.5  # the length check still passes
    assert [c.name for c in by_name["shout"].failed_checks()] == ["text"]
    assert (report.total, report.passed) == (3, 2)
    assert report.pass_rate == pytest.approx(2 / 3)
    assert report.mean_score == pytest.approx((1 + 1 + 0.5) / 3)
    assert report.summary()["passed"] == 2
    assert report.provider == "none"


async def test_async_scorers_work_too() -> None:
    report = await run_suite(echo_suite(echo_score_async), EchoAgent())
    assert report.passed == 2


async def test_only_the_named_cases_run_and_unknown_names_are_refused() -> None:
    report = await run_suite(echo_suite(), EchoAgent(), cases=["same"])
    assert [r.case for r in report.results] == ["same"]
    with pytest.raises(UnknownCaseError, match=r"no case named nope for echo \(available: longer"):
        await run_suite(echo_suite(), EchoAgent(), cases=["nope"])


async def test_cases_can_run_concurrently() -> None:
    report = await run_suite(echo_suite(), EchoAgent(), concurrency=3)
    assert [r.case for r in report.results] == ["shout", "same", "longer"]  # order is kept
    assert report.passed == 2


async def test_a_broken_scorer_is_an_error_not_a_crash() -> None:
    def broken(case: EchoCase, output: EchoOutput) -> CaseScore:
        if case.name == "same":
            raise RuntimeError("bug in the scorer")
        return echo_score(case, output)

    report = await run_suite(echo_suite(broken), EchoAgent())
    by_name = {r.case: r for r in report.results}
    assert by_name["same"].status is CaseStatus.ERROR
    assert by_name["same"].error == "scoring failed: RuntimeError: bug in the scorer"
    assert by_name["longer"].status is CaseStatus.PASSED


async def test_an_agent_that_raises_is_an_error_not_a_crash() -> None:
    class Exploding(EchoAgent):
        async def arun(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("boom")

    report = await run_suite(echo_suite(), Exploding(), cases=["same"])
    assert report.results[0].status is CaseStatus.ERROR
    assert report.results[0].error == "ConnectionError: boom"


async def test_agent_failures_are_errors_with_the_reason() -> None:
    suite = Suite(
        agent="echo",
        load_cases=echo_cases,
        make_input=lambda c: EchoInput(text=c.text),
        score=echo_score,
    )

    class Failing(EchoAgent):
        async def execute(self, data: EchoInput, ctx: Any) -> EchoOutput:
            raise ValueError("nope")

    report = await run_suite(suite, Failing(), cases=["same"])
    assert report.results[0].status is CaseStatus.ERROR
    assert report.results[0].error == "execution: nope"


# --------------------------------------------------------------------------- a real suite


def refactor_answer(source: str) -> str:
    return json.dumps(
        {
            "summary": "Reference refactoring for the case.",
            "changes": ["reference"],
            "refactored_source": source,
        }
    )


async def test_usage_is_recorded_for_llm_agents() -> None:
    suite = all_suites()["refactor"]
    llm = FakeProvider(script=[refactor_answer(REFERENCE_SOURCES["deep_nesting"])])
    report = await run_suite(suite, RefactorAgent(llm), cases=["deep_nesting"])
    (result,) = report.results
    assert result.status is CaseStatus.PASSED
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.run_id is not None
    assert result.duration_ms > 0
    assert report.tokens == result.tokens


async def test_a_wrong_answer_that_the_agent_accepts_is_a_failed_case_with_named_checks() -> None:
    cheat = REFERENCE_SOURCES["deep_nesting"].replace(
        "            cost += 10", "            cost += 10\n    elif insured:\n        cost += 10"
    )
    suite = all_suites()["refactor"]
    llm = FakeProvider(script=[refactor_answer(cheat)])
    report = await run_suite(suite, RefactorAgent(llm), cases=["deep_nesting"])
    (result,) = report.results
    assert result.status is CaseStatus.FAILED
    assert [c.name for c in result.failed_checks()] == ["hidden tests pass"]


async def test_an_agent_that_never_gets_it_right_is_an_error() -> None:
    suite = all_suites()["refactor"]
    original = suite.load_cases()[0].input["files"]
    llm = FakeProvider(script=[refactor_answer("def (:\n")] * 3)
    report = await run_suite(suite, RefactorAgent(llm), cases=["deep_nesting"])
    (result,) = report.results
    assert result.status is CaseStatus.ERROR
    assert result.error is not None
    assert result.error.startswith("output_validation:")
    assert original  # the case itself loaded fine
    assert result.input_tokens > 0  # the tokens spent on the attempts still count


# --------------------------------------------------------------------------- reports


async def test_reports_are_saved_as_json(tmp_path: Path) -> None:
    report = await run_suite(echo_suite(), EchoAgent(), provider="fake", model="m")
    path = report.save(tmp_path / "evals")
    assert path.parent == tmp_path / "evals"
    assert path.name.endswith("-echo.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["summary"]["total"] == 3
    assert data["summary"]["pass_rate"] == 0.667
    assert data["provider"] == "fake"
    assert {r["case"] for r in data["results"]} == {"shout", "same", "longer"}
    assert data["results"][0]["checks"][0]["name"] == "text"


# --------------------------------------------------------------------------- wiring


def test_every_agent_with_cases_has_a_suite() -> None:
    registry.discover()
    suites = all_suites()
    assert sorted(suites) == [
        "api-design",
        "bug-fix",
        "code-review",
        "db-schema",
        "doc-gen",
        "performance",
        "refactor",
        "security-audit",
        "test-gen",
    ]
    for name, suite in suites.items():
        cases = suite.load_cases()
        assert len(cases) == 5
        agent_class = registry.get(name)
        for case in cases:
            assert isinstance(suite.make_input(case), agent_class.input_model)
