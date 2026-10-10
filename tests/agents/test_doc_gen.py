"""Documentation agent: placing docstrings, the rubric, the agent loop, repo mode, scoring.

FakeProvider plays the model; doctests really run (pytest in a subprocess).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agentforge.agents.doc_gen import DocGenAgent, DocGenInput, DocGenOutput, DocStyle
from agentforge.agents.doc_gen.docstrings import (
    DocstringError,
    apply_docstrings,
    lint,
    python_blocks,
    render,
)
from agentforge.agents.doc_gen.scoring import load_cases, score
from agentforge.core import ErrorKind
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools.python_ast import same_code
from tests.agents.docgen_refs import REFERENCE_DOCS

CASES = {c.name: c for c in load_cases()}
TEMP = CASES["temperature"]
TEMP_DOCS: dict[str, str] = REFERENCE_DOCS["temperature"]["docstrings"]
TEMP_README: str = REFERENCE_DOCS["temperature"]["readme"]


def answer(docs: dict[str, str] | None = None, readme: str | None = TEMP_README) -> str:
    docs = TEMP_DOCS if docs is None else docs
    return json.dumps(
        {
            "summary": "Temperature conversion and parsing.",
            "docstrings": [{"name": k, "docstring": v} for k, v in docs.items()],
            "readme": readme,
        }
    )


async def run(llm: FakeProvider, **data: Any) -> DocGenOutput:
    result = await DocGenAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


# --------------------------------------------------------------------------- placing docstrings

SRC = """#!/usr/bin/env python
import os


class Cart:
    def __init__(self, owner):
        self.owner = owner

    @property
    def total(self):
        return 1

    @staticmethod
    @cache
    def make():
        return Cart("x")


def one(): return 1
"""


def test_render_one_line_multi_line_and_backslashes() -> None:
    assert render("Short.", "    ") == ['    """Short."""']
    assert render("Sum.\n\n    Args:\n        x: X.", "  ") == [
        '  """Sum.',
        "",
        "  Args:",
        "      x: X.",
        '  """',
    ]
    assert render(r"Path C:\temp.", "") == ['r"""Path C:\\temp."""']
    assert render('Say "hi"', "")[-1] == '"""'  # no """" at the end
    with pytest.raises(DocstringError, match='"""'):
        render('Has """ inside.', "")


def test_apply_inserts_after_shebang_and_before_decorators() -> None:
    applied = apply_docstrings(
        SRC,
        {"<module>": "Carts.", "Cart": "A cart.", "Cart.make": "Make one.", "Cart.total": "Total."},
    )
    lines = applied.source.split("\n")
    assert lines[:3] == ["#!/usr/bin/env python", '"""Carts."""', ""]
    assert '    """A cart."""' in lines
    assert lines[lines.index('    """A cart."""') + 1] == ""  # blank line after a class docstring
    make = lines.index("    def make():")
    assert lines[make + 1] == '        """Make one."""'
    assert lines[make - 2] == "    @staticmethod"
    assert same_code(SRC, applied.source)
    assert applied.written == ("<module>", "Cart", "Cart.make", "Cart.total")


def test_existing_docstrings_are_kept_unless_rewriting() -> None:
    first = apply_docstrings(SRC, {"Cart": "A cart."}).source
    again = apply_docstrings(first, {"Cart": "Changed."})
    assert again.kept == ("Cart",)
    assert again.source == first
    rewritten = apply_docstrings(first, {"Cart": "Changed.\n\nMore."}, rewrite_existing=True)
    assert '    """Changed.' in rewritten.source
    assert "A cart." not in rewritten.source
    assert same_code(SRC, rewritten.source)


@pytest.mark.parametrize(
    ("docs", "message"),
    [
        ({"Cart.nope": "x."}, "no such definition: Cart.nope"),
        ({"one": "One."}, "one-line definition"),
    ],
)
def test_apply_errors(docs: dict[str, str], message: str) -> None:
    with pytest.raises(DocstringError, match=message):
        apply_docstrings(SRC, docs)


def test_a_body_on_the_signature_line_cannot_take_a_docstring() -> None:
    source = "def f(\n    x,\n) -> int: ...\n\n\ndef g(\n    x,\n):\n    return x\n"
    with pytest.raises(DocstringError, match="'f' is a one-line definition"):
        apply_docstrings(source, {"f": "F."})
    assert '    """G."""' in apply_docstrings(source, {"g": "G."}).source


