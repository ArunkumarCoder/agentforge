"""The HTTP API, exercised in-process with FastAPI's test client. Offline: fake LLM, echo agent."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agentforge import __version__
from agentforge.agents.echo import EchoAgent, EchoInput, EchoOutput
from agentforge.agents.security_audit import SecurityAuditAgent
from agentforge.agents.security_audit.scoring import load_cases
from agentforge.api import create_app
from agentforge.api.app import default_factory
from agentforge.config import ApiSettings, Settings
from agentforge.core.agent import BaseAgent, RunContext
from agentforge.core.registry import UnknownAgentError
from agentforge.llm import FakeProvider
from tests.agents.secaudit_refs import reference_answer

WEB = next(c for c in load_cases() if c.name == "web_app")


def settings(**api: Any) -> Settings:
    return Settings(_env_file=None, api=ApiSettings(**api))  # type: ignore[call-arg]


def client(**api: Any) -> TestClient:
    return TestClient(create_app(settings(**api)))


class SlowEcho(EchoAgent):
    timeout_s = 0.05

    async def execute(self, data: EchoInput, ctx: RunContext) -> EchoOutput:
        await asyncio.sleep(1)
        return EchoOutput(text="x", length=1)


class BrokenEcho(EchoAgent):
    async def execute(self, data: EchoInput, ctx: RunContext) -> EchoOutput:
        raise RuntimeError("boom")


def factory_of(agent: BaseAgent[Any, Any]) -> Any:
    return lambda name: agent


# --------------------------------------------------------------------------- reading


def test_health_is_open_even_with_a_key() -> None:
    body = client(api_key="secret").get("/health").json()
    assert body == {"status": "ok", "version": __version__, "agents": body["agents"]}
    assert body["agents"] >= 11


def test_lists_every_agent_sorted() -> None:
    agents = client().get("/agents").json()
    names = [a["name"] for a in agents]
    assert names == sorted(names)
    assert {"echo", "code-review", "security-audit", "performance", "migration"} <= set(names)
    echo = next(a for a in agents if a["name"] == "echo")
    assert echo["tags"] == ["demo", "no-llm"]


def test_shows_one_agent_with_schemas() -> None:
    card = client().get("/agents/echo").json()
    assert card["name"] == "echo"
    assert "text" in card["input_schema"]["properties"]
    assert "length" in card["output_schema"]["properties"]


def test_unknown_agent_is_404() -> None:
    http = client()
    assert http.get("/agents/nope").status_code == 404
    response = http.post("/agents/nope/run", json={})
    assert response.status_code == 404
    assert "no agent named 'nope'" in response.json()["detail"]


def test_openapi_document_lists_the_routes() -> None:
    paths = client().get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/health",
        "/agents",
        "/agents/{name}",
        "/agents/{name}/run",
        "/runs",
        "/runs/stats",
        "/runs/{run_id}",
    }


# --------------------------------------------------------------------------- running


def test_runs_an_agent_and_returns_the_run_result() -> None:
    response = client().post("/agents/echo/run", json={"text": "hi", "upper": True})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["output"] == {"text": "HI", "length": 2}
    assert response.headers["x-run-id"] == body["run_id"]


def test_an_llm_agent_returns_its_full_output() -> None:
    answer = json.dumps(asyncio.run(reference_answer(WEB)))
    agent = SecurityAuditAgent(FakeProvider(script=[answer]))
    http = TestClient(create_app(settings(), agent_factory=factory_of(agent)))
    response = http.post("/agents/security-audit/run", json=WEB.input)
    assert response.status_code == 200
    output = response.json()["output"]
    assert output["risk"] == "high"  # fields of the agent's own output, not just the base class
    assert {f["category"] for f in output["findings"]} >= {"sql_injection", "path_traversal"}


def test_invalid_input_is_422_with_the_run_result() -> None:
    response = client().post("/agents/echo/run", json={"text": ""})
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["kind"] == "input_validation"


def test_an_answer_that_never_passes_the_checks_is_502() -> None:
    agent = SecurityAuditAgent(FakeProvider(script=["not json"] * 3))
    http = TestClient(create_app(settings(), agent_factory=factory_of(agent)))
    response = http.post("/agents/security-audit/run", json=WEB.input)
    assert response.status_code == 502
    assert response.json()["error"]["kind"] == "output_validation"


def test_timeout_is_504_and_other_failures_are_500() -> None:
    slow = TestClient(create_app(settings(), agent_factory=factory_of(SlowEcho())))
    assert slow.post("/agents/echo/run", json={"text": "a"}).status_code == 504
    broken = TestClient(create_app(settings(), agent_factory=factory_of(BrokenEcho())))
    response = broken.post("/agents/echo/run", json={"text": "a"})
    assert response.status_code == 500
    assert response.json()["error"]["message"] == "boom"


def test_body_must_be_a_json_object() -> None:
    http = client()
    assert http.post("/agents/echo/run", content=b"{nope").status_code == 422
    response = http.post("/agents/echo/run", json=["a"])
    assert response.status_code == 422
    assert "JSON object" in response.json()["detail"]
    empty = http.post("/agents/echo/run", content=b"")
    assert empty.status_code == 422  # echo needs "text"; the empty body was read as {}
    assert empty.json()["error"]["kind"] == "input_validation"


def test_large_bodies_are_refused() -> None:
    response = client(max_body_bytes=1000).post("/agents/echo/run", json={"text": "a" * 2000})
    assert response.status_code == 413


# --------------------------------------------------------------------------- repo_path


def test_repo_path_is_refused_by_default() -> None:
    response = client().post("/agents/migration/run", json={"repo_path": "."})
    assert response.status_code == 403
    assert "send the code in 'files'" in response.json()["detail"]


def test_repo_path_needs_a_root_folder_even_when_allowed() -> None:
    response = client(allow_repo_paths=True).post("/agents/migration/run", json={"repo_path": "."})
    assert response.status_code == 403


def test_repo_path_inside_the_root_is_allowed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    http = client(allow_repo_paths=True, repo_root=tmp_path)
    response = http.post("/agents/migration/run", json={"repo_path": "project"})
    assert response.status_code == 200
    assert response.json()["output"]["sites_before"] == []


@pytest.mark.parametrize("path", ["..", "../elsewhere", "/etc", "project/../../x"])
def test_repo_path_outside_the_root_is_refused(tmp_path: Path, path: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    http = client(allow_repo_paths=True, repo_root=root)
    response = http.post("/agents/migration/run", json={"repo_path": path})
    assert response.status_code == 403
    assert "outside the allowed folder" in response.json()["detail"]


def test_repo_path_must_be_a_string(tmp_path: Path) -> None:
    http = client(allow_repo_paths=True, repo_root=tmp_path)
    assert http.post("/agents/migration/run", json={"repo_path": 5}).status_code == 422


# --------------------------------------------------------------------------- auth


def test_a_configured_key_protects_everything_but_health() -> None:
    http = client(api_key="s3cret-key")
    for method, url in (("get", "/agents"), ("get", "/agents/echo"), ("post", "/agents/echo/run")):
        response = getattr(http, method)(url)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
    bad = {"Authorization": "Bearer nope"}
    assert http.get("/agents", headers=bad).status_code == 401
    assert http.get("/agents", headers={"Authorization": "Basic s3cret-key"}).status_code == 401
    good = {"Authorization": "Bearer s3cret-key"}
    assert http.get("/agents", headers=good).status_code == 200
    run = http.post("/agents/echo/run", json={"text": "hi"}, headers=good)
    assert run.status_code == 200


def test_no_key_configured_means_open() -> None:
    assert client().get("/agents").status_code == 200


# --------------------------------------------------------------------------- factory and limits


def test_default_factory_shares_one_provider_and_skips_it_for_plain_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[object] = []

    def fake_create(_settings: Settings) -> FakeProvider:
        provider = FakeProvider(script=[])
        made.append(provider)
        return provider

    monkeypatch.setattr("agentforge.api.app.create_provider", fake_create)
    make = default_factory(settings())
    assert isinstance(make("echo"), EchoAgent)
    assert made == []
    first, second = make("security-audit"), make("performance")
    assert len(made) == 1
    assert first.llm is second.llm  # type: ignore[attr-defined]
    with pytest.raises(UnknownAgentError):
        make("nope")


def test_an_unconfigured_provider_is_503() -> None:
    http = TestClient(
        create_app(Settings(_env_file=None, llm={"provider": "anthropic"}))  # type: ignore[call-arg]
    )
    response = http.post("/agents/security-audit/run", json={"files": {"a.py": "x = 1\n"}})
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY is not set" in response.json()["detail"]


async def test_concurrent_runs_are_limited() -> None:
    running = peak = 0

    class Counting(EchoAgent):
        async def execute(self, data: EchoInput, ctx: RunContext) -> EchoOutput:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return EchoOutput(text=data.text, length=len(data.text))

    app = create_app(settings(max_concurrent_runs=2), agent_factory=factory_of(Counting()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = await asyncio.gather(
            *(http.post("/agents/echo/run", json={"text": "a"}) for _ in range(6))
        )
    assert [r.status_code for r in responses] == [200] * 6
    assert peak == 2


def test_a_real_server_answers_over_a_socket() -> None:
    """Start uvicorn on a free port in a thread and call it like a real client would."""
    import threading
    import time

    import uvicorn

    config = uvicorn.Config(
        create_app(settings()), host="127.0.0.1", port=0, log_config=None, lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/health", timeout=5).json()["status"] == "ok"
        run = httpx.post(f"{base}/agents/echo/run", json={"text": "net"}, timeout=5)
        assert run.json()["output"]["text"] == "net"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
