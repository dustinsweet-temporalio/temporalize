"""
BusinessImpactWorkflow on the time-skipping test server: with the real activities over
the fake store and model (end to end), and with mock activities (failure, cancellation,
progress, the one-run-at-a-time policy).
"""
import asyncio
import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

from impact import constants as C
from impact.activities import FORMAT_ERROR, ImpactActivities, _scrub
from impact.models import ImpactInput, ImpactProgress, MarkInput, PrepareResult, ProcessRef, ProcessResult, SynthesizeInput, SynthesizeResult
from impact.worker import build_worker
from impact.workflow import BusinessImpactWorkflow
from semantic_ingress.report.impact.context import ImpactContext
from semantic_ingress.report.impact.mapping import load_mapping
from semantic_ingress.report.impact.service import new_request
from tests.impact.conftest import ROOT, fx

SECRET = "Acme Payroll loses 7 batches a month; CFO Jane Roe escalated it."


@pytest.fixture
def queue():
    return f"impact-{uuid.uuid4().hex[:8]}"


def mapping():
    return load_mapping(ROOT / "knowledge" / "business-impact.yaml")


async def start(env, queue, request_id="r1", repo_id="demo", wid=None):
    return await env.client.start_workflow(
        BusinessImpactWorkflow.run, ImpactInput(repo_id=repo_id, request_id=request_id),
        id=wid or f"impact-{uuid.uuid4().hex[:8]}", task_queue=queue,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL)


# --- end to end: real activities, fake store and model --------------------------------------

async def test_a_run_end_to_end_with_the_real_activities(impact_env, queue):
    store, llm = fx.FakeStore(fx.report_doc(fx.standard_processes())), fx.FakeLLM()
    request = new_request(store, "demo", ImpactContext(text=SECRET), False, "temporal")
    worker, executor = build_worker(impact_env.client, ImpactActivities(store, llm, mapping()), task_queue=queue, slots=4)
    try:
        async with worker:
            handle = await start(impact_env, queue, request["_id"])
            outcome = await handle.result()
            progress = await handle.query(BusinessImpactWorkflow.progress)
            history = await handle.fetch_history()
    finally:
        executor.shutdown(wait=False)
    assert outcome.status == "ok" and outcome.processes == 2 and outcome.calls == 3
    assert progress.phase == "DONE" and progress.done == progress.total == 4 and progress.failed == 0
    assert {s.name for s in progress.processes} == {"Order payment", "Nightly export", "Report email", "Lookup"}
    assert store.requests[request["_id"]]["status"] == "ok"
    assert store.impact["status"] == "ok" and [p["name"] for p in store.impact["data"]["processes"]] == [
        "Order payment", "Nightly export"]
    # Only ids, keys, names and counts are in the history: never the customer context.
    blob = history.to_json()
    assert "Acme Payroll" not in blob and "Jane Roe" not in blob and "loses 7 batches" not in blob


async def test_model_format_errors_are_scrubbed_before_they_reach_history():
    from pydantic import BaseModel, ValidationError

    class M(BaseModel):
        x: int

    try:
        M.model_validate({"x": SECRET})
    except ValidationError as e:
        assert "Acme Payroll" in str(e)  # pydantic quotes the input it rejected
        assert str(_scrub(e)) == FORMAT_ERROR
    from semantic_ingress.core.llm_client import LLMTransientError
    assert str(_scrub(LLMTransientError(f"Invalid model response: {SECRET}"))) == FORMAT_ERROR


# --- mock activities ---------------------------------------------------------------------

