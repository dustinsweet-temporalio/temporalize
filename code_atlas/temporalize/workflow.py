"""
TemporalizeWorkflow: one Temporalize run (workflow id tz-<run>), screens 5 to 7.

    wait for the index and the Temporal report (the sync-<index> workflow)
    -> analyze scope: recommendation, systems, variants
    -> wait for scope_confirmed (the user picks the process, focus and criteria: screens 3-5)
    -> draft the plan and the design -> wait for plan_approved (the user, screen 6),
       revising the design for each design_edit_requested on the way
    -> prepare the workspace -> generate -> checks (fix, re-check)
    -> review passes, at most MAX_REVIEW_PASSES (fix between passes)
    -> scenarios: side by side, resilience, replay, smoke (fix and re-run on failure)
    -> results

The user can close the browser at any point: the run waits in the workflow, and a
worker restart resumes it from history. Decisions are written to the run document
by the API before the signal is sent, so the signals carry no data.

Deterministic: imports only the standard library, temporalio and the payload modules.
"""
import asyncio

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporalize import constants as C
    from temporalize.models import MarkInput, RunInput, RunState, StepInput, StepResult


def error_text(e: BaseException) -> str:
    cause = e
    while isinstance(cause, ActivityError) and cause.cause is not None:
        cause = cause.cause
    text = cause.message if isinstance(cause, ApplicationError) else (str(cause) or type(cause).__name__)
    return text[:C.ERROR_CHARS]


