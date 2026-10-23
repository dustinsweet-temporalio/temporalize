"""
Indexing from the web UI.

    GET    /api/fs?path=           a folder under REPOS_ROOT: its subfolders (the folder picker)
                                   and, when it is a git repository, its HEAD
    POST   /api/sync/check         what a sync would do now: HEAD's files compared with the index
                                   by blob hash (no model calls)
    POST   /api/sync               start a sync: a new index, an incremental sync or a full re-index
    GET    /api/syncs              running syncs, and those that closed in the last hours, with progress
    DELETE /api/repos/{repo_id}    delete an index: its database (summaries, report) and mirrored source

A sync started here is the workflow index.sh runs (sync-<name>), with memo origin "ui";
the compose `index-worker` service runs it (semantic_ingress/temporal/service.py). Repositories
are read where they are on the host: compose mounts REPOS_ROOT at the same path in the
app and the worker, read-only.
"""
import asyncio
import logging
import os
import re
import shutil
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from semantic_ingress.config import LLMConfig, LensConfig, TemporalConfig
from semantic_ingress.core.llm_client import set_pricing
from semantic_ingress.storage.staging_store import STAGING_DATABASE, WORKER_STATUS
from semantic_ingress.temporal.constants import (
    ORIGIN_UI,
    PROGRESS_QUERY,
    WORKFLOW_NAME,
    sync_memo,
    task_queue,
    workflow_id,
)
from semantic_ingress.temporal.models import Progress, SyncOutcome, SyncParams, SyncWorkflowInput
from semantic_ingress.temporal.progress import describe, percent, usage_cost

from config import DB_PREFIX, HOST_HOME, REPOS_ROOT, SOURCE_DIR
from utils.mongo import get_mongo_client

logger = logging.getLogger("code_atlas.indexing")
router = APIRouter()

NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
RECENT = timedelta(hours=6)              # closed syncs are listed for this long
QUERY_TIMEOUT = timedelta(seconds=3)     # a progress query with no worker to answer it
MAX_ENTRIES = 500                        # subfolders listed by the folder picker

set_pricing(LLMConfig())  # cost estimates at the configured INDEX_PRICE_* values


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def ui_settings() -> dict:
    """What the UI needs to offer indexing (merged into /api/config)."""
    return {
        "indexing_enabled": bool(REPOS_ROOT) and Path(REPOS_ROOT).is_dir(),
        "repos_root": REPOS_ROOT or None,
        "source_dir": SOURCE_DIR,
        "db_prefix": DB_PREFIX,
    }


# ----------------------------------------------------------------------
# Folders and repositories
# ----------------------------------------------------------------------

class PathError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _root() -> Path:
    if not REPOS_ROOT:
        raise PathError("Indexing from the UI is off: set REPOS_ROOT in .env, then docker compose up -d.", 503)
    root = Path(REPOS_ROOT)
    if not root.is_dir():
        raise PathError(f"REPOS_ROOT ({REPOS_ROOT}) is not mounted in the app container. "
                        "Run docker compose up -d to apply the mount.", 503)
    return root.resolve()


def _resolve(path: str) -> Path:
    """An existing folder under REPOS_ROOT (the root itself for a blank path). "~" is the host's home."""
    root = _root()
    text = path.strip()
    if not text:
        return root
    if text == "~" or text.startswith("~/"):
        text = (HOST_HOME or str(root)) + text[1:]
    folder = Path(text)
    if not folder.is_absolute():
        raise PathError(f"Enter the full path, such as {root}/my-service.")
    folder = folder.resolve()
    if folder != root and root not in folder.parents:
        raise PathError(f"Only folders under {root} can be indexed. To index elsewhere, change REPOS_ROOT in .env.")
    if not folder.exists():
        raise PathError(f"There is no folder at {folder}.", 404)
    if not folder.is_dir():
        raise PathError(f"{folder} is a file, not a folder.")
    return folder


