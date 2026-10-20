"""
Activities of the business-impact workflow: the request steps of
semantic_ingress.report.impact.service, the same ones the in-process runner uses.

Synchronous (the Anthropic and MongoDB clients block), on the worker's thread pool.
Each reads the request and the report from MongoDB by id and writes its result
there, so a retry is harmless. Errors are classified as for indexing (the
indexer's translated_errors: permanent model errors are non-retryable, rate
limits retried with their retry-after hint); a missing request or a report
rebuilt mid-run is IMPACT_INVALID. Messages that could quote a model answer
(which may echo customer context) are replaced, because failures are kept in
workflow history. The context itself is never logged.
"""
import json
from typing import Callable

import structlog
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from impact import constants as C
from impact.models import (
    ImpactInput,
    MarkInput,
    PrepareResult,
    ProcessRef,
    ProcessResult,
    SynthesizeInput,
    SynthesizeResult,
)

logger = structlog.get_logger()

FORMAT_ERROR = "The model's answer did not match the expected format."
MAX_TOKENS_ERROR = ("The model's answer hit max_tokens before it was complete; raise INDEX_MAX_TOKENS or lower "
                    "INDEX_EFFORT if this repeats.")


def _invalid(message: str) -> ApplicationError:
    return ApplicationError(message[:C.ERROR_CHARS], type=C.IMPACT_INVALID, non_retryable=True)


def _scrub(e: Exception) -> Exception:
    """A model-format error without the model's text (validation errors quote the input)."""
    from semantic_ingress.core.llm_client import LLMTransientError

    if isinstance(e, (ValidationError, json.JSONDecodeError)) or "Invalid model response" in str(e):
        return LLMTransientError(FORMAT_ERROR)
    if "hit max_tokens" in str(e):
        return LLMTransientError(MAX_TOKENS_ERROR)
    return e


def _run(fn: Callable, *args, heartbeat: bool = False):
    """Call a service step with heartbeats (long steps) and classified, scrubbed errors."""
    from semantic_ingress.report.impact.service import RequestError
    from semantic_ingress.report.impact.translate import ImpactUnavailable
    from semantic_ingress.temporal.activities import heartbeating, translated_errors

    def call():
        try:
            return fn(*args)
        except (RequestError, ImpactUnavailable) as e:
            raise _invalid(str(e)) from None
        except ApplicationError:
            raise
        except Exception as e:
            scrubbed = _scrub(e)
            if scrubbed is e:
                raise
            raise scrubbed from None

    with translated_errors():
        if not heartbeat:
            return call()
        with heartbeating(C.HEARTBEAT_INTERVAL_SECONDS):
            return call()


class ImpactActivities:
    def __init__(self, store, llm, mapping) -> None:
        self.store = store
        self.llm = llm          # LLMClient(..., internal_retries=False): the retry policy owns retries
        self.mapping = mapping

    def all(self) -> list[Callable]:
        return [self.prepare_impact, self.translate_process, self.synthesize_impact, self.mark_impact_status]

    @activity.defn(name=C.PREPARE_IMPACT)
    def prepare_impact(self, inp: ImpactInput) -> PrepareResult:
        from semantic_ingress.report.impact.service import prepare_request

        keys = _run(prepare_request, self.store, inp.repo_id, inp.request_id)
        request = self.store.get_impact_request(inp.repo_id, inp.request_id) or {}
        logger.info("impact_prepared", request_id=inp.request_id, processes=len(keys))
        return PrepareResult(keys=keys, names=dict(request.get("names") or {}))

    @activity.defn(name=C.TRANSLATE_PROCESS)
    def translate_process(self, inp: ProcessRef) -> ProcessResult:
        from semantic_ingress.report.impact.service import translate_request_process

        out = _run(translate_request_process, self.llm, self.store, inp.repo_id, inp.request_id, inp.key,
                   self.mapping, heartbeat=True)
        return ProcessResult(key=inp.key, status=out.impact["status"], calls=out.calls,
                             cost_usd=out.usage.total_cost_usd)

    @activity.defn(name=C.SYNTHESIZE_IMPACT)
    def synthesize_impact(self, inp: SynthesizeInput) -> SynthesizeResult:
        from semantic_ingress.report.impact.service import finish_request, record_process_failure

        def go():
            for key, error in inp.failures.items():
                record_process_failure(self.store, inp.repo_id, inp.request_id, key, error)
            return finish_request(self.llm, self.store, inp.repo_id, inp.request_id, self.mapping,
                                  usage_usd=inp.cost_usd, calls=inp.calls)

        result = _run(go, heartbeat=True)
        return SynthesizeResult(processes=len(result.data["processes"]), calls=result.calls,
                                cost_usd=result.data["cost_usd"])

    @activity.defn(name=C.MARK_IMPACT_STATUS)
    def mark_impact_status(self, inp: MarkInput) -> None:
        from semantic_ingress.report.impact.service import fail_request

        _run(fail_request, self.store, inp.repo_id, inp.request_id, inp.error or inp.status, inp.status)
