"""Code Review agent: diff parsing, post-processing rules, repo mode and golden-case scoring.

All offline: FakeProvider plays the model with scripted JSON reviews.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentforge.agents.code_review import (
    CodeReviewAgent,
    ReviewInput,
    ReviewOutput,
    Severity,
    Verdict,
)
from agentforge.agents.code_review.diff import parse_unified_diff
from agentforge.agents.code_review.scoring import load_cases, score
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider

CASES = {c.name: c for c in load_cases()}
SQL_DIFF = CASES["sql_injection"].input["diff"]


def finding(**overrides: Any) -> dict[str, Any]:
    base = {
        "file": "app/db.py",
        "line": 16,
        "severity": "critical",
        "category": "security",
        "title": "SQL injection in search_users",
        "explanation": (
            "User input is interpolated into the SQL string, so a crafted name can run "
            "arbitrary SQL."
        ),
        "suggestion": "Use a parameterised query: WHERE name LIKE ? with (f'%{name}%',)",
    }
    return {**base, **overrides}


def review(*findings: dict[str, Any], verdict: str = "request_changes") -> str:
    return json.dumps(
        {"summary": "Adds a user search function.", "verdict": verdict, "findings": list(findings)}
    )


async def run(llm: FakeProvider, **data: Any) -> ReviewOutput:
    result = await CodeReviewAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


# --------------------------------------------------------------------------- diff parser


def test_parse_unified_diff_tracks_new_line_numbers() -> None:
    files = parse_unified_diff(SQL_DIFF)
    assert list(files) == ["app/db.py"]
    changed = files["app/db.py"]
    assert changed.added_lines == {12, 13, 14, 15, 16, 17}
    assert changed.hunk_lines == set(range(9, 18))


def test_parse_handles_deleted_files_and_multiple_files() -> None:
    diff = (
        "--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x = 1\n-y = 2\n"
        "--- a/new.py\n+++ b/new.py\n@@ -1 +1,2 @@\n x = 1\n+y = 2\n\\ No newline at end of file\n"
    )
    files = parse_unified_diff(diff)
    assert list(files) == ["new.py"]
    assert files["new.py"].added_lines == {2}


# --------------------------------------------------------------------------- agent


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("code-review") is CodeReviewAgent
    assert "security" in CodeReviewAgent.card().tags


async def test_reviews_a_pasted_diff() -> None:
    llm = FakeProvider(script=[review(finding())])
    output = await run(llm, diff=SQL_DIFF)
    assert output.verdict is Verdict.REQUEST_CHANGES
    assert output.findings[0].severity is Severity.CRITICAL
    request = llm.requests[0]
    assert "```diff" in request.messages[1].content
    assert "Focus: all categories" in request.messages[1].content
    assert "code review" in request.messages[0].content
    assert '"findings"' in request.messages[0].content  # output schema in the prompt
    assert request.tools == []  # no tools for a pasted diff


async def test_findings_outside_the_diff_are_dropped_and_lines_checked() -> None:
    llm = FakeProvider(
        script=[
            review(
                finding(),
                finding(file="app/other.py", title="Made-up file"),
                finding(
                    file="b/app/db.py",
                    line=99,
                    severity="minor",
                    category="style",
                    title="Odd line",
                ),
            )
        ]
    )
    output = await run(llm, diff=SQL_DIFF)
    assert [f.file for f in output.findings] == ["app/db.py", "app/db.py"]
    assert output.findings[1].line is None  # line 99 is not in the diff


async def test_verdict_must_follow_from_findings() -> None:
    blocking = FakeProvider(script=[review(finding(severity="major"), verdict="approve")])
    assert (await run(blocking, diff=SQL_DIFF)).verdict is Verdict.REQUEST_CHANGES

    only_nits = FakeProvider(script=[review(finding(severity="nit", category="style"))])
    assert (await run(only_nits, diff=SQL_DIFF)).verdict is Verdict.COMMENT

    nothing = FakeProvider(script=[review()])
    assert (await run(nothing, diff=SQL_DIFF)).verdict is Verdict.APPROVE


async def test_sorted_by_severity_and_capped() -> None:
    llm = FakeProvider(
        script=[
            review(
                finding(severity="nit", category="style", title="Name could be clearer", line=14),
                finding(severity="critical"),
                finding(
                    severity="minor", category="maintainability", title="Missing type hint", line=14
                ),
            )
        ]
    )
    output = await run(llm, diff=SQL_DIFF, max_findings=2)
    assert [f.severity for f in output.findings] == [Severity.CRITICAL, Severity.MINOR]


async def test_focus_filters_non_blocking_findings_but_keeps_blockers() -> None:
    llm = FakeProvider(
        script=[review(finding(), finding(severity="nit", category="style", title="Style nit"))]
    )
    output = await run(llm, diff=SQL_DIFF, focus=["performance"])
    assert [f.category.value for f in output.findings] == ["security"]
    assert "Focus: performance" in llm.requests[0].messages[1].content


async def test_bad_json_is_repaired() -> None:
    llm = FakeProvider(script=["Looks risky to me!", review(finding())])
    output = await run(llm, diff=SQL_DIFF)
    assert len(output.findings) == 1
    assert len(llm.requests) == 2


def test_input_needs_exactly_one_source() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ReviewInput()
    with pytest.raises(ValueError, match="exactly one"):
        ReviewInput(diff="x", repo_path=".")


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed git commands in tests
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
    )


@needs_git
async def test_repo_mode_reads_the_diff_and_offers_read_tools(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "db.py").write_text("def find():\n    return 1\n", encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "app" / "db.py").write_text(
        "def find():\n    return 1\n\n\ndef search(q):\n    return f'SELECT {q}'\n",
        encoding="utf-8",
    )
    llm = FakeProvider(script=[review(finding(line=6))])
    output = await run(llm, repo_path=str(tmp_path))
    assert output.findings[0].file == "app/db.py"
    request = llm.requests[0]
    assert "+def search(q):" in request.messages[1].content
    assert sorted(t.name for t in request.tools) == ["read_file", "search_text"]


@needs_git
async def test_repo_without_changes_skips_the_model(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    llm = FakeProvider(script=[])
    output = await run(llm, repo_path=str(tmp_path))
    assert output.verdict is Verdict.APPROVE
    assert llm.requests == []


async def test_repo_mode_reports_git_errors(tmp_path: Path) -> None:
    result = await CodeReviewAgent(FakeProvider(script=[])).arun(
        {"repo_path": str(tmp_path), "base": "nope"}
    )
    assert not result.ok
    assert result.error is not None
    assert "git diff failed" in result.error.message


# --------------------------------------------------------------------------- golden cases


def test_five_golden_cases_load_with_valid_inputs() -> None:
    assert sorted(CASES) == [
        "clean_refactor",
        "hardcoded_secret",
        "missing_await",
        "off_by_one",
        "sql_injection",
    ]
    for case in CASES.values():
        assert case.review_input().diff


def test_scoring_a_good_review_passes() -> None:
    good = ReviewOutput.model_validate_json(review(finding()))
    result = score(CASES["sql_injection"], good)
    assert result.passed, result.checks
    assert result.score == 1.0


def test_scoring_catches_wrong_severity_category_or_line() -> None:
    case = CASES["sql_injection"]
    for bad in (
        finding(severity="minor"),
        finding(category="style"),
        finding(line=40),
        finding(title="Problem", explanation="Something is not quite right here.", suggestion=None),
    ):
        result = score(case, ReviewOutput.model_validate_json(review(bad)))
        assert not result.passed
        assert 0 < result.score < 1


def test_scoring_a_clean_change() -> None:
    case = CASES["clean_refactor"]
    approve = ReviewOutput.model_validate_json(review(verdict="approve"))
    assert score(case, approve).passed
    noisy = ReviewOutput.model_validate_json(
        review(*(finding(file="net/ports.py", severity="major", category="bug") for _ in range(4)))
    )
    failed = [c.name for c in score(case, noisy).checks if not c.passed]
    assert failed == ["verdict", "nothing worse than minor", "at most 3 findings"]


async def test_fake_model_end_to_end_on_a_golden_case() -> None:
    case = CASES["hardcoded_secret"]
    llm = FakeProvider(
        script=[
            review(
                finding(
                    file="billing/payments.py",
                    line=9,
                    title="Hardcoded live API key",
                    explanation=(
                        "A live payments credential is committed to source control and "
                        "leaks to anyone with repo access."
                    ),
                )
            )
        ]
    )
    output = await run(llm, **case.input)
    assert score(case, output).passed