def _git_head(folder: Path) -> dict | None:
    """The repository's HEAD; None when the folder has no .git, {"error": ...} when it cannot be read."""
    if not (folder / ".git").exists():
        return None
    import pygit2

    from semantic_ingress.core import git_adapter  # noqa: F401  (turns off libgit2's ownership check)

    try:
        repo = pygit2.Repository(str(folder))
    except pygit2.GitError as e:
        return {"error": f"The git repository cannot be read: {e}"}
    if repo.head_is_unborn:
        return {"error": "The repository has no commits yet. Only committed files are indexed, so commit first."}
    commit = repo[repo.head.target]
    sha = str(commit.id)
    return {
        "commit": sha,
        "short": sha[:7],
        "branch": None if repo.head_is_detached else repo.head.shorthand,
        "subject": (commit.message or "").strip().splitlines()[0][:120] if (commit.message or "").strip() else "",
        "committed_at": datetime.fromtimestamp(commit.commit_time, timezone.utc).isoformat(),
    }


def _repository(path: str) -> Path:
    """A folder under REPOS_ROOT that is a git repository with commits; raises PathError otherwise."""
    folder = _resolve(path)
    head = _git_head(folder)
    if head is None:
        raise PathError(f"{folder} has no .git folder, so it cannot be indexed. "
                        "Pick the repository's root folder (the one that contains .git).")
    if "error" in head:
        raise PathError(head["error"])
    return folder


def suggest_name(folder_name: str) -> str:
    """An index name from a folder name: the characters index names allow, others replaced by _."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", folder_name)[:64] or "repo"


@router.get("/api/fs")
def browse(path: str = ""):
    """A folder for the picker: its subfolders (git repositories marked) and its own git HEAD."""
    try:
        root = _root()
        folder = _resolve(path)
    except PathError as e:
        return _error(str(e), e.status)
    try:
        children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        return _error(f"Cannot list {folder}: {e.strerror or e}", 403)
    entries, truncated = [], False
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if not child.is_dir():
                continue
            git = (child / ".git").exists()
        except OSError:
            continue
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        entries.append({"name": child.name, "path": str(child), "git": git})
    return {
        "path": str(folder),
        "root": str(root),
        "parent": str(folder.parent) if folder != root else None,
        "git": _git_head(folder),
        "entries": entries,
        "truncated": truncated,
        "suggested_name": suggest_name(folder.name),
    }


# ----------------------------------------------------------------------
# Is a sync needed?
# ----------------------------------------------------------------------

class CheckRequest(BaseModel):
    repo_id: str = Field(pattern=NAME_PATTERN)
    repo_path: str = Field(min_length=1)
    force: bool = False


_store = None


def _ingress_store():
    """The indexer's Mongo store (what preview_sync reads), created on first use."""
    global _store
    if _store is None:
        from semantic_ingress.config import MongoConfig
        from semantic_ingress.storage import MongoStore

        _store = MongoStore(MongoConfig(), DB_PREFIX)
    return _store


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def check_message(p, force: bool) -> str:
    """One or two sentences on what a sync would do, from a SyncPreview."""
    short = p.commit_sha[:7]
    files = _plural(p.n_files, "file")
    if p.mode == "full":
        if p.indexed_commit is None and p.reason.startswith("nothing"):
            return f"Not indexed yet. Indexing summarizes {files} at {short}."
        if force:
            return f"A full re-index summarizes all {files} at {short} again and replaces the index."
        return f"A full re-index is needed ({p.reason}). It summarizes {files} at {short}."
    if not p.needed:
        moved = (f" HEAD moved from {p.indexed_commit[:7]} to {short}, but no indexed file changed."
                 if p.indexed_commit and p.indexed_commit != p.commit_sha else "")
        return f"Up to date: the index matches the repository at {short}.{moved} No sync is needed."
    changes = ", ".join(f"{n:,} {label}" for n, label in
                        ((p.n_added, "added"), (p.n_modified, "modified"), (p.n_deleted, "deleted")) if n)
    since = f" since {p.indexed_commit[:7]}" if p.indexed_commit else ""
    return (f"Sync needed: {changes}{since}. An incremental sync summarizes "
            f"{_plural(p.n_added + p.n_modified, 'file')}.")


@router.post("/api/sync/check")
def check_sync(request: CheckRequest):
    """Compare HEAD with the index (as a sync's plan does) without staging or generating anything."""
    from semantic_ingress.config import FileFilterConfig
    from semantic_ingress.core.git_adapter import GitAdapter
    from semantic_ingress.core.sync_steps import preview_sync

    try:
        folder = _repository(request.repo_path)
    except PathError as e:
        return _error(str(e), e.status)
    try:
        preview = preview_sync(_ingress_store(), GitAdapter(folder, FileFilterConfig()), request.repo_id, request.force)
    except Exception as e:
        logger.error(f"Sync check failed for {request.repo_id}: {e}", exc_info=True)
        return _error(f"Could not compare the repository with the index: {e}", 500)
    return {**asdict(preview), "message": check_message(preview, request.force)}


