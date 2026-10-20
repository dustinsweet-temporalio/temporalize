"""
Business impact from the web UI, on demand, from the stored Temporal report.

    POST   /api/impact/{repo_id}/check     what a run would do (no model calls): processes that would
                                           call the model, cached ones, skipped ones, and an estimate
    POST   /api/impact/{repo_id}           start a run: {context, fresh}; 409 while one is running
    GET    /api/impact/{repo_id}/status    the latest run: progress while it runs, then how it ended
    GET    /api/impact/{repo_id}           the stored impact, with `stale` when the report was rebuilt since
    GET    /api/impact/{repo_id}/markdown?view=exec|champion|discovery
    DELETE /api/impact/{repo_id}           cancel the running run

The customer context is written to MongoDB with the request; the run gets only the
repository and request ids, so the context never enters Temporal workflow history.
Runs go to BusinessImpactWorkflow on the `impact-worker` service, or, with
IMPACT_RUNNER=inprocess, to a thread in this app (a stopgap: a restart loses the run).
"""
import asyncio
import logging
import os
import re
import threading
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from semantic_ingress.config import LLMConfig
from semantic_ingress.core.llm_client import set_pricing
from semantic_ingress.report.impact.context import ImpactContext

from config import DB_PREFIX, IMPACT_RUNNER

logger = logging.getLogger("code_atlas.impact")
router = APIRouter()

REPO_PATTERN = r"^[A-Za-z0-9_-]+$"

set_pricing(LLMConfig())  # estimates at the configured INDEX_PRICE_* values


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class ImpactRunRequest(BaseModel):
    context: ImpactContext = Field(default_factory=ImpactContext)
    fresh: bool = Field(default=False, description="Skip cached answers (Regenerate); results are still cached")


# ----------------------------------------------------------------------
# Shared clients (created on first use)
# ----------------------------------------------------------------------

_store = None
_mapping = None


def store():
    """The indexer's Mongo store: the report, the impact, its requests and the cache."""
    global _store
    if _store is None:
        from semantic_ingress.config import MongoConfig
        from semantic_ingress.storage import MongoStore

        _store = MongoStore(MongoConfig(), DB_PREFIX)
    return _store


def mapping():
    global _mapping
    if _mapping is None:
        from semantic_ingress.report.impact.mapping import load_mapping

        _mapping = load_mapping()
    return _mapping


def llm():
    from semantic_ingress.core.llm_client import LLMClient

    return LLMClient(LLMConfig())


def model_id() -> str:
    """The model a run uses (INDEX_MODEL, as the report): part of every cache key, so /check must match it."""
    return LLMConfig().model_id


# ----------------------------------------------------------------------
# Runners: where a run executes. Both read and write only the request in MongoDB.
# ----------------------------------------------------------------------

class AlreadyRunning(Exception):
    pass


def request_progress(request: dict) -> dict:
    """Progress as the request records it (both runners keep it up to date)."""
    results, names = request.get("results") or {}, request.get("names") or {}
    return {"request_id": request["_id"], "phase": request.get("phase"), "total": request.get("total", 0),
            "done": len(results), "failed": sum(1 for r in results.values() if r.get("status") == "failed"),
            "processes": [{"key": k, "name": names.get(k, ""), "status": results[k]["status"] if k in results
                           else "pending"} for k in request.get("keys", [])]}


