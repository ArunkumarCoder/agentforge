"""Run the Documentation agent's golden cases against a real model. Opt-in: `make test-live`.

Uses the provider configured in .env (AGENTFORGE_LLM__PROVIDER / __MODEL and the API key).
Prints a score card; the test passes when at least 4 of the 5 cases pass.
"""

import pytest

from agentforge.agents.doc_gen import DocGenAgent
from agentforge.agents.doc_gen.scoring import load_cases, score
from agentforge.config import LLMProvider, load_settings
from agentforge.llm import create_provider

pytestmark = pytest.mark.llm


async def test_doc_gen_golden_cases() -> None:
    settings = load_settings()
    if settings.llm.provider is LLMProvider.FAKE:
        pytest.skip("set AGENTFORGE_LLM__PROVIDER (and a key) in .env to run live evals")
    agent = DocGenAgent(create_provider(settings))
    passed = 0
    print()
    for case in load_cases():
        result = await agent.arun(case.doc_input())
        if not result.ok or result.output is None:
            print(f"  ERROR {case.name}: {result.error}")
            continue
        card = await score(case, result.output)
        passed += card.passed
        failed = [f"{c.name} ({c.detail})" for c in card.checks if not c.passed]
        print(
            f"  {'PASS' if card.passed else 'FAIL'} {case.name:<18} "
            f"score={card.score:.2f} coverage={result.output.coverage_before:g}%"
            f"->{result.output.coverage_after:g}% tokens={result.usage.total_tokens} "
            f"cost=${result.usage.cost_usd:.4f} {'; '.join(failed)}"
        )
    assert passed >= 4, f"only {passed}/5 golden cases passed"