# ----------------------------------------------------------------------
# Start a sync
# ----------------------------------------------------------------------

_client = None
_client_lock = asyncio.Lock()


async def temporal_client():
    """One Temporal client for the app, connected on first use (the SDK reconnects after that)."""
    global _client
    async with _client_lock:
        if _client is None:
            from temporalio.client import Client

            config = TemporalConfig()
            _client = await asyncio.wait_for(Client.connect(config.address, namespace=config.namespace), 10)
    return _client


async def _running(client, repo_id: str) -> bool:
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError, RPCStatusCode

    try:
        description = await client.get_workflow_handle(workflow_id(repo_id)).describe()
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return description.status == WorkflowExecutionStatus.RUNNING


def _index_exists(repo_id: str) -> bool:
    return f"{DB_PREFIX}_{repo_id}" in get_mongo_client().list_database_names()


class SyncRequest(BaseModel):
    repo_id: str = Field(pattern=NAME_PATTERN)
    repo_path: str = Field(min_length=1)
    mode: Literal["new", "incremental", "force"]


class SyncStartError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


async def begin_sync(repo_id: str, folder: Path, mode: str, memo_extra: dict | None = None) -> dict:
    """
    Start a sync of `folder` (already validated) on the worker service, after the checks
    `sync` makes (the preflight included). `memo_extra` adds to the run's memo (Temporalize
    passes the model and extra exclusions). Raises SyncStartError with an HTTP status.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.common import WorkflowIDConflictPolicy

    from semantic_ingress.config import IngestionConfig
    from semantic_ingress.preflight import PreflightError, run_preflight

    try:
        exists = await asyncio.to_thread(_index_exists, repo_id)
    except Exception as e:
        raise SyncStartError(f"MongoDB is not reachable: {e}", 503) from e
    if mode == "new" and exists:
        raise SyncStartError(f"An index named {repo_id} already exists. Choose another name, or sync {repo_id} "
                             "from the index list.", 409)
    if mode != "new" and not exists:
        raise SyncStartError(f"There is no index named {repo_id}.", 404)
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise SyncStartError("ANTHROPIC_API_KEY is not set. Add it to .env, then docker compose up -d.", 400)
    try:
        client = await temporal_client()
        if await _running(client, repo_id):
            raise SyncStartError(f"A sync of {repo_id} is already running.", 409)
    except SyncStartError:
        raise
    except Exception as e:
        raise SyncStartError(f"The Temporal server is not reachable: {e}", 503) from e

    try:
        config = IngestionConfig(repo_path=folder, output_dir=Path("/tmp"), repo_id=repo_id, prefix=DB_PREFIX)
        await asyncio.to_thread(run_preflight, config, False)
    except PreflightError as e:
        raise SyncStartError(f"Preflight failed, so nothing was started: {e}", 400) from e
    except Exception as e:
        raise SyncStartError(str(e), 400) from e

    params = SyncParams(
        repo_id=repo_id, prefix=DB_PREFIX, repo_path=str(folder), force=mode == "force",
        window_size=config.temporal.window_size, lens_enabled=config.lens.enabled,
    )
    memo = sync_memo(repo_id, str(folder), ORIGIN_UI, report=params.lens_enabled and not params.no_report)
    try:
        handle = await client.start_workflow(
            WORKFLOW_NAME, SyncWorkflowInput(params=params),
            id=workflow_id(repo_id), task_queue=task_queue(repo_id),
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            memo={**memo, **(memo_extra or {})},
        )
    except WorkflowAlreadyStartedError:
        raise SyncStartError(f"A sync of {repo_id} is already running.", 409) from None
    except Exception as e:
        logger.error(f"Starting the sync of {repo_id} failed: {e}", exc_info=True)
        raise SyncStartError(f"Could not start the sync: {e}", 503) from e
    logger.info(f"Started {handle.id} for {folder}")
    return {"repo_id": repo_id, "workflow_id": handle.id, "run_id": handle.first_execution_run_id}


@router.post("/api/sync")
async def start_sync(request: SyncRequest):
    """
    Start a sync on the worker service, after the checks `sync` makes (the preflight
    included), so a problem shows in the form instead of in a stuck run.
    """
    try:
        folder = await asyncio.to_thread(_repository, request.repo_path)
    except PathError as e:
        return _error(str(e), e.status)
    try:
        return await begin_sync(request.repo_id, folder, request.mode)
    except SyncStartError as e:
        return _error(str(e), e.status)


# ----------------------------------------------------------------------
# Progress
# ----------------------------------------------------------------------

_closed: dict[str, dict] = {}   # run id -> what the UI shows for it (a closed run never changes)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _worker_error(repo_id: str) -> str | None:
    doc = get_mongo_client()[STAGING_DATABASE][WORKER_STATUS].find_one({"_id": repo_id})
    return doc.get("error") if doc else None


def _failure_message(e: BaseException) -> str:
    """The innermost message of a workflow failure (an activity error, or the termination reason)."""
    cause, message = e, str(e)
    while cause is not None:
        message = getattr(cause, "message", None) or str(cause) or message
        cause = getattr(cause, "cause", None)
    return message


async def _progress(client, execution, memo: dict) -> dict:
    report = memo.get("report", LensConfig().enabled)
    try:
        p = await client.get_workflow_handle(execution.id).query(
            PROGRESS_QUERY, result_type=Progress, rpc_timeout=QUERY_TIMEOUT
        )
    except Exception:
        # No worker is answering: the worker service is down or cannot run it, or the index.sh that ran it stopped.
        if memo.get("origin") == ORIGIN_UI:
            error = await asyncio.to_thread(_worker_error, memo.get("repo_id", ""))
            text = (f"The indexer worker cannot run this sync: {error}" if error else
                    "Waiting for the indexer worker (if this lasts, check: docker compose up -d index-worker)")
        else:
            text = "Waiting for a worker: run the same ./scripts/index.sh command again to resume"
        return {"phase": None, "percent": 0, "description": text, "waiting": True}
    description = describe(p, lens=report)
    if p.phase == "REPORT":  # the report takes minutes: say what it is doing
        try:
            from semantic_ingress.report.service import report_progress

            rp = await asyncio.to_thread(report_progress, _ingress_store(), memo.get("repo_id", ""))
            if rp and rp.get("detail"):
                description = f"Building the Temporal report: {rp['detail']}"
        except Exception:
            pass
    return {
        "phase": p.phase, "percent": percent(p, report), "description": description,
        "done": p.done, "total": p.total, "failed": p.failed, "cost_usd": round(usage_cost(p.usage), 4),
        "mode": p.plan.mode if p.plan else None,
    }


async def _outcome(client, execution) -> dict:
    """What a closed run did, for the panel: a line, its cost, and whether it went well."""
    from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError

    handle = client.get_workflow_handle(execution.id, run_id=execution.run_id, result_type=SyncOutcome)
    if execution.status != WorkflowExecutionStatus.COMPLETED:
        try:
            await handle.result(follow_runs=False)
            message = ""
        except WorkflowFailureError as e:
            message = _failure_message(e)
        except Exception as e:
            message = str(e)
        label = {"FAILED": "Failed", "TERMINATED": "Terminated", "CANCELED": "Canceled",
                 "TIMED_OUT": "Timed out"}.get(execution.status.name, execution.status.name.title())
        return {"result": "error", "percent": 100, "description": f"{label}: {message}" if message else label}

    o: SyncOutcome = await handle.result(follow_runs=False)
    failed = len(o.failed) + o.failed_overflow
    cost = usage_cost(o.usage) + (o.report.cost_usd if o.report and o.report.status == "ok" else 0.0)
    if o.dry_run:
        summary = f"Dry run: {_plural(o.would_upsert or 0, 'document')} would be written"
    elif o.plan and o.plan.mode == "incremental" and not o.files_done and not o.plan.n_deleted:
        summary = "Already up to date: no file had changed"
    else:
        summary = f"Indexed: {_plural(o.files_done, 'file')} and {_plural(o.folders_generated, 'folder')} summarized"
        if o.plan and o.plan.n_deleted:
            summary += f", {_plural(o.plan.n_deleted, 'deleted file')} removed"
    problems = []
    if failed:
        problems.append(f"{_plural(failed, 'item')} failed (the next sync retries them)")
    if o.persist_error or o.finalize_error:
        problems.append(f"writing the index failed: {o.persist_error or o.finalize_error}")
    if o.mirror is not None and o.mirror.total_failures:
        problems.append(f"{_plural(o.mirror.total_failures, 'source file')} not mirrored")
    if o.report is not None and o.report.status == "failed":
        problems.append(f"the Temporal report failed: {o.report.error}")
    return {
        "result": "warning" if problems else "ok", "percent": 100, "failed": failed, "cost_usd": round(cost, 4),
        "description": summary + ("; " + "; ".join(problems) if problems else ""),
        "dry_run": o.dry_run,
    }


async def _job(client, execution) -> dict:
    from temporalio.client import WorkflowExecutionStatus

    memo = await execution.memo()
    job = {
        "repo_id": memo.get("repo_id") or execution.id.removeprefix("sync-"),
        "workflow_id": execution.id,
        "run_id": execution.run_id,
        "origin": memo.get("origin", "cli"),
        "repo_path": memo.get("repo_path"),
        "status": execution.status.name.lower(),
        "started_at": _iso(execution.start_time),
        "closed_at": _iso(execution.close_time),
    }
    if execution.status == WorkflowExecutionStatus.RUNNING:
        job.update(await _progress(client, execution, memo))
    else:
        if execution.run_id not in _closed:
            _closed[execution.run_id] = await _outcome(client, execution)
        job.update(_closed[execution.run_id])
    return job


@router.get("/api/syncs")
async def list_syncs():
    """Sync runs (from the UI and from index.sh): running ones with progress, recently closed ones with their outcome."""
    from temporalio.client import WorkflowExecutionStatus

    since = (datetime.now(timezone.utc) - RECENT).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = f"WorkflowType = '{WORKFLOW_NAME}' AND (ExecutionStatus = 'Running' OR CloseTime > '{since}')"
    try:
        client = await temporal_client()
        latest = {}  # workflow id -> its newest run (Continue-As-New closes the earlier ones)
        async for execution in client.list_workflows(query, limit=200):
            if execution.status == WorkflowExecutionStatus.CONTINUED_AS_NEW:
                continue
            seen = latest.get(execution.id)
            if seen is None or execution.start_time > seen.start_time:
                latest[execution.id] = execution
        jobs = list(await asyncio.gather(*(_job(client, e) for e in latest.values())))
    except Exception as e:
        logger.warning(f"Listing syncs failed: {e}")
        return _error(f"The Temporal server is not reachable: {e}", 503)
    jobs.sort(key=lambda j: j["started_at"] or "", reverse=True)
    return {"syncs": jobs}


# ----------------------------------------------------------------------
# Delete
# ----------------------------------------------------------------------

def _delete_index(repo_id: str) -> None:
    """The mirrored source first (the step most likely to fail), then the database."""
    root = Path(SOURCE_DIR)
    for folder in (root / repo_id, root / ".oids" / repo_id):
        if folder.exists():
            shutil.rmtree(folder)
    client = get_mongo_client()
    client.drop_database(f"{DB_PREFIX}_{repo_id}")
    client[STAGING_DATABASE][WORKER_STATUS].delete_one({"_id": repo_id})


@router.delete("/api/repos/{repo_id}")
async def delete_index(repo_id: str):
    """Delete an index: summaries, lenses, the Temporal report and the mirrored source. The repository is untouched."""
    if not re.match(NAME_PATTERN, repo_id):
        return _error("Invalid index name.", 400)
    try:
        if not await asyncio.to_thread(_index_exists, repo_id):
            return _error(f"There is no index named {repo_id}.", 404)
    except Exception as e:
        return _error(f"MongoDB is not reachable: {e}", 503)
    try:
        if await _running(await temporal_client(), repo_id):
            return _error(f"A sync of {repo_id} is running. Wait for it to finish (or terminate it in the "
                          "Temporal UI), then delete the index.", 409)
    except Exception as e:
        return _error(f"Could not check for a running sync (Temporal server not reachable: {e}).", 503)
    try:
        await asyncio.to_thread(_delete_index, repo_id)
    except OSError as e:
        return _error(f"Could not delete the cached copy of the source: {e.strerror or e}. "
                      "The app needs the source-data volume read-write (docker compose up -d).", 500)
    except Exception as e:
        logger.error(f"Deleting {repo_id} failed: {e}", exc_info=True)
        return _error(f"Could not delete the index: {e}", 500)
    logger.info(f"Deleted index {repo_id}")
    return {"deleted": repo_id}
