"""
Temporalize validation harness. Written by Temporalize into temporal-poc/; do not edit.

validate.py (written by the generator) imports this module and calls `main()` with one
async function per scenario:

    import tz_harness as h

    async def side_by_side() -> dict: ...
    async def resilience() -> dict: ...
    async def replay() -> dict: ...
    async def smoke() -> dict: ...

    if __name__ == "__main__":
        h.main({"side_by_side": side_by_side, "resilience": resilience, "replay": replay, "smoke": smoke})

Each scenario returns a dict with any of: ok (bool), checks, criteria, workflow_ids, notes.

    checks:   [{"check": "Report rows", "original": "42", "temporal": "42",
                "result": "match" | "intended" | "new" | "mismatch", "detail": "..."}]
    criteria: [{"id": "<criterion id from PLAN.md>", "passed": bool, "evidence": "..."}]

`python validate.py <scenario> --out <file.json>` always writes that JSON, even when the
scenario raises (ok false, the traceback in notes). If a scenario omits ok, it is true
when no check is a mismatch and no criterion failed.

The Temporal target comes from TZ_TEMPORAL_ADDRESS, TZ_TEMPORAL_NAMESPACE and
TZ_TEMPORAL_API_KEY (an API key means Temporal Cloud, with TLS).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Coroutine, Sequence

POC = Path(__file__).resolve().parent
TESTDATA = POC / "testdata"
TZ = POC / ".tz"
STATE_FILE = TZ / "state.json"
CALLS_FILE = TZ / "calls.json"
ATTEMPTS_FILE = TZ / "attempts.json"

_lock = threading.RLock()


# ----------------------------------------------------------------------
# Test data and run identity
# ----------------------------------------------------------------------

def _meta() -> dict:
    path = TESTDATA / "failures.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def run_id() -> str:
    return os.environ.get("TZ_RUN_ID") or _meta().get("run_id") or "local"


def load_testdata(name: str) -> Any:
    """A test data file: parsed JSON for .json files, text otherwise."""
    path = TESTDATA / name
    text = path.read_text()
    return json.loads(text) if path.suffix == ".json" else text


def frozen_now() -> datetime:
    """The frozen 'now' of the test data (naive unless the value carries an offset)."""
    value = _meta().get("frozen_time")
    if not value:
        return datetime(2026, 1, 5, 7, 0, 0)  # a Monday morning
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def failures() -> list[dict]:
    """Injected failures: [{"boundary": "login", "times": 2, "detail": "..."}]."""
    return list(_meta().get("failures") or [])


# ----------------------------------------------------------------------
# State shared between scenarios (and between worker A and worker B)
# ----------------------------------------------------------------------

def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path: Path, value: Any) -> None:
    TZ.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str))
    tmp.replace(path)


def load_state() -> dict:
    with _lock:
        return _read(STATE_FILE, {})


def save_state(**fields: Any) -> dict:
    with _lock:
        state = _read(STATE_FILE, {})
        state.update(fields)
        _write(STATE_FILE, state)
        return state


def remember_workflows(scenario: str, workflow_ids: Sequence[str]) -> None:
    """Record the workflows a scenario ran, for the replay scenario."""
    with _lock:
        state = _read(STATE_FILE, {})
        state.setdefault("workflows", {})[scenario] = list(workflow_ids)
        _write(STATE_FILE, state)


class StubLog:
    """
    Calls made to stubbed boundaries, persisted in .tz/calls.json so counts survive a worker
    restart. Stubs call `StubLog.record("login", ...)` on every call. When TZ_STUB_DELAY is
    set (the restart helper sets it), each call sleeps that long, so a worker can be stopped
    mid-run.
    """

    @staticmethod
    def reset() -> None:
        with _lock:
            _write(CALLS_FILE, [])

    @staticmethod
    def record(boundary: str, **details: Any) -> None:
        with _lock:
            calls = _read(CALLS_FILE, [])
            calls.append({"boundary": boundary, "at": time.time(), **{k: str(v)[:200] for k, v in details.items()}})
            _write(CALLS_FILE, calls)
        delay = float(os.environ.get("TZ_STUB_DELAY") or 0)
        if delay:
            time.sleep(delay)

    @staticmethod
    def calls(boundary: str | None = None) -> list[dict]:
        with _lock:
            calls = _read(CALLS_FILE, [])
        return [c for c in calls if boundary is None or c["boundary"] == boundary]

    @staticmethod
    def counts() -> dict[str, int]:
        out: dict[str, int] = {}
        for c in StubLog.calls():
            out[c["boundary"]] = out.get(c["boundary"], 0) + 1
        return out


def enable_failures(on: bool = True) -> None:
    """Turn injected failures on (resilience) or off (side_by_side, smoke); resets the attempt counters."""
    os.environ["TZ_FAILURES"] = "1" if on else "0"
    with _lock:
        _write(ATTEMPTS_FILE, {})


def should_fail(boundary: str) -> bool:
    """
    True while `boundary` has failed fewer times than failures.json says. Stubs call it at the
    top of the call and raise when it is true, so a retried activity fails exactly N times.
    Off unless enable_failures(True) was called.
    """
    if os.environ.get("TZ_FAILURES") != "1":
        return False
    times = sum(int(f.get("times", 1)) for f in failures() if f.get("boundary") == boundary)
    with _lock:
        attempts = _read(ATTEMPTS_FILE, {})
        done = int(attempts.get(boundary, 0))
        if done >= times:
            return False
        attempts[boundary] = done + 1
        _write(ATTEMPTS_FILE, attempts)
        return True


def attempts(boundary: str) -> int:
    """How many injected failures `boundary` has produced so far."""
    with _lock:
        return int(_read(ATTEMPTS_FILE, {}).get(boundary, 0))


# ----------------------------------------------------------------------
# Temporal
# ----------------------------------------------------------------------

async def connect():
    from temporalio.client import Client

    address = os.environ.get("TZ_TEMPORAL_ADDRESS") or "localhost:7233"
    namespace = os.environ.get("TZ_TEMPORAL_NAMESPACE") or "default"
    api_key = os.environ.get("TZ_TEMPORAL_API_KEY") or ""
    kwargs: dict[str, Any] = {"namespace": namespace}
    if api_key:
        kwargs.update(api_key=api_key, tls=True)
    return await asyncio.wait_for(Client.connect(address, **kwargs), 30)


def task_queue(scenario: str) -> str:
    """A task queue of its own for each scenario run, so no stale worker picks up its tasks."""
    return f"tz-{run_id()}-{scenario}-{uuid.uuid4().hex[:6]}"


def workflow_id(scenario: str, unique: bool = True) -> str:
    """tz-<run>-<scenario>[-<random>]. The smoke run uses workflow_id("smoke", unique=False)."""
    base = f"tz-{run_id()}-{scenario}"
    return f"{base}-{uuid.uuid4().hex[:6]}" if unique else base


def _worker(client, queue: str, workflows: Sequence[type], activities: Sequence[Callable],
            graceful_seconds: float = 0.0):
    from temporalio.worker import Worker

    executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tz-activity")
    worker = Worker(client, task_queue=queue, workflows=list(workflows), activities=list(activities),
                    activity_executor=executor, max_concurrent_activities=16,
                    graceful_shutdown_timeout=timedelta(seconds=graceful_seconds))
    return worker, executor


@asynccontextmanager
async def run_worker(client, queue: str, workflows: Sequence[type], activities: Sequence[Callable]):
    """A worker for the duration of the block (sync activities run on a thread pool)."""
    worker, executor = _worker(client, queue, workflows, activities)
    try:
        async with worker:
            yield worker
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


async def completed_activities(handle) -> list[str]:
    """Names of the activities the workflow's history shows as completed, in order."""
    from temporalio.api.enums.v1 import EventType

    scheduled: dict[int, str] = {}
    done: list[str] = []
    async for event in handle.fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            scheduled[event.event_id] = event.activity_task_scheduled_event_attributes.activity_type.name
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            sid = event.activity_task_completed_event_attributes.scheduled_event_id
            done.append(scheduled.get(sid, "?"))
    return done


