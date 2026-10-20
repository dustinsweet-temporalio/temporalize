"""
Code Atlas web server.

Serves the chat UI and a small API:

    GET  /api/health   service status (database, API key, model)
    GET  /api/config   UI settings (the Temporal UI link)
    GET  /api/repos    indexed repositories
    GET  /api/report/{repo_id}  the stored Temporal report (data for the report view)
    GET  /api/report/{repo_id}/markdown  the detailed report as Markdown (as ./scripts/report.sh writes it)
    GET  /api/compass/{repo_id}  the Compass Web URL that opens this index's database
    POST /api/chat     one message of a conversation; streams the answer as SSE (see chat())
    POST /api/chat/{repo_id}/{session_id}/stop    stop the turn in flight ({turn_id})
    DELETE /api/chat/{repo_id}/{session_id}       end the conversation's session workflow
    GET  /api/chat/{repo_id}/{session_id}/turns/{turn_id}/stream  reattach to a turn (after a reload)

Indexing from the UI (pick a folder, check, sync, follow progress, delete) is in indexing.py;
business impact from the Temporal report (/api/impact/...) is in impact_api.py.

Run with: uvicorn app:app --host 0.0.0.0 --port 8080
"""
import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import List, Literal
from urllib.parse import quote

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from chat import constants as C
from chat.context import trim_to_budget
from chat.models import ChatMessage as TurnMessage
from chat.models import ChatSessionInput, SendMessageInput, TurnLookup
from chat.streaming import turn_key
from chat.workflow import ChatSessionWorkflow
from config import (
    CHAT_CONTEXT_CHARS,
    CHAT_IDLE_HOURS,
    CHAT_TASK_QUEUE,
    CHAT_TURN_TIMEOUT_MINUTES,
    COMPASS_PUBLIC_URL,
    COMPASS_URL,
    DB_PREFIX,
    FILE_SUMMARIES_COLLECTION,
    FOLDER_SUMMARIES_COLLECTION,
    INDEX_META_COLLECTION,
    LOG_FORMAT,
    LOG_LEVEL,
    MODEL_ID,
    REDIS_URL,
    REPORTS_COLLECTION,
    SOURCE_DIR,
    TEMPORAL_ADDRESS,
    TEMPORAL_NAMESPACE,
)
from impact_api import router as impact_router
from indexing import router as indexing_router, ui_settings as indexing_settings
from utils.mongo import get_mongo_client
from utils.formatters.lens import index_lens_info
from utils.validation import validate_repository

logging.basicConfig(level=getattr(logging, LOG_LEVEL), format=LOG_FORMAT)
logger = logging.getLogger("code_atlas.app")

WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).resolve().parent.parent / "web"))

UUID_PATTERN = r"^[0-9a-fA-F-]{36}$"
REPO_PATTERN = r"^[A-Za-z0-9_-]+$"


# ---- connections: Redis at startup, Temporal on first use (the app starts without it) ----

def _redis_factory():
    return aioredis.from_url(REDIS_URL, decode_responses=True)


async def _temporal_connect() -> Client:
    return await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)


_temporal: Client | None = None
_temporal_lock = asyncio.Lock()


async def get_temporal() -> Client:
    global _temporal
    async with _temporal_lock:
        if _temporal is None:
            _temporal = await _temporal_connect()
        return _temporal


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = _redis_factory()
    try:
        yield
    finally:
        await app.state.redis.aclose()


