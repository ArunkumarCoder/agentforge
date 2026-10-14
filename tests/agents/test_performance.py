"""Performance agent: triage rules, evidence checks, measured hotspots and the final report.

FakeProvider plays the model; the static scan and cProfile really run over each case.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agentforge.agents.performance import (
    PerformanceAgent,
    PerformanceInput,
    PerformanceOutput,
)
from agentforge.agents.performance.schema import Kind
from agentforge.agents.performance.scoring import load_cases, score
from agentforge.core import ErrorKind
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools.perf_scan import KINDS
from tests.agents.perf_refs import evidence_for, reference_answer

CASES = {c.name: c for c in load_cases()}
REPORT = CASES["report_builder"]
PROFILED = CASES["profile_hotspot"]


async def run(llm: FakeProvider, **data: Any) -> PerformanceOutput:
    result = await PerformanceAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


def reply(answer: dict[str, Any]) -> str:
    return json.dumps(answer)


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("performance") is PerformanceAgent
    assert "performance" in PerformanceAgent.card().tags


def test_kinds_stay_in_sync_with_the_scanner() -> None:
    assert set(Kind.__value__.__args__) == set(KINDS)  # type: ignore[attr-defined]


def test_input_rules() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        PerformanceInput()
    with pytest.raises(ValueError, match="exactly one"):
        PerformanceInput(files={"a.py": "x"}, repo_path=".")


async def test_builds_a_prioritised_report() -> None:
    llm = FakeProvider(script=[reply(await reference_answer(REPORT))])
    output = await run(llm, **REPORT.input)
    assert output.candidates == 2
    assert not output.profiled
    assert output.bottleneck is None
    assert [f.kind for f in output.findings] == ["list_membership", "string_concat"]
    assert output.findings[0].impact.value == "medium"  # sorted: medium before low
    assert output.findings[0].function == "unique_customers"
    assert output.findings[0].evidence.startswith("if order[")
    assert score(REPORT, output).passed
    user = llm.requests[0].messages[1].content
    assert "P1: [list_membership]" in user
    assert "if order[" in user  # the code is shown with line numbers
    assert llm.requests[0].tools == []


async def test_all_golden_cases_score_full_marks_with_the_reference_answer() -> None:
    for case in CASES.values():
        output = await run(FakeProvider(script=[reply(await reference_answer(case))]), **case.input)
        card = score(case, output)
        assert card.passed, (case.name, [c for c in card.checks if not c.passed])


async def test_profile_makes_the_measured_function_the_bottleneck() -> None:
    llm = FakeProvider(script=[reply(await reference_answer(PROFILED))])
    output = await run(llm, **PROFILED.input)
    assert output.profiled
    assert output.bottleneck == "app/tools.py:checksum"
    assert output.hotspots[0].function == "checksum"
    top = output.findings[0]
    assert (top.id, top.source, top.kind) == ("H1", "profile", "hotspot")
    assert top.share is not None and top.share > 0.5
    assert [f.function for f in output.dismissed] == ["dedupe"]
    user = llm.requests[0].messages[1].content
    assert "Profile of `app/main.py`" in user
    assert "checksum (app/tools.py:1)" in user
    assert "H1: [hotspot]" in user


async def test_a_clean_file_still_asks_the_model() -> None:
    llm = FakeProvider(
        script=[reply({"summary": "Nothing slow found in this module.", "triage": []})]
    )
    output = await run(llm, files={"a.py": "def add(a, b):\n    return a + b\n"})
    assert output.findings == []
    assert output.candidates == 0
    assert "The tools found no candidates" in llm.requests[0].messages[1].content


# --------------------------------------------------------------------------- verification


async def corrected(answer: dict[str, Any], message: str, case: Any = REPORT) -> None:
    good = await reference_answer(case)
    llm = FakeProvider(script=[reply(answer), reply(good)])
    await run(llm, **case.input)
    assert message in llm.requests[1].messages[-1].content


async def test_a_missing_triage_entry_goes_back() -> None:
    good = await reference_answer(REPORT)
    await corrected({**good, "triage": good["triage"][:-1]}, "no triage entry for: P2")


async def test_unknown_and_duplicate_ids_go_back() -> None:
    good = await reference_answer(REPORT)
    bad = {**good, "triage": [*good["triage"], {**good["triage"][0], "finding_id": "P99"}]}
    await corrected(bad, "P99 is not a candidate id")
    bad = {**good, "triage": [*good["triage"], good["triage"][0]]}
    await corrected(bad, "P1 is triaged more than once")


async def test_confirmed_without_a_fix_goes_back() -> None:
    good = await reference_answer(REPORT)
    triage = [dict(t) for t in good["triage"]]
    triage[0]["fix"] = None
    await corrected({**good, "triage": triage}, "P1 is confirmed but has no fix")


async def test_dismissing_a_measured_hotspot_goes_back() -> None:
    good = await reference_answer(PROFILED)
    triage = [dict(t) for t in good["triage"]]
    hot = next(t for t in triage if t["finding_id"] == "H1")
    hot.update(verdict="false_positive", fix=None)
    await corrected({**good, "triage": triage}, "cannot be dismissed", case=PROFILED)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"file": "nope.py"}, "file not found"),
        ({"line": 400}, "the file has only"),
        ({"evidence": "total = sum(everything)"}, "evidence does not appear"),
        (
            {"kind": "list_membership", "line": 5, "evidence": "not in seen"},
            "already reported as P1",
        ),
    ],
    ids=["file", "line", "invented-evidence", "duplicate"],
)
async def test_bad_additional_findings_go_back(change: dict[str, Any], message: str) -> None:
    good = await reference_answer(REPORT)
    extra = {
        "file": "reports/build.py", "line": 5, "kind": "algorithm", "impact": "high",
        "title": "Quadratic de-duplication", "evidence": "not in seen", "fix": "use a set",
        "description": "Every item is compared with every earlier item.",
        **change,
    }  # fmt: skip
    await corrected({**good, "additional": [extra]}, message)


async def test_evidence_may_sit_nearby_and_ignore_whitespace() -> None:
    good = await reference_answer(REPORT)
    extra = {
        "file": "reports/build.py", "line": 3, "kind": "algorithm", "impact": "low",
        "title": "Result built twice", "evidence": 'if order["customer"]   not in seen:',
        "fix": "use a set", "description": "Both lists are grown in step; one would do.",
    }  # fmt: skip
    llm = FakeProvider(script=[reply({**good, "additional": [extra]})])
    output = await run(llm, **REPORT.input)
    assert any(f.source == "llm" for f in output.findings)
    assert len(llm.requests) == 1


async def test_gives_up_after_two_corrections() -> None:
    good = await reference_answer(REPORT)
    bad = reply({**good, "triage": []})
    result = await PerformanceAgent(FakeProvider(script=[bad] * 3)).arun(REPORT.input)
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "after 2 repair attempts" in result.error.message


# --------------------------------------------------------------------------- start conditions


async def test_profile_script_must_be_among_the_files() -> None:
    result = await PerformanceAgent(FakeProvider(script=[])).arun(
        {"files": {"a.py": "x = 1\n"}, "profile_script": "b.py"}
    )
    assert result.error is not None
    assert "not among the analysed files" in result.error.message


async def test_a_failing_profile_script_is_reported() -> None:
    result = await PerformanceAgent(FakeProvider(script=[])).arun(
        {"files": {"a.py": "1 / 0\n"}, "profile_script": "a.py"}
    )
    assert result.error is not None
    assert "ZeroDivisionError" in result.error.message


async def test_too_many_candidates_are_refused() -> None:
    code = "def f(db, ids):\n    for i in ids:\n" + "        db.execute(i)\n" * 100
    result = await PerformanceAgent(FakeProvider(script=[])).arun({"files": {"a.py": code}})
    assert result.error is not None
    assert "too many to triage" in result.error.message


async def test_repo_mode_reads_files_and_gives_the_model_tools(tmp_path: Path) -> None:
    for rel, content in REPORT.input["files"].items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("for a in b:\n    db.execute(a)\n", "utf-8")
    (tmp_path / "notes.txt").write_text("not python", encoding="utf-8")
    llm = FakeProvider(script=[reply(await reference_answer(REPORT))])
    output = await run(llm, repo_path=str(tmp_path))
    assert output.candidates == 2
    user = llm.requests[0].messages[1].content
    assert "Analyse 1 file(s)" in user
    assert "available through your tools" in user
    assert {t.name for t in llm.requests[0].tools} >= {"read_file"}


async def test_repo_mode_profiles_a_script_and_paths_narrow_the_scan(tmp_path: Path) -> None:
    candidates, _ = await evidence_for(PROFILED)
    assert "H1" in candidates
    for rel, content in PROFILED.input["files"].items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "z.py").write_text("x = 1\n", encoding="utf-8")
    llm = FakeProvider(script=[reply(await reference_answer(PROFILED))])
    output = await run(
        llm, repo_path=str(tmp_path), paths=["app/tools.py"], profile_script="app/main.py"
    )
    assert output.bottleneck == "app/tools.py:checksum"
    user = llm.requests[0].messages[1].content
    assert "Analyse 2 file(s)" in user  # tools.py plus the profile script
    assert "z.py" not in user


async def test_empty_repo_is_refused(tmp_path: Path) -> None:
    result = await PerformanceAgent(FakeProvider(script=[])).arun({"repo_path": str(tmp_path)})
    assert result.error is not None
    assert "no readable Python files" in result.error.message


async def test_instructions_reach_the_prompt() -> None:
    llm = FakeProvider(script=[reply(await reference_answer(REPORT))])
    await run(llm, **REPORT.input, instructions="focus on the nightly export")
    assert "focus on the nightly export" in llm.requests[0].messages[1].content
