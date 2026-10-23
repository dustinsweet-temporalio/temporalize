"""
TemporalizeWorkflow on the time-skipping test server, with mock activities: the two
waits for the user, the review loop's limit, fix-and-rerun of a failing scenario with a
final evidence pass, and failure marking.
"""
import asyncio
import uuid
from collections import Counter

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporalize import constants as C
from temporalize.models import MarkInput, RunInput, StepInput, StepResult
from temporalize.workflow import TemporalizeWorkflow


@pytest_asyncio.fixture(scope="session")
async def tz_env():
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()


class Mocks:
    """Activities by name that record their calls; behavior set per test."""

    def __init__(self, review_counts=(2, 1, 1), failing_scenario: str | None = None, fail_at: str | None = None,
                 generate_chunks: int = 1, revise_fails: bool = False):
        self.calls: list[tuple] = []
        self.review_counts = list(review_counts)
        self.failing_scenario = failing_scenario
        self.fail_at = fail_at
        self.marked: list[MarkInput] = []
        self.generate_chunks = generate_chunks
        self.revise_fails = revise_fails
        self.edit_marked: list[MarkInput] = []

    def _record(self, name, inp):
        self.calls.append((name, getattr(inp, "n", None), getattr(inp, "kind", None), getattr(inp, "scenario", None)))
        if self.fail_at == name:
            from temporalio.exceptions import ApplicationError

            raise ApplicationError("boom", type=C.TZ_INVALID, non_retryable=True)

    def all(self):
        def simple(name):
            @activity.defn(name=name)
            async def fn(inp: RunInput) -> StepResult:
                self._record(name, inp)
                return StepResult()
            return fn

        @activity.defn(name=C.GENERATE_CODE)
        async def generate(inp: StepInput) -> StepResult:
            self._record(C.GENERATE_CODE, inp)
            done = sum(1 for c in self.calls if c[0] == C.GENERATE_CODE) >= self.generate_chunks
            return StepResult(ok=done)

        @activity.defn(name=C.RUN_CHECKS)
        async def checks(inp: StepInput) -> StepResult:
            self._record(C.RUN_CHECKS, inp)
            return StepResult(ok=inp.n >= 1)   # fails once, passes after the fix

        @activity.defn(name=C.REVIEW_CODE)
        async def review(inp: StepInput) -> StepResult:
            self._record(C.REVIEW_CODE, inp)
            return StepResult(count=self.review_counts[inp.n - 1])

        @activity.defn(name=C.FIX_CODE)
        async def fix(inp: StepInput) -> StepResult:
            self._record(C.FIX_CODE, inp)
            return StepResult()

        @activity.defn(name=C.RUN_SCENARIO)
        async def scenario(inp: StepInput) -> StepResult:
            self._record(C.RUN_SCENARIO, inp)
            return StepResult(ok=not (inp.scenario == self.failing_scenario and inp.n == 0))

        @activity.defn(name=C.MARK_RUN)
        async def mark(inp: MarkInput) -> StepResult:
            self.marked.append(inp)
            return StepResult()

        @activity.defn(name=C.REVISE_DESIGN)
        async def revise(inp: RunInput) -> StepResult:
            self._record(C.REVISE_DESIGN, inp)
            if self.revise_fails:
                from temporalio.exceptions import ApplicationError

                raise ApplicationError("model refused", type=C.LLM_PERMANENT, non_retryable=True)
            return StepResult()

        @activity.defn(name=C.MARK_DESIGN_EDIT)
        async def mark_edit(inp: MarkInput) -> StepResult:
            self.edit_marked.append(inp)
            return StepResult()

        return [simple(C.WAIT_FOR_INDEX), simple(C.ANALYZE_SCOPE), simple(C.DRAFT_PLAN),
                simple(C.PREPARE_WORKSPACE), simple(C.FINALIZE_RESULTS), generate, checks, review, fix, scenario, mark,
                revise, mark_edit]


