"""
BusinessImpactWorkflow: one business-impact run (workflow id impact-<index>).

    PREPARE -> PROCESSES (up to PROCESS_CONCURRENCY at a time) -> SYNTHESIZE -> DONE

The input is the repository and request ids; activities read the request (with
the customer context) and the stored Temporal report from MongoDB and write their
results there, so neither the context nor any model answer enters the history.
A process whose activity fails for good is recorded as failed and the run goes on.
On cancellation the request is marked cancelled, then the workflow ends cancelled;
a run that fails for good is marked failed first.

Deterministic: imports only the standard library, temporalio and the payload
modules; logs through workflow.logger.
"""
import asyncio

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from impact import constants as C
    from impact.models import (
        ImpactInput,
        ImpactOutcome,
        ImpactProgress,
        MarkInput,
        PrepareResult,
        ProcessRef,
        ProcessResult,
        ProcessState,
        SynthesizeInput,
        SynthesizeResult,
    )


def error_text(e: BaseException) -> str:
    """The message of the underlying failure of an activity error."""
    cause = e
    while isinstance(cause, ActivityError) and cause.cause is not None:
        cause = cause.cause
    text = cause.message if isinstance(cause, ApplicationError) else (str(cause) or type(cause).__name__)
    return text[:C.ERROR_CHARS]


@workflow.defn(name=C.WORKFLOW_NAME)
class BusinessImpactWorkflow:
    def __init__(self) -> None:
        self._progress = ImpactProgress()

    @workflow.query(name=C.PROGRESS_QUERY)
    def progress(self) -> ImpactProgress:
        return self._progress

    @workflow.run
    async def run(self, inp: ImpactInput) -> ImpactOutcome:
        p = self._progress
        p.request_id = inp.request_id
        try:
            p.phase = "PREPARE"
            prep: PrepareResult = await workflow.execute_activity(
                C.PREPARE_IMPACT, inp, result_type=PrepareResult,
                start_to_close_timeout=C.PREPARE_TIMEOUT, retry_policy=C.LOCAL_RETRY,
            )
            p.phase, p.total = "PROCESSES", len(prep.keys)
            p.processes = [ProcessState(key=k, name=prep.names.get(k, "")) for k in prep.keys]
            states = {s.key: s for s in p.processes}
            failures: dict[str, str] = {}
            calls, cost = 0, 0.0
            slots = asyncio.Semaphore(C.PROCESS_CONCURRENCY)

            async def one(key: str) -> None:
                nonlocal calls, cost
                async with slots:
                    try:
                        r: ProcessResult = await workflow.execute_activity(
                            C.TRANSLATE_PROCESS, ProcessRef(inp.repo_id, inp.request_id, key), result_type=ProcessResult,
                            start_to_close_timeout=C.PROCESS_TIMEOUT, heartbeat_timeout=C.HEARTBEAT_TIMEOUT,
                            retry_policy=C.IMPACT_RETRY,
                        )
                    except ActivityError as e:  # one bad process does not fail the run
                        failures[key] = error_text(e)
                        states[key].status = "failed"
                        p.failed += 1
                        workflow.logger.warning("impact %s: process %s failed", inp.request_id, key)
                    else:
                        states[key].status = r.status
                        calls, cost = calls + r.calls, cost + r.cost_usd
                        if r.status == "failed":
                            p.failed += 1
                    p.done += 1

            await asyncio.gather(*(one(k) for k in prep.keys))

            p.phase = "SYNTHESIZE"
            out: SynthesizeResult = await workflow.execute_activity(
                C.SYNTHESIZE_IMPACT, SynthesizeInput(inp.repo_id, inp.request_id, calls, cost, failures),
                result_type=SynthesizeResult, start_to_close_timeout=C.SYNTHESIZE_TIMEOUT,
                heartbeat_timeout=C.HEARTBEAT_TIMEOUT, retry_policy=C.IMPACT_RETRY,
            )
            p.phase = "DONE"
            return ImpactOutcome(status="ok", processes=out.processes, calls=out.calls, cost_usd=out.cost_usd)
        except asyncio.CancelledError:
            workflow.logger.info("impact %s: cancelled", inp.request_id)
            await self._mark(inp, "cancelled", "Cancelled.")
            raise
        except ActivityError as e:  # prepare or synthesize failed for good
            await self._mark(inp, "failed", error_text(e))
            raise ApplicationError(error_text(e), type="IMPACT_FAILED", non_retryable=True) from e

    async def _mark(self, inp: ImpactInput, status: str, error: str) -> None:
        await workflow.execute_activity(
            C.MARK_IMPACT_STATUS, MarkInput(inp.repo_id, inp.request_id, status, error),
            start_to_close_timeout=C.MARK_TIMEOUT, retry_policy=C.LOCAL_RETRY,
        )
