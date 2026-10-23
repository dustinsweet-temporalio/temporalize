"""tz_harness (copied into a temporary temporal-poc/): injected failures, the stub log, and the CLI contract."""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from temporalize.generate.workspace import HARNESS_SOURCE


@pytest.fixture
def poc(tmp_path: Path) -> Path:
    poc = tmp_path / "temporal-poc"
    (poc / "testdata").mkdir(parents=True)
    shutil.copyfile(HARNESS_SOURCE, poc / "tz_harness.py")
    (poc / "testdata" / "failures.json").write_text(json.dumps(
        {"run_id": "r-0009", "frozen_time": "2026-10-19T07:00:00", "failures": [{"boundary": "login", "times": 2}]}))
    (poc / "testdata" / "shifts.json").write_text('[{"id": 1}]')
    return poc


@pytest.fixture
def h(poc: Path, monkeypatch):
    spec = importlib.util.spec_from_file_location("tz_harness_under_test", poc / "tz_harness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.delenv("TZ_FAILURES", raising=False)
    monkeypatch.delenv("TZ_STUB_DELAY", raising=False)
    monkeypatch.delenv("TZ_RUN_ID", raising=False)
    return mod


def test_testdata_and_identity(h):
    assert h.run_id() == "r-0009"
    assert h.frozen_now().isoformat() == "2026-10-19T07:00:00"
    assert h.load_testdata("shifts.json") == [{"id": 1}]
    assert h.workflow_id("smoke", unique=False) == "tz-r-0009-smoke"
    assert h.task_queue("x").startswith("tz-r-0009-x-")


def test_should_fail_exactly_n_times_only_when_enabled(h):
    assert h.should_fail("login") is False          # off by default
    h.enable_failures(True)
    assert [h.should_fail("login") for _ in range(4)] == [True, True, False, False]
    assert h.attempts("login") == 2
    assert h.should_fail("email") is False
    h.enable_failures(True)                          # resets the counters
    assert h.should_fail("login") is True


def test_stub_log_persists(h):
    h.StubLog.reset()
    h.StubLog.record("login", user="a")
    h.StubLog.record("login", user="b")
    h.StubLog.record("email")
    assert h.StubLog.counts() == {"login": 2, "email": 1}
    assert [c["user"] for c in h.StubLog.calls("login")] == ["a", "b"]


def test_check_rows(h):
    assert h.check("rows", 42, 42)["result"] == "match"
    assert h.check("rows", 42, 41)["result"] == "mismatch"
    assert h.check("start", "Sat", "Thu", "intended")["result"] == "intended"


def _validate(poc: Path, body: str, scenario: str) -> tuple[int, dict]:
    (poc / "validate.py").write_text("import tz_harness as h\n" + body)
    out = poc / ".tz" / f"{scenario}.json"
    proc = subprocess.run([sys.executable, str(poc / "validate.py"), scenario, "--out", str(out)],
                          cwd=str(poc), capture_output=True, text=True, timeout=60)
    return proc.returncode, json.loads(out.read_text())


def test_main_writes_json_and_derives_ok(poc: Path):
    body = '''
async def good():
    return {"checks": [h.check("rows", 1, 1)], "criteria": [h.criterion("same", True, "1 row")], "workflow_ids": ["w1"]}
async def bad():
    return {"checks": [h.check("rows", 1, 2)]}
async def boom():
    raise RuntimeError("kaput")
h.main({"good": good, "bad": bad, "boom": boom})
'''
    code, result = _validate(poc, body, "good")
    assert code == 0 and result["ok"] and result["workflow_ids"] == ["w1"]
    assert json.loads((poc / ".tz" / "state.json").read_text())["workflows"]["good"] == ["w1"]
    code, result = _validate(poc, body, "bad")
    assert code == 1 and result["ok"] is False and result["checks"][0]["result"] == "mismatch"
    code, result = _validate(poc, body, "boom")
    assert code == 1 and result["ok"] is False and "kaput" in result["notes"]
