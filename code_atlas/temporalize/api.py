"""
Temporalize API (/api/tz/*), mounted by app.py. The contract is in docs/TEMPORALIZE.md.

Screens 1 and 2 run here, in the app, with no model calls: settings, the clone or the
local folder, and the local scan (a background task that ticks the checklist on the run
document). "Start assessment" (screen 2) starts the Code Atlas sync of the repository and
the run's TemporalizeWorkflow. Once the report is in, the user picks the process from
Code Atlas's recommendation (screen 3), the focus (4) and the criteria (5); those are
saved here, and confirming them signals the workflow, as do the plan's approval and
design change requests (screen 6).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from temporalize import constants as C
from temporalize import settings as tz_settings
from temporalize import store
from temporalize.sources import SourceError

logger = logging.getLogger("code_atlas.temporalize")
router = APIRouter(prefix="/api/tz")

RUN_PATTERN = r"^r-\d{4,}$"
PLAN_SECTIONS = ("claims", "changes", "testdata", "design")
LOCKED = {"assessing", "scoping", "not_suitable", "planning", "plan_ready", "generating", "done", "failed", "stopped"}
PICKING = ("scoping", "not_suitable")    # screens 3-5: the report is in, the scope not confirmed yet

_tasks: set[asyncio.Task] = set()


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _keep(task: asyncio.Task) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _client():
    from indexing import temporal_client

    return await temporal_client()


# ----------------------------------------------------------------------
# Settings (screen 1)
# ----------------------------------------------------------------------

class TemporalBody(BaseModel):
    mode: Literal["local", "cloud"] = "local"
    address: str = ""
    namespace: str = ""
    api_key: str = ""


class SettingsBody(BaseModel):
    api_key: str = ""
    model: str = ""
    temporal: TemporalBody = Field(default_factory=TemporalBody)


class KeyCheck(BaseModel):
    api_key: str = ""
    model: str = ""


@router.get("/settings")
async def get_settings():
    try:
        return await asyncio.to_thread(tz_settings.view)
    except Exception as e:
        return _error(f"MongoDB is not reachable: {e}", 503)


@router.post("/settings/check-key")
async def check_key(body: KeyCheck):
    return await asyncio.to_thread(tz_settings.check_key, body.api_key, body.model)


@router.put("/settings")
async def save_settings(body: SettingsBody):
    if body.api_key.strip():
        check = await asyncio.to_thread(tz_settings.check_key, body.api_key, body.model)
        if not check["ok"]:
            return _error(check["error"], 400)
    if body.temporal.mode == "cloud" and not (body.temporal.address and body.temporal.namespace):
        return _error("Temporal Cloud needs an address (namespace.account.tmprl.cloud:7233) and a namespace.", 400)
    try:
        return await asyncio.to_thread(tz_settings.save, body.model_dump())
    except Exception as e:
        return _error(f"Could not save the settings: {e}", 503)


# ----------------------------------------------------------------------
# The run document as the UI reads it
# ----------------------------------------------------------------------

def _now_text(run: dict) -> str:
    status = run.get("status")
    if status == "scanning":
        running = next((c for c in run.get("checks", []) if c.get("state") == "running"), None)
        return "Scanning locally" if not running or running["id"] not in ("temporal", "anthropic", "clone") \
            else "Checking your setup"
    if status == "scan_failed":
        return "Setup check failed"
    if status == "ready":
        return "Ready to start the assessment"
    if status == "assessing":
        stages = (run.get("assessment") or {}).get("stages", [])
        running = next((s for s in stages if s.get("state") == "running"), None)
        return {"index": "Indexing with Code Atlas", "report": "Building the Temporal report",
                "variants": "Naming the systems it talks to"}.get((running or {}).get("id"), "Starting the assessment")
    if status == "scoping":
        return {3: "Waiting for your pick", 4: "Waiting for your focus", 5: "Waiting for your criteria"}.get(
            run.get("step") or 3, "Waiting for your scope")
    if status == "not_suitable":
        return "Not suitable"
    if status == "planning":
        return "Drafting the plan"
    if status == "plan_ready":
        return "Waiting for your approval"
    if status == "generating":
        stages = (run.get("generate") or {}).get("stages", [])
        running = next((s for s in stages if s.get("state") == "running"), None)
        if running and running["id"].startswith("review-"):
            return f"Review pass {running['id'].split('-')[1]} of {C.MAX_REVIEW_PASSES}"
        return (running or {}).get("label", "Generating").rstrip("…")
    if status == "done":
        n = (run.get("results") or {}).get("review_passes")
        return f"Done, after {n} review pass{'' if n == 1 else 'es'}" if n else "Done"
    if status == "failed":
        return "Failed"
    if status == "stopped":
        return "Stopped"
    return "Waiting for setup"


def _calls(run: dict) -> int:
    calls = int(run.get("calls") or 0)
    assessment = run.get("assessment") or {}
    if assessment.get("started_at") and run.get("repo", {}).get("index"):
        try:
            calls += max(0, store.index_calls(run["repo"]["index"]) - int(assessment.get("calls_base") or 0))
        except Exception:
            pass
    return calls


def _sent(run: dict) -> list[str]:
    if not (run.get("assessment") or {}).get("started_at"):
        return []
    sent = ["Source files"]
    if (run.get("scope") or {}).get("confirmed"):
        sent.append("brief")
        if (run.get("brief") or {}).get("notes"):
            sent.append("notes")
    return sent


def view(run: dict) -> dict:
    """The run document for the browser: no secrets, plus the strip and links."""
    out = {k: v for k, v in run.items() if k not in ("_id", "token", "calls")}
    out["id"] = run["_id"]
    source = dict(run.get("source") or {})
    source.pop("token", None)
    out["source"] = source
    out["strip"] = {"now": _now_text(run), "calls": _calls(run), "sent": _sent(run)}
    # The run itself always runs on the compose dev server; the POC runs on the settings' target.
    local = tz_settings.temporal_target({})
    links = {"atlas": "/atlas", "temporal_ui": local["ui_base"],
             "target_ui": tz_settings.temporal_target(tz_settings.run_settings(run))["ui_base"]}
    if (run.get("assessment") or {}).get("workflow_id"):
        links["temporal_ui"] = tz_settings.workflow_url(local, run["assessment"]["workflow_id"])
    if (run.get("generate") or {}).get("smoke", {}).get("url"):
        links["smoke"] = run["generate"]["smoke"]["url"]
    out["links"] = links
    if run.get("results"):
        try:
            from temporalize.generate.packet import packet_view

            out["packet"] = packet_view(run)
        except Exception as e:  # the results stay readable without the write-up
            logger.warning(f"Packet view for {run.get('_id')} failed: {e}")
    return out


def _load(run_id: str) -> dict | JSONResponse:
    if not re.match(RUN_PATTERN, run_id):
        return _error("Invalid run id.", 400)
    try:
        run = store.get_run(run_id)
    except Exception as e:
        return _error(f"MongoDB is not reachable: {e}", 503)
    if run is None:
        return _error(f"There is no run {run_id}.", 404)
    return run


@router.get("/runs")
async def list_runs():
    try:
        runs = await asyncio.to_thread(store.list_runs)
    except Exception as e:
        return _error(f"MongoDB is not reachable: {e}", 503)
    def row(r: dict) -> dict:
        scope = r.get("scope") or {}
        proc = next((p for p in scope.get("processes") or [] if p.get("key") == scope.get("selected")), None)
        return {"id": r["_id"], "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
                "status": r.get("status"), "step": r.get("step"), "repo": (r.get("repo") or {}).get("name"),
                "source": (r.get("source") or {}).get("url") or (r.get("source") or {}).get("path"),
                "process": proc.get("name") if proc and scope.get("confirmed") else None,
                "model": (r.get("settings") or {}).get("model"), "calls": _calls(r), "error": r.get("error"),
                "outcomes": (r.get("results") or {}).get("outcomes"),
                "criteria_summary": (r.get("results") or {}).get("criteria_summary")}

    return {"runs": [await asyncio.to_thread(row, r) for r in runs]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    return await asyncio.to_thread(view, run)


# ----------------------------------------------------------------------
# Screen 2: clone, check and scan (in the app; no model calls)
# ----------------------------------------------------------------------

class SourceBody(BaseModel):
    kind: Literal["git", "local"]
    url: str = ""
    branch: str = "main"
    token: str = ""
    path: str = ""


class NewRun(BaseModel):
    source: SourceBody


class Exclude(BaseModel):
    patterns: list[str] | None = None
    folders: list[str] = Field(default_factory=list)


class Rescan(BaseModel):
    exclude: Exclude | None = None


CHECKS = [
    ("temporal", "Connected to Temporal"),
    ("anthropic", "Anthropic key accepted"),
    ("clone", "Repository cloned"),
    ("imports", "Read imports"),
    ("how_it_runs", "Checked how it runs today"),
    ("secrets", "Scanned for secrets"),
    ("atlas", "Code Atlas is ready"),
]


def _initial_checks(kind: str, target: dict) -> list[dict]:
    lines = []
    for cid, label in CHECKS:
        if cid == "temporal":
            label = f"Connected to {target['label']}"
        if cid == "clone" and kind == "local":
            label = "Repository found"
        lines.append({"id": cid, "label": label, "detail": "", "state": "pending", "error": None})
    return lines


@router.post("/runs")
async def create_run(body: NewRun):
    from temporalize import sources

    src = body.source
    if src.kind == "git":
        try:
            url = sources.normalize_url(src.url)
        except SourceError as e:
            return _error(str(e), 400)
        source = {"kind": "git", "url": url, "branch": src.branch.strip() or "main", "token_set": bool(src.token)}
    else:
        if not src.path.strip():
            return _error("Enter the repository's folder, or pick it with Browse.", 400)
        source = {"kind": "local", "path": src.path.strip()}
    try:
        target = await asyncio.to_thread(tz_settings.temporal_target)
        snap = await asyncio.to_thread(tz_settings.snapshot)
        run = await asyncio.to_thread(store.create_run, {
            "status": "scanning", "step": 2, "source": source, "repo": {}, "settings": snap,
            "checks": _initial_checks(src.kind, target),
            "brief": {"mode": "process", "process": "", "focus": [], "notes": ""}, "criteria": [],
            "exclude": {"patterns": None, "folders": []},
        })
    except Exception as e:
        return _error(f"MongoDB is not reachable: {e}", 503)
    _keep(asyncio.create_task(_scan_run(run["_id"], src.token or None)))
    return view(run)


@router.post("/runs/{run_id}/rescan")
async def rescan(run_id: str, body: Rescan | None = None, token: str = ""):
    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if run.get("status") in LOCKED:
        return _error("The assessment has started; the scan can no longer change.", 409)
    if run.get("status") == "scanning":
        return _error("The scan is still running.", 409)
    fields: dict = {"status": "scanning"}
    if body and body.exclude is not None:
        fields["exclude"] = body.exclude.model_dump()
    target = await asyncio.to_thread(tz_settings.temporal_target)
    fields["checks"] = _initial_checks(run["source"]["kind"], target)
    await asyncio.to_thread(store.update_run, run_id, fields)
    _keep(asyncio.create_task(_scan_run(run_id, token or None)))
    return view(await asyncio.to_thread(store.get_run, run_id))


def _index_for(folder: Path, preferred: str) -> tuple[str, bool]:
    """The index for this folder: the one already built from it, else a free name. (name, exists)"""
    from config import DB_PREFIX, INDEX_META_COLLECTION
    from indexing import suggest_name
    from utils.mongo import get_mongo_client

    client = get_mongo_client()
    prefix = f"{DB_PREFIX}_"
    names = [n[len(prefix):] for n in client.list_database_names() if n.startswith(prefix)]
    for name in names:
        meta = client[f"{prefix}{name}"][INDEX_META_COLLECTION].find_one({}, {"repo_path": 1}) or {}
        if meta.get("repo_path") == str(folder):
            return name, True
    base = suggest_name(preferred)
    name, n = base, 2
    while name in names:
        name, n = f"{base}-{n}", n + 1
    return name, False


async def _scan_run(run_id: str, token: str | None) -> None:
    """The screen 2 checklist, line by line. A failed line stops the scan with its error."""
    from temporalize import scan, sources

    def line(cid: str, state: str, detail: str | None = None, error: str | None = None, label: str | None = None):
        store.set_stage(run_id, "checks", cid, state, detail, label, error)

    current = None
    try:
        run = await asyncio.to_thread(store.get_run, run_id)
        settings = await asyncio.to_thread(store.get_settings)
        target = tz_settings.temporal_target(settings)

        current = "temporal"
        await asyncio.to_thread(line, "temporal", "running", target["display_address"])
        await tz_settings.check_temporal(target)
        await asyncio.to_thread(line, "temporal", "done", target["display_address"])

        current = "anthropic"
        await asyncio.to_thread(line, "anthropic", "running")
        check = await asyncio.to_thread(tz_settings.check_key, "", settings.get("model") or "")
        if not check["ok"]:
            raise RuntimeError(check["error"])
        await asyncio.to_thread(line, "anthropic", "done")

        current = "clone"
        await asyncio.to_thread(line, "clone", "running")
        source = run["source"]
        if source["kind"] == "git":
            if run.get("repo", {}).get("path") and not token:
                folder = Path(run["repo"]["path"])  # a rescan: keep the clone
            else:
                folder = await asyncio.to_thread(sources.clone, source["url"], source["branch"], token)
            preferred = sources.repo_name(source["url"])
        else:
            folder = await asyncio.to_thread(sources.local, source["path"])
            preferred = folder.name
        head = await asyncio.to_thread(sources.head, folder)
        index, exists = await asyncio.to_thread(_index_for, folder, preferred)
        repo = {"path": str(folder), "name": preferred, "index": index, "index_exists": exists,
                "commit": head["commit"], "branch": head["branch"] or source.get("branch")}
        await asyncio.to_thread(store.update_run, run_id, {"repo": repo})

        exclude = run.get("exclude") or {}
        patterns = exclude.get("patterns")
        if patterns is None:
            patterns = list(scan.DEFAULT_EXTRA_EXCLUDES)
        exclude = {"patterns": patterns, "folders": exclude.get("folders") or []}

        branch = repo["branch"] or "HEAD"

        def on_step(step_id: str, state: str, detail: str | None = None) -> None:
            nonlocal current
            if step_id == "count":  # counting belongs to the repository line: "main, 19 Python files"
                current = "clone"
                if state == "done":
                    line("clone", "done", f"{branch}, {detail}")
                elif state == "failed":
                    line("clone", "failed", None, detail)
                return
            current = step_id
            line(step_id, state, None if state == "failed" else detail, detail if state == "failed" else None)

        result = await asyncio.to_thread(scan.scan, folder, exclude, on_step)

        current = "atlas"
        await asyncio.to_thread(line, "atlas", "running")
        await asyncio.to_thread(_atlas_ready, folder, index)
        await asyncio.to_thread(line, "atlas", "done", f"index {index}" + (" (sync)" if exists else " (new)"))

        await asyncio.to_thread(store.update_run, run_id, {
            "scan": result, "exclude": exclude, "status": "ready", "error": None,
            # Focus areas and criteria wait for Code Atlas's recommendation (screens 4 and 5).
        })
    except Exception as e:
        message = str(e) or type(e).__name__
        logger.warning(f"Run {run_id}: setup check {current} failed: {message}")
        try:
            if current:
                await asyncio.to_thread(line, current, "failed", None, message)
            await asyncio.to_thread(store.update_run, run_id, {"status": "scan_failed", "error": message})
        except Exception:
            logger.exception("Could not record the scan failure")


def _atlas_ready(folder: Path, index: str) -> None:
    """Code Atlas's own preflight (knowledge pack, Anthropic, MongoDB, Docs MCP), without starting anything."""
    from semantic_ingress.config import IngestionConfig
    from semantic_ingress.preflight import run_preflight

    from config import DB_PREFIX

    run_preflight(IngestionConfig(repo_path=folder, output_dir=Path("/tmp"), repo_id=index, prefix=DB_PREFIX), False)


