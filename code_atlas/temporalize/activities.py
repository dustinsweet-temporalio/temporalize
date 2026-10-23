"""
Activities of TemporalizeWorkflow. Each reads the run document by id, does one step,
and writes what it produced (and its checklist line) back to the run document, so a
retry is harmless and the UI follows along by polling the document.

Synchronous steps (model calls, git, subprocesses) run on the worker's thread pool and
heartbeat from a helper thread. Model errors are classified as for indexing
(semantic_ingress translated_errors): permanent ones are not retried.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Callable

import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from temporalize import constants as C
from temporalize import store
from temporalize.models import MarkInput, RunInput, RunState, StepInput, StepResult

logger = structlog.get_logger()

GEN = "generate.stages"
REVIEW_LABEL = "Code Atlas review, pass {n}"
SCENARIO_LABELS = {
    "side_by_side": "Side-by-side run on the test data",
    "resilience": "Injected failures and a worker restart",
    "replay": "Replay check",
    "smoke": "Smoke run, stub mode",
}


def invalid(message: str) -> ApplicationError:
    return ApplicationError(message[:C.ERROR_CHARS], type=C.TZ_INVALID, non_retryable=True)


def _run_doc(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise invalid(f"Run {run_id} no longer exists.")
    return run


_store = None


def _report_store():
    global _store
    if _store is None:
        from semantic_ingress.config import MongoConfig
        from semantic_ingress.storage import MongoStore

        from config import DB_PREFIX

        _store = MongoStore(MongoConfig(), DB_PREFIX)
    return _store


def _report(run: dict) -> dict:
    doc = _report_store().get_report(run["repo"]["index"])
    if not doc or not doc.get("data"):
        detail = f" {doc.get('error')}" if doc and doc.get("error") else ""
        raise invalid(f"Code Atlas did not produce a Temporal report for this repository.{detail}")
    return doc["data"]


def _process(run: dict) -> dict:
    from temporalize.assess import process_by_key

    key = (run.get("scope") or {}).get("selected")
    process = process_by_key(_report(run), key) if key else None
    if process is None:
        raise invalid("The selected process is no longer in the report. Confirm the scope again.")
    return process


def _llm(run_id: str):
    """The run's model (as captured when it was created), with today's key."""
    from temporalize.llm import RunLLM
    from temporalize.settings import run_settings

    return RunLLM(run_id, run_settings(_run_doc(run_id)))


def _sync(fn: Callable, *args):
    """Run a step with heartbeats and classified errors."""
    from semantic_ingress.temporal.activities import heartbeating, translated_errors

    with translated_errors(), heartbeating(C.HEARTBEAT_INTERVAL_SECONDS):
        return fn(*args)


def _beat(message: str) -> None:
    try:
        activity.heartbeat(message)
    except Exception:
        pass


class TemporalizeActivities:
    def __init__(self, client) -> None:
        self.client = client   # the Temporal client the worker uses (to follow the index sync)

    def all(self) -> list[Callable]:
        return [self.wait_for_index, self.analyze_scope, self.draft_plan, self.revise_design, self.mark_design_edit,
                self.prepare_workspace, self.generate_code, self.run_checks, self.review_code, self.fix_code,
                self.run_scenario, self.finalize_results, self.mark_run, self.run_state]

    # ------------------------------------------------------------------
    # Screen 5
    # ------------------------------------------------------------------

    @activity.defn(name=C.WAIT_FOR_INDEX)
    async def wait_for_index(self, inp: RunInput) -> StepResult:
        """Follow the index sync until it closes, mirroring its progress onto the checklist."""
        from semantic_ingress.temporal.constants import PROGRESS_QUERY
        from semantic_ingress.temporal.models import Progress
        from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError

        run = await asyncio.to_thread(_run_doc, inp.run_id)
        sync_id = run["assessment"]["sync_workflow_id"]
        stage = "assessment.stages"
        handle = self.client.get_workflow_handle(sync_id)
        while True:
            activity.heartbeat()
            description = await handle.describe()
            if description.status != WorkflowExecutionStatus.RUNNING:
                break
            try:
                p: Progress = await handle.query(PROGRESS_QUERY, result_type=Progress,
                                                 rpc_timeout=timedelta(seconds=3))
            except Exception:
                p = None
            await asyncio.to_thread(self._mirror_progress, inp.run_id, stage, p,
                                    run["repo"]["index"], run["assessment"].get("started_at") or "")
            await asyncio.sleep(3)
        try:
            await handle.result()
        except WorkflowFailureError as e:
            from indexing import _failure_message

            await asyncio.to_thread(store.set_stage, inp.run_id, stage, "index", "failed", None, None,
                                    _failure_message(e))
            raise invalid(f"Indexing failed: {_failure_message(e)}") from None

        def finish() -> int:
            from config import FILE_SUMMARIES_COLLECTION
            from utils.mongo import get_mongo_client

            from temporalize.assess import report_processes

            index = run["repo"]["index"]
            db = get_mongo_client()[f"{_prefix()}_{index}"]
            files = db[FILE_SUMMARIES_COLLECTION].estimated_document_count()
            folders = db["folder_summaries"].estimated_document_count()
            store.set_stage(inp.run_id, stage, "index", "done", f"{files} files, {folders} folders")
            store.update_run(inp.run_id, {"assessment.index": {
                "phase": "done", "files_done": files, "files_total": files, "folders_done": folders,
                "folders_total": folders, "updated_at": store.now()}})
            try:
                n = len(report_processes(_report(run)))
            except ApplicationError as e:
                store.set_stage(inp.run_id, stage, "report", "failed", None, None, e.message)
                raise
            store.set_stage(inp.run_id, stage, "report", "done", f"{n} process{'' if n == 1 else 'es'} found")
            rp = _report_progress(index, run["assessment"].get("started_at") or "")
            if rp:
                store.update_run(inp.run_id, {"assessment.report": {**{k: rp.get(k) for k in ("processes", "started_at")},
                                                                    "phase": "done", "detail": "Done",
                                                                    "finished_at": store.now()}})
            if n == 0:
                store.set_stage(inp.run_id, stage, "variants", "failed", None, None,
                                "The report found no process to assess.")
                raise invalid("The Temporal report found no process in this repository to build a POC from.")
            return n

        n = await asyncio.to_thread(finish)
        return StepResult(count=n)

    @staticmethod
    def _mirror_progress(run_id: str, stage: str, p, index: str = "", since: str = "") -> None:
        if p is None:
            store.set_stage(run_id, stage, "index", "running", "Waiting for the indexer worker")
            return
        counts = index_counts(p)
        if counts:
            store.update_run(run_id, {"assessment.index": {**counts, "updated_at": store.now()}})
        if p.phase in ("STARTING", "PLAN"):
            store.set_stage(run_id, stage, "index", "running", "Comparing the repository with the index")
        elif p.phase == "FILES":
            store.set_stage(run_id, stage, "index", "running", f"{p.done} of {p.total}")
        elif p.phase in ("LENS_REFRESH", "FOLDERS"):
            store.set_stage(run_id, stage, "index", "running", f"Folders: {p.done} of {p.total}")
        elif p.phase == "REPORT":
            n = p.plan.n_files if p.plan else None
            store.set_stage(run_id, stage, "index", "done", f"{n} of {n}" if n else "Done")
            rp = _report_progress(index, since)
            store.set_stage(run_id, stage, "report", "running", (rp or {}).get("detail") or "Starting")
            if rp:
                store.update_run(run_id, {"assessment.report": {k: rp.get(k) for k in
                                                                ("phase", "detail", "processes", "started_at")}})
        else:
            store.set_stage(run_id, stage, "index", "running", "Writing the index")

    @activity.defn(name=C.ANALYZE_SCOPE)
    def analyze_scope(self, inp: RunInput) -> StepResult:
        from temporalize.assess import analyze_scope

        run = _run_doc(inp.run_id)
        stage = "assessment.stages"
        store.set_stage(inp.run_id, stage, "variants", "running", "")

        def go() -> dict:
            return analyze_scope(_llm(inp.run_id), run, _report(run))

        scope = _sync(go)
        n = len(scope["systems"])
        store.set_stage(inp.run_id, stage, "variants", "done",
                        f"{n} system{'' if n == 1 else 's'} it talks to" if n else "Done")
        any_ok = any(p["verdict_class"] != "not suitable" for p in scope["processes"])
        store.update_run(inp.run_id, {"scope": scope, "status": "scoping" if any_ok else "not_suitable",
                                      "step": max(3, run.get("step") or 3)})
        return StepResult(count=len(scope["processes"]))

    # ------------------------------------------------------------------
    # Screen 6
    # ------------------------------------------------------------------

    @activity.defn(name=C.DRAFT_PLAN)
    def draft_plan(self, inp: RunInput) -> StepResult:
        from temporalize.assess import draft_plan

        run = _run_doc(inp.run_id)
        store.update_run(inp.run_id, {"plan.status": "drafting"})
        plan = _sync(lambda: draft_plan(_llm(inp.run_id), run, _process(run)))
        store.update_run(inp.run_id, {"plan": plan, "status": "plan_ready"})
        return StepResult(count=len(plan["claims"]))

    @activity.defn(name=C.REVISE_DESIGN)
    def revise_design(self, inp: RunInput) -> StepResult:
        """Apply the user's design change request (screen 6 "Make edits"), with the skill and the docs."""
        from temporalize.assess import design_poc

        run = _run_doc(inp.run_id)
        plan = run.get("plan") or {}
        edit = plan.get("design_edit") or {}
        request = (edit.get("request") or "").strip()
        if not request or edit.get("status") == "done":
            return StepResult()
        store.update_run(inp.run_id, {"plan.design_edit.status": "revising", "plan.design_edit.started_at": store.now()})
        design, research = _sync(lambda: design_poc(_llm(inp.run_id), run, _process(run), plan, request,
                                                     plan.get("design")))
        fresh = _run_doc(inp.run_id)
        latest = (fresh.get("plan") or {}).get("design_edit") or {}
        edits = list((fresh.get("plan") or {}).get("design_edits") or [])
        edits.append({"request": request, "note": design.get("change_note", ""), "at": store.now()})
        fields = {"plan.design": design, "plan.design_edits": edits, "plan.approved.design": False,
                  "plan.research.design_edit": research}
        # A request typed while this one ran stays pending (the workflow revises again); otherwise this one is done.
        if (latest.get("request") or "").strip() == request:
            fields["plan.design_edit"] = {"status": "done", "request": "", "finished_at": store.now()}
        store.update_run(inp.run_id, fields)
        return StepResult()

    @activity.defn(name=C.MARK_DESIGN_EDIT)
    def mark_design_edit(self, inp: MarkInput) -> StepResult:
        store.update_run(inp.run_id, {"plan.design_edit.status": "failed", "plan.design_edit.error": inp.error or None})
        return StepResult()

    # ------------------------------------------------------------------
    # Screen 7
    # ------------------------------------------------------------------

    @activity.defn(name=C.PREPARE_WORKSPACE)
    def prepare_workspace(self, inp: RunInput) -> StepResult:
        from temporalize.generate import prepare_workspace

        run = _run_doc(inp.run_id)
        branch = f"temporalize/{inp.run_id}"
        store.set_stage(inp.run_id, GEN, "copy", "running")
        ws = _sync(prepare_workspace, run, _process(run))
        from pathlib import Path

        from temporalize.generate.agent import reset_sessions

        reset_sessions(Path(ws["workspace"]))
        store.update_run(inp.run_id, {"generate.workspace": ws["workspace"], "generate.branch": ws["branch"],
                                      "generate.base_commit": ws["base_commit"]})
        store.set_stage(inp.run_id, GEN, "copy", "done", "", f"Copied repo to {ws.get('branch') or branch}")
        return StepResult()

    @activity.defn(name=C.GENERATE_CODE)
    def generate_code(self, inp: StepInput) -> StepResult:
        """One chunk of the generator's conversation; ok=False means the workflow calls again."""
        from temporalize.generate import generate_code

        run = _run_doc(inp.run_id)

        def beat(message: str) -> None:  # "turn 12": shown on the checklist line
            _beat(message)
            store.set_stage(inp.run_id, GEN, "scaffold", "running", f"Writing the workflow and activities, {message}")

        store.set_stage(inp.run_id, GEN, "scaffold", "running", "Writing the workflow and activities")
        out = _sync(generate_code, _llm(inp.run_id), run, _process(run), beat)
        if not out.get("done", True):
            return StepResult(ok=False, count=out.get("turns", 0))
        n = len(out.get("files_changed") or [])
        store.set_stage(inp.run_id, GEN, "scaffold", "done", f"{n} file{'' if n == 1 else 's'}, {out.get('turns', 0)} turns")
        store.update_run(inp.run_id, {"generate.summary": out.get("summary", ""), "generate.research": out.get("research") or {}})
        return StepResult(count=n)

    @activity.defn(name=C.RUN_CHECKS)
    def run_checks(self, inp: StepInput) -> StepResult:
        from temporalize.generate import run_checks

        run = _run_doc(inp.run_id)
        store.set_stage(inp.run_id, GEN, "checks", "running", "" if inp.n == 0 else f"Re-check {inp.n}")
        out = _sync(run_checks, run)
        failed = [i["label"] for i in out["items"] if not i["ok"]]
        store.update_run(inp.run_id, {"generate.checks": out})
        if out["ok"]:
            store.set_stage(inp.run_id, GEN, "checks", "done", "All passed" if inp.n == 0 else "Passed after a fix")
        elif inp.n >= C.MAX_FIX_ATTEMPTS:
            store.set_stage(inp.run_id, GEN, "checks", "failed", f"Still failing: {', '.join(failed)}")
        else:
            store.set_stage(inp.run_id, GEN, "checks", "running", f"Fixing: {', '.join(failed)}")
        return StepResult(ok=out["ok"], count=len(failed))

    @activity.defn(name=C.REVIEW_CODE)
    def review_code(self, inp: StepInput) -> StepResult:
        from temporalize.generate import review_code

        run = _run_doc(inp.run_id)
        sid = f"review-{inp.n}"
        store.set_stage(inp.run_id, GEN, sid, "running", f"pass {inp.n} of {C.MAX_REVIEW_PASSES}",
                        REVIEW_LABEL.format(n=inp.n))
        out = _sync(review_code, _llm(inp.run_id), run, _process(run), inp.n)
        findings = out.get("findings", [])
        blocking = [f for f in findings if f.get("severity") in ("high", "medium")]
        passes = [p for p in (run.get("generate") or {}).get("review_passes", []) if p.get("n") != inp.n]
        passes.append({"n": inp.n, "findings": findings, "blocking": len(blocking), "fixed": 0})
        store.update_run(inp.run_id, {"generate.review_passes": sorted(passes, key=lambda p: p["n"])})
        if not blocking:
            detail = "No findings" if not findings else f"{len(findings)} minor finding{'s' if len(findings) > 1 else ''}"
            store.set_stage(inp.run_id, GEN, sid, "done", detail)
        elif inp.n >= C.MAX_REVIEW_PASSES:
            store.set_stage(inp.run_id, GEN, sid, "done", f"{len(blocking)} left, listed in the results")
        else:
            store.set_stage(inp.run_id, GEN, sid, "running", f"{len(blocking)} findings, fixing")
        return StepResult(count=len(blocking))

    @activity.defn(name=C.FIX_CODE)
    def fix_code(self, inp: StepInput) -> StepResult:
        from temporalize.generate import fix_code

        run = _run_doc(inp.run_id)
        gen = run.get("generate") or {}
        if inp.kind == "checks":
            details = gen.get("checks") or {}
        elif inp.kind == "review":
            details = next((p for p in gen.get("review_passes", []) if p["n"] == inp.n), {})
        else:
            details = (gen.get("scenarios") or {}).get(inp.scenario) or {}
            details = {**details, "scenario": inp.scenario}
        session = f"fix-{inp.kind}-{inp.scenario or inp.n}" + (f"-{inp.n}" if inp.scenario else "")
        out = _sync(fix_code, _llm(inp.run_id), run, _process(run), inp.kind, details, _beat, session)
        if not out.get("done", True):
            return StepResult(ok=False, count=out.get("turns", 0))
        passes = gen.get("review_passes", [])
        this = next((p for p in passes if p["n"] == inp.n), None) if inp.kind == "review" else None
        if this is not None:
            this["fixed"] = this.get("blocking", 0)
            this["fix_summary"] = out.get("summary", "")
            store.update_run(inp.run_id, {"generate.review_passes": passes})
            store.set_stage(inp.run_id, GEN, f"review-{inp.n}", "done", f"{this['fixed']} findings, all fixed")
            store.set_stage(inp.run_id, GEN, f"review-{inp.n + 1}", "pending", "", REVIEW_LABEL.format(n=inp.n + 1))
        return StepResult(detail=out.get("summary", "")[:500])

    @activity.defn(name=C.RUN_SCENARIO)
    def run_scenario(self, inp: StepInput) -> StepResult:
        from temporalize.generate import run_scenario
        from temporalize.settings import run_settings, temporal_target, workflow_url

        run = _run_doc(inp.run_id)
        label = SCENARIO_LABELS.get(inp.scenario, inp.scenario)
        note = "" if inp.n == 0 else ("Final run" if inp.n == 99 else f"Re-run {inp.n}")
        store.set_stage(inp.run_id, GEN, inp.scenario, "running", note, label)
        settings = run_settings(run)
        out = _sync(run_scenario, run, inp.scenario, settings)
        fields = {f"generate.scenarios.{inp.scenario}": out}
        detail = f"{sum(1 for c in out.get('checks', []) if c.get('result') != 'mismatch')} of " \
                 f"{len(out.get('checks', []))} checks" if out.get("checks") else ""
        if inp.scenario == "smoke" and out.get("workflow_ids"):
            target = temporal_target(settings)
            wid = out["workflow_ids"][0]
            fields["generate.smoke"] = {"workflow_id": wid, "url": workflow_url(target, wid)}
            detail = "Opens in Temporal UI"
        store.update_run(inp.run_id, fields)
        if out.get("ok"):
            store.set_stage(inp.run_id, GEN, inp.scenario, "done", detail)
        elif inp.n >= C.MAX_FIX_ATTEMPTS:
            store.set_stage(inp.run_id, GEN, inp.scenario, "failed", detail, None,
                            (out.get("notes") or out.get("output") or "Failed")[-400:])
        else:
            store.set_stage(inp.run_id, GEN, inp.scenario, "running", "Failed, fixing")
        return StepResult(ok=bool(out.get("ok")))

    @activity.defn(name=C.FINALIZE_RESULTS)
    def finalize_results(self, inp: RunInput) -> StepResult:
        from temporalize.generate import build_results

        run = _run_doc(inp.run_id)
        scenarios = (run.get("generate") or {}).get("scenarios") or {}
        from temporalize.settings import run_settings

        results = _sync(build_results, run, scenarios, run_settings(run), _llm(inp.run_id))
        store.update_run(inp.run_id, {"results": results, "status": "done", "step": 8,
                                      "generate.finished_at": store.now()})
        return StepResult()

    @activity.defn(name=C.RUN_STATE)
    def run_state(self, inp: RunInput) -> RunState:
        """What a retried run can skip, from the run document."""
        run = _run_doc(inp.run_id)
        scope, plan = run.get("scope") or {}, run.get("plan") or {}
        return RunState(scoped=bool(scope.get("processes")), scope_confirmed=bool(scope.get("confirmed")),
                        planned=plan.get("status") == "ready",
                        approved=bool(plan.get("approved")) and all((plan.get("approved") or {}).values())
                        and (run.get("generate") or {}).get("started_at") is not None)

    @activity.defn(name=C.MARK_RUN)
    def mark_run(self, inp: MarkInput) -> StepResult:
        run = store.get_run(inp.run_id)
        if run is None:
            return StepResult()
        store.update_run(inp.run_id, {"status": inp.status, "error": inp.error or None})
        return StepResult()


