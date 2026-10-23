"""
Temporalize state in MongoDB (database `code_atlas_temporalize`, which is not an index
database, so /api/repos never lists it).

    settings      one document: the Anthropic key, the model and the Temporal target
    runs          one document per run (r-0001, ...): everything every screen shows
    counters      run-id sequence

Model calls made by indexing are counted by the indexer in `code_atlas_ingest.model_calls`
(semantic_ingress.runtime); a run's own calls are counted on the run document.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pymongo import ReturnDocument

DATABASE = "code_atlas_temporalize"
SETTINGS_ID = "settings"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db():
    from utils.mongo import get_mongo_client

    return get_mongo_client()[DATABASE]


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def get_settings() -> dict:
    return _db().settings.find_one({"_id": SETTINGS_ID}) or {"_id": SETTINGS_ID}


def save_settings(fields: dict) -> dict:
    return _db().settings.find_one_and_update(
        {"_id": SETTINGS_ID}, {"$set": {**fields, "updated_at": now()}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )


# ----------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------

def next_run_id() -> str:
    doc = _db().counters.find_one_and_update(
        {"_id": "runs"}, {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return f"r-{doc['n']:04d}"


def create_run(doc: dict) -> dict:
    doc = {"_id": next_run_id(), "created_at": now(), "updated_at": now(), "calls": 0, **doc}
    _db().runs.insert_one(doc)
    return doc


def get_run(run_id: str) -> dict | None:
    return _db().runs.find_one({"_id": run_id})


def list_runs(limit: int = 20) -> list[dict]:
    fields = {"_id": 1, "created_at": 1, "updated_at": 1, "status": 1, "step": 1, "repo": 1, "source.kind": 1,
              "source.url": 1, "source.path": 1, "scope.selected": 1, "scope.confirmed": 1, "scope.processes.key": 1,
              "scope.processes.name": 1, "calls": 1, "assessment.started_at": 1, "assessment.calls_base": 1,
              "error": 1, "settings.model": 1, "results.outcomes": 1, "results.criteria_summary": 1}
    return list(_db().runs.find({}, fields).sort("created_at", -1).limit(limit))


def update_run(run_id: str, fields: dict[str, Any], unset: list[str] | None = None) -> dict | None:
    """$set dotted fields (and $unset others) on a run; returns the updated document."""
    update: dict[str, Any] = {"$set": {**fields, "updated_at": now()}}
    if unset:
        update["$unset"] = {k: "" for k in unset}
    return _db().runs.find_one_and_update({"_id": run_id}, update, return_document=ReturnDocument.AFTER)


def add_calls(run_id: str, n: int = 1) -> None:
    _db().runs.update_one({"_id": run_id}, {"$inc": {"calls": n}})


def set_stage(run_id: str, group: str, stage_id: str, state: str, detail: str | None = None,
              label: str | None = None, error: str | None = None) -> None:
    """
    Upsert one line of a checklist (`group` is checks, assessment.stages or generate.stages):
    state is pending, running, done, failed or skipped.
    """
    db = _db().runs
    line = {"id": stage_id, "state": state, "error": error}
    if detail is not None:
        line["detail"] = detail
    if label is not None:
        line["label"] = label
    sets = {f"{group}.$.{k}": v for k, v in line.items()}
    result = db.update_one({"_id": run_id, f"{group}.id": stage_id}, {"$set": {**sets, "updated_at": now()}})
    if result.matched_count == 0:  # no such line yet: append it (a later review pass goes after the last one)
        push: dict[str, Any] = {"$each": [{"label": label or stage_id, "detail": "", **line}]}
        if stage_id.startswith("review-"):
            doc = db.find_one({"_id": run_id}, {group: 1}) or {}
            for part in group.split("."):
                doc = doc.get(part, {}) if isinstance(doc, dict) else {}
            ids = [s.get("id", "") for s in doc] if isinstance(doc, list) else []
            reviews = [i for i, sid in enumerate(ids) if sid.startswith("review-")]
            if reviews:
                push["$position"] = reviews[-1] + 1
        db.update_one({"_id": run_id}, {"$push": {group: push}, "$set": {"updated_at": now()}})


def index_calls(repo_id: str) -> int:
    """Model calls the indexer has made for this index so far (all runs)."""
    from utils.mongo import get_mongo_client
    from semantic_ingress.runtime import index_call_key, read_call_count

    return read_call_count(get_mongo_client(), index_call_key(repo_id))
