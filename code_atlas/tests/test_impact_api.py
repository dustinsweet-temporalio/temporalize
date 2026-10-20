"""
The business-impact API (impact_api.py) with the in-process runner, a fake store and
a fake model: prerequisites, one run at a time, status, stale detection and the views.
"""
import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import impact_api  # noqa: E402

# The domain tests' fakes (semantic_ingress/tests), loaded by path: both test trees are named `tests`.
_spec = importlib.util.spec_from_file_location(
    "impact_fixtures", Path(__file__).resolve().parents[2] / "semantic_ingress" / "tests" / "impact_fixtures.py")
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)


class GatedLLM(fx.FakeLLM):
    """Holds every call until `gate` is set, so a run stays open."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()

    def generate(self, *args, **kwargs):
        assert self.gate.wait(10), "the test never released the model"
        return super().generate(*args, **kwargs)


@pytest.fixture
def api(monkeypatch):
    store, llm = fx.FakeStore(fx.report_doc(fx.standard_processes())), GatedLLM()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(impact_api, "_store", store)
    monkeypatch.setattr(impact_api, "llm", lambda: llm)
    monkeypatch.setattr(impact_api, "model_id", lambda: llm.config.model_id)
    monkeypatch.setattr(impact_api, "_runner", impact_api.InProcessRunner())
    app = FastAPI()
    app.include_router(impact_api.router)
    with TestClient(app) as client:
        yield client, store, llm


def wait_until_closed(client, repo="demo", seconds=5.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        body = client.get(f"/api/impact/{repo}/status").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("the run did not finish")


CONTEXT = {"text": "About 4 failed orders a month. Each costs around 250 in refunds.", "currency": "EUR"}


def test_a_run_from_start_to_the_views(api):
    client, store, llm = api
    res = client.post("/api/impact/demo", json={"context": CONTEXT})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["runner"] == "inprocess" and body["workflow_id"] == "inprocess-demo"
    status = client.get("/api/impact/demo/status").json()
    assert status["status"] == "running" and status["request_id"] == body["request_id"]
    llm.gate.set()
    status = wait_until_closed(client)
    assert status["status"] == "ok" and status["done"] == status["total"] == 4 and status["failed"] == 0

    impact = client.get("/api/impact/demo").json()
    assert impact["status"] == "ok" and not impact["stale"] and impact["request_id"] == body["request_id"]
    assert [p["name"] for p in impact["data"]["processes"]] == ["Order payment", "Nightly export"]
    assert impact["data"]["context"]["currency"] == "EUR"
    for view, heading in (("exec", "# Business impact: demo"), ("champion", "# Business impact for engineering"),
                          ("discovery", "# Discovery sheet: demo")):
        md = client.get(f"/api/impact/demo/markdown?view={view}")
        assert md.status_code == 200 and md.text.startswith(heading)
    assert client.get("/api/impact/demo/markdown?view=nope").status_code == 422


def test_a_second_start_while_running_is_409(api):
    client, store, llm = api
    assert client.post("/api/impact/demo", json={}).status_code == 200
    again = client.post("/api/impact/demo", json={})
    assert again.status_code == 409 and again.json()["progress"]["phase"] in ("PREPARE", "PROCESSES")
    assert len(store.requests) == 1  # the refused start recorded nothing
    llm.gate.set()
    assert wait_until_closed(client)["status"] == "ok"
    assert client.post("/api/impact/demo", json={}).status_code == 200  # free again once it closed
    wait_until_closed(client)


def test_no_report_is_422(api):
    client, store, _ = api
    store.doc = None
    res = client.post("/api/impact/demo", json={})
    assert res.status_code == 422 and res.json()["error"].startswith("Build the Temporal report first.")
    assert client.post("/api/impact/demo/check", json={}).status_code == 422
    assert store.requests == {}


def test_a_full_status_report_is_422(api):
    client, store, _ = api
    store.doc = fx.report_doc(None, status="full")
    res = client.post("/api/impact/demo", json={})
    assert res.status_code == 422 and "already uses Temporal" in res.json()["error"]


def test_stale_after_the_report_is_rebuilt(api):
    client, store, llm = api
    llm.gate.set()
    client.post("/api/impact/demo", json={})
    wait_until_closed(client)
    assert client.get("/api/impact/demo").json()["stale"] is False
    store.doc = fx.report_doc(fx.standard_processes(), generated_at="2026-10-22T08:00:00+00:00")
    assert client.get("/api/impact/demo").json()["stale"] is True
    store.doc = fx.report_doc(fx.standard_processes(), commit="fff0000aaa")
    assert client.get("/api/impact/demo").json()["stale"] is True


def test_check_counts_calls_cache_and_skips_without_calling_the_model(api):
    client, store, llm = api
    body = client.post("/api/impact/demo/check", json={}).json()
    assert body["will_call"] == ["Order payment", "Nightly export"] and body["cached"] == []
    assert {s["reason"] for s in body["skipped"]} == {"error", "not suitable", "no grounded consequence"}
    assert body["summary_calls"] == 1 and body["est_cost_usd"] > 0 and llm.calls == 0
    assert body["message"].startswith("2 processes will use the model")
    llm.gate.set()
    client.post("/api/impact/demo", json={})
    wait_until_closed(client)
    again = client.post("/api/impact/demo/check", json={}).json()
    assert again["will_call"] == [] and again["cached"] == ["Order payment", "Nightly export"]
    assert again["est_cost_usd"] == 0
    fresh = client.post("/api/impact/demo/check", json={"fresh": True}).json()
    assert len(fresh["will_call"]) == 2
    other = client.post("/api/impact/demo/check", json={"context": CONTEXT}).json()
    assert len(other["will_call"]) == 2  # new context: not cached


def test_cancel_stops_the_run(api):
    client, store, llm = api
    client.post("/api/impact/demo", json={})
    assert client.delete("/api/impact/demo").json() == {"cancelled": True}
    llm.gate.set()
    status = wait_until_closed(client)
    assert status["status"] == "cancelled" and store.impact is None
    assert client.delete("/api/impact/demo").status_code == 404


def test_status_and_impact_before_any_run(api):
    client, _, _ = api
    assert client.get("/api/impact/demo/status").json() == {"status": "none"}
    assert client.get("/api/impact/demo").status_code == 404
    assert client.get("/api/impact/demo/markdown").status_code == 404


def test_a_request_left_running_shows_as_interrupted(api):
    client, store, _ = api
    store.requests["r1"] = {"_id": "r1", "status": "running", "phase": "PROCESSES", "created_at": "2026-10-20",
                            "keys": [], "results": {}}
    body = client.get("/api/impact/demo/status").json()
    assert body["status"] == "interrupted" and "Generate again" in body["error"]


def test_bad_input_is_refused(api, monkeypatch):
    client, _, _ = api
    assert client.post("/api/impact/bad name!", json={}).status_code in (400, 404)
    assert client.post("/api/impact/demo", json={"context": {"text": "x" * 4001}}).status_code == 422
    assert client.post("/api/impact/demo", json={"context": {"nope": 1}}).status_code == 422
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    res = client.post("/api/impact/demo", json={})
    assert res.status_code == 503 and "ANTHROPIC_API_KEY" in res.json()["error"]