class Mocks:
    """Stand-ins for the four activities; `translate` decides each process's fate."""

    def __init__(self, keys=("a", "b", "c"), translate=None):
        self.keys = list(keys)
        self.translate = translate
        self.marked: list[MarkInput] = []
        self.synthesized: list[SynthesizeInput] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def activities(self):
        mock = self

        @activity.defn(name=C.PREPARE_IMPACT)
        async def prepare(inp: ImpactInput) -> PrepareResult:
            if inp.request_id == "missing":
                raise ApplicationError("request missing", type=C.IMPACT_INVALID, non_retryable=True)
            return PrepareResult(keys=mock.keys, names={k: f"Process {k}" for k in mock.keys})

        @activity.defn(name=C.TRANSLATE_PROCESS)
        async def translate(inp: ProcessRef) -> ProcessResult:
            mock.started.set()
            if mock.translate:
                return await mock.translate(inp)
            return ProcessResult(key=inp.key, status="ok", calls=1, cost_usd=0.01)

        @activity.defn(name=C.SYNTHESIZE_IMPACT)
        async def synthesize(inp: SynthesizeInput) -> SynthesizeResult:
            mock.synthesized.append(inp)
            return SynthesizeResult(processes=len(mock.keys) - len(inp.failures), calls=inp.calls + 1,
                                    cost_usd=inp.cost_usd)

        @activity.defn(name=C.MARK_IMPACT_STATUS)
        async def mark(inp: MarkInput) -> None:
            mock.marked.append(inp)

        return [prepare, translate, synthesize, mark]


def mock_worker(env, queue, mocks):
    return Worker(env.client, task_queue=queue, workflows=[BusinessImpactWorkflow], activities=mocks.activities())


async def test_one_failing_process_does_not_fail_the_run(impact_env, queue):
    async def translate(inp):
        if inp.key == "b":
            raise ApplicationError("model refused", type=C.LLM_PERMANENT, non_retryable=True)
        return ProcessResult(key=inp.key, status="ok", calls=1, cost_usd=0.01)

    mocks = Mocks(translate=translate)
    async with mock_worker(impact_env, queue, mocks):
        handle = await start(impact_env, queue)
        outcome = await handle.result()
        progress = await handle.query(BusinessImpactWorkflow.progress)
    assert outcome.status == "ok" and outcome.processes == 2
    assert mocks.synthesized[0].failures == {"b": "model refused"} and mocks.synthesized[0].calls == 2
    assert progress.failed == 1 and {s.key: s.status for s in progress.processes} == {"a": "ok", "b": "failed", "c": "ok"}
    assert mocks.marked == []


async def test_progress_query_while_processes_run(impact_env, queue):
    mocks = Mocks()

    async def translate(inp):
        await mocks.release.wait()
        return ProcessResult(key=inp.key, status="ok")

    mocks.translate = translate
    async with mock_worker(impact_env, queue, mocks):
        handle = await start(impact_env, queue, request_id="req-42")
        await asyncio.wait_for(mocks.started.wait(), 10)
        p: ImpactProgress = await handle.query(BusinessImpactWorkflow.progress)
        assert (p.request_id, p.phase, p.total, p.done) == ("req-42", "PROCESSES", 3, 0)
        assert [s.name for s in p.processes] == ["Process a", "Process b", "Process c"]
        mocks.release.set()
        await handle.result()


async def test_cancellation_marks_the_request_cancelled(impact_env, queue):
    mocks = Mocks()

    async def translate(inp):
        while True:  # heartbeat until canceled, as the real activity does
            activity.heartbeat()
            await asyncio.sleep(0.1)

    mocks.translate = translate
    async with mock_worker(impact_env, queue, mocks):
        handle = await start(impact_env, queue)
        await asyncio.wait_for(mocks.started.wait(), 10)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        assert (await handle.describe()).status == WorkflowExecutionStatus.CANCELED
    assert [(m.status, m.request_id) for m in mocks.marked] == [("cancelled", "r1")] and mocks.synthesized == []


async def test_a_failing_prepare_marks_the_request_failed(impact_env, queue):
    mocks = Mocks()
    async with mock_worker(impact_env, queue, mocks):
        handle = await start(impact_env, queue, request_id="missing")
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert [(m.status, m.error) for m in mocks.marked] == [("failed", "request missing")]


async def test_a_second_start_while_running_fails(impact_env, queue):
    mocks = Mocks()

    async def translate(inp):
        await mocks.release.wait()
        return ProcessResult(key=inp.key, status="ok")

    mocks.translate = translate
    wid = C.workflow_id(f"demo{uuid.uuid4().hex[:6]}")
    async with mock_worker(impact_env, queue, mocks):
        first = await start(impact_env, queue, wid=wid)
        with pytest.raises(WorkflowAlreadyStartedError):
            await start(impact_env, queue, request_id="r2", wid=wid)
        mocks.release.set()
        await first.result()
        again = await start(impact_env, queue, request_id="r3", wid=wid)  # free once the first closed
        await again.result()