async def _run(env, mocks: Mocks, signal: bool = True):
    queue = f"tz-{uuid.uuid4().hex[:8]}"
    async with Worker(env.client, task_queue=queue, workflows=[TemporalizeWorkflow], activities=mocks.all()):
        handle = await env.client.start_workflow(TemporalizeWorkflow.run, RunInput("r-0001"),
                                                 id=f"tz-test-{uuid.uuid4().hex[:8]}", task_queue=queue)
        for _ in range(100):
            if await handle.query(TemporalizeWorkflow.progress) == "WAIT_SCOPE":
                break
            await asyncio.sleep(0.05)
        assert await handle.query(TemporalizeWorkflow.progress) == "WAIT_SCOPE"
        names = [c[0] for c in mocks.calls]
        assert C.DRAFT_PLAN not in names  # nothing happens until the user confirms the scope
        if not signal:
            return handle
        await handle.signal(TemporalizeWorkflow.scope_confirmed)
        for _ in range(100):
            if await handle.query(TemporalizeWorkflow.progress) == "WAIT_APPROVAL":
                break
            await asyncio.sleep(0.05)
        assert C.PREPARE_WORKSPACE not in [c[0] for c in mocks.calls]
        await handle.signal(TemporalizeWorkflow.plan_approved)
        with env.auto_time_skipping_disabled():  # no timers to skip; real time avoids heartbeat-timeout jumps
            return await handle.result(), handle


async def test_the_run_waits_for_the_user_and_stops_review_at_the_limit(tz_env):
    mocks = Mocks(review_counts=(2, 1, 1))
    result, handle = await _run(tz_env, mocks)
    assert result == "done"
    reviews = [c for c in mocks.calls if c[0] == C.REVIEW_CODE]
    assert [c[1] for c in reviews] == [1, 2, 3]                     # three passes, then stop
    review_fixes = [c for c in mocks.calls if c[0] == C.FIX_CODE and c[2] == "review"]
    assert [c[1] for c in review_fixes] == [1, 2]                   # no fix after the last pass
    check_runs = [c for c in mocks.calls if c[0] == C.RUN_CHECKS]
    assert [c[1] for c in check_runs] == [0, 1]
    assert Counter(c[3] for c in mocks.calls if c[0] == C.RUN_SCENARIO) == Counter(C.SCENARIOS)
    assert mocks.calls[-1][0] == C.FINALIZE_RESULTS


async def test_a_clean_review_ends_the_loop_early(tz_env):
    mocks = Mocks(review_counts=(0, 5, 5))
    await _run(tz_env, mocks)
    assert [c[1] for c in mocks.calls if c[0] == C.REVIEW_CODE] == [1]


async def test_a_failing_scenario_is_fixed_and_every_scenario_runs_again(tz_env):
    mocks = Mocks(review_counts=(0,), failing_scenario="resilience")
    await _run(tz_env, mocks)
    runs = [(c[3], c[1]) for c in mocks.calls if c[0] == C.RUN_SCENARIO]
    assert ("resilience", 0) in runs and ("resilience", 1) in runs
    assert [s for s, n in runs if n == 99] == C.SCENARIOS           # the final evidence pass
    assert any(c[0] == C.FIX_CODE and c[2] == "scenario" and c[3] == "resilience" for c in mocks.calls)


async def test_a_failed_step_marks_the_run_failed(tz_env):
    mocks = Mocks(fail_at=C.ANALYZE_SCOPE)
    queue = f"tz-{uuid.uuid4().hex[:8]}"
    async with Worker(tz_env.client, task_queue=queue, workflows=[TemporalizeWorkflow], activities=mocks.all()):
        handle = await tz_env.client.start_workflow(TemporalizeWorkflow.run, RunInput("r-0002"),
                                                    id=f"tz-test-{uuid.uuid4().hex[:8]}", task_queue=queue)
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert [m.status for m in mocks.marked] == ["failed"] and "boom" in mocks.marked[0].error


