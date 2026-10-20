"""
Business-impact runs on stored requests: the steps the in-process runner and the
Temporal workflow's activities share.

A request (impact_requests, one per Generate) holds the customer context, the
report it was pinned to, its status and the per-process results, so only ids
pass through a workflow. The result (reports, `{repo_id}::business_impact`) is
the latest good impact; a failed run keeps it and marks it failed, as the
Temporal report does (report/service.py).
"""
import threading
import uuid
from datetime import datetime, timezone

import structlog

from semantic_ingress.report.cache import ReportCache
from semantic_ingress.report.impact.context import ImpactContext
from semantic_ingress.report.impact.mapping import ImpactMapping
from semantic_ingress.report.impact.render import render
from semantic_ingress.report.impact.translate import (
    ImpactResult,
    ProcessInput,
    ProcessOutcome,
    check_report,
    failed_process,
    path_titles,
    prepare,
    source_info,
    synthesize,
    translate_process,
)

logger = structlog.get_logger()

RUNNING, OK, FAILED, CANCELLED = "running", "ok", "failed", "cancelled"
REPORT_CHANGED = "The Temporal report was rebuilt during this run. Generate the business impact again."


class RequestError(Exception):
    """The request cannot run (missing, or its report changed); retrying will not help."""


class RunCancelled(Exception):
    """The run was cancelled (in-process runner)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_request(store, repo_id: str, ctx: ImpactContext, fresh: bool, runner: str) -> dict:
    """Check the stored report and record a new request (raises ImpactUnavailable)."""
    doc = store.get_report(repo_id)
    check_report(doc)
    request = {
        "_id": uuid.uuid4().hex, "repo_id": repo_id, "status": RUNNING, "phase": "PREPARE", "error": None,
        "created_at": _now(), "context": ctx.model_dump(), "fresh": fresh, "runner": runner,
        "source_report": source_info(doc), "total": 0, "keys": [], "results": {},
    }
    store.put_impact_request(repo_id, request)
    return request


def _load(store, repo_id: str, request_id: str) -> tuple[dict, dict, ImpactContext]:
    request = store.get_impact_request(repo_id, request_id)
    if request is None:
        raise RequestError(f"Business-impact request {request_id} not found (it expires after a week).")
    doc = store.get_report(repo_id)
    if doc is None or source_info(doc) != request["source_report"]:
        raise RequestError(REPORT_CHANGED)
    return request, doc, ImpactContext.model_validate(request["context"])


def prepare_request(store, repo_id: str, request_id: str) -> list[str]:
    """The keys of the processes to translate; recorded on the request with the phase."""
    _, doc, _ = _load(store, repo_id, request_id)
    pins, _ = prepare(doc)
    keys = [p.key for p in pins]
    store.update_impact_request(repo_id, request_id, {"phase": "PROCESSES", "total": len(keys), "keys": keys})
    return keys


def _pin(doc: dict, key: str) -> ProcessInput:
    pins, _ = prepare(doc)
    for pin in pins:
        if pin.key == key:
            return pin
    raise RequestError(f"process {key} is not in the report")


def translate_request_process(llm, store, repo_id: str, request_id: str, key: str,
                              mapping: ImpactMapping) -> ProcessOutcome:
    """One process of a request; its result is recorded on the request (a repeat overwrites it)."""
    request, doc, ctx = _load(store, repo_id, request_id)
    cache = ReportCache(store, repo_id, request["fresh"])
    out = translate_process(llm, cache, mapping, ctx, _pin(doc, key), path_titles(doc))
    store.update_impact_request(repo_id, request_id, {f"results.{key}": out.impact})
    return out


def record_process_failure(store, repo_id: str, request_id: str, key: str, error: str) -> None:
    """A process whose translation failed for good, so the run can still finish."""
    _, doc, _ = _load(store, repo_id, request_id)
    store.update_impact_request(repo_id, request_id,
                                {f"results.{key}": failed_process(_pin(doc, key), path_titles(doc), error)})


def finish_request(llm, store, repo_id: str, request_id: str, mapping: ImpactMapping,
                   usage_usd: float = 0.0, calls: int = 0) -> ImpactResult:
    """Rank, merge and summarize the recorded results, store the impact, and mark the request done."""
    from semantic_ingress.core.llm_client import LLMUsage

    store.update_impact_request(repo_id, request_id, {"phase": "SYNTHESIZE"})
    request, doc, ctx = _load(store, repo_id, request_id)
    _, skipped = prepare(doc)
    results = request.get("results") or {}
    impacts = [results[k] for k in request["keys"] if k in results]
    cache = ReportCache(store, repo_id, request["fresh"])
    result = synthesize(llm, cache, mapping, ctx, doc, impacts, skipped,
                        LLMUsage(input_tokens=0, output_tokens=0), calls)
    result.data["cost_usd"] = round(result.data["cost_usd"] + usage_usd, 4)
    now = _now()
    store.put_business_impact(repo_id, {
        "repo_id": repo_id, "request_id": request_id, "status": OK, "error": None,
        "generated_at": now, "attempted_at": now, "source_report": result.data["source_report"],
        "context_hash": ctx.hash, "mapping_version": mapping.version, "cost_usd": result.data["cost_usd"],
        "data": result.data, "markdown": render(result.data),
    })
    store.update_impact_request(repo_id, request_id, {"status": OK, "phase": "DONE", "finished_at": now,
                                                      "cost_usd": result.data["cost_usd"]})
    logger.info("impact_stored", repo_id=repo_id, request_id=request_id, cost_usd=result.data["cost_usd"],
                calls=result.calls)
    return result


def fail_request(store, repo_id: str, request_id: str, error: str, status: str = FAILED) -> None:
    """Mark the request failed (or cancelled). A failure also marks the stored impact; its data is kept."""
    now = _now()
    store.update_impact_request(repo_id, request_id, {"status": status, "error": error[:1000], "finished_at": now})
    if status == FAILED:
        previous = store.get_business_impact(repo_id) or {"repo_id": repo_id, "data": None}
        store.put_business_impact(repo_id, {**previous, "status": FAILED, "error": error[:1000], "attempted_at": now,
                                            "request_id": request_id})
    logger.info("impact_request_closed", repo_id=repo_id, request_id=request_id, status=status)


def run_request(llm, store, repo_id: str, request_id: str, mapping: ImpactMapping,
                stop: threading.Event | None = None) -> ImpactResult:
    """The whole request in this process (the in-process runner); a failure is recorded, then raised."""
    from concurrent.futures import ThreadPoolExecutor

    from semantic_ingress.report.impact.translate import PROCESS_WORKERS

    def one(key: str) -> ProcessOutcome | None:
        if stop is not None and stop.is_set():
            return None
        try:
            return translate_request_process(llm, store, repo_id, request_id, key, mapping)
        except RequestError:
            raise
        except Exception as e:
            logger.error("impact_process_failed", request_id=request_id, process_key=key, error=str(e)[:300])
            record_process_failure(store, repo_id, request_id, key, str(e))
            return None

    try:
        keys = prepare_request(store, repo_id, request_id)
        with ThreadPoolExecutor(max_workers=PROCESS_WORKERS) as pool:
            outcomes = [o for o in pool.map(one, keys) if o is not None]
        if stop is not None and stop.is_set():
            raise RunCancelled()
        return finish_request(llm, store, repo_id, request_id, mapping,
                              usage_usd=sum(o.usage.total_cost_usd for o in outcomes),
                              calls=sum(o.calls for o in outcomes))
    except RunCancelled:
        fail_request(store, repo_id, request_id, "Cancelled.", status=CANCELLED)
        raise
    except Exception as e:
        fail_request(store, repo_id, request_id, str(e))
        raise
