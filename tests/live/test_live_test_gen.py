"""Run the Test Generation agent's golden cases against a real model. Opt-in: `make test-live`.

Uses the provider configured in .env (AGENTFORGE_LLM__PROVIDER / __MODEL and the API key).
Prints a score card; the test passes when at least 4 of the 5 cases pass.
"""

import pytest

from agentforge.agents.test_gen import TestGenAgent
from agentforge.agents.test_gen.scoring import load_cases, score
from agentforge.config import LLMProvider, load_settings
from agentforge.llm import create_provider

pytestmark = pytest.mark.llm


async def test_test_gen_golden_cases() -> None:
    settings = load_settings()
    if settings.llm.provider is LLMProvider.FAKE:
        pytest.skip("set AGENTFORGE_LLM__PROVIDER (and a key) in .env to run live evals")
    agent = TestGenAgent(create_provider(settings))
    passed = 0
    print()
    for case in load_cases():
        result = await agent.arun(case.gen_input())
        if not result.ok or result.output is None:
            print(f"  ERROR {case.name}: {result.error}")
            continue
        card = await score(case, result.output)
        passed += card.passed
        failed = [f"{c.name} ({c.detail})" for c in card.checks if not c.passed]
        run = result.output.run
        print(
            f"  {'PASS' if card.passed else 'FAIL'} {case.name:<14} "
            f"score={card.score:.2f} attempts={result.output.attempts} "
            f"coverage={run.coverage if run else None}% "
            f"tokens={result.usage.total_tokens} cost=${result.usage.cost_usd:.4f} "
            f"{'; '.join(failed)}"
        )
    assert passed >= 4, f"only {passed}/5 golden cases passed"