app = FastAPI(title="Code Atlas", docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(indexing_router)
app.include_router(impact_router)


@app.middleware("http")
async def revalidate_ui_files(request, call_next):
    """Browsers revalidate the UI files (cheap, via ETag) so a rebuilt app is picked up at once."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    repo_id: str = Field(pattern=REPO_PATTERN, description="The index this conversation is about")
    session_id: str = Field(pattern=UUID_PATTERN, description="The browser's conversation id")
    turn_id: str = Field(pattern=UUID_PATTERN, description="This message's id (also the Update id)")
    prior_count: int = Field(ge=0, description="Messages the browser holds before this one")
    resync: bool = Field(default=False, description="Replace the session's context with the tail sent")
    messages: List[ChatMessage] = Field(min_length=1, description="Context tail, then the new user message")


class StopRequest(BaseModel):
    turn_id: str = Field(pattern=UUID_PATTERN)


def _sse(payload) -> bytes:
    """Encode one server-sent event. JSON encoding keeps newlines out of the frame."""
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


async def _check(coro, timeout: float = 3.0) -> str:
    try:
        await asyncio.wait_for(coro, timeout)
        return "ok"
    except Exception as e:
        return f"unavailable: {e or type(e).__name__}"


async def _temporal_health() -> None:
    client = await get_temporal()
    if not await client.service_client.check_health(timeout=timedelta(seconds=3)):
        raise RuntimeError("not serving")


@app.get("/api/health")
async def health(recheck: bool = False):
    """
    App status. temporal_docs is the Temporal Docs MCP check (cached; recheck=true forces it);
    temporal and redis are what chat runs on.
    """
    from utils.temporal_docs import status as docs_status

    status = {
        "model": MODEL_ID,
        "api_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "source_dir": SOURCE_DIR,
        "database": await _check(asyncio.to_thread(get_mongo_client().admin.command, "ping")),
        "temporal": await _check(_temporal_health()),
        "redis": await _check(app.state.redis.ping()),
    }
    status["temporal_docs"] = await asyncio.to_thread(docs_status, recheck=recheck)
    ok = all(status[k] == "ok" for k in ("database", "temporal", "redis")) and status["api_key_configured"]
    return JSONResponse(status, status_code=200 if ok else 503)


@app.get("/api/config")
def ui_config():
    """Settings the UI needs: where the Temporal UI (indexing runs) is, as the browser reaches it, and indexing."""
    return {
        "temporal_ui_url": os.environ.get("TEMPORAL_UI_URL", "").strip() or "http://localhost:8233",
        "temporal_namespace": os.environ.get("TEMPORAL_NAMESPACE", "").strip() or "default",
        "chat_context_chars": CHAT_CONTEXT_CHARS,
        **indexing_settings(),
    }


@app.get("/api/repos")
def list_repos():
    """Indexed repositories with file and folder counts."""
    try:
        client = get_mongo_client()
        prefix = f"{DB_PREFIX}_"
        repos = []
        for db_name in sorted(client.list_database_names()):
            if not db_name.startswith(prefix):
                continue
            db = client[db_name]
            info = index_lens_info(db)
            meta = db[INDEX_META_COLLECTION].find_one({}, {"repo_path": 1, "commit_sha": 1}) or {}
            repos.append({
                "repo_id": db_name[len(prefix):],
                "files": db[FILE_SUMMARIES_COLLECTION].estimated_document_count(),
                "folders": db[FOLDER_SUMMARIES_COLLECTION].estimated_document_count(),
                "pack_version": info["pack_version"],
                "pack_versions": info["pack_versions"],
                "lens": info["lens"],
                "indexed_at": info["indexed_at"],
                "prompt_versions": info["prompt_versions"],
                "repo_path": meta.get("repo_path"),
                "commit_sha": meta.get("commit_sha"),
            })
        return {"repos": repos}
    except Exception as e:
        logger.error(f"Failed to list repositories: {e}")
        return JSONResponse({"error": str(e)}, status_code=503)


def _stored_report(repo_id: str) -> tuple[dict, dict] | JSONResponse:
    """The stored report document and the index's meta document, or the error response to send."""
    if not re.match(r"^[A-Za-z0-9_-]+$", repo_id):
        return JSONResponse({"error": "Invalid index name."}, status_code=400)
    try:
        db = get_mongo_client()[f"{DB_PREFIX}_{repo_id}"]
        doc = db[REPORTS_COLLECTION].find_one({"_id": f"{repo_id}::temporal_report"})
        meta = db[INDEX_META_COLLECTION].find_one({}) or {}
    except Exception as e:
        logger.error(f"Failed to read the report for {repo_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=503)
    if not doc or not doc.get("data"):
        detail = f" The last attempt failed: {doc.get('error')}" if doc and doc.get("error") else ""
        return JSONResponse({"error": f"No Temporal report for this index yet.{detail}"}, status_code=404)
    return doc, meta


@app.get("/api/report/{repo_id}")
def get_report(repo_id: str):
    """The Temporal report stored with an index (built during indexing), with its freshness."""
    found = _stored_report(repo_id)
    if isinstance(found, JSONResponse):
        return found
    doc, meta = found
    index_commit = meta.get("commit_sha")
    return {
        "status": doc.get("status"), "error": doc.get("error"),
        "generated_at": doc.get("generated_at"), "commit_sha": doc.get("commit_sha"),
        "index_commit": index_commit,
        "stale": bool(index_commit and doc.get("commit_sha") and index_commit != doc["commit_sha"]),
        "data": doc["data"],
    }


@app.get("/api/report/{repo_id}/markdown")
def get_report_markdown(repo_id: str):
    """The detailed Temporal report: the full Markdown document ./scripts/report.sh writes."""
    found = _stored_report(repo_id)
    if isinstance(found, JSONResponse):
        return found
    doc, _ = found
    markdown = doc.get("markdown")
    if not markdown:
        # A report stored without its Markdown: render it from the data, as the indexer does.
        from semantic_ingress.report.render import render

        try:
            markdown = render(doc["data"])
        except Exception as e:
            logger.error(f"Failed to render the detailed report for {repo_id}: {e}", exc_info=True)
            return JSONResponse({"error": f"Could not render the detailed report: {e}"}, status_code=500)
    return PlainTextResponse(markdown, media_type="text/markdown")


@app.get("/api/compass/{repo_id}")
def compass_link(repo_id: str):
    """
    Where Compass Web shows this index's database: <public url>/<connection id>/<database>.

    Compass Web gives its connection a new random id each time it starts, so the id is
    looked up from the service on every request rather than built into the UI.
    """
    if not re.match(r"^[A-Za-z0-9_-]+$", repo_id):
        return JSONResponse({"error": "Invalid index name."}, status_code=400)
    if not (COMPASS_URL and COMPASS_PUBLIC_URL):
        return JSONResponse({"error": "Compass Web is not configured (COMPASS_URL, COMPASS_PUBLIC_URL)."}, status_code=404)
    is_valid, _, error = validate_repository(repo_id)
    if not is_valid:
        return JSONResponse({"error": f"Index '{repo_id}' is not available: {error}"}, status_code=404)
    try:
        with urllib.request.urlopen(f"{COMPASS_URL.rstrip('/')}/api/connection-info", timeout=5) as res:
            connections = json.load(res)
        connection_id = connections[0]["id"]
    except Exception as e:
        logger.error(f"Compass Web lookup failed: {e}")
        return JSONResponse({"error": f"Compass Web is not reachable ({e}). Is the compass service running? "
                                      "docker compose up -d compass"}, status_code=503)
    database = f"{DB_PREFIX}_{repo_id}"
    return {"url": f"{COMPASS_PUBLIC_URL.rstrip('/')}/{quote(connection_id, safe='')}/{quote(database, safe='')}",
            "database": database}


# ---- chat: one Temporal workflow per conversation, tokens relayed from Redis ----

# Updates in flight. The browser may disconnect; the turn still runs to the end and is
# recorded in the session, so the task is kept here rather than tied to the request.
_turn_tasks: set[asyncio.Task] = set()


def _keep(task: asyncio.Task) -> None:
    _turn_tasks.add(task)

    def done(t: asyncio.Task) -> None:
        _turn_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.info(f"Chat turn ended with an error: {_failure_text(t.exception())}")

    task.add_done_callback(done)


def _failure_type(e: BaseException) -> str | None:
    return getattr(e.cause, "type", None) if isinstance(e, WorkflowUpdateFailedError) else None


def _workflow_closed(e: BaseException) -> bool:
    """The workflow completed while the Update was in flight, or was gone when it arrived."""
    if _failure_type(e) == C.UPDATE_WORKFLOW_COMPLETED:
        return True
    return isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND


def _failure_text(e: BaseException) -> str:
    if isinstance(e, WorkflowUpdateFailedError) and e.cause is not None:
        return getattr(e.cause, "message", None) or str(e.cause)
    return str(e) or type(e).__name__


async def _send_turn(client: Client, request: ChatRequest, kept: list, dropped: int):
    """
    The message as an Update-with-Start: it starts the session (seeded with `kept`) if
    none is running. Retries once on RESYNC_REQUIRED (resending the tail) and once when
    the workflow closed under the Update; each retry needs a new Update id, because the
    same id would attach to the failed Update.
    """
    text = request.messages[-1].content
    history = kept if request.resync else None
    update_id = request.turn_id
    resynced = reopened = False
    while True:
        op = WithStartWorkflowOperation(  # single use: a new one per attempt
            ChatSessionWorkflow.run,
            ChatSessionInput(
                session_id=request.session_id, repo_id=request.repo_id, messages=kept, dropped=dropped,
                idle_timeout_seconds=int(CHAT_IDLE_HOURS * 3600), context_char_budget=CHAT_CONTEXT_CHARS,
                turn_timeout_seconds=CHAT_TURN_TIMEOUT_MINUTES * 60,
            ),
            id=C.workflow_id(request.repo_id, request.session_id),
            task_queue=CHAT_TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            memo={"repo_id": request.repo_id, "kind": "chat"},
        )
        try:
            return await client.execute_update_with_start_workflow(
                ChatSessionWorkflow.send_message,
                SendMessageInput(turn_id=request.turn_id, text=text, client_count=request.prior_count, history=history),
                start_workflow_operation=op,
                id=update_id,
            )
        except Exception as e:
            if _failure_type(e) == C.RESYNC_REQUIRED and not resynced:
                resynced, history, update_id = True, kept, f"{request.turn_id}:resync"
                continue
            if _workflow_closed(e) and not reopened:
                reopened, update_id = True, f"{request.turn_id}:retry"
                continue
            raise


async def relay(redis, turn_id: str, update_task: asyncio.Task):
    """
    Relay the turn's Redis Stream as SSE until the Update has finished and the stream is
    drained, then reconcile with the Update result, which is authoritative: a Redis outage
    or a lost chunk never changes the answer the browser ends up with.
    """
    key, last, streamed, redis_ok = turn_key(turn_id), "0-0", [], redis is not None
    idle_since = time.monotonic()
    while True:
        finished = update_task.done()  # captured before reading, so the final drain misses nothing
        entries = []
        if redis_ok:
            try:
                resp = await redis.xread({key: last}, count=200, block=None if finished else 400)
                entries = resp[0][1] if resp else []
            except Exception as e:
                logger.warning(f"Chat stream relay degraded for {turn_id}: {e}")
                redis_ok = False  # fall back to the final answer only
        else:
            await asyncio.wait({update_task}, timeout=0.4)
        for entry_id, fields in entries:
            last = entry_id
            if fields.get("t") == "chunk":
                streamed.append(fields.get("d", ""))
                yield _sse(fields.get("d", ""))
            elif fields.get("t") == "reset":
                streamed.clear()
                yield _sse({"reset": True})
        if entries:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since > 15:
            yield b": keepalive\n\n"
            idle_since = time.monotonic()
        if finished and not entries:
            break
    try:
        result = await update_task
    except Exception as e:
        yield _sse({"error": _failure_text(e)})
        yield b"data: [DONE]\n\n"
        return
    if result.stopped:
        yield _sse({"stopped": True})
    else:
        sofar = "".join(streamed)
        if result.answer.startswith(sofar):
            if result.answer[len(sofar):]:
                yield _sse(result.answer[len(sofar):])
        else:
            yield _sse({"reset": True})
            yield _sse(result.answer)
    yield b"data: [DONE]\n\n"


@app.post("/api/chat")
async def chat(request: ChatRequest):
    """
    One message of a conversation, as a Temporal Update on the conversation's session
    workflow (started, seeded with the tail sent, if none is running). Streams server-sent
    events: `data: "<text chunk>"`, `data: {"reset": true}` (discard the text so far),
    `data: {"stopped": true}`, `data: {"error": "..."}`, `: keepalive` comments, then
    `data: [DONE]`.
    """
    if request.messages[-1].role != "user":
        return JSONResponse({"error": "The last message must be the user's."}, status_code=400)
    text = request.messages[-1].content
    prior = request.messages[:-1]
    if len(text) > C.MAX_TEXT_CHARS:
        return JSONResponse({"error": f"The message is longer than {C.MAX_TEXT_CHARS:,} characters."}, status_code=400)
    if len(prior) > 1 and sum(len(m.content) for m in prior) > CHAT_CONTEXT_CHARS:  # one message is always kept
        return JSONResponse({"error": "The conversation sent is larger than the context budget."}, status_code=400)

    # Reject an unknown index before any model call.
    is_valid, _, error = await asyncio.to_thread(validate_repository, request.repo_id)
    if not is_valid:
        return JSONResponse({"error": f"Index '{request.repo_id}' is not available: {error}"}, status_code=400)
    try:
        client = await get_temporal()
    except Exception as e:
        logger.error(f"Temporal is not reachable: {e}")
        return JSONResponse({"error": f"Chat is unavailable: Temporal is not reachable ({e})."}, status_code=503)

    kept, _ = trim_to_budget([TurnMessage(m.role, m.content) for m in prior], CHAT_CONTEXT_CHARS)
    dropped = max(0, request.prior_count - len(kept))
    update_task = asyncio.create_task(_send_turn(client, request, kept, dropped))
    _keep(update_task)
    return StreamingResponse(
        relay(app.state.redis, request.turn_id, update_task),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _signal_session(repo_id: str, session_id: str, signal: str, *args) -> Response:
    if not (re.match(REPO_PATTERN, repo_id) and re.match(UUID_PATTERN, session_id)):
        return JSONResponse({"error": "Invalid conversation."}, status_code=400)
    try:
        client = await get_temporal()
        await client.get_workflow_handle(C.workflow_id(repo_id, session_id)).signal(signal, *args)
    except RPCError as e:
        if e.status != RPCStatusCode.NOT_FOUND:  # no running session: nothing to stop or close
            return JSONResponse({"error": str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": f"Temporal is not reachable ({e})."}, status_code=503)
    return Response(status_code=202)


@app.post("/api/chat/{repo_id}/{session_id}/stop")
async def stop_turn(repo_id: str, session_id: str, body: StopRequest):
    """Stop the turn in flight; its SSE stream then ends with {"stopped": true}."""
    return await _signal_session(repo_id, session_id, C.CANCEL_TURN_SIGNAL, body.turn_id)


@app.delete("/api/chat/{repo_id}/{session_id}")
async def close_session(repo_id: str, session_id: str):
    """End the conversation's session workflow (the browser deleted the conversation)."""
    return await _signal_session(repo_id, session_id, C.CLOSE_SIGNAL)


REATTACH_WAIT_SECONDS = 30  # for a turn whose stream has not started yet


async def _lookup_turn(repo_id: str, session_id: str, turn_id: str) -> TurnLookup | None:
    """The session's record of a turn, or None when the session workflow does not exist."""
    client = await get_temporal()
    handle = client.get_workflow_handle(C.workflow_id(repo_id, session_id))
    try:
        return await handle.query(C.GET_TURN_QUERY, turn_id, result_type=TurnLookup)
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            return None
        raise


async def reattach_relay(redis, repo_id: str, session_id: str, turn_id: str, start: str = "0-0"):
    """
    Replay a turn's stream until its `done` event, then reconcile with the session's
    record. A turn that fails writes no `done`, so a quiet stream is checked against the
    session's record, and the replay gives up after the turn timeout.
    """
    key, last, streamed = turn_key(turn_id), start, []
    deadline = time.monotonic() + CHAT_TURN_TIMEOUT_MINUTES * 60 + 60
    idle_since = time.monotonic()
    stopped = finished = False
    record = None
    while not finished:
        resp = await redis.xread({key: last}, count=200, block=1000)
        entries = resp[0][1] if resp else []
        for entry_id, fields in entries:
            last = entry_id
            t = fields.get("t")
            if t == "chunk":
                streamed.append(fields.get("d", ""))
                yield _sse(fields.get("d", ""))
            elif t == "reset":
                streamed.clear()
                yield _sse({"reset": True})
            elif t == "done":
                finished, stopped = True, fields.get("d") == "stopped"
        if entries or finished:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since > 15:
            yield b": keepalive\n\n"
            idle_since = time.monotonic()
            try:
                record = await _lookup_turn(repo_id, session_id, turn_id)
            except Exception:
                record = None
            if record is not None and record.found:
                break
            if time.monotonic() > deadline:
                yield _sse({"error": "The response did not finish."})
                yield b"data: [DONE]\n\n"
                return
    if record is None or not record.found:
        try:
            record = await _lookup_turn(repo_id, session_id, turn_id)
        except Exception:
            record = None  # the stream is all we have
    if stopped or (record and record.stopped):
        yield _sse({"stopped": True})
    elif record and record.found and record.answer != "".join(streamed):
        yield _sse({"reset": True})
        yield _sse(record.answer)
    yield b"data: [DONE]\n\n"


@app.get("/api/chat/{repo_id}/{session_id}/turns/{turn_id}/stream")
async def reattach(repo_id: str, session_id: str, turn_id: str, request: Request):
    """
    Reattach to a turn after a reload: replay its token stream (from the start, or from
    the Last-Event-ID header), the same SSE events as POST /api/chat. When the stream is
    gone, the answer the session recorded comes as one chunk; 404 when neither exists.
    """
    if not (re.match(REPO_PATTERN, repo_id) and re.match(UUID_PATTERN, session_id)
            and re.match(UUID_PATTERN, turn_id)):
        return JSONResponse({"error": "Invalid turn."}, status_code=400)
    redis = app.state.redis
    try:
        deadline = time.monotonic() + REATTACH_WAIT_SECONDS
        while True:
            if await redis.exists(turn_key(turn_id)):
                start = request.headers.get("last-event-id") or "0-0"
                return StreamingResponse(
                    reattach_relay(redis, repo_id, session_id, turn_id, start),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            record = await _lookup_turn(repo_id, session_id, turn_id)
            if record is None:
                return JSONResponse({"error": "No session for this conversation."}, status_code=404)
            if record.found:
                final = _sse({"stopped": True}) if record.stopped else _sse(record.answer)

                async def one():
                    yield final
                    yield b"data: [DONE]\n\n"

                return StreamingResponse(one(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
            if time.monotonic() > deadline:  # the session never ran this turn
                return JSONResponse({"error": "This response is no longer available."}, status_code=404)
            await asyncio.sleep(1)  # the turn may not have started streaming yet
    except Exception as e:
        logger.error(f"Reattach to {turn_id} failed: {e}")
        return JSONResponse({"error": f"Could not reattach ({e})."}, status_code=503)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
