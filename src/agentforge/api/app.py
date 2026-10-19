"""FastAPI application.

    GET  /health                 liveness, no auth
    GET  /agents                 every agent: name, version, description, tags
    GET  /agents/{name}          one agent with its input and output JSON Schemas
    POST /agents/{name}/run      body = the agent's input (a JSON object); returns the RunResult
    GET  /runs                   saved runs, newest first (?agent=&status=&limit=&offset=)
    GET  /runs/stats             per-agent totals: runs, success rate, tokens, cost (?agent=&days=)
    GET  /runs/{run_id}          one saved run with its input and output

The ``/runs`` routes need run storage (``AGENTFORGE_STORAGE__URL``); without it they answer 503
and runs are simply not saved. A run is saved after it finishes, whatever its status; if the
database fails the run still returns normally and a warning is logged.

``POST .../run`` always answers with the full ``RunResult``. The HTTP status says how it
went: 200 succeeded, 422 the input was invalid, 502 the model's answer never passed the
agent's checks, 504 timeout, 500 any other failure. The ``X-Run-Id`` header carries the run id.

Safety: agents run the code you give them (tests, profile scripts), so this is not a public
service. By default it listens on loopback only, refuses ``repo_path`` (code is sent in
``files``), caps the body size and the number of concurrent runs, and, if an API key is
configured, requires it as a Bearer token on everything except ``/health``.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agentforge import __version__
from agentforge.config import ApiSettings, Settings, get_settings
from agentforge.core.agent import BaseAgent
from agentforge.core.errors import AgentForgeError
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import UnknownAgentError, registry
from agentforge.core.schemas import AgentCard, ErrorKind, RunStatus
from agentforge.llm import LLMProvider, create_provider
from agentforge.storage import AgentStats, RunPage, RunRecord, RunStore, StorageError, open_store

type AgentFactory = Callable[[str], BaseAgent[Any, Any]]

_STATUS_FOR_ERROR = {
    ErrorKind.INPUT_VALIDATION: 422,
    ErrorKind.OUTPUT_VALIDATION: 502,
    ErrorKind.TIMEOUT: 504,
    ErrorKind.EXECUTION: 500,
}


class Health(BaseModel):
    status: str
    version: str
    agents: int


class AgentSummary(BaseModel):
    name: str
    version: str
    description: str
    tags: list[str]


def default_factory(settings: Settings) -> AgentFactory:
    """Build agents with one shared LLM provider, created on first use."""
    provider: LLMProvider | None = None

    def make(name: str) -> BaseAgent[Any, Any]:
        nonlocal provider
        cls = registry.get(name)
        if not issubclass(cls, LLMAgent):
            return cls()
        if provider is None:
            provider = create_provider(settings)
        return cls(llm=provider)

    return make


def create_app(
    settings: Settings | None = None,
    *,
    agent_factory: AgentFactory | None = None,
    store: RunStore | None = None,
) -> FastAPI:
    """Build the application. Tests pass their own settings, agent factory and run store.

    Without an explicit ``store`` the one in ``settings.storage`` is opened here, so a bad
    database URL stops the server at startup (``StorageError``) instead of on the first run.
    """
    settings = settings or get_settings()
    api = settings.api
    registry.discover()
    factory = agent_factory or default_factory(settings)
    slots = asyncio.Semaphore(api.max_concurrent_runs)
    owned = store is None
    runs_db = store if store is not None else open_store(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if owned and runs_db is not None:
            runs_db.close()

    app = FastAPI(
        title="AgentForge",
        version=__version__,
        description="Run AgentForge's agents over HTTP. Agents execute the code you send "
        "them, so keep this service private.",
        lifespan=lifespan,
    )

    def require_store() -> RunStore:
        if runs_db is None:
            raise HTTPException(
                status_code=503,
                detail="run storage is not configured (set AGENTFORGE_STORAGE__URL)",
            )
        return runs_db

    def require_key(request: Request) -> None:
        if api.api_key is None:
            return
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        expected = api.api_key.get_secret_value()
        if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip(), expected):
            raise HTTPException(
                status_code=401,
                detail="missing or wrong API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    protected = [Depends(require_key)]

    @app.get("/health", tags=["service"])
    def health() -> Health:
        return Health(status="ok", version=__version__, agents=len(registry))

    @app.get("/agents", tags=["agents"], dependencies=protected)
    def list_agents() -> list[AgentSummary]:
        return [
            AgentSummary(name=c.name, version=c.version, description=c.description, tags=c.tags)
            for c in registry.cards()
        ]

    @app.get("/agents/{name}", tags=["agents"], dependencies=protected)
    def get_agent(name: str) -> AgentCard:
        try:
            return registry.get(name).card()
        except UnknownAgentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post(
        "/agents/{name}/run",
        tags=["agents"],
        dependencies=protected,
        response_model=None,
        responses={
            200: {"description": "RunResult: the agent succeeded"},
            422: {"description": "RunResult or error: the input was invalid"},
            502: {"description": "RunResult: the answer never passed the agent's checks"},
            504: {"description": "RunResult: the agent timed out"},
        },
    )
    async def run_agent(name: str, request: Request) -> JSONResponse:
        payload = await _read_object(request, api)
        _check_repo_path(payload, api)
        try:
            agent = factory(name)
        except UnknownAgentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except AgentForgeError as exc:  # e.g. the LLM provider is not configured
            raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from None
        async with slots:
            result = await agent.arun(payload)
        if runs_db is not None:
            await asyncio.to_thread(runs_db.try_save, result, input=payload, source="api")
        code = 200 if result.error is None else _STATUS_FOR_ERROR[result.error.kind]
        return JSONResponse(
            status_code=code,
            content=result.model_dump(mode="json", serialize_as_any=True),
            headers={"X-Run-Id": result.run_id},
        )

    @app.get("/runs", tags=["runs"], dependencies=protected)
    def list_runs(
        agent: str | None = None,
        status: RunStatus | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RunPage:
        return _storage_call(
            lambda: require_store().list_runs(
                agent=agent, status=status, limit=limit, offset=offset
            )
        )

    # Declared before /runs/{run_id} so that "stats" is not read as a run id.
    @app.get("/runs/stats", tags=["runs"], dependencies=protected)
    def run_stats(
        agent: str | None = None, days: Annotated[int | None, Query(ge=1, le=3650)] = None
    ) -> list[AgentStats]:
        since = timedelta(days=days) if days is not None else None
        return _storage_call(lambda: require_store().stats(agent=agent, since=since))

    @app.get("/runs/{run_id}", tags=["runs"], dependencies=protected)
    def get_run(run_id: str) -> RunRecord:
        record = _storage_call(lambda: require_store().get(run_id))
        if record is None:
            raise HTTPException(status_code=404, detail=f"no run with id '{run_id}'")
        return record

    return app


def _storage_call[T](call: Callable[[], T]) -> T:
    """Turn a database failure into a 503 instead of a stack trace."""
    try:
        return call()
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


async def _read_object(request: Request, api: ApiSettings) -> dict[str, Any]:
    body = await request.body()
    if len(body) > api.max_body_bytes:
        raise HTTPException(
            status_code=413, detail=f"body is larger than {api.max_body_bytes} bytes"
        )
    try:
        payload = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"body is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    return payload


def _check_repo_path(payload: dict[str, Any], api: ApiSettings) -> None:
    """``repo_path`` makes the server read its own disk: off unless a root folder is set."""
    repo = payload.get("repo_path")
    if repo is None:
        return
    if not api.allow_repo_paths or api.repo_root is None:
        raise HTTPException(
            status_code=403,
            detail="repo_path is disabled on this server; send the code in 'files'",
        )
    if not isinstance(repo, str):
        raise HTTPException(status_code=422, detail="repo_path must be a string")
    root = api.repo_root.resolve()
    resolved = (root / repo).resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=403, detail="repo_path is outside the allowed folder")
    payload["repo_path"] = str(resolved)
