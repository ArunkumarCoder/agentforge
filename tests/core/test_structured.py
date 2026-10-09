"""Tests for extracting and validating JSON from model replies."""

import pytest
from pydantic import Field

from agentforge.core.schemas import AgentOutput
from agentforge.core.structured import (
    OutputParseError,
    extract_json,
    parse_model_output,
    schema_for_prompt,
)


class Answer(AgentOutput):
    title: str
    score: int = Field(ge=0, le=10)


@pytest.mark.parametrize(
    "text",
    [
        '{"title": "ok", "score": 3}',
        '  {"title": "ok", "score": 3}\n',
        'Here you go:\n```json\n{"title": "ok", "score": 3}\n```\nDone.',
        '```\n{"title": "ok", "score": 3}\n```',
        'Sure! {"title": "ok", "score": 3} Hope that helps.',
    ],
)
def test_json_is_found_in_common_wrappings(text: str) -> None:
    assert parse_model_output(text, Answer) == Answer(title="ok", score=3)


def test_not_json() -> None:
    with pytest.raises(OutputParseError, match="not valid JSON") as info:
        extract_json("I could not do it.")
    assert info.value.raw == "I could not do it."


def test_broken_json_reports_position() -> None:
    with pytest.raises(OutputParseError, match="line 1 column"):
        extract_json('{"title": "ok", "score": }')


def test_schema_errors_are_listed_compactly() -> None:
    with pytest.raises(OutputParseError) as info:
        parse_model_output('{"score": 42, "extra": 1}', Answer)
    message = str(info.value)
    assert "title: Field required" in message
    assert "score: Input should be less than or equal to 10" in message
    assert "extra: Extra inputs are not permitted" in message


def test_many_errors_are_capped() -> None:
    class Wide(AgentOutput):
        a: int
        b: int
        c: int

    from agentforge.core.structured import describe_errors

    try:
        Wide.model_validate({})
    except Exception as exc:
        assert describe_errors(exc, limit=2).endswith("; and 1 more")  # type: ignore[arg-type]


def test_schema_for_prompt_is_json_schema() -> None:
    text = schema_for_prompt(Answer)
    assert '"title"' in text
    assert '"maximum": 10' in text