# ----------------------------------------------------------------------
# Screens 3 and 4: brief and criteria
# ----------------------------------------------------------------------

class BriefBody(BaseModel):
    mode: Literal["process", "discover"] = "process"
    process: str = ""
    focus: list[str] = Field(default_factory=list, max_length=2)
    notes: str = Field(default="", max_length=20000)
    criteria: list[dict] | None = None   # an imported brief file may carry its criteria


class CriteriaBody(BaseModel):
    criteria: list[dict]


def _editable(run: dict) -> JSONResponse | None:
    """The brief and criteria are set after Code Atlas's recommendation, until the scope is confirmed."""
    if run.get("status") not in PICKING:
        return _error("The focus and criteria are set once Code Atlas has recommended a process."
                      if run.get("status") in ("scanning", "scan_failed", "ready", "assessing")
                      else "The scope is confirmed, so the focus and criteria can no longer change.", 409)
    if (run.get("scope") or {}).get("confirmed"):
        return _error("The scope is confirmed, so the focus and criteria can no longer change.", 409)
    return None


def _systems(run: dict) -> list[dict]:
    """The selected process's external systems as Code Atlas named them, in the rules' shape."""
    from temporalize import rules

    scope = run.get("scope") or {}
    return rules.tools_from_systems(scope.get("systems") or [], scope.get("selected"))


