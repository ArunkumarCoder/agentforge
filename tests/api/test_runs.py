"""Run history through the HTTP API: runs are saved, listed, shown and summarised."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentforge.api import create_app
from agentforge.config import ApiSettings, Settings, StorageSettings
from agentforge.storage import RunStore, StorageError
from tests.api.test_app import BrokenEcho, factory_of


def make(store: RunStore | None = None, *, key: str | None = None, **kw: Any) -> TestClient:
    settings = Settings(_env_file=None, api=ApiSettings(api_key=key), **kw)  # type: ignore[call-arg]
    return TestClient(create_app(settings, store=store))


@pytest.fixture
def store() -> RunStore:
    return RunStore("sqlite://")


def test_runs_are_saved_and_can_be_read_back(store: RunStore) -> None:
    http = make(store)
    sent = http.post("/agents/echo/run", json={"text": "hi", "upper": True})
    run_id = sent.headers["x-run-id"]
    record = http.get(f"/runs/{run_id}").json()
    assert record["run_id"] == run_id
    assert record["status"] == "succeeded"
    assert record["source"] == "api"
    assert record["input"] == {"text": "hi", "upper": True}
    assert record["output"] == {"text": "HI", "length": 2}


def test_failed_runs_are_saved_too(store: RunStore) -> None:
    http = make(store)
    run_id = http.post("/agents/echo/run", json={"text": ""}).headers["x-run-id"]
    record = http.get(f"/runs/{run_id}").json()
    assert record["status"] == "failed"
    assert record["error_kind"] == "input_validation"
    boom = TestClient(
        create_app(Settings(_env_file=None), agent_factory=factory_of(BrokenEcho()), store=store)
    )  # type: ignore[call-arg]
    run_id = boom.post("/agents/echo/run", json={"text": "a"}).headers["x-run-id"]
    assert http.get(f"/runs/{run_id}").json()["error_message"] == "boom"


def test_list_filters_and_pages(store: RunStore) -> None:
    http = make(store)
    for text in ("a", "b", "c"):
        http.post("/agents/echo/run", json={"text": text})
    http.post("/agents/echo/run", json={"text": ""})
    everything = http.get("/runs").json()
    assert everything["total"] == 4
    assert "input" not in everything["runs"][0]  # summaries are small
    assert http.get("/runs", params={"status": "failed"}).json()["total"] == 1
    assert http.get("/runs", params={"agent": "nope"}).json()["total"] == 0
    page = http.get("/runs", params={"limit": 2, "offset": 2}).json()
    assert (page["limit"], page["offset"], len(page["runs"])) == (2, 2, 2)
    # newest first
    assert everything["runs"][0]["status"] == "failed"


def test_list_validates_its_parameters(store: RunStore) -> None:
    http = make(store)
    assert http.get("/runs", params={"limit": 0}).status_code == 422
    assert http.get("/runs", params={"limit": 500}).status_code == 422
    assert http.get("/runs", params={"offset": -1}).status_code == 422
    assert http.get("/runs", params={"status": "weird"}).status_code == 422


def test_stats_are_not_mistaken_for_a_run_id(store: RunStore) -> None:
    http = make(store)
    http.post("/agents/echo/run", json={"text": "a"})
    http.post("/agents/echo/run", json={"text": ""})
    (stat,) = http.get("/runs/stats").json()
    assert (stat["agent"], stat["runs"], stat["failed"]) == ("echo", 2, 1)
    assert stat["success_rate"] == 0.5
    assert http.get("/runs/stats", params={"days": 1}).json()[0]["runs"] == 2
    assert http.get("/runs/stats", params={"agent": "other"}).json() == []
    assert http.get("/runs/stats", params={"days": 0}).status_code == 422


def test_unknown_run_is_404(store: RunStore) -> None:
    response = make(store).get("/runs/missing")
    assert response.status_code == 404
    assert "no run with id 'missing'" in response.json()["detail"]


def test_without_storage_runs_work_but_history_is_503() -> None:
    http = make()
    assert http.post("/agents/echo/run", json={"text": "a"}).status_code == 200
    for path in ("/runs", "/runs/stats", "/runs/abc"):
        response = http.get(path)
        assert response.status_code == 503
        assert "AGENTFORGE_STORAGE__URL" in response.json()["detail"]


def test_the_history_routes_need_the_api_key(store: RunStore) -> None:
    http = make(store, key="secret")
    for path in ("/runs", "/runs/stats", "/runs/abc"):
        assert http.get(path).status_code == 401
    good = {"Authorization": "Bearer secret"}
    assert http.get("/runs", headers=good).status_code == 200


def test_a_database_failure_never_fails_the_run(store: RunStore, logs: Any) -> None:
    class Broken(RunStore):
        def save(self, *args: Any, **kwargs: Any) -> None:
            raise StorageError("disk full")

    http = make(Broken("sqlite://"))
    response = http.post("/agents/echo/run", json={"text": "hi"})
    assert response.status_code == 200
    assert response.json()["output"]["text"] == "hi"
    assert "runs.save_failed" in logs.events()


def test_a_database_failure_while_reading_is_503(store: RunStore) -> None:
    http = make(store)
    store.close()  # in-memory: the tables are gone
    response = http.get("/runs")
    assert response.status_code == 503
    assert "cannot list runs" in response.json()["detail"]


def test_the_store_is_opened_from_the_settings_and_closed_at_shutdown(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'runs.db'}"
    settings = Settings(_env_file=None, storage=StorageSettings(url=url))  # type: ignore[call-arg]
    with TestClient(create_app(settings)) as http:
        run_id = http.post("/agents/echo/run", json={"text": "hi"}).headers["x-run-id"]
        assert http.get(f"/runs/{run_id}").status_code == 200
    reopened = RunStore(url)  # the row was written to the file
    assert reopened.get(run_id) is not None
    reopened.close()


def test_a_bad_database_url_stops_the_app_at_startup() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, storage=StorageSettings(url="postgresql://u:p@127.0.0.1:1/none")
    )
    with pytest.raises(StorageError):
        create_app(settings)


def test_openapi_lists_the_history_routes() -> None:
    paths = make().get("/openapi.json").json()["paths"]
    assert {"/runs", "/runs/stats", "/runs/{run_id}"} <= set(paths)