def index_counts(p) -> dict | None:
    """Live file and folder counts from the sync's progress query (screen 3): done and total of each."""
    plan = p.plan
    if plan is None or p.phase in ("STARTING", "PLAN"):
        return None
    files_total = plan.n_files
    folders_total = sum(level.count for level in plan.levels) if plan.levels else None
    if p.phase == "FILES":
        files_done, folders_done = p.done, 0
    elif p.phase == "LENS_REFRESH":
        files_done, folders_done = files_total, 0
    elif p.phase == "FOLDERS":
        files_done, folders_done = files_total, p.done
        folders_total = p.total or folders_total
    else:  # REPO, PERSIST, FINALIZE, MIRROR, REPORT, ...
        files_done, folders_done = files_total, folders_total or 0
    return {"phase": p.phase.lower(), "mode": plan.mode, "files_done": files_done, "files_total": files_total,
            "folders_done": folders_done, "folders_total": folders_total, "repo_files": plan.n_snapshot_files or None,
            "unchanged": plan.n_unchanged}


def _report_progress(index: str, since: str) -> dict | None:
    """The report build's live progress for this index, if it belongs to this run's build."""
    from semantic_ingress.report.service import report_progress

    try:
        rp = report_progress(_report_store(), index)
    except Exception:
        return None
    return rp if rp and (rp.get("started_at") or "") >= since else None


def _prefix() -> str:
    from config import DB_PREFIX

    return DB_PREFIX
