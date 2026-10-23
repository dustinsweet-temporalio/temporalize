"""
Build the Temporal report and store it as the repository's current report.

Shared by the `report` command and the `build_temporal_report` activity of the
sync workflow.
"""
import dataclasses
from datetime import datetime, timezone

from semantic_ingress.config import IngestionConfig
from semantic_ingress.core.llm_client import LLMClient
from semantic_ingress.preflight import PreflightResult
from semantic_ingress.report.run import ReportResult, build_report
from semantic_ingress.storage import MongoStore
from semantic_ingress.storage.staging_store import STAGING_DATABASE

PROGRESS_COLLECTION = "report_progress"   # in the staging database: {_id: repo_id, phase, detail, processes, ...}


def report_progress(store: MongoStore, repo_id: str) -> dict | None:
    """The latest report build's progress for this index (see report.run.ReportProgress)."""
    return store.client[STAGING_DATABASE][PROGRESS_COLLECTION].find_one({"_id": repo_id})


def _progress_sink(store: MongoStore, repo_id: str):
    if getattr(store, "client", None) is None:   # a store without MongoDB (tests): no progress
        return None
    collection = store.client[STAGING_DATABASE][PROGRESS_COLLECTION]
    started = datetime.now(timezone.utc).isoformat()

    def sink(state: dict) -> None:
        collection.replace_one({"_id": repo_id}, {"_id": repo_id, **state, "started_at": started,
                                                  "updated_at": datetime.now(timezone.utc).isoformat()}, upsert=True)

    return sink


def build_and_store_report(config: IngestionConfig, checks: PreflightResult, store: MongoStore,
                           repo_id: str, fresh: bool = False) -> ReportResult:
    """
    Build the Temporal report and store it as the repository's current report.

    On failure the last good report is kept, marked with the failure, and the
    error is raised to the caller.
    """
    source_root = config.source.root / repo_id
    inv_config = dataclasses.replace(config.llm, effort=config.report.investigate_effort)
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = build_report(
            LLMClient(config.llm), LLMClient(inv_config), checks.pack, checks.rubric, store, repo_id,
            checks.docs.search if checks.docs else None,
            source_root if source_root.is_dir() else None,
            fresh=fresh, progress_sink=_progress_sink(store, repo_id),
        )
    except Exception as e:
        previous = store.get_report(repo_id) or {"repo_id": repo_id}
        store.put_report(repo_id, {**previous, "status": "failed", "error": str(e), "attempted_at": now})
        raise
    store.put_report(repo_id, {
        "repo_id": repo_id, "status": "ok", "error": None, "generated_at": now, "attempted_at": now,
        "commit_sha": result.data["commit"], "pack_version": checks.pack.version,
        "rubric_version": checks.rubric.version, "cost_usd": result.data["cost_usd"],
        "data": result.data, "markdown": result.markdown,
    })
    return result
