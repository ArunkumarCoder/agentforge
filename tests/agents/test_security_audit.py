"""Security Audit agent: triage rules, evidence checks and the final report.

FakeProvider plays the model; bandit and the secrets scanner really run over each case.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agentforge.agents.security_audit import (
    SecurityAuditAgent,
    SecurityAuditInput,
    SecurityAuditOutput,
)
from agentforge.agents.security_audit.schema import Category
from agentforge.agents.security_audit.scoring import load_cases, score
from agentforge.core import ErrorKind
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools.security_scan import CATEGORIES
from tests.agents.secaudit_refs import reference_answer, scanner_findings

CASES = {c.name: c for c in load_cases()}
WEB = CASES["web_app"]


async def run(llm: FakeProvider, **data: Any) -> SecurityAuditOutput:
    result = await SecurityAuditAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


def reply(answer: dict[str, Any]) -> str:
    return json.dumps(answer)


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("security-audit") is SecurityAuditAgent
    assert "security" in SecurityAuditAgent.card().tags


def test_categories_stay_in_sync_with_the_scanner() -> None:
    assert set(Category.__value__.__args__) == set(CATEGORIES)  # type: ignore[attr-defined]


def test_input_rules() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        SecurityAuditInput()
    with pytest.raises(ValueError, match="exactly one"):
        SecurityAuditInput(files={"a.py": "x"}, repo_path=".")


async def test_builds_a_prioritised_report() -> None:
    llm = FakeProvider(script=[reply(await reference_answer(WEB))])
    output = await run(llm, **WEB.input)
    assert output.scanner_findings == 3
    assert output.risk == "high"
    severities = [f.severity.value for f in output.findings]
    assert severities == sorted(severities, key=["critical", "high", "medium", "low", "info"].index)
    by_line = {f.line: f for f in output.findings}
    assert by_line[15].category == "sql_injection"
    assert by_line[15].source == "scanner"
    assert by_line[29].category == "path_traversal"
    assert by_line[29].source == "llm"
    assert by_line[29].evidence.startswith("return send_file(")
    assert output.dismissed == []
    assert score(WEB, output).passed
    user = llm.requests[0].messages[1].content
    assert "S1:" in user
    assert "   7  SESSION_SECRET" in user  # code is shown with line numbers
    assert llm.requests[0].tools == []


async def test_all_golden_cases_score_full_marks_with_the_reference_answer() -> None:
    for case in CASES.values():
        output = await run(FakeProvider(script=[reply(await reference_answer(case))]), **case.input)
        card = score(case, output)
        assert card.passed, (case.name, [c for c in card.checks if not c.passed])


async def test_false_positives_are_set_aside() -> None:
    case = CASES["cli_tool"]
    output = await run(FakeProvider(script=[reply(await reference_answer(case))]), **case.input)
    scanner_ids = {f.id for f in [*output.findings, *output.dismissed] if f.source == "scanner"}
    assert len(scanner_ids) == output.scanner_findings
    assert output.dismissed  # the decoys
    assert all(f.status.value == "false_positive" for f in output.dismissed)
    assert not {f.id for f in output.dismissed} & {f.id for f in output.findings}


async def test_nothing_found_by_scanners_still_asks_the_model() -> None:
    llm = FakeProvider(
        script=[reply({"summary": "Nothing found by the scanners or by review.", "triage": []})]
    )
    output = await run(llm, files={"a.py": "def add(a, b):\n    return a + b\n"})
    assert output.risk == "none"
    assert output.scanner_findings == 0
    assert "The scanners found nothing" in llm.requests[0].messages[1].content


async def test_min_severity_hides_low_scanner_findings() -> None:
    low = CASES["cli_tool"]
    loud = await run(FakeProvider(script=[reply(await reference_answer(low))]), **low.input)
    quiet_input = {**low.input, "min_severity": "high"}
    quiet_case = low.model_copy(update={"input": quiet_input})
    quiet = await run(
        FakeProvider(script=[reply(await reference_answer(quiet_case))]), **quiet_input
    )
    assert quiet.scanner_findings < loud.scanner_findings


# --------------------------------------------------------------------------- verification


async def corrected(answer: dict[str, Any], message: str, case: Any = WEB) -> None:
    good = await reference_answer(case)
    llm = FakeProvider(script=[reply(answer), reply(good)])
    await run(llm, **case.input)
    assert message in llm.requests[1].messages[-1].content


async def test_a_missing_triage_entry_goes_back() -> None:
    good = await reference_answer(WEB)
    bad = {**good, "triage": good["triage"][:-1]}
    await corrected(bad, "no triage entry for: S3")


async def test_unknown_and_duplicate_ids_go_back() -> None:
    good = await reference_answer(WEB)
    bad = {**good, "triage": [*good["triage"], {**good["triage"][0], "finding_id": "S99"}]}
    await corrected(bad, "S99 is not a scanner finding id")
    bad = {**good, "triage": [*good["triage"], good["triage"][0]]}
    await corrected(bad, "S1 is triaged more than once")


async def test_confirmed_without_a_fix_goes_back() -> None:
    good = await reference_answer(WEB)
    triage = [dict(t) for t in good["triage"]]
    confirmed = next(t for t in triage if t["verdict"] == "confirmed")
    confirmed["fix"] = None
    await corrected(
        {**good, "triage": triage}, f"{confirmed['finding_id']} is confirmed but has no fix"
    )


async def test_dismissing_a_high_confidence_high_severity_finding_goes_back() -> None:
    good = await reference_answer(WEB)
    # B201 (flask debug=True) is rated high/medium by bandit; B608 is medium. Use the
    # scanner's own rating: find one that is high/high among the golden cases.
    for case in CASES.values():
        ref = await reference_answer(case)
        raw = await scanner_findings(case)
        idx = next(
            (i for i, f in enumerate(raw) if f.severity == "high" and f.confidence == "high"), None
        )
        if idx is None:
            continue
        triage = [dict(t) for t in ref["triage"]]
        triage[idx].update(verdict="false_positive", fix=None, severity="info")
        await corrected({**ref, "triage": triage}, "cannot be a false positive", case=case)
        return
    pytest.fail(f"no high/high scanner finding in any golden case ({good['summary']})")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"file": "nope.py"}, "file not found"),
        ({"line": 400}, "the file has only"),
        ({"evidence": "os.system(user_input)"}, "evidence does not appear"),
        (
            {"line": 33, "evidence": "app.run(debug=True)", "category": "misconfiguration"},
            "already reported by the scanner",
        ),
    ],
    ids=["file", "line", "invented-evidence", "duplicate"],
)
async def test_bad_additional_findings_go_back(change: dict[str, Any], message: str) -> None:
    good = await reference_answer(WEB)
    extra = {**good["additional"][0], **change}
    await corrected({**good, "additional": [extra]}, message)


async def test_evidence_may_sit_a_line_or_two_away_and_ignore_whitespace() -> None:
    good = await reference_answer(WEB)
    extra = {
        **good["additional"][0],
        "line": 28,
        "evidence": "return   send_file(os.path.join(UPLOAD_DIR,   filename))",
    }
    llm = FakeProvider(script=[reply({**good, "additional": [extra]})])
    output = await run(llm, **WEB.input)
    assert any(f.category == "path_traversal" for f in output.findings)
    assert len(llm.requests) == 1  # accepted first time


async def test_gives_up_after_two_corrections() -> None:
    good = await reference_answer(WEB)
    bad = reply({**good, "triage": []})
    result = await SecurityAuditAgent(FakeProvider(script=[bad] * 3)).arun(WEB.input)
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "after 2 repair attempts" in result.error.message


# --------------------------------------------------------------------------- start conditions


async def test_too_many_findings_are_refused() -> None:
    code = "".join(f'PASSWORD{i} = "k9$Tz!w3Qp8vLm2xN7bR4cY{i}"\n' for i in range(100))
    result = await SecurityAuditAgent(FakeProvider(script=[])).arun({"files": {"a.py": code}})
    assert result.error is not None
    assert "too many to triage" in result.error.message


async def test_repo_mode_reads_files_and_gives_the_model_tools(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    for rel, content in WEB.input["files"].items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("eval(input())\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00")
    llm = FakeProvider(script=[reply(await reference_answer(WEB))])
    output = await run(llm, repo_path=str(tmp_path))
    assert output.scanner_findings == 3
    user = llm.requests[0].messages[1].content
    assert "Audit 1 file(s)." in user  # node_modules, .env and the png were skipped
    assert "available through your tools" in user
    assert {t.name for t in llm.requests[0].tools} >= {"read_file"}


async def test_repo_mode_paths_narrow_the_audit(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "x.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b" / "y.py").write_text("y = 2\n", encoding="utf-8")
    llm = FakeProvider(script=[reply({"summary": "Nothing found in a/x.py.", "triage": []})])
    await run(llm, repo_path=str(tmp_path), paths=["a"])
    user = llm.requests[0].messages[1].content
    assert "## a/x.py" in user
    assert "y.py" not in user


async def test_empty_repo_is_refused(tmp_path: Path) -> None:
    result = await SecurityAuditAgent(FakeProvider(script=[])).arun({"repo_path": str(tmp_path)})
    assert result.error is not None
    assert "no readable source" in result.error.message


async def test_instructions_reach_the_prompt() -> None:
    llm = FakeProvider(script=[reply(await reference_answer(WEB))])
    await run(llm, **WEB.input, instructions="focus on the download flow")
    assert "focus on the download flow" in llm.requests[0].messages[1].content
