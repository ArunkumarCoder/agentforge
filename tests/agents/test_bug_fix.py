"""Bug Fix agent: prompt building, patch verification with one repair, repo mode, golden cases.

All offline: FakeProvider plays the model with scripted JSON fixes. The golden-case
tests also prove each case's behaviour check fails on the buggy code and passes on
a reference fix, so the evals measure what they claim to.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agentforge.agents.bug_fix import BugFixAgent, BugFixInput, BugFixOutput, Confidence
from agentforge.agents.bug_fix.scoring import load_cases, run_check, score
from agentforge.core import ErrorKind
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools.patching import apply_patch

CASES = {c.name: c for c in load_cases()}

REFERENCE_FIXES = {
    "zero_division": """--- a/stats/metrics.py
+++ b/stats/metrics.py
@@ -4,2 +4,4 @@
 def average(values: list[float]) -> float:
+    if not values:
+        return 0.0
     return sum(values) / len(values)
""",
    "key_error": """--- a/app/config.py
+++ b/app/config.py
@@ -9,3 +9,3 @@
 def load(user_settings: dict[str, Any]) -> dict[str, Any]:
-    merged = dict(user_settings)
+    merged = {**DEFAULTS, **user_settings}
     return merged
""",
    "none_attribute": """--- a/release/version.py
+++ b/release/version.py
@@ -9,2 +9,2 @@
 def parse_version(text: str) -> tuple[int, int, int]:
-    match = _VERSION.match(text)
+    match = _VERSION.match(text.removeprefix("v"))
""",
    "index_error": """--- a/feed/recent.py
+++ b/feed/recent.py
@@ -7,2 +7,2 @@
     result = []
-    for i in range(max(len(items) - n, 0), len(items) + 1):
+    for i in range(max(len(items) - n, 0), len(items)):
""",
    "type_error": """--- a/billing/invoice.py