def test_apply_to_an_empty_module() -> None:
    assert apply_docstrings("", {"<module>": "Nothing yet."}).source == '"""Nothing yet."""\n'
    commented = apply_docstrings("# todo\n", {"<module>": "Soon."}).source
    assert commented == '# todo\n"""Soon."""\n'
    assert same_code("", '"""Nothing yet."""\n')


# --------------------------------------------------------------------------- rubric

FUNC = """
def scale(values, factor, *, clamp=False):
    if factor == 0:
        raise ValueError("zero")
    return [v * factor for v in values]


def pairs(items):
    yield from zip(items, items[1:])


class Box:
    @property
    def size(self):
        return 1
"""


@pytest.mark.parametrize(
    ("style", "doc"),
    [
        (
            DocStyle.GOOGLE,
            "Scale values.\n\nArgs:\n    values: V.\n    factor: F.\n    clamp: C.\n\n"
            "Returns:\n    List.\n\nRaises:\n    ValueError: If factor is 0.",
        ),
        (
            DocStyle.NUMPY,
            "Scale values.\n\nParameters\n----------\nvalues\nfactor\nclamp\n\n"
            "Returns\n-------\nlist\n\nRaises\n------\nValueError",
        ),
        (
            DocStyle.SPHINX,
            "Scale values.\n\n:param values: V.\n:param factor: F.\n:param clamp: C.\n"
            ":returns: List.\n:raises ValueError: If factor is 0.",
        ),
    ],
)
def test_rubric_accepts_each_style(style: DocStyle, doc: str) -> None:
    source = apply_docstrings(FUNC, {"scale": doc}).source
    assert lint(source, ["scale"], style) == []


def test_rubric_names_every_problem() -> None:
    source = apply_docstrings(
        FUNC,
        {"scale": "scales values\n\nArgs:\n    values: V.", "pairs": "Pairs.", "Box.size": "Size."},
    ).source
    assert lint(source, ["scale", "pairs", "Box.size", "Box"], DocStyle.GOOGLE) == [
        "scale: the first line should be a one-sentence summary ending in '.'",
        "scale: parameter 'factor' is not described",
        "scale: parameter 'clamp' is not described",
        "scale: say when it raises ValueError (a Raises section)",
        "scale: it returns a value; add a Returns section",
        "pairs: add a 'Args:' section",
        "pairs: parameter 'items' is not described",
        "pairs: it is a generator; add a Yields section",
        "Box: missing docstring",
    ]  # Box.size is a property: no Returns section needed


def test_python_blocks_skip_doctest_sessions() -> None:
    md = "```python\nx = 1\n```\n\n```py\n>>> x\n1\n```\n\n```bash\nls\n```\n"
    assert python_blocks(md) == ["x = 1\n"]


# --------------------------------------------------------------------------- agent


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("doc-gen") is DocGenAgent
    assert "documentation" in DocGenAgent.card().tags


def test_input_rules() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        DocGenInput(target="a.py")
    with pytest.raises(ValueError, match="not in files"):
        DocGenInput(target="a.py", files={"b.py": "x = 1"})


async def test_documents_a_module_and_runs_its_examples() -> None:
    llm = FakeProvider(script=[answer()])
    output = await run(llm, **TEMP.input)
    assert output.coverage_before == 0.0
    assert output.coverage_after == 100.0
    assert output.doctests is not None
    assert output.doctests.passed
    assert output.doctests.tests == 3
    assert '    """Convert degrees Celsius to degrees Fahrenheit.' in output.documented_source
    assert same_code(TEMP.input["files"]["units/temperature.py"], output.documented_source)
    user = llm.requests[0].messages[1].content
    assert "in **google** docstring style" in user
    assert (
        "Write docstrings for: <module>, celsius_to_fahrenheit, fahrenheit_to_celsius, "
        "parse_temperature" in user
    )
    assert llm.requests[0].tools == []


async def test_incomplete_docs_go_back_with_the_problems() -> None:
    partial = {k: v for k, v in TEMP_DOCS.items() if k != "fahrenheit_to_celsius"}
    partial["parse_temperature"] = "Parse a reading.\n\nArgs:\n    text: The reading."
    llm = FakeProvider(script=[answer(partial, readme=None), answer()])
    output = await run(llm, **TEMP.input)
    assert output.coverage_after == 100.0
    feedback = llm.requests[1].messages[-1].content
    assert "still missing docstrings: fahrenheit_to_celsius" in feedback
    assert "parse_temperature: say when it raises ValueError" in feedback
    assert "parse_temperature: it returns a value; add a Returns section" in feedback
    assert "readme is empty" in feedback