@router.put("/runs/{run_id}/brief")
async def save_brief(run_id: str, body: BriefBody):
    from temporalize import rules

    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if (bad := _editable(run)) is not None:
        return bad
    brief = body.model_dump(exclude={"criteria"})
    existing = body.criteria if body.criteria is not None else run.get("criteria") or []
    criteria = rules.suggest_criteria(run.get("scan") or {}, brief, existing, _systems(run))
    updated = await asyncio.to_thread(store.update_run, run_id, {
        "brief": brief, "criteria": criteria, "step": max(5, run.get("step") or 2)})
    return view(updated)


@router.put("/runs/{run_id}/criteria")
async def save_criteria(run_id: str, body: CriteriaBody):
    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if (bad := _editable(run)) is not None:
        return bad
    clean = []
    for i, c in enumerate(body.criteria):
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        clean.append({"id": str(c.get("id") or f"custom-{i}"), "text": text[:300], "why": str(c.get("why") or "")[:200],
                      "priority": "P0" if c.get("priority") == "P0" else "P1", "on": bool(c.get("on", True)),
                      "custom": bool(c.get("custom"))})
    updated = await asyncio.to_thread(store.update_run, run_id, {"criteria": clean})
    return view(updated)


@router.get("/runs/{run_id}/brief.json")
async def export_brief(run_id: str):
    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    body = {"temporalize_brief": 1, "repository": run.get("source", {}).get("url") or run.get("repo", {}).get("name"),
            "brief": run.get("brief"), "criteria": run.get("criteria")}
    return Response(json.dumps(body, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="temporalize-brief-{run_id}.json"'})


# ----------------------------------------------------------------------
# Start assessment (the first point where anything leaves the machine)
# ----------------------------------------------------------------------

@router.post("/runs/{run_id}/start")
async def start(run_id: str):
    from indexing import SyncStartError, begin_sync
    from temporalio.common import WorkflowIDConflictPolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from temporalize.models import RunInput
    from temporalize.worker import TASK_QUEUE

    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if run.get("status") != "ready":
        return _error("The assessment has already started." if run.get("status") in LOCKED
                      else "Finish the scan first.", 409)
    settings = await asyncio.to_thread(tz_settings.run_settings, run)
    repo = run["repo"]
    index = repo["index"]
    exists = await asyncio.to_thread(lambda: _index_for(Path(repo["path"]), repo["name"])[1])
    from temporalize.scan import index_patterns

    extra = index_patterns(run.get("exclude") or {})   # the index leaves out what the scan left out
    base = await asyncio.to_thread(store.index_calls, index)
    try:
        sync = await begin_sync(index, Path(repo["path"]), "incremental" if exists else "new",
                                memo_extra={"model": settings.get("model") or "", "exclude_extra": extra,
                                            "temporalize_run": run_id})
    except SyncStartError as e:
        return _error(str(e), e.status)
    stages = [{"id": "index", "label": "Indexed files", "detail": "", "state": "running", "error": None},
              {"id": "report", "label": "Built the Temporal report", "detail": "", "state": "pending", "error": None},
              {"id": "variants", "label": "Named the systems it talks to", "detail": "", "state": "pending", "error": None}]
    await asyncio.to_thread(store.update_run, run_id, {
        "status": "assessing", "step": 3, "assessment": {
            "started_at": store.now(), "sync_workflow_id": sync["workflow_id"], "calls_base": base,
            "workflow_id": C.workflow_id(run_id), "stages": stages}})
    try:
        client = await _client()
        await client.start_workflow(C.WORKFLOW_NAME, RunInput(run_id), id=C.workflow_id(run_id), task_queue=TASK_QUEUE,
                                    id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                                    memo={"run_id": run_id, "index": index})
    except WorkflowAlreadyStartedError:
        pass
    except Exception as e:
        logger.error(f"Starting {C.workflow_id(run_id)} failed: {e}", exc_info=True)
        await asyncio.to_thread(store.update_run, run_id, {"status": "failed", "error": f"Could not start the run: {e}"})
        return _error(f"Could not start the run workflow: {e}", 503)
    return view(await asyncio.to_thread(store.get_run, run_id))


async def _signal(run_id: str, signal: str) -> JSONResponse | None:
    try:
        client = await _client()
        await client.get_workflow_handle(C.workflow_id(run_id)).signal(signal)
    except Exception as e:
        return _error(f"Could not reach the run's workflow: {e}", 503)
    return None


# ----------------------------------------------------------------------
# Screen 5: scope
# ----------------------------------------------------------------------

class ScopeBody(BaseModel):
    selected: str
    variants: list[str] = Field(default_factory=list)       # ids of the included variants
    in_scope: list[str] | None = None                       # ids of the scope tags kept
    steps: list[str] | None = None                          # ids of the process steps in scope (None: all)
    risks: list[str] | None = None                          # ids of the failures the POC should address (None: all)
    notes: str = Field(default="", max_length=10000)        # anything else about scope, for the plan and the generator
    borderline_confirmed: bool = False


def _selection(run: dict, body: ScopeBody) -> dict | JSONResponse:
    """The run fields for the process the user picked (screen 3), or the error to return."""
    from temporalize import rules
    from temporalize.assess import in_scope_tags

    scope = run.get("scope") or {}
    proc = next((p for p in scope.get("processes", []) if p["key"] == body.selected), None)
    if proc is None:
        return _error("Pick one of the processes found.", 400)
    if proc["verdict_class"] == "not suitable":
        return _error("This process is not suitable for Temporal, so there is no POC to build. "
                      "Pick another process.", 409)
    if proc["verdict_class"] == "borderline" and not body.borderline_confirmed:
        return _error("This process is borderline. Confirm that you want to build it anyway.", 409)
    own = proc.get("variants")
    if own is None:  # a run analyzed before variants were found per process
        own = scope.get("variants", []) if body.selected == scope.get("selected") else []
    variants = [{**v, "included": v["id"] in body.variants} for v in own]
    if variants and not any(v["included"] for v in variants):
        return _error("Include at least one way the code runs this process.", 400)
    steps = [{**st, "included": body.steps is None or st["id"] in body.steps} for st in proc.get("steps") or []]
    if steps and not any(st["included"] for st in steps):
        return _error("Keep at least one step in scope.", 400)
    risks = [{**r, "included": body.risks is None or r["id"] in body.risks} for r in proc.get("risks") or []]
    tags = in_scope_tags({**proc, "variants": [v for v in variants if v["included"]]})
    if body.in_scope is not None:
        tags = [t for t in tags if t["id"] in body.in_scope] or tags
    systems = rules.tools_from_systems(scope.get("systems") or [], body.selected)
    brief = run.get("brief") or {}
    return {"scope.selected": body.selected, "scope.variants": variants, "scope.in_scope": tags,
            "scope.steps": steps, "scope.risks": risks, "scope.notes": body.notes.strip(),
            "scope.borderline_confirmed": body.borderline_confirmed, "scope.picked": True,
            "focus_options": rules.focus_options(run.get("scan") or {}, systems),
            "criteria": rules.suggest_criteria(run.get("scan") or {}, brief, run.get("criteria") or [], systems),
            "step": max(4, run.get("step") or 3)}


@router.put("/runs/{run_id}/selection")
async def save_selection(run_id: str, body: ScopeBody):
    """Screen 3: the process to build (from Code Atlas's recommendation) and what of it is in scope."""
    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if run.get("status") not in PICKING or (run.get("scope") or {}).get("confirmed"):
        return _error("A process can be picked once the report is ready, until the scope is confirmed.", 409)
    fields = _selection(run, body)
    if isinstance(fields, JSONResponse):
        return fields
    return view(await asyncio.to_thread(store.update_run, run_id, fields))


@router.post("/runs/{run_id}/scope")
async def confirm_scope(run_id: str, body: ScopeBody | None = None):
    """Screen 5: confirm the pick, the focus and the criteria; the workflow drafts the plan."""
    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if run.get("status") not in PICKING or (run.get("scope") or {}).get("confirmed"):
        return _error("Scope can be confirmed once the report is ready, and only once.", 409)
    if body is not None:
        fields = _selection(run, body)
        if isinstance(fields, JSONResponse):
            return fields
        run = await asyncio.to_thread(store.update_run, run_id, fields)
    scope = run.get("scope") or {}
    if not scope.get("picked"):
        return _error("Pick the process to build first (screen 3).", 400)
    proc = next((p for p in scope.get("processes", []) if p["key"] == scope.get("selected")), None)
    if proc is None or proc["verdict_class"] == "not suitable":
        return _error("Pick a process that is not marked not suitable.", 409)
    if not any(c.get("on") for c in run.get("criteria") or []):
        return _error("Pick at least one success criterion.", 400)
    await asyncio.to_thread(store.update_run, run_id, {
        "scope.confirmed": True, "status": "planning", "step": 6, "plan": {"status": "drafting"}})
    if (bad := await _signal(run_id, C.SCOPE_SIGNAL)) is not None:
        return bad
    return view(await asyncio.to_thread(store.get_run, run_id))


# ----------------------------------------------------------------------
# Screen 6: plan
# ----------------------------------------------------------------------

class ClaimEdit(BaseModel):
    id: str
    status: Literal["pending", "accepted", "edited", "rejected"]
    edited_text: str | None = None


class ChangeEdit(BaseModel):
    id: str
    decision: Literal["pending", "approve", "decline"]


class DataFileEdit(BaseModel):
    name: str
    content: str = Field(max_length=200_000)


class PlanBody(BaseModel):
    claims: list[ClaimEdit] | None = None
    changes: list[ChangeEdit] | None = None
    testdata_files: list[DataFileEdit] | None = None
    testdata_included: bool | None = None   # screen 6 checkbox: the synthetic test data is included
    approve: Literal["claims", "changes", "testdata", "design"] | None = None
    reopen: Literal["claims", "changes", "testdata", "design"] | None = None
    approve_all: bool = False     # the whole plan as it stands: undecided claims kept, undecided fixes included


@router.put("/runs/{run_id}/plan")
async def edit_plan(run_id: str, body: PlanBody):
    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    plan = run.get("plan") or {}
    if run.get("status") != "plan_ready" or plan.get("status") != "ready":
        return _error("The plan can be edited once it is drafted and until it is approved.", 409)
    approved = dict(plan.get("approved") or {})
    if body.claims:
        edits = {c.id: c for c in body.claims}
        for c in plan["claims"]:
            if c["id"] in edits:
                e = edits[c["id"]]
                c["status"] = e.status
                c["edited_text"] = (e.edited_text or "").strip()[:600] or None if e.status == "edited" else None
        approved["claims"] = False
    if body.changes:
        edits = {c.id: c.decision for c in body.changes}
        for c in plan["changes"]:
            c["decision"] = edits.get(c["id"], c["decision"])
        approved["changes"] = False
    if body.testdata_included is not None:
        plan["testdata"]["included"] = body.testdata_included
        approved["testdata"] = False
    if body.testdata_files is not None:
        files = {f["name"]: f for f in plan["testdata"]["files"]}
        for f in body.testdata_files:
            if f.name in files:
                files[f.name]["content"] = f.content
        approved["testdata"] = False
    if body.reopen:
        approved[body.reopen] = False
    if body.approve_all:
        for c in plan["claims"]:
            if c["status"] == "pending":
                c["status"] = "accepted"
        for c in plan["changes"]:
            if c["decision"] == "pending":
                c["decision"] = "approve"
        if not any(c["status"] in ("accepted", "edited") for c in plan["claims"]):
            return _error("Keep at least one behavior: the POC has to preserve something.", 400)
        bad = _bad_testdata(plan)
        if bad:
            return _error(bad, 400)
        if (plan.get("design_edit") or {}).get("status") in ("pending", "revising"):
            return _error("The design is being revised. Approve once the revision is in.", 409)
        approved = {s: True for s in PLAN_SECTIONS}
    if body.approve:
        if body.approve == "claims" and any(c["status"] == "pending" for c in plan["claims"]):
            return _error("Accept, edit or reject every claim first.", 400)
        if body.approve == "claims" and not any(c["status"] in ("accepted", "edited") for c in plan["claims"]):
            return _error("Keep at least one behavior: the POC has to preserve something.", 400)
        if body.approve == "changes" and any(c["decision"] == "pending" for c in plan["changes"]):
            return _error("Approve or decline every change first.", 400)
        if body.approve == "testdata" and (bad := _bad_testdata(plan)):
            return _error(bad, 400)
        approved[body.approve] = True
    plan["approved"] = approved
    updated = await asyncio.to_thread(store.update_run, run_id, {"plan": plan})
    return view(updated)


def _bad_testdata(plan: dict) -> str | None:
    if plan["testdata"].get("included") is False:
        return "The POC is checked against the original on the test data, so it has to be included."
    for f in plan["testdata"]["files"]:
        if f["name"].endswith(".json"):
            try:
                json.loads(f["content"])
            except ValueError as e:
                return f"{f['name']} is not valid JSON: {e}"
    return None


class DesignEdit(BaseModel):
    request: str = Field(min_length=3, max_length=4000)


@router.post("/runs/{run_id}/design-edit")
async def design_edit(run_id: str, body: DesignEdit):
    """Screen 6 "Make edits": the change request is stored, then the workflow revises the design."""
    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    plan = run.get("plan") or {}
    if run.get("status") != "plan_ready" or plan.get("status") != "ready":
        return _error("The design can be changed once the plan is drafted and until it is approved.", 409)
    current = plan.get("design_edit") or {}
    request = body.request.strip()
    if current.get("status") in ("pending", "revising") and current.get("request"):
        request = f"{current['request']}\n\n{request}"   # one revision covers both requests
    await asyncio.to_thread(store.update_run, run_id, {
        "plan.design_edit": {"status": "pending", "request": request, "requested_at": store.now()},
        "plan.approved.design": False})
    if (bad := await _signal(run_id, C.DESIGN_EDIT_SIGNAL)) is not None:
        return bad
    return view(await asyncio.to_thread(store.get_run, run_id))


GENERATE_STAGES = [
    ("copy", "Copied repo to {branch}"),
    ("scaffold", "Fitted business logic into the POC scaffold"),
    ("checks", "Checks: imports, lint, types, determinism"),
    ("review-1", "Code Atlas review, pass 1"),
    ("side_by_side", "Side-by-side run on the test data"),
    ("resilience", "Injected failures and a worker restart"),
    ("replay", "Replay check"),
    ("smoke", "Smoke run, stub mode"),
]


@router.post("/runs/{run_id}/approve")
async def approve(run_id: str):
    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    plan = run.get("plan") or {}
    if run.get("status") != "plan_ready":
        return _error("The plan is not ready for approval.", 409)
    missing = [s for s in PLAN_SECTIONS if not (plan.get("approved") or {}).get(s)]
    if missing:
        return _error(f"Approve every part first: {', '.join(missing)} still open.", 400)
    branch = f"temporalize/{run_id}"
    stages = [{"id": sid, "label": label.format(branch=branch), "detail": "", "state": "pending", "error": None}
              for sid, label in GENERATE_STAGES]
    await asyncio.to_thread(store.update_run, run_id, {
        "status": "generating", "step": 7,
        "generate": {"branch": branch, "started_at": store.now(), "stages": stages, "review_passes": []}})
    if (bad := await _signal(run_id, C.PLAN_SIGNAL)) is not None:
        return bad
    return view(await asyncio.to_thread(store.get_run, run_id))


@router.post("/runs/{run_id}/retry")
async def retry(run_id: str):
    """
    Try a failed or stopped run again from where it got to: decisions already made are kept,
    a drafted plan is kept, and generation starts over from a fresh workspace.
    """
    from indexing import SyncStartError, _running, begin_sync
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from temporalize.models import RunInput
    from temporalize.scan import index_patterns
    from temporalize.worker import TASK_QUEUE

    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if run.get("status") not in ("failed", "stopped"):
        return _error("Only a failed or stopped run can be tried again.", 409)
    assessment = run.get("assessment") or {}
    if not assessment.get("started_at"):
        return _error("This run never started its assessment; start it from screen 2.", 409)
    scope, plan, gen = run.get("scope") or {}, run.get("plan") or {}, run.get("generate") or {}
    approved = plan.get("status") == "ready" and all((plan.get("approved") or {}).get(s) for s in PLAN_SECTIONS)
    fields: dict = {"error": None}
    if approved and gen.get("started_at"):
        branch = f"temporalize/{run_id}"
        fields.update(status="generating", step=7, generate={
            "branch": branch, "started_at": store.now(), "review_passes": [],
            "stages": [{"id": sid, "label": label.format(branch=branch), "detail": "", "state": "pending",
                        "error": None} for sid, label in GENERATE_STAGES]})
    elif plan.get("status") == "ready":
        fields.update(status="plan_ready", step=6)
    elif scope.get("confirmed"):
        fields.update({"status": "planning", "step": 6, "plan": {"status": "drafting"}})
    elif scope.get("processes"):
        fields.update(status="scoping", step=max(3, run.get("step") or 3))
    else:
        # The index or the report did not finish: sync again (incremental: finished summaries are kept).
        repo, settings = run["repo"], await asyncio.to_thread(tz_settings.run_settings, run)
        try:
            if not await _running(await _client(), repo["index"]):
                exists = await asyncio.to_thread(lambda: _index_for(Path(repo["path"]), repo["name"])[1])
                sync = await begin_sync(repo["index"], Path(repo["path"]), "incremental" if exists else "new",
                                        memo_extra={"model": settings.get("model") or "",
                                                    "exclude_extra": index_patterns(run.get("exclude") or {}),
                                                    "temporalize_run": run_id})
                fields["assessment.sync_workflow_id"] = sync["workflow_id"]
        except SyncStartError as e:
            return _error(str(e), e.status)
        fields.update({"status": "assessing", "step": 3, "assessment.stages": [
            {"id": "index", "label": "Indexed files", "detail": "", "state": "running", "error": None},
            {"id": "report", "label": "Built the Temporal report", "detail": "", "state": "pending", "error": None},
            {"id": "variants", "label": "Named the systems it talks to", "detail": "", "state": "pending", "error": None}]})
    await asyncio.to_thread(store.update_run, run_id, fields)
    try:
        client = await _client()
        await client.start_workflow(C.WORKFLOW_NAME, RunInput(run_id, resume=True), id=C.workflow_id(run_id),
                                    task_queue=TASK_QUEUE, id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                                    memo={"run_id": run_id, "index": run["repo"]["index"], "resumed": True})
    except WorkflowAlreadyStartedError:
        return _error("The run is still running.", 409)
    except Exception as e:
        await asyncio.to_thread(store.update_run, run_id, {"status": "failed", "error": f"Could not restart: {e}"})
        return _error(f"Could not restart the run workflow: {e}", 503)
    return view(await asyncio.to_thread(store.get_run, run_id))


@router.delete("/runs/{run_id}")
async def stop(run_id: str):
    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if (run.get("assessment") or {}).get("workflow_id"):
        try:
            client = await _client()
            await client.get_workflow_handle(C.workflow_id(run_id)).cancel()
        except Exception as e:
            logger.info(f"Cancel of {run_id}: {e}")
    await asyncio.to_thread(store.update_run, run_id, {"status": "stopped"})
    return {"stopped": run_id}


# ----------------------------------------------------------------------
# Screen 8: diff, files, review packet, push
# ----------------------------------------------------------------------

def _workspace(run: dict) -> JSONResponse | None:
    if not (run.get("generate") or {}).get("workspace"):
        return _error("The POC has not been generated yet.", 409)
    return None


@router.get("/runs/{run_id}/diff")
async def get_diff(run_id: str):
    from temporalize.generate import diff

    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if (bad := _workspace(run)) is not None:
        return bad
    try:
        return PlainTextResponse(await asyncio.to_thread(diff, run))
    except Exception as e:
        return _error(f"Could not read the diff: {e}", 500)


@router.get("/runs/{run_id}/file")
async def get_file(run_id: str, path: str):
    from temporalize.generate import read_file

    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if (bad := _workspace(run)) is not None:
        return bad
    try:
        return PlainTextResponse(await asyncio.to_thread(read_file, run, path))
    except (ValueError, FileNotFoundError, IsADirectoryError) as e:
        return _error(str(e), 404)


@router.get("/runs/{run_id}/packet")
async def get_packet(run_id: str):
    from temporalize.generate import packet

    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if (bad := _workspace(run)) is not None:
        return bad
    try:
        data = await asyncio.to_thread(packet, run)
    except Exception as e:
        logger.error(f"Packet for {run_id} failed: {e}", exc_info=True)
        return _error(f"Could not build the review packet: {e}", 500)
    name = f"temporalize-{(run.get('repo') or {}).get('name', 'repo')}-{run_id}.zip"
    return StreamingResponse(io.BytesIO(data), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/runs/{run_id}/packet.html")
async def get_packet_html(run_id: str):
    """The review packet as one page (also packet.html in the export)."""
    from fastapi.responses import HTMLResponse

    from temporalize.generate.packet import packet_html

    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if not run.get("results"):
        return _error("The review packet is ready when the run finishes.", 409)
    try:
        return HTMLResponse(await asyncio.to_thread(packet_html, run))
    except Exception as e:
        logger.error(f"Packet page for {run_id} failed: {e}", exc_info=True)
        return _error(f"Could not build the review packet: {e}", 500)


class PushBody(BaseModel):
    remote_url: str = ""
    token: str = Field(min_length=1)


@router.post("/runs/{run_id}/push")
async def push_branch(run_id: str, body: PushBody):
    from temporalize.generate import push
    from temporalize.sources import normalize_url

    run = await asyncio.to_thread(_load, run_id)
    if isinstance(run, JSONResponse):
        return run
    if (bad := _workspace(run)) is not None:
        return bad
    url = body.remote_url.strip() or (run.get("source") or {}).get("url") or ""
    try:
        url = normalize_url(url)
    except SourceError as e:
        return _error(str(e), 400)
    try:
        result = await asyncio.to_thread(push, run, url, body.token.strip())
    except Exception as e:
        return _error(f"Push failed: {e}", 500)
    if not result.get("ok"):
        return _error(result.get("error") or "Push failed.", 400)
    await asyncio.to_thread(store.update_run, run_id, {"pushed": {"url": result.get("url"), "remote": url,
                                                                  "at": store.now()}})
    return result
