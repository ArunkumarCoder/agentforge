"""Token prices and cost calculation.

Prices are in US dollars per **million** tokens. Model names are matched by
the longest known prefix, so a dated model ID such as
``claude-haiku-4-5-20251001`` uses the ``claude-haiku-4-5`` price.

Prices change. The defaults below are a starting point: check them against
the providers' pricing pages and override them with ``PriceTable({...})`` or
``PriceTable.default().with_prices({...})``. An unknown model costs 0 and is
logged once as ``llm.price.unknown`` so it doesn't go unnoticed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from agentforge.core.schemas import Usage
from agentforge.log import get_logger

PER_MILLION = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float

    def __post_init__(self) -> None:
        if self.input_per_mtok < 0 or self.output_per_mtok < 0:
            raise ValueError("prices cannot be negative")


# Verify against https://www.anthropic.com/pricing and https://openai.com/api/pricing
# before relying on cost numbers. Local and fake models are free.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": ModelPrice(1.00, 5.00),
    "gpt-5-mini": ModelPrice(0.25, 2.00),
    "fake": ModelPrice(0.0, 0.0),
}


class PriceTable:
    """Looks up a model's price and turns token usage into dollars."""

    def __init__(self, prices: Mapping[str, ModelPrice] | None = None) -> None:
        self._prices = dict(prices or {})
        self._warned: set[str] = set()
        self._log = get_logger("llm.pricing")

    @classmethod
    def default(cls) -> PriceTable:
        return cls(DEFAULT_PRICES)

    def with_prices(self, prices: Mapping[str, ModelPrice]) -> PriceTable:
        """Return a new table with ``prices`` added or overriding existing entries."""
        return PriceTable({**self._prices, **prices})

    def price_for(self, model: str) -> ModelPrice | None:
        matches = [prefix for prefix in self._prices if model.startswith(prefix)]
        return self._prices[max(matches, key=len)] if matches else None

    def cost(self, model: str, usage: Usage, *, free: bool = False) -> float:
        """Dollar cost of ``usage`` on ``model``. ``free=True`` skips lookup (local models)."""
        if free:
            return 0.0
        price = self.price_for(model)
        if price is None:
            if model not in self._warned:
                self._warned.add(model)
                self._log.warning("llm.price.unknown", model=model)
            return 0.0
        return round(
            (
                usage.input_tokens * price.input_per_mtok
                + usage.output_tokens * price.output_per_mtok
            )
            / PER_MILLION,
            8,
        )