async def test_wrong_doctest_output_goes_back() -> None:
    wrong = {**TEMP_DOCS}
    wrong["celsius_to_fahrenheit"] = wrong["celsius_to_fahrenheit"].replace("212.0", "212")
    llm = FakeProvider(script=[answer(wrong), answer()])
    output = await run(llm, **TEMP.input)
    assert output.doctests is not None
    assert output.doctests.passed
    feedback = llm.requests[1].messages[-1].content
    assert "fail as doctests" in feedback
    assert "Expected:" in feedback


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (answer({**TEMP_DOCS, "convert": "Nope."}), "no such definition: convert"),
        (answer(readme="```python\ndef (:\n```"), "readme code block 1 is not valid Python"),
        (
            json.dumps(
                {
                    "summary": "Twice the same name.",
                    "docstrings": [{"name": "<module>", "docstring": "Temps."}] * 2,
                }
            ),
            "Repeated: <module>",
        ),
    ],
    ids=["unknown-name", "broken-readme", "duplicate"],
)
async def test_unusable_answers_are_explained(bad: str, message: str) -> None:
    llm = FakeProvider(script=[bad, answer()])
    await run(llm, **TEMP.input)
    assert message in llm.requests[1].messages[-1].content


async def test_gives_up_after_two_corrections() -> None:
    llm = FakeProvider(script=[answer({})] * 3)
    result = await DocGenAgent(llm).arun(TEMP.input)
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "after 2 repair attempts" in result.error.message


async def test_partly_documented_module_keeps_existing_docstrings() -> None:
    case = CASES["partly_documented"]
    ref = REFERENCE_DOCS["partly_documented"]
    llm = FakeProvider(script=[answer(ref["docstrings"], ref["readme"])])
    output = await run(llm, **case.input)
    assert output.coverage_before == 50.0
    assert output.coverage_after == 100.0
    assert "Collapse runs of whitespace" in output.documented_source
    assert "Write docstrings for: truncate, initials" in llm.requests[0].messages[1].content
    assert "Leave existing docstrings alone." in llm.requests[0].messages[1].content


async def test_repo_mode_reads_the_target_and_runs_doctests_there(tmp_path: Path) -> None:
    target = "src/units/temperature.py"
    (tmp_path / "src/units").mkdir(parents=True)
    (tmp_path / "src/units/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / target).write_text(TEMP.input["files"]["units/temperature.py"], encoding="utf-8")
    original = (tmp_path / target).read_text(encoding="utf-8")
    llm = FakeProvider(script=[answer()])
    output = await run(llm, target=target, repo_path=str(tmp_path), readme=False)
    assert output.doctests is not None
    assert output.doctests.passed
    assert sorted(t.name for t in llm.requests[0].tools) == [
        "list_files",
        "read_file",
        "search_text",
    ]
    assert (tmp_path / target).read_text(encoding="utf-8") == original  # nothing written


async def test_repo_mode_missing_target(tmp_path: Path) -> None:
    result = await DocGenAgent(FakeProvider(script=[])).arun(
        {"target": "nope.py", "repo_path": str(tmp_path)}
    )
    assert result.error is not None
    assert "not found" in result.error.message


# --------------------------------------------------------------------------- golden cases


def test_five_golden_cases_load() -> None:
    assert sorted(CASES) == sorted(REFERENCE_DOCS)
    styles = {case.doc_input().style for case in CASES.values()}
    assert styles == {DocStyle.GOOGLE, DocStyle.NUMPY, DocStyle.SPHINX}


@pytest.mark.parametrize("name", sorted(REFERENCE_DOCS))
async def test_reference_docs_score_full_marks(name: str) -> None:
    ref = REFERENCE_DOCS[name]
    output = DocGenOutput.model_validate_json(answer(ref["docstrings"], ref["readme"]))
    card = await score(CASES[name], output)
    assert card.passed, [c for c in card.checks if not c.passed]


async def test_scoring_explains_failures() -> None:
    thin = {k: "Does a thing." for k in TEMP_DOCS}
    card = await score(TEMP, DocGenOutput.model_validate_json(answer(thin, "# temperature\n")))
    failed = {c.name: c.detail for c in card.checks if not c.passed}
    assert set(failed) == {
        "rubric",
        "readme covers the API",
        "parse_temperature mentions Celsius, ValueError",
    }
    assert "celsius_to_fahrenheit" in failed["readme covers the API"]

    bad = DocGenOutput.model_validate_json(answer({"nope": "Nope."}))
    assert [c.name for c in (await score(TEMP, bad)).checks] == ["docstrings apply"]
