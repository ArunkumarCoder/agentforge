"""Tests for token pricing and cost calculation."""

import pytest

from agentforge.core import Message, Usage
from agentforge.llm import FakeProvider, LLMRequest, ModelPrice, PriceTable
from tests.conftest import LogCapture

TABLE = PriceTable(
    {
        "claude-haiku": ModelPrice(1.0, 5.0),
        "claude-haiku-4-5": ModelPrice(2.0, 10.0),
        "cheap": ModelPrice(0.1, 0.4),
    }
)


def test_cost_per_million_tokens() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=200_000)
    assert TABLE.cost("cheap", usage) == pytest.approx(0.1 + 0.08)


def test_small_usage_keeps_precision() -> None:
    assert TABLE.cost("cheap", Usage(input_tokens=12, output_tokens=7)) == pytest.approx(
        12 * 0.1 / 1e6 + 7 * 0.4 / 1e6
    )


def test_longest_prefix_wins_for_dated_model_ids() -> None:
    price = TABLE.price_for("claude-haiku-4-5-20251001")
    assert price == ModelPrice(2.0, 10.0)
    assert TABLE.price_for("claude-haiku-3") == ModelPrice(1.0, 5.0)


def test_unknown_model_costs_zero_and_warns_once(logs: LogCapture) -> None:
    table = PriceTable({})
    usage = Usage(input_tokens=100, output_tokens=100)
    assert table.cost("mystery", usage) == 0
    assert table.cost("mystery", usage) == 0
    assert logs.events().count("llm.price.unknown") == 1


def test_free_skips_lookup(logs: LogCapture) -> None:
    assert PriceTable({}).cost("llama3.2", Usage(input_tokens=5), free=True) == 0
    assert "llm.price.unknown" not in logs.events()


def test_with_prices_overrides_without_mutating() -> None:
    updated = TABLE.with_prices({"cheap": ModelPrice(9, 9)})
    assert updated.price_for("cheap") == ModelPrice(9, 9)
    assert TABLE.price_for("cheap") == ModelPrice(0.1, 0.4)


def test_default_table_has_known_models() -> None:
    table = PriceTable.default()
    assert table.price_for("claude-haiku-4-5") is not None
    assert table.price_for("gpt-5-mini") is not None


def test_negative_price_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        ModelPrice(-1, 0)


async def test_provider_fills_in_cost(logs: LogCapture) -> None:
    class PaidFake(FakeProvider):
        is_local = False  # pretend it's a paid API

    provider = PaidFake(model="cheap-v2", script=["one two three"], pricing=TABLE)
    response = await provider.complete(LLMRequest(messages=[Message.user("a b c d")]))
    assert response.usage.input_tokens == 4
    assert response.usage.output_tokens == 3
    assert response.usage.cost_usd == pytest.approx((4 * 0.1 + 3 * 0.4) / 1e6)
    logged = next(e for e in logs.entries if e["event"] == "llm.response")
    assert logged["cost_usd"] == response.usage.cost_usd


async def test_local_providers_are_free() -> None:
    provider = FakeProvider(model="cheap", pricing=TABLE)
    response = await provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert response.usage.cost_usd == 0
    assert response.usage.input_tokens == 1
