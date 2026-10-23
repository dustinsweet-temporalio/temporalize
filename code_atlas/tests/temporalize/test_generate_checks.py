"""Determinism scanner, path safety of the agent's tools, and the static checks on a sample POC."""
from pathlib import Path

import pytest

from temporalize.generate.agent import Tools
from temporalize.generate.checks import check_determinism, scan_determinism
from temporalize.generate.workspace import WorkspaceError, safe_path

WORKFLOW = '''
import asyncio, datetime, random, time, uuid, os
from temporalio import workflow

@workflow.defn
class Audit:
    @workflow.run
    async def run(self, day: str) -> str:
        now = datetime.datetime.now()
        stamp = time.time()
        pick = random.choice([1, 2])
        rid = uuid.uuid4()
        key = os.environ.get("KEY")
        with open("x") as f:
            pass
        ok = workflow.now()
        r = workflow.random().random()
        await asyncio.sleep(1)
        return "done"

def helper():
    return datetime.datetime.now()  # outside a workflow: allowed
'''


def test_scanner_flags_nondeterministic_calls_in_workflows_only():
    found = scan_determinism(WORKFLOW, "workflows.py")
    calls = {f["call"] for f in found}
    assert {"datetime.datetime.now", "time.time", "random.choice", "uuid.uuid4", "open"} <= calls
    assert any(c.startswith("os.environ") for c in calls)
    assert not any(c.startswith("workflow.") or c == "asyncio.sleep" for c in calls)
    lines = {f["line"] for f in found}
    assert max(lines) < WORKFLOW.splitlines().index("def helper():") + 1


def test_scanner_reports_syntax_errors():
    found = scan_determinism("def broken(:\n", "bad.py")
    assert found and found[0]["call"] == "syntax error"


def test_scanner_accepts_clean_workflow():
    src = '''
from temporalio import workflow
from datetime import timedelta
with workflow.unsafe.imports_passed_through():
    import activities

@workflow.defn(name="Clean")
class Clean:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(activities.go, start_to_close_timeout=timedelta(seconds=30))
        workflow.logger.info("at %s", workflow.now())
'''
    assert scan_determinism(src) == []


def test_check_determinism_ignores_harness_and_legacy(tmp_path: Path):
    poc = tmp_path / "temporal-poc"
    (poc / "legacy").mkdir(parents=True)
    (poc / "legacy" / "old.py").write_text(WORKFLOW)
    (poc / "tz_harness.py").write_text(WORKFLOW)
    (poc / "workflows.py").write_text("from temporalio import workflow\n")
    assert check_determinism(tmp_path)["ok"]
    (poc / "workflows.py").write_text(WORKFLOW)
    item = check_determinism(tmp_path)
    assert not item["ok"] and "temporal-poc/workflows.py" in item["output"]


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "a/../../x", "temporal-poc/../../x"])
def test_safe_path_rejects_escapes(tmp_path: Path, bad: str):
    with pytest.raises(WorkspaceError):
        safe_path(tmp_path, bad)


def test_safe_path_allows_inside(tmp_path: Path):
    assert safe_path(tmp_path, "temporal-poc/x.py") == (tmp_path / "temporal-poc" / "x.py").resolve()
    assert safe_path(tmp_path, ".") == tmp_path.resolve()


def test_tools_stay_in_workspace(tmp_path: Path):
    tools = Tools(tmp_path, {})
    assert tools.handle("write_file", {"path": "../escape.txt", "content": "x"}).startswith("Error")
    assert not (tmp_path.parent / "escape.txt").exists()
    assert tools.handle("write_file", {"path": "temporal-poc/a.py", "content": "x = 1\n"}).startswith("Wrote")
    assert "1\tx = 1" in tools.handle("read_file", {"path": "temporal-poc/a.py"})
    assert tools.handle("replace_in_file", {"path": "temporal-poc/a.py", "old": "x = 1", "new": "x = 2"}).startswith("Replaced")
    assert "not found" in tools.handle("replace_in_file", {"path": "temporal-poc/a.py", "old": "nope", "new": ""})
    assert "temporal-poc/a.py" in tools.handle("list_files", {"path": "."})
    out = tools.handle("run_command", {"command": "pwd"})
    assert out.startswith("exit code 0") and str(tmp_path.resolve()) in out
    assert tools.handle("read_file", {"path": "/etc/hosts"}).startswith("Error")