class InProcessRunner:
    """Runs a request on a thread of this app. One run per index; cancel stops between processes."""

    name = "inprocess"

    def __init__(self) -> None:
        self._runs: dict[str, tuple[str, threading.Event, asyncio.Task]] = {}

    def workflow_id(self, repo_id: str) -> str:
        return f"inprocess-{repo_id}"

    async def running(self, repo_id: str) -> str | None:
        """The running request's id, or None."""
        run = self._runs.get(repo_id)
        return run[0] if run and not run[2].done() else None

    async def start(self, repo_id: str, request_id: str) -> str:
        from semantic_ingress.report.impact.service import run_request

        if await self.running(repo_id):
            raise AlreadyRunning()
        stop = threading.Event()

        async def go() -> None:
            try:
                await asyncio.to_thread(run_request, llm(), store(), repo_id, request_id, mapping(), stop)
            except Exception as e:  # recorded on the request by run_request
                logger.warning(f"Business impact for {repo_id} ended: {type(e).__name__}: {str(e)[:200]}")
            finally:
                self._runs.pop(repo_id, None)

        self._runs[repo_id] = (request_id, stop, asyncio.create_task(go()))
        return self.workflow_id(repo_id)

    async def progress(self, repo_id: str) -> dict | None:
        request_id = await self.running(repo_id)
        if request_id is None:
            return None
        request = await asyncio.to_thread(store().get_impact_request, repo_id, request_id)
        return request_progress(request) if request else {"request_id": request_id, "phase": "PREPARE"}

    async def cancel(self, repo_id: str) -> bool:
        run = self._runs.get(repo_id)
        if run is None or run[2].done():
            return False
        run[1].set()
        return True


_runner = None


def runner():
    global _runner
    if _runner is None:
        if IMPACT_RUNNER == "inprocess":
            _runner = InProcessRunner()
        else:
            from impact.runner import TemporalRunner

            _runner = TemporalRunner()
    return _runner


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------

def _valid(repo_id: str) -> JSONResponse | None:
    return None if re.match(REPO_PATTERN, repo_id) else _error("Invalid index name.", 400)


def _stale(doc: dict, report: dict | None) -> bool:
    src = doc.get("source_report") or {}
    if not report:
        return False
    return any(report.get(k) != src.get(k) for k in ("generated_at", "commit_sha"))


@router.post("/api/impact/{repo_id}/check")
async def check_impact(repo_id: str, body: ImpactRunRequest | None = None):
    """What Generate would do now, and roughly what it would cost. No model calls."""
    from semantic_ingress.report.impact.translate import ImpactUnavailable, estimate_run

    if (bad := _valid(repo_id)) is not None:
        return bad
    body = body or ImpactRunRequest()
    try:
        est = await asyncio.to_thread(estimate_run, store(), repo_id, body.context, mapping(), model_id(), body.fresh)
    except ImpactUnavailable as e:
        return _error(str(e), 422)
    except Exception as e:
        logger.error(f"Business-impact check failed for {repo_id}: {e}", exc_info=True)
        return _error(f"Could not check the report: {e}", 503)
    n, cached, skipped = len(est["will_call"]), len(est["cached"]), len(est["skipped"])
    parts = [f"{n} process{'' if n == 1 else 'es'} will use the model (est. ~${est['est_cost_usd']:.2f})"]
    if cached:
        parts.append(f"{cached} reused from the last run")
    if skipped:
        parts.append(f"{skipped} skipped (not a fit, or nothing to translate)")
    return {**est, "message": "; ".join(parts) + "."}


@router.post("/api/impact/{repo_id}")
async def start_impact(repo_id: str, body: ImpactRunRequest):
    """Record the request (with its context) in MongoDB, then start the run with only the ids."""
    from semantic_ingress.report.impact.service import fail_request, new_request
    from semantic_ingress.report.impact.translate import ImpactUnavailable

    if (bad := _valid(repo_id)) is not None:
        return bad
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return _error("ANTHROPIC_API_KEY is not set. Add it to .env, then docker compose up -d.", 503)
    run = runner()
    try:
        if await run.running(repo_id):
            return JSONResponse({"error": "A business-impact run for this index is already in progress.",
                                 "progress": await run.progress(repo_id)}, status_code=409)
    except Exception as e:
        return _error(f"The Temporal server is not reachable: {e}", 503)
    try:
        request = await asyncio.to_thread(new_request, store(), repo_id, body.context, body.fresh, run.name)
    except ImpactUnavailable as e:
        return _error(str(e), 422)
    except Exception as e:
        logger.error(f"Recording the business-impact request for {repo_id} failed: {e}", exc_info=True)
        return _error(f"MongoDB is not reachable: {e}", 503)
    try:
        workflow_id = await run.start(repo_id, request["_id"])
    except AlreadyRunning:
        await asyncio.to_thread(fail_request, store(), repo_id, request["_id"], "Another run started first.",
                                "cancelled")
        return JSONResponse({"error": "A business-impact run for this index is already in progress.",
                             "progress": await run.progress(repo_id)}, status_code=409)
    except Exception as e:
        logger.error(f"Starting the business-impact run for {repo_id} failed: {e}", exc_info=True)
        await asyncio.to_thread(fail_request, store(), repo_id, request["_id"], f"Could not start: {e}")
        return _error(f"Could not start the run: {e}", 503)
    logger.info(f"Started business impact {workflow_id} (request {request['_id']})")
    return {"workflow_id": workflow_id, "request_id": request["_id"], "runner": run.name}


