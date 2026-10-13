"""Run the Security Audit agent's golden cases against a real model. Opt-in: `make test-live`.

Uses the provider configured in .env (AGENTFORGE_LLM__PROVIDER / __MODEL and the API key).
Prints a score card; the test passes when at least 4 of the 5 cases pass.
"""

import pytest

from agentforge.agents.security_audit import SecurityAuditAgent
from agentforge.agents.security_audit.scoring import load_cases, score
from agentforge.config import LLMProvider, load_settings
from agentforge.llm import create_provider

pytestmark = pytest.mark.llm


async def test_security_audit_golden_cases() -> None:
    settings = load_settings()
    if settings.llm.provider is LLMProvider.FAKE:
        pytest.skip("set AGENTFORGE_LLM__PROVIDER (and a key) in .env to run live evals")
    agent = SecurityAuditAgent(create_provider(settings))
    passed = 0
    print()
    for case in load_cases():
        result = await agent.arun(case.audit_input())
        if not result.ok or result.output is None:
            print(f"  ERROR {case.name}: {result.error}")
            continue
        card = score(case, result.output)
        passed += card.passed
        failed = [f"{c.name} ({c.detail})" for c in card.checks if not c.passed]
        print(
            f"  {'PASS' if card.passed else 'FAIL'} {case.name:<13} "
            f"score={card.score:.2f} open={len(result.output.findings)} "
            f"dismissed={len(result.output.dismissed)} tokens={result.usage.total_tokens} "
            f"cost=${result.usage.cost_usd:.4f} {'; '.join(failed)}"
        )
    assert passed >= 4, f"only {passed}/5 golden cases passed"
