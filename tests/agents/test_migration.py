"""Migration agent: site, API, plan and test verification.

FakeProvider plays the model; the scanner and the tests inside each case really run.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agentforge.agents.migration import MigrationAgent, MigrationInput, MigrationOutput
from agentforge.agents.migration.scoring import assertion_count, load_cases, score
from agentforge.core import ErrorKind
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools.py_migrate import RULES

CASES = {c.name: c for c in load_cases()}
REFS: dict[str, dict[str, str]] = json.loads(
    (Path(__file__).parent / "migrate_refs.json").read_text(encoding="utf-8")
)
CLOCK = CASES["clock_utc"]
UNITTEST = CASES["unittest_aliases"]


def answer(name: str, **overrides: Any) -> str:
    files = REFS[name]
    base: dict[str, Any] = {
        "summary": "Migrated the deprecated and removed APIs.",
        "plan": [
            {
                "title": "Replace the old APIs",
                "risk": "low",
                "files": list(files),
                "sites": [],
                "rationale": "Mechanical spelling changes with identical behaviour.",
            }
        ],
        "files": files,
    }
    return json.dumps({**base, **overrides})


async def run(llm: FakeProvider, **data: Any) -> MigrationOutput:
    result = await MigrationAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("migration") is MigrationAgent
    assert "migration" in MigrationAgent.card().tags


def test_input_rules() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        MigrationInput()
    with pytest.raises(ValueError, match="unknown rule"):
        MigrationInput(files={"a.py": "x = 1"}, rules=["nope"])
    assert MigrationInput(files={"a.py": "x = 1"}, rules=list(RULES))


async def test_migrates_and_proves_it() -> None:
    llm = FakeProvider(script=[answer("unittest_aliases")])
    output = await run(llm, **UNITTEST.input)
    assert output.changed == ["tests/test_cart.py"]
    assert output.compiled
    assert len(output.sites_before) == 4
    assert output.sites_after == []
    assert output.tests_before is not None and not output.tests_before.passed  # removed APIs
    assert output.tests_after is not None and output.tests_after.passed
    assert output.behaviour_verified
    assert output.diff.startswith("--- a/tests/test_cart.py\n+++ b/tests/test_cart.py\n")
    assert "-        self.assertEquals(cart.total_items(), 2)" in output.diff
    user = llm.requests[0].messages[1].content
    assert "M1: [unittest-aliases] tests/test_cart.py:10" in user
    assert "The tests currently FAIL" in user
    assert "- unittest-aliases: use assertEqual" in user
    assert llm.requests[0].tools == []


async def test_all_golden_cases_score_full_marks_with_the_references() -> None:
    for name, case in CASES.items():
        output = await run(FakeProvider(script=[answer(name)]), **case.input)
        card = await score(case, output)
        assert card.passed, (name, [c for c in card.checks if not c.passed])


async def test_clean_code_skips_the_model() -> None:
    llm = FakeProvider(script=[])
    output = await run(llm, files={"a.py": "def add(a, b):\n    return a + b\n"})
    assert llm.requests == []
    assert output.changed == []
    assert output.compiled
    assert not output.behaviour_verified


async def test_rules_limit_what_is_migrated() -> None:
    llm = FakeProvider(script=[])
    files = {"a.py": "import imp\n"}
    output = await run(llm, files=files, rules=["utcnow"])
    assert llm.requests == []
    assert output.sites_before == []


# --------------------------------------------------------------------------- verification


async def corrected(bad: str, message: str, name: str = "clock_utc") -> None:
    llm = FakeProvider(script=[bad, answer(name)])
    await run(llm, **CASES[name].input)
    assert message in llm.requests[1].messages[-1].content


async def test_unknown_unchanged_and_broken_files_go_back() -> None:
    clock = REFS["clock_utc"]["billing/clock.py"]
    original = CLOCK.input["files"]["billing/clock.py"]
    await corrected(
        answer("clock_utc", files={"other.py": "x = 1\n"}), "is not one of the files given"
    )
    await corrected(
        answer("clock_utc", files={"billing/clock.py": "def (:\n"}), "is not valid Python"
    )
    await corrected(answer("clock_utc", files={"billing/clock.py": original}), "is unchanged")
    await corrected(answer("clock_utc", files={}), "`files` is empty")
    assert clock != original


async def test_a_site_left_behind_goes_back() -> None:
    clock = REFS["clock_utc"]["billing/clock.py"]
    partial = clock.replace(
        "datetime.now(UTC).replace(tzinfo=None) + timedelta", "datetime.utcnow() + timedelta"
    )
    await corrected(
        answer("clock_utc", files={"billing/clock.py": partial}),
        "billing/clock.py:17 still has [utcnow]",
    )


async def test_a_new_kind_of_site_goes_back() -> None:
    clock = REFS["clock_utc"]["billing/clock.py"]
    worse = "from typing import List\n" + clock
    await corrected(
        answer("clock_utc", files={"billing/clock.py": worse}), "still has [typing-generics]"
    )


async def test_changed_public_api_goes_back() -> None:
    clock = REFS["clock_utc"]["billing/clock.py"]
    renamed = clock.replace("def expiry(days: int)", "def expiry(days: int, extra: int)")
    await corrected(
        answer("clock_utc", files={"billing/clock.py": renamed}), "the public API changed"
    )


async def test_plan_problems_go_back() -> None:
    await corrected(answer("clock_utc", plan=[]), "`plan` is empty")
    step = {
        "title": "Replace utcnow", "risk": "low", "files": ["tests/test_clock.py"],
        "sites": ["M99"], "rationale": "Keeps results naive in UTC.",
    }  # fmt: skip
    llm = FakeProvider(script=[answer("clock_utc", plan=[step]), answer("clock_utc")])
    await run(llm, **CLOCK.input)
    feedback = llm.requests[1].messages[-1].content
    assert "the plan does not mention changed file billing/clock.py" in feedback
    assert "unknown site(s): M99" in feedback


async def test_a_behaviour_change_is_caught_by_the_tests() -> None:
    aware = REFS["clock_utc"]["billing/clock.py"].replace(".replace(tzinfo=None)", "")
    llm = FakeProvider(
        script=[answer("clock_utc", files={"billing/clock.py": aware}), answer("clock_utc")]
    )
    output = await run(llm, **CLOCK.input)
    feedback = llm.requests[1].messages[-1].content
    assert "The tests fail on the migrated code (they passed before)" in feedback
    assert "TypeError" in feedback  # pytest's own message is quoted
    assert output.behaviour_verified


async def test_skipping_a_site_needs_a_real_id_and_a_reason() -> None:
    skip = {"site_id": "M99", "reason": "cannot be migrated safely here"}
    await corrected(answer("clock_utc", skipped=[skip]), "skipped site M99 does not exist")


async def test_everything_skipped_is_a_valid_answer() -> None:
    skipped = [
        {"site_id": f"M{i}", "reason": "Naive and aware datetimes are mixed elsewhere."}
        for i in range(1, 5)
    ]
    reply = json.dumps({"summary": "Left everything for a manual review.", "skipped": skipped})
    llm = FakeProvider(script=[reply])
    output = await run(llm, **CLOCK.input)
    assert output.changed == []
    assert len(output.sites_after) == 4
    assert not output.behaviour_verified


async def test_a_skipped_site_may_remain() -> None:
    clock = REFS["clock_utc"]["billing/clock.py"]
    partial = clock.replace(
        "datetime.now(UTC).replace(tzinfo=None) + timedelta", "datetime.utcnow() + timedelta"
    )
    skip = {"site_id": "M4", "reason": "Needs a decision from the billing team."}
    output = await run(
        FakeProvider(
            script=[answer("clock_utc", files={"billing/clock.py": partial}, skipped=[skip])]
        ),
        **CLOCK.input,
    )
    assert [s.line for s in output.sites_after] == [17]


async def test_gives_up_after_two_corrections() -> None:
    bad = answer("clock_utc", files={})
    result = await MigrationAgent(FakeProvider(script=[bad] * 3)).arun(CLOCK.input)
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "after 2 repair attempts" in result.error.message


def test_assertion_count() -> None:
    source = (
        "import pytest\n\ndef t(self):\n    assert 1\n    self.assertEqual(1, 1)\n"
        "    with pytest.raises(ValueError):\n        pass\n    self.helper()\n"
    )
    assert assertion_count(source) == 3


# --------------------------------------------------------------------------- start conditions


async def test_too_many_sites_are_refused() -> None:
    code = "import imp\n" * 130
    result = await MigrationAgent(FakeProvider(script=[])).arun({"files": {"a.py": code}})
    assert result.error is not None
    assert "too many" in result.error.message


async def test_empty_repo_is_refused(tmp_path: Path) -> None:
    result = await MigrationAgent(FakeProvider(script=[])).arun({"repo_path": str(tmp_path)})
    assert result.error is not None
    assert "no readable Python files" in result.error.message


def write_repo(root: Path, name: str) -> None:
    for rel, content in CASES[name].input["files"].items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


async def test_repo_mode_runs_the_tests_and_gives_the_model_tools(tmp_path: Path) -> None:
    write_repo(tmp_path, "typing_modernize")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("import imp\n", encoding="utf-8")
    llm = FakeProvider(script=[answer("typing_modernize")])
    output = await run(llm, repo_path=str(tmp_path))
    assert output.changed == ["inventory/models.py"]
    assert output.tests_before is not None and output.tests_before.passed
    assert output.tests_after is not None and output.tests_after.passed
    assert output.behaviour_verified
    user = llm.requests[0].messages[1].content
    assert "node_modules" not in user
    assert "available through your tools" in user
    assert {t.name for t in llm.requests[0].tools} >= {"read_file"}
    # the repository itself is never written to
    assert (tmp_path / "inventory" / "models.py").read_text(encoding="utf-8") == (
        CASES["typing_modernize"].input["files"]["inventory/models.py"]
    )


async def test_repo_mode_explicit_tests_and_paths(tmp_path: Path) -> None:
    write_repo(tmp_path, "plugin_loader")
    llm = FakeProvider(script=[json.dumps({"summary": "Nothing to do here at all.", "plan": []})])
    result = await MigrationAgent(llm).arun(
        {
            "repo_path": str(tmp_path),
            "paths": ["plugins/constants.py"],
            "tests": ["tests/missing.py"],
        }
    )
    assert result.error is not None
    assert "test file 'tests/missing.py' not found" in result.error.message

    output = await run(
        FakeProvider(script=[]),
        repo_path=str(tmp_path),
        paths=["plugins/constants.py"],
        tests=["tests/test_loader.py"],
    )
    assert output.sites_before == []  # constants.py has nothing to migrate


async def test_instructions_reach_the_prompt() -> None:
    llm = FakeProvider(script=[answer("clock_utc")])
    await run(llm, **CLOCK.input, instructions="datetimes must stay naive")
    assert "datetimes must stay naive" in llm.requests[0].messages[1].content
