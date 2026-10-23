"""Workspace preparation on a temporary git repository, results from canned scenario output, the packet."""
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from temporalize.generate.results import (
    build_results,
    changes_summary,
    criteria_results,
    criteria_summary,
    packet,
)
from temporalize.generate.workspace import diff, plan_markdown, prepare_workspace

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

CRITERIA = [
    {"id": "restart", "text": "Survives a worker restart", "priority": "P0", "on": True},
    {"id": "retry", "text": "Failed logins retry", "priority": "P0", "on": True},
    {"id": "same", "text": "Same report as the original", "priority": "P0", "on": True},
    {"id": "alert", "text": "Failures send an alert", "priority": "P0", "on": True},
    {"id": "schedules", "text": "Polling replaced by Schedules", "priority": "P1", "on": False},
]

PROCESS = {"name": "Daily timesheet audit", "entry": "audit.py",
           "card": {"trigger": {"text": "cron"}, "steps": [], "files": ["audit.py"]},
           "design": {"elements": [{"kind": "workflow", "name": "AuditWorkflow", "files": ["audit.py"], "does": "Runs it"}],
                      "removes": [], "stays": [], "why": "x"}}


def _run(repo_path: str, commit: str = "") -> dict:
    return {
        "_id": "r-0007",
        "repo": {"path": repo_path, "commit": commit, "name": "demo"},
        "brief": {"focus": ["recover"]},
        "criteria": CRITERIA,
        "scope": {"selected": "p1", "processes": [{"key": "p1", "name": "Daily timesheet audit"}],
                  "variants": [{"id": "v1", "name": "Weekday mornings", "trigger": "systemd timer", "included": True},
                               {"id": "v2", "name": "On demand", "trigger": "CLI", "included": False}]},
        "plan": {
            "claims": [{"id": "c1", "text": "Audits two days", "status": "accepted", "refs": ["audit.py@L1-2"]},
                       {"id": "c2", "text": "Rates notes", "status": "edited", "edited_text": "Rates each note once"},
                       {"id": "c3", "text": "Wrong claim", "status": "rejected"}],
            "changes": [{"id": "x1", "title": "Fix the Monday start date", "detail": "Use Thursday", "decision": "approve"},
                        {"id": "x2", "title": "Drop retries", "detail": "", "decision": "decline"}],
            "testdata": {"summary": "Fake shifts", "frozen_time": "2026-10-19T07:00:00",
                         "failures": [{"boundary": "login", "times": 2}],
                         "files": [{"name": "shifts.json", "content": "[{\"id\": 1}]"},
                                   {"name": "../evil.json", "content": "{}"}]},
            "design": {"summary": "1 workflow", "elements": [{"kind": "workflow", "name": "AuditWorkflow", "does": "Runs it"}]},
        },
        "generate": {},
    }


def test_plan_markdown_has_only_approved_items():
    md = plan_markdown(_run("/x"), PROCESS)
    assert "Audits two days" in md and "Rates each note once" in md and "Wrong claim" not in md
    assert "[x1] Fix the Monday start date" in md and "Declined" in md and "Drop retries" in md
    assert "[restart] P0" in md and "schedules" not in md
    assert "Weekday mornings" in md and "On demand" not in md
    assert "fails 2 time(s)" in md


def test_criteria_results_and_summary():
    scenarios = {
        "resilience": {"criteria": [{"id": "restart", "passed": True, "evidence": "Finished on worker B"},
                                    {"id": "retry", "passed": True, "evidence": "Attempt 3"}]},
        "side_by_side": {"criteria": [{"id": "same", "passed": True, "evidence": "42 rows"},
                                      {"id": "retry", "passed": False, "evidence": "Not in this scenario"}]},
    }
    res = criteria_results(CRITERIA, scenarios)
    by = {r["id"]: r for r in res}
    assert by["restart"]["passed"] and not by["retry"]["passed"]
    assert by["alert"] == {**by["alert"], "passed": False, "evidence": "Not checked by the harness"}
    assert "schedules" not in by
    assert criteria_summary(CRITERIA, res) == "2 of 4 P0 passed. 1 P1 not selected."
    all_pass = [{**r, "passed": True} for r in res]
    assert criteria_summary(CRITERIA, all_pass) == "4 of 4 P0 passed. 1 P1 not selected."