@workflow.defn(name=C.WORKFLOW_NAME)
class TemporalizeWorkflow:
    def __init__(self) -> None:
        self._phase = "STARTING"
        self._scope = False
        self._approved = False
        self._edits = 0          # design edit requests signalled
        self._edits_done = 0     # handled

    @workflow.signal(name=C.SCOPE_SIGNAL)
    def scope_confirmed(self) -> None:
        self._scope = True

    @workflow.signal(name=C.PLAN_SIGNAL)
    def plan_approved(self) -> None:
        self._approved = True

    @workflow.signal(name=C.DESIGN_EDIT_SIGNAL)
    def design_edit_requested(self) -> None:
        self._edits += 1

    @workflow.query(name=C.PROGRESS_QUERY)
    def progress(self) -> str:
        return self._phase

    async def _step(self, name: str, inp, timeout, retry=C.LOCAL_RETRY, heartbeat: bool = False) -> StepResult:
        return await workflow.execute_activity(
            name, inp, result_type=StepResult, start_to_close_timeout=timeout, retry_policy=retry,
            heartbeat_timeout=C.HEARTBEAT_TIMEOUT if heartbeat else None,
        )

    async def _agent(self, name: str, inp: StepInput) -> StepResult:
        """
        The coding agent as a series of short activities: each runs a few turns from the saved
        conversation and says whether the agent is done, so a lost worker costs at most one chunk.
        """
        r = StepResult()
        for _ in range(C.MAX_AGENT_CHUNKS):
            r = await self._step(name, inp, C.AGENT_CHUNK_TIMEOUT, C.LLM_RETRY, heartbeat=True)
            if r.ok:
                break
        return r

    @workflow.run
    async def run(self, inp: RunInput) -> str:
        rid = inp.run_id
        try:
            done = RunState()
            if inp.resume:
                done = await workflow.execute_activity(C.RUN_STATE, RunInput(rid), result_type=RunState,
                                                       start_to_close_timeout=C.LOCAL_TIMEOUT, retry_policy=C.LOCAL_RETRY)
                self._scope = self._scope or done.scope_confirmed
                self._approved = self._approved or done.approved
            if not done.scoped:
                self._phase = "INDEX"
                await self._step(C.WAIT_FOR_INDEX, RunInput(rid), C.INDEX_TIMEOUT, heartbeat=True)
                self._phase = "SCOPE"
                await self._step(C.ANALYZE_SCOPE, RunInput(rid), C.MODEL_STEP_TIMEOUT, C.LLM_RETRY, heartbeat=True)
            self._phase = "WAIT_SCOPE"
            await workflow.wait_condition(lambda: self._scope)

            if not done.planned:
                self._phase = "PLAN"
                await self._step(C.DRAFT_PLAN, RunInput(rid), C.MODEL_STEP_TIMEOUT, C.LLM_RETRY, heartbeat=True)
            self._phase = "WAIT_APPROVAL"
            while True:
                await workflow.wait_condition(lambda: self._approved or self._edits > self._edits_done)
                if self._approved:
                    break
                self._edits_done = self._edits   # requests made while revising are folded into the next one
                self._phase = "REVISE_DESIGN"
                try:
                    await self._step(C.REVISE_DESIGN, RunInput(rid), C.MODEL_STEP_TIMEOUT, C.LLM_RETRY, heartbeat=True)
                except ActivityError as e:   # a failed edit leaves the design as it was; the run goes on
                    await workflow.execute_activity(C.MARK_DESIGN_EDIT, MarkInput(rid, "failed", error_text(e)),
                                                    start_to_close_timeout=C.LOCAL_TIMEOUT, retry_policy=C.LOCAL_RETRY)
                self._phase = "WAIT_APPROVAL"

            self._phase = "GENERATE"
            await self._step(C.PREPARE_WORKSPACE, RunInput(rid), C.LOCAL_TIMEOUT)
            await self._agent(C.GENERATE_CODE, StepInput(rid))

            self._phase = "CHECKS"
            for attempt in range(C.MAX_FIX_ATTEMPTS + 1):
                r = await self._step(C.RUN_CHECKS, StepInput(rid, attempt), C.CHECKS_TIMEOUT, heartbeat=True)
                if r.ok or attempt == C.MAX_FIX_ATTEMPTS:
                    break
                await self._agent(C.FIX_CODE, StepInput(rid, attempt, "checks"))

            self._phase = "REVIEW"
            for n in range(1, C.MAX_REVIEW_PASSES + 1):
                r = await self._step(C.REVIEW_CODE, StepInput(rid, n), C.MODEL_STEP_TIMEOUT, C.LLM_RETRY, True)
                if r.count == 0 or n == C.MAX_REVIEW_PASSES:
                    break  # clean, or the loop limit: what is left is listed in the results
                await self._agent(C.FIX_CODE, StepInput(rid, n, "review"))

            self._phase = "SCENARIOS"
            fixed = False
            for scenario in C.SCENARIOS:
                for attempt in range(C.MAX_FIX_ATTEMPTS + 1):
                    r = await self._step(C.RUN_SCENARIO, StepInput(rid, attempt, scenario=scenario),
                                         C.SCENARIO_TIMEOUT, heartbeat=True)
                    if r.ok or attempt == C.MAX_FIX_ATTEMPTS:
                        break
                    fixed = True
                    await self._agent(C.FIX_CODE, StepInput(rid, attempt, "scenario", scenario))
            if fixed:
                # A fix may have changed what earlier scenarios proved: run them all once more, as evidence.
                for scenario in C.SCENARIOS:
                    await self._step(C.RUN_SCENARIO, StepInput(rid, 99, scenario=scenario), C.SCENARIO_TIMEOUT,
                                     heartbeat=True)

            self._phase = "RESULTS"
            await self._step(C.FINALIZE_RESULTS, RunInput(rid), C.MODEL_STEP_TIMEOUT, heartbeat=True)
            self._phase = "DONE"
            return "done"
        except asyncio.CancelledError:
            await self._mark(rid, "stopped", "Stopped.")
            raise
        except ActivityError as e:
            await self._mark(rid, "failed", error_text(e))
            raise ApplicationError(error_text(e), type="TZ_FAILED", non_retryable=True) from e

    async def _mark(self, rid: str, status: str, error: str) -> None:
        await workflow.execute_activity(C.MARK_RUN, MarkInput(rid, status, error),
                                        start_to_close_timeout=C.LOCAL_TIMEOUT, retry_policy=C.LOCAL_RETRY)