+++ b/billing/invoice.py
@@ -6,1 +6,1 @@
-    return name + ": " + quantity + " x " + f"{unit_price:.2f}" + " = " + f"{total:.2f}"
+    return f"{name}: {quantity} x {unit_price:.2f} = {total:.2f}"
""",
}

KEY_ERROR = CASES["key_error"]


def answer(patch: str | None = None, **overrides: Any) -> str:
    base = {
        "root_cause": "load() never merges DEFAULTS, so a missing key raises KeyError.",
        "explanation": "Start from the defaults and overlay the user's settings.",
        "location": {"file": "app/config.py", "line": 10},
        "confidence": "high",
        "patch": patch if patch is not None else REFERENCE_FIXES["key_error"],
        "test_suggestion": "assert timeout({}) == 30 and timeout({'timeout': 5}) == 5",
    }
    return json.dumps({**base, **overrides})


async def run(llm: FakeProvider, **data: Any) -> BugFixOutput:
    result = await BugFixAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


# --------------------------------------------------------------------------- agent


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("bug-fix") is BugFixAgent
    assert "debugging" in BugFixAgent.card().tags


async def test_fixes_pasted_files_and_verifies_the_patch() -> None:
    llm = FakeProvider(script=[answer()])
    output = await run(llm, **KEY_ERROR.input)
    assert output.patch_applies is True
    assert output.confidence is Confidence.HIGH
    request = llm.requests[0]
    user = request.messages[1].content
    assert "KeyError: 'timeout'" in user
    assert "### app/config.py" in user
    assert "fall back to the defaults" in user  # the user's description
    assert '"patch"' in request.messages[0].content  # output schema in the prompt
    assert request.tools == []  # no tools when the files are pasted


async def test_patch_that_does_not_apply_gets_one_repair() -> None:
    wrong = REFERENCE_FIXES["key_error"].replace("merged = dict(", "merged = copy(")
    llm = FakeProvider(script=[answer(wrong), answer()])
    output = await run(llm, **KEY_ERROR.input)
    assert output.patch_applies is True
    feedback = llm.requests[1].messages[-1].content
    assert "The patch does not apply" in feedback
    assert "could not find: 'merged = copy(user_settings)'" in feedback


async def test_patch_that_never_applies_is_an_output_error() -> None:
    wrong = answer(REFERENCE_FIXES["type_error"])  # right format, wrong file
    result = await BugFixAgent(FakeProvider(script=[wrong, wrong])).arun(KEY_ERROR.input)
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "which was not provided" in result.error.message


async def test_patch_that_changes_nothing_is_rejected() -> None:
    noop = (
        "--- a/app/config.py\n+++ b/app/config.py\n@@ -10 +10 @@\n"
        "-    merged = dict(user_settings)\n+    merged = dict(user_settings)\n"
    )
    llm = FakeProvider(script=[answer(noop), answer()])
    await run(llm, **KEY_ERROR.input)
    assert "does not change anything" in llm.requests[1].messages[-1].content


async def test_repo_mode_reads_files_with_tools_and_checks_the_patch(tmp_path: Path) -> None:
    for rel, content in KEY_ERROR.input["files"].items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content, encoding="utf-8")
    llm = FakeProvider(script=[answer()])
    output = await run(llm, error=KEY_ERROR.input["error"], repo_path=str(tmp_path))
    assert output.patch_applies is True
    request = llm.requests[0]
    assert "available through your tools" in request.messages[1].content
    assert sorted(t.name for t in request.tools) == ["list_files", "read_file", "search_text"]
    # nothing is written to the repository
    assert "dict(user_settings)" in (tmp_path / "app/config.py").read_text(encoding="utf-8")


async def test_repo_mode_rejects_paths_outside_the_repo(tmp_path: Path) -> None:
    escape = REFERENCE_FIXES["key_error"].replace("app/config.py", "../config.py")
    result = await BugFixAgent(FakeProvider(script=[answer(escape), answer(escape)])).arun(
        {"error": "KeyError", "repo_path": str(tmp_path)}
    )
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION


def test_input_needs_exactly_one_source() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        BugFixInput(error="boom")
    with pytest.raises(ValueError, match="exactly one"):
        BugFixInput(error="boom", files={"a.py": "x"}, repo_path=".")


# --------------------------------------------------------------------------- golden cases


def test_five_golden_cases_load_with_valid_inputs() -> None:
    assert sorted(CASES) == sorted(REFERENCE_FIXES)
    for case in CASES.values():
        data = case.fix_input()
        assert case.expect.file in data.files
        assert "Traceback" in data.error


@pytest.mark.parametrize("name", sorted(REFERENCE_FIXES))
def test_behaviour_check_fails_on_the_bug_and_passes_on_the_fix(name: str) -> None:
    case = CASES[name]
    files = case.fix_input().files
    broken, detail = run_check(files, case.expect.check)
    assert not broken
    assert "Error" in detail
    fixed = {**files, **apply_patch(files, REFERENCE_FIXES[name])}
    ok, detail = run_check(fixed, case.expect.check)
    assert ok, detail


def test_scoring_a_good_fix_passes() -> None:
    result = score(KEY_ERROR, BugFixOutput.model_validate_json(answer()))
    assert result.passed, result.checks
    assert result.score == 1.0


def test_scoring_explains_each_failure() -> None:
    applies_but_wrong = REFERENCE_FIXES["key_error"].replace(
        "{**DEFAULTS, **user_settings}", "dict(user_settings, timeout=30)"
    )
    vague = BugFixOutput.model_validate_json(
        answer(applies_but_wrong, root_cause="Something is off here.", explanation="Changed it.")
    )
    checks = {c.name: c for c in score(KEY_ERROR, vague).checks}
    assert checks["patch applies"].passed
    assert checks["changes app/config.py"].passed
    assert not checks["root cause named"].passed
    assert not checks["bug is fixed"].passed
    assert "AssertionError" in checks["bug is fixed"].detail

    broken = BugFixOutput.model_validate_json(answer(REFERENCE_FIXES["type_error"]))
    failed = [c.name for c in score(KEY_ERROR, broken).checks if not c.passed]
    assert failed == ["patch applies", "changes app/config.py", "bug is fixed"]


def test_run_check_refuses_paths_outside_the_temp_dir() -> None:
    ok, detail = run_check({"../evil.py": "x = 1\n"}, "pass")
    assert not ok
    assert "outside" in detail


async def test_fake_model_end_to_end_on_a_golden_case() -> None:
    case = CASES["none_attribute"]
    llm = FakeProvider(
        script=[
            answer(
                REFERENCE_FIXES["none_attribute"],
                root_cause="re.match anchors at the start, so the 'v' prefix makes it return None.",
                location={"file": "release/version.py", "line": 9},
            )
        ]
    )
    output = await run(llm, **case.input)
    assert score(case, output).passed