def test_changes_summary():
    rows = [{"status": "A", "path": "temporal-poc/workflows.py"}, {"status": "A", "path": "temporal-poc/activities.py"},
            {"status": "M", "path": "audit.py"}, {"status": "A", "path": "temporal-poc/legacy/audit.py"}]
    text = changes_summary(rows)
    assert "1 original file changed (audit.py)." in text
    assert "Originals moved to temporal-poc/legacy/." in text
    assert "3 files added in temporal-poc/." in text
    assert changes_summary([{"status": "A", "path": "temporal-poc/x.py"}]).startswith("Original files left unchanged.")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    (src / "audit.py").write_text("def run():\n    return 42\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "init")
    return src


@needs_git
def test_prepare_workspace_and_results(repo: Path, tmp_path: Path):
    commit = _git(repo, "rev-parse", "HEAD").strip()
    run = _run(str(repo), commit)
    out = prepare_workspace(run, PROCESS, ws_root=tmp_path / "ws")
    ws = Path(out["workspace"])
    assert out == {"workspace": str(ws), "branch": "temporalize/r-0007", "base_commit": commit}
    assert _git(ws, "rev-parse", "--abbrev-ref", "HEAD").strip() == "temporalize/r-0007"
    poc = ws / "temporal-poc"
    assert (poc / "tz_harness.py").exists() and (poc / "PLAN.md").exists()
    assert json.loads((poc / "testdata" / "shifts.json").read_text()) == [{"id": 1}]
    assert (poc / "testdata" / "evil.json").exists() and not (ws / "evil.json").exists()
    meta = json.loads((poc / "testdata" / "failures.json").read_text())
    assert meta["run_id"] == "r-0007" and meta["failures"][0]["boundary"] == "login"
    assert _git(ws, "status", "--porcelain").strip() == ""   # committed; .tz is ignored

    # Retry-safe: preparing again starts from a clean clone.
    (poc / "junk.py").write_text("x")
    out2 = prepare_workspace(run, PROCESS, ws_root=tmp_path / "ws")
    assert out2 == out and not (poc / "junk.py").exists()

    # The generator's work, then the results.
    run["generate"] = {**out, "review_passes": [{"n": 1, "findings": [{"severity": "low", "title": "naming"}]}]}
    (poc / "workflows.py").write_text("from temporalio import workflow\n")
    (ws / "audit.py").write_text("def run():\n    return 43\n")
    text = diff(run)
    assert "temporal-poc/workflows.py" in text and "audit.py" in text
    scenarios = {
        "side_by_side": {"ok": True, "checks": [{"check": "Report rows", "original": "42", "temporal": "42", "result": "match"},
                                                 {"check": "Monday start", "original": "Sat", "temporal": "Thu", "result": "intended"}],
                         "criteria": [{"id": "same", "passed": True, "evidence": "42 rows"}], "workflow_ids": ["a"]},
        "resilience": {"ok": True, "checks": [{"check": "Login fails twice", "original": "No report",
                                               "temporal": "Report sent on attempt 3", "result": "odd"}],
                       "criteria": [{"id": "restart", "passed": True, "evidence": "B finished"}]},
        "replay": {"ok": True, "checks": []},
        "smoke": {"ok": False, "workflow_ids": ["tz-r-0007-smoke"]},
    }
    settings = {"temporal": {"mode": "cloud", "address": "ns.acct.tmprl.cloud:7233", "namespace": "ns.acct", "api_key": "k"}}
    res = build_results(run, scenarios, settings)
    assert [o["ok"] for o in res["outcomes"]] == [False, True, None]
    assert [r["result"] for r in res["comparison"]] == ["match", "intended", "mismatch"]
    assert res["criteria_summary"] == "2 of 4 P0 passed. 1 P1 not selected."
    assert res["diff_stats"]["files"] >= 2 and res["review_passes"] == 1 and res["leftover_findings"] == []
    assert res["temporal_ui_url"] == "https://cloud.temporal.io/namespaces/ns.acct/workflows/tz-r-0007-smoke"
    assert any("smoke" in x for x in res["limitations"])
    assert _git(ws, "log", "-1", "--format=%s").strip() == "Temporalize: POC"

    run["results"] = res
    z = zipfile.ZipFile(io.BytesIO(packet(run)))
    names = set(z.namelist())
    assert {"README.md", "diff.patch", "PLAN.md", "testdata/shifts.json", "temporal-poc/workflows.py"} <= names
    readme = z.read("README.md").decode()
    assert "Report sent on attempt 3" in readme and "2 of 4 P0 passed" in readme