@router.get("/api/impact/{repo_id}/status")
async def impact_status(repo_id: str):
    """While a run is open: its progress (from the workflow). After: the request in MongoDB says how it ended."""
    if (bad := _valid(repo_id)) is not None:
        return bad
    try:
        progress = await runner().progress(repo_id)
    except Exception as e:
        logger.warning(f"Business-impact progress for {repo_id} unavailable: {e}")
        progress = None
    if progress is not None:
        return {"status": "running", **progress}
    try:
        request = await asyncio.to_thread(store().latest_impact_request, repo_id)
    except Exception as e:
        return _error(f"MongoDB is not reachable: {e}", 503)
    if request is None:
        return {"status": "none"}
    out = {**request_progress(request), "status": request["status"], "error": request.get("error"),
           "created_at": request.get("created_at"), "finished_at": request.get("finished_at")}
    if request["status"] == "running":
        # Recorded as running, but no run is open: the app (in-process) or the workflow ended without finishing.
        out.update(status="interrupted",
                   error="The run stopped before it finished (the app or worker restarted, or the run was "
                         "terminated). Generate again.")
    return out


@router.get("/api/impact/{repo_id}")
async def get_impact(repo_id: str):
    """The stored business impact; `stale` when the Temporal report changed since it was built."""
    if (bad := _valid(repo_id)) is not None:
        return bad
    try:
        doc = await asyncio.to_thread(store().get_business_impact, repo_id)
        report = await asyncio.to_thread(store().get_report, repo_id) if doc else None
    except Exception as e:
        return _error(f"MongoDB is not reachable: {e}", 503)
    if not doc:
        return _error("No business impact for this index yet.", 404)
    return {"status": doc.get("status"), "error": doc.get("error"), "generated_at": doc.get("generated_at"),
            "attempted_at": doc.get("attempted_at"), "request_id": doc.get("request_id"),
            "source_report": doc.get("source_report"), "stale": _stale(doc, report), "data": doc.get("data"),
            "context": doc.get("context")}


@router.get("/api/impact/{repo_id}/markdown")
async def get_impact_markdown(repo_id: str, view: Literal["exec", "champion", "discovery"] = "exec"):
    if (bad := _valid(repo_id)) is not None:
        return bad
    try:
        doc = await asyncio.to_thread(store().get_business_impact, repo_id)
    except Exception as e:
        return _error(f"MongoDB is not reachable: {e}", 503)
    if not doc or not doc.get("data"):
        return _error("No business impact for this index yet.", 404)
    markdown = (doc.get("markdown") or {}).get(view)
    if not markdown:
        from semantic_ingress.report.impact.render import render

        markdown = render(doc["data"])[view]
    return PlainTextResponse(markdown, media_type="text/markdown")


@router.delete("/api/impact/{repo_id}")
async def cancel_impact(repo_id: str):
    if (bad := _valid(repo_id)) is not None:
        return bad
    try:
        cancelled = await runner().cancel(repo_id)
    except Exception as e:
        return _error(f"Could not cancel: {e}", 503)
    if not cancelled:
        return _error("No business-impact run is in progress for this index.", 404)
    return {"cancelled": True}