async def restart_scenario(client, workflows: Sequence[type], activities: Sequence[Callable],
                           workflow_run: Any, arg: Any = None, *, workflow_id: str | None = None,
                           after_activities: int = 1, stub_delay: float = 1.0,
                           timeout: float = 300.0) -> dict:
    """
    Start the workflow on worker A, stop worker A once `after_activities` activities have
    completed, then finish the run on worker B (a new worker, as after a crash or deploy).

    Stub calls are slowed by `stub_delay` seconds so the stop lands mid-run. Returns
    {"result", "workflow_id", "completed_before_restart": [activity names],
     "completed_total": [...], "calls_before_restart": {boundary: n}, "calls_total": {boundary: n},
     "restarted_mid_run": bool}.
    """
    queue = task_queue("restart")
    wid = workflow_id or globals()["workflow_id"]("restart")
    previous_delay = os.environ.get("TZ_STUB_DELAY")
    os.environ["TZ_STUB_DELAY"] = str(stub_delay)
    try:
        worker_a, executor_a = _worker(client, queue, workflows, activities, graceful_seconds=max(10.0, stub_delay * 5))
        run_a = asyncio.create_task(worker_a.run())
        args = [] if arg is None else [arg]
        handle = await client.start_workflow(workflow_run, *args, id=wid, task_queue=queue)
        deadline = time.monotonic() + timeout
        before: list[str] = []
        finished_early = False
        while time.monotonic() < deadline:
            before = await completed_activities(handle)
            if len(before) >= after_activities:
                break
            desc = await handle.describe()
            if desc.status is not None and desc.status.name != "RUNNING":
                finished_early = True
                break
            await asyncio.sleep(0.2)
        await worker_a.shutdown()
        await asyncio.gather(run_a, return_exceptions=True)
        executor_a.shutdown(wait=False, cancel_futures=True)
        before = await completed_activities(handle)
        calls_before = StubLog.counts()

        async with run_worker(client, queue, workflows, activities):
            result = await asyncio.wait_for(handle.result(), timeout)
        total = await completed_activities(handle)
        return {"result": result, "workflow_id": wid, "completed_before_restart": before,
                "completed_total": total, "calls_before_restart": calls_before, "calls_total": StubLog.counts(),
                "restarted_mid_run": not finished_early and len(total) > len(before)}
    finally:
        if previous_delay is None:
            os.environ.pop("TZ_STUB_DELAY", None)
        else:
            os.environ["TZ_STUB_DELAY"] = previous_delay


