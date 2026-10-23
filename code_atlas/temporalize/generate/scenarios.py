"""Run one validation scenario of the generated POC (temporal-poc/validate.py) and read its result."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from temporalize.generate.checks import poc_env
from temporalize.generate.workspace import POC_DIR, run_id_of, workspace_path

SCENARIO_TIMEOUT = 600
OUTPUT_CHARS = 6000
SCENARIOS = ("side_by_side", "resilience", "replay", "smoke")


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_CHARS else "…" + text[-OUTPUT_CHARS:]


def run_scenario(run: dict, scenario: str, settings: dict) -> dict:
    from temporalize.settings import harness_env, temporal_target

    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario {scenario}.")
    ws = workspace_path(run)
    out = ws / POC_DIR / ".tz" / f"{scenario}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()   # a stale result must never count
    validate = ws / POC_DIR / "validate.py"
    if not validate.exists():
        return {"ok": False, "checks": [], "criteria": [], "workflow_ids": [], "notes": "",
                "output": "temporal-poc/validate.py does not exist.", "exit_code": None}
    env = {**os.environ, **poc_env(ws), **harness_env(temporal_target(settings)), "TZ_RUN_ID": run_id_of(run)}
    try:
        proc = subprocess.run([sys.executable, f"{POC_DIR}/validate.py", scenario, "--out", str(out)],
                              cwd=str(ws), capture_output=True, text=True, timeout=SCENARIO_TIMEOUT, env=env,
                              stdin=subprocess.DEVNULL)
        output, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired as e:
        raw = e.stdout or ""
        output = (raw.decode(errors="replace") if isinstance(raw, bytes) else raw) + f"\nTimed out after {SCENARIO_TIMEOUT}s."
        code = None
    try:
        result = json.loads(out.read_text())
        if not isinstance(result, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return {"ok": False, "checks": [], "criteria": [], "workflow_ids": [], "notes": "No valid result file was written.",
                "output": _tail(output), "exit_code": code}
    result.setdefault("checks", [])
    result.setdefault("criteria", [])
    result.setdefault("workflow_ids", [])
    result.setdefault("notes", "")
    result["ok"] = bool(result.get("ok")) and code == 0
    result["output"] = _tail(output)
    result["exit_code"] = code
    return result
