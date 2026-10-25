"""Data Analysis agent: the checks on the final script, the repair loop and the golden cases.

FakeProvider plays the model; the scripts it "writes" really run in the sandbox with pandas.
"""

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from agentforge.agents.data_analysis import (
    DataAnalysisAgent,
    DataAnalysisInput,
    DataAnalysisOutput,
)
from agentforge.agents.data_analysis.agent import claim_states, values_match
from agentforge.agents.data_analysis.scoring import load_cases, score
from agentforge.core import ErrorKind, ToolCall
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools.pyexec import PNG_MAGIC
from tests.agents.dataanalysis_refs import reference_answer

CASES = {c.name: c for c in load_cases()}


def reply(answer: dict[str, Any]) -> str:
    return json.dumps(answer)


async def run(llm: FakeProvider, **data: Any) -> DataAnalysisOutput:
    result = await DataAnalysisAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("data-analysis") is DataAnalysisAgent
    assert "data" in DataAnalysisAgent.card().tags


def test_input_rules() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        DataAnalysisInput(question="What is the mean?")
    with pytest.raises(ValueError, match="exactly one"):
        DataAnalysisInput(question="What is the mean?", files={"a.csv": "x\n1"}, repo_path=".")


async def test_every_golden_case_scores_full_marks_with_the_reference_answer() -> None:
    assert len(CASES) == 5
    for case in CASES.values():
        llm = FakeProvider(script=[reply(reference_answer(case))])
        output = await run(llm, **case.input)
        card = score(case, output)
        assert card.passed, (case.name, [c for c in card.checks if not c.passed])


async def test_the_profile_is_in_the_prompt_and_the_tool_is_offered() -> None:
    case = CASES["order_value"]
    llm = FakeProvider(script=[reply(reference_answer(case))])
    await run(llm, **case.input)
    user = llm.requests[0].messages[1].content
    assert "## orders.csv: 24 rows x 4 columns" in user
    assert "- status:" in user
    assert "'completed'" in user  # the few distinct values are shown, so traps are visible
    assert [t.name for t in llm.requests[0].tools] == ["run_python"]
    assert "emit(" in llm.requests[0].messages[0].content


async def test_charts_come_back_as_png_data() -> None:
    case = CASES["growth_rate"]
    output = await run(FakeProvider(script=[reply(reference_answer(case))]), **case.input)
    (chart,) = output.chart_files
    assert chart.name == "revenue_by_month"
    assert base64.b64decode(chart.png_base64).startswith(PNG_MAGIC)
    assert chart.bytes > 1000
    assert output.results["growth_percent"] == pytest.approx(7.947019867549665)
    assert output.isolation in {"namespace", "guard"}


async def test_the_model_can_explore_with_run_python_before_answering() -> None:
    case = CASES["order_value"]
    explore = ToolCall(
        id="t1",
        name="run_python",
        arguments={
            "code": "import pandas as pd\nprint(pd.read_csv('orders.csv').status.value_counts())"
        },
    )
    llm = FakeProvider(script=[explore, reply(reference_answer(case))])
    output = await run(llm, **case.input)
    assert score(case, output).passed
    tool_message = llm.requests[1].messages[-1]
    assert "completed" in tool_message.content
    assert "exit code: 0" in tool_message.content


# ------------------------------------------------------------------ the checks


async def one_bad_then_good(case_name: str, bad: dict[str, Any]) -> tuple[FakeProvider, str]:
    case = CASES[case_name]
    good = reference_answer(case)
    llm = FakeProvider(script=[reply({**good, **bad}), reply(good)])
    output = await run(llm, **case.input)
    assert score(case, output).passed
    assert len(llm.requests) == 2
    return llm, llm.requests[-1].messages[-1].content


async def test_a_reported_number_that_the_script_did_not_emit_is_sent_back() -> None:
    good = reference_answer(CASES["order_value"])
    wrong = [{**good["findings"][0], "value": 70.0}, good["findings"][1]]
    _, feedback = await one_bad_then_good("order_value", {"findings": wrong})
    assert "you reported 70.0 but the script emitted 68.87941176470588" in feedback


async def test_a_finding_for_a_key_that_was_never_emitted_is_sent_back() -> None:
    good = reference_answer(CASES["order_value"])
    wrong = [{**good["findings"][0], "key": "invented_key"}, good["findings"][1]]
    _, feedback = await one_bad_then_good("order_value", {"findings": wrong})
    assert "never emitted it" in feedback
    assert "completed_orders" in feedback  # tells the model what it did emit