async def replay(client, workflow_ids: Sequence[str], workflows: Sequence[type]) -> list[dict]:
    """Replay each workflow's history against the current code: [{"workflow_id", "ok", "error"}]."""
    from temporalio.worker import Replayer

    replayer = Replayer(workflows=list(workflows))
    out = []
    for wid in workflow_ids:
        try:
            history = await client.get_workflow_handle(wid).fetch_history()
            await replayer.replay_workflow(history)
            out.append({"workflow_id": wid, "ok": True, "error": None})
        except Exception as e:
            out.append({"workflow_id": wid, "ok": False, "error": f"{type(e).__name__}: {e}"[:1000]})
    return out


def remembered_workflows() -> list[str]:
    """Workflow ids recorded by earlier scenarios (side_by_side, resilience, ...), for replay."""
    ids: list[str] = []
    for scenario_ids in (load_state().get("workflows") or {}).values():
        ids += [i for i in scenario_ids if i not in ids]
    return ids


# ----------------------------------------------------------------------
# Results and the CLI
# ----------------------------------------------------------------------

def check(name: str, original: Any, temporal: Any, result: str | None = None, detail: str = "") -> dict:
    """A comparison row; result defaults to match when the values are equal, mismatch otherwise."""
    if result is None:
        result = "match" if original == temporal else "mismatch"
    return {"check": name, "original": _show(original), "temporal": _show(temporal), "result": result,
            "detail": detail}


def criterion(cid: str, passed: bool, evidence: str) -> dict:
    return {"id": cid, "passed": bool(passed), "evidence": evidence}


def _show(value: Any) -> str:
    if isinstance(value, str):
        return value[:300]
    try:
        return json.dumps(value, default=str)[:300]
    except (TypeError, ValueError):
        return str(value)[:300]


def write_result(out_path: str | Path, ok: bool, checks: list | None = None, criteria: list | None = None,
                 workflow_ids: list | None = None, notes: str = "") -> dict:
    result = {"ok": bool(ok), "checks": checks or [], "criteria": criteria or [],
              "workflow_ids": workflow_ids or [], "notes": notes}
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str))
    return result


def main(scenarios: dict[str, Callable[[], Coroutine[Any, Any, dict]]]) -> None:
    parser = argparse.ArgumentParser(description="Temporalize validation")
    parser.add_argument("scenario", choices=sorted(scenarios))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out = args.out or str(TZ / f"{args.scenario}.json")
    try:
        r: dict = asyncio.run(scenarios[args.scenario]()) or {}
        checks: list = r.get("checks") or []
        criteria: list = r.get("criteria") or []
        ok = r.get("ok")
        if ok is None:
            ok = not any(c.get("result") == "mismatch" for c in checks) and all(c.get("passed") for c in criteria)
        if r.get("workflow_ids"):
            remember_workflows(args.scenario, r["workflow_ids"])
        result = write_result(out, ok, checks, criteria, r.get("workflow_ids"), r.get("notes", ""))
    except BaseException as e:  # always write the result file
        result = write_result(out, False, notes=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-4000:]}")
    print(json.dumps({"scenario": args.scenario, "ok": result["ok"], "checks": len(result["checks"]),
                      "criteria": len(result["criteria"])}))
    raise SystemExit(0 if result["ok"] else 1)