async def test_cancel_while_waiting_marks_the_run_stopped(tz_env):
    mocks = Mocks()
    queue = f"tz-{uuid.uuid4().hex[:8]}"
    async with Worker(tz_env.client, task_queue=queue, workflows=[TemporalizeWorkflow], activities=mocks.all()):
        handle = await tz_env.client.start_workflow(TemporalizeWorkflow.run, RunInput("r-0003"),
                                                    id=f"tz-test-{uuid.uuid4().hex[:8]}", task_queue=queue)
        for _ in range(100):
            if await handle.query(TemporalizeWorkflow.progress) == "WAIT_SCOPE":
                break
            await asyncio.sleep(0.05)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert [m.status for m in mocks.marked] == ["stopped"]


async def test_generation_runs_in_chunks_until_the_agent_is_done(tz_env):
    mocks = Mocks(review_counts=(0,), generate_chunks=3)
    await _run(tz_env, mocks)
    assert sum(1 for c in mocks.calls if c[0] == C.GENERATE_CODE) == 3
    assert [c[0] for c in mocks.calls].index(C.RUN_CHECKS) > max(
        i for i, c in enumerate(mocks.calls) if c[0] == C.GENERATE_CODE)


async def _at(handle, phase: str) -> None:
    for _ in range(200):
        if await handle.query(TemporalizeWorkflow.progress) == phase:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached {phase}")


async def test_design_edits_are_applied_before_approval(tz_env):
    mocks = Mocks(review_counts=(0,))
    queue = f"tz-{uuid.uuid4().hex[:8]}"
    async with Worker(tz_env.client, task_queue=queue, workflows=[TemporalizeWorkflow], activities=mocks.all()):
        handle = await tz_env.client.start_workflow(TemporalizeWorkflow.run, RunInput("r-0004"),
                                                    id=f"tz-test-{uuid.uuid4().hex[:8]}", task_queue=queue)
        await _at(handle, "WAIT_SCOPE")
        await handle.signal(TemporalizeWorkflow.scope_confirmed)
        await _at(handle, "WAIT_APPROVAL")
        await handle.signal(TemporalizeWorkflow.design_edit_requested)
        for _ in range(200):
            if sum(1 for c in mocks.calls if c[0] == C.REVISE_DESIGN) == 1:
                break
            await asyncio.sleep(0.05)
        await _at(handle, "WAIT_APPROVAL")
        await handle.signal(TemporalizeWorkflow.design_edit_requested)
        for _ in range(200):
            if sum(1 for c in mocks.calls if c[0] == C.REVISE_DESIGN) == 2:
                break
            await asyncio.sleep(0.05)
        await _at(handle, "WAIT_APPROVAL")
        assert C.PREPARE_WORKSPACE not in [c[0] for c in mocks.calls]   # edits never start the build
        await handle.signal(TemporalizeWorkflow.plan_approved)
        with tz_env.auto_time_skipping_disabled():
            assert await handle.result() == "done"
    names = [c[0] for c in mocks.calls]
    assert names.count(C.REVISE_DESIGN) == 2 and names.index(C.REVISE_DESIGN) < names.index(C.PREPARE_WORKSPACE)


async def test_a_failed_design_edit_leaves_the_run_waiting(tz_env):
    mocks = Mocks(review_counts=(0,), revise_fails=True)
    queue = f"tz-{uuid.uuid4().hex[:8]}"
    async with Worker(tz_env.client, task_queue=queue, workflows=[TemporalizeWorkflow], activities=mocks.all()):
        handle = await tz_env.client.start_workflow(TemporalizeWorkflow.run, RunInput("r-0005"),
                                                    id=f"tz-test-{uuid.uuid4().hex[:8]}", task_queue=queue)
        await _at(handle, "WAIT_SCOPE")
        await handle.signal(TemporalizeWorkflow.scope_confirmed)
        await _at(handle, "WAIT_APPROVAL")
        await handle.signal(TemporalizeWorkflow.design_edit_requested)
        for _ in range(200):
            if mocks.edit_marked:
                break
            await asyncio.sleep(0.05)
        await _at(handle, "WAIT_APPROVAL")
        assert [m.status for m in mocks.edit_marked] == ["failed"] and "model refused" in mocks.edit_marked[0].error
        assert mocks.marked == []                                        # the run itself did not fail
        await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await handle.result()