async def test_a_sentence_that_does_not_state_the_number_is_sent_back() -> None:
    good = reference_answer(CASES["order_value"])
    vague = [
        {**good["findings"][0], "claim": "The mean is about right for completed orders."},
        good["findings"][1],
    ]
    _, feedback = await one_bad_then_good("order_value", {"findings": vague})
    assert "does not contain" in feedback


async def test_a_script_that_fails_is_sent_back_with_its_traceback() -> None:
    _, feedback = await one_bad_then_good(
        "order_value", {"code": "import pandas as pd\ndf = pd.read_csv('missing.csv')\n"}
    )
    assert "the final script failed" in feedback
    assert "FileNotFoundError" in feedback
    assert "user_code.py" in feedback


async def test_a_declared_chart_that_was_not_saved_is_sent_back() -> None:
    _, feedback = await one_bad_then_good(
        "order_value", {"charts": [{"name": "ghost", "title": "A chart that is not there"}]}
    )
    assert "chart 'ghost' was not saved" in feedback


async def test_a_script_that_never_ends_is_reported_as_too_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentforge.agents.data_analysis.agent as module

    monkeypatch.setattr(module, "RUN_TIMEOUT_S", 2.0)
    _, feedback = await one_bad_then_good("order_value", {"code": "while True:\n    pass\n"})
    assert "timed out" in feedback


async def test_gives_up_after_two_corrections() -> None:
    case = CASES["order_value"]
    bad = {**reference_answer(case), "code": "raise SystemExit(3)\n"}
    llm = FakeProvider(script=[reply(bad)] * 3)
    result = await DataAnalysisAgent(llm).arun(case.input)
    assert not result.ok
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert len(llm.requests) == 3


# ------------------------------------------------------------------ input handling


async def test_unsafe_file_names_are_refused_before_any_model_call() -> None:
    llm = FakeProvider(script=[])
    result = await DataAnalysisAgent(llm).arun(
        {"question": "What is the mean of x?", "files": {"../escape.csv": "x\n1\n"}}
    )
    assert not result.ok
    assert result.error is not None
    assert "not an allowed file name" in result.error.message
    assert llm.requests == []


async def test_a_folder_is_read_but_secrets_and_other_files_are_not(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sales.csv").write_text("units\n1\n2\n3\n")
    (tmp_path / "data" / "notes.md").write_text("not data")
    (tmp_path / ".env").write_text("SECRET=1")
    answer = {
        "answer": "The units add up to 6 in total.",
        "code": "import pandas as pd\nfrom agentforge_sandbox import emit\n"
        "emit('total_units', int(pd.read_csv('data/sales.csv').units.sum()))\n",
        "findings": [{"claim": "Total units: 6.", "key": "total_units", "value": 6}],
    }
    llm = FakeProvider(script=[reply(answer)])
    output = await run(llm, question="What is the sum of units?", repo_path=str(tmp_path))
    user = llm.requests[0].messages[1].content
    assert "data/sales.csv: 3 rows" in user
    assert "notes.md" not in user
    assert ".env" not in user
    assert output.results == {"total_units": 6}


async def test_a_folder_without_data_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("hello")
    result = await DataAnalysisAgent(FakeProvider(script=[])).arun(
        {"question": "What is in here?", "repo_path": str(tmp_path)}
    )
    assert not result.ok
    assert result.error is not None
    assert "no readable data files" in result.error.message


# ------------------------------------------------------------------ helpers


def test_values_match() -> None:
    assert values_match(68.879411764, 68.87941176470588)  # copied digits are fine
    assert not values_match(68.88, 68.87941176470588)  # a rounded value is not what was emitted
    assert values_match(68.87941176470588, 68.87941176470588)
    assert values_match(17, 17.0)
    assert values_match(" south ", "south")
    assert not values_match("south", "North")
    assert values_match(True, True)
    assert not values_match(True, 1)
    assert not values_match(1, "1")


@pytest.mark.parametrize(
    ("claim", "value", "ok"),
    [
        ("The mean is 68.88.", 68.87941176470588, True),
        ("Revenue grew 7.95% last month.", 7.947019867549665, True),
        ("Growth was 7.9%.", 0.0794702, True),  # percent written for a fraction
        ("Total spend is 1,234,567 dollars.", 1234567.0, True),
        ("The mean is 70.", 68.87941176470588, False),
        ("The mean is large.", 68.87941176470588, False),
        ("The top region is South.", "south", True),
        ("The top region is North.", "south", False),
    ],
)
def test_claim_states(claim: str, value: Any, ok: bool) -> None:
    assert claim_states(claim, value) is ok
