"""Run the Data Analysis agent's golden cases against a real model. Opt-in: `make test-live`.

Uses the provider configured in .env. Needs pandas and matplotlib (`uv sync`). Prints a score
card; the test passes when at least 4 of the 5 cases pass.
"""

import pytest

from agentforge.agents.data_analysis import DataAnalysisAgent
from agentforge.agents.data_analysis.scoring import load_cases, score
from agentforge.config import LLMProvider, load_settings
from agentforge.llm import create_provider

pytestmark = pytest.mark.llm


async def test_data_analysis_golden_cases() -> None:
    settings = load_settings()
    if settings.llm.provider is LLMProvider.FAKE:
        pytest.skip("set AGENTFORGE_LLM__PROVIDER (and a key) in .env to run live evals")
    agent = DataAnalysisAgent(create_provider(settings))
    passed = 0
    print()
    for case in load_cases():
        result = await agent.arun(case.analysis_input())
        if not result.ok or result.output is None:
            print(f"  ERROR {case.name}: {result.error}")
            continue
        card = score(case, result.output)
        passed += card.passed
        failed = [f"{c.name} ({c.detail})" for c in card.checks if not c.passed]
        print(
            f"  {'PASS' if card.passed else 'FAIL'} {case.name:<16} score={card.score:.2f} "
            f"findings={len(result.output.findings)} charts={len(result.output.chart_files)} "
            f"tokens={result.usage.total_tokens} cost=${result.usage.cost_usd:.4f} "
            f"{'; '.join(failed)}"
        )
    assert passed >= 4, f"only {passed}/5 golden cases passed"
