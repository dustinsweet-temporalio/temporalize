"""Generate and fix: the coding agent on the workspace, with the run's Temporal target."""
from __future__ import annotations

from typing import Callable

from temporalize.generate import prompts
from temporalize.generate.agent import run_agent
from temporalize.generate.workspace import WorkspaceError, run_id_of, workspace_path

GENERATE_TURNS = 80
FIX_TURNS = 40
# One activity runs at most this much of the agent's conversation, then the workflow calls again.
CHUNK_TURNS = 8
CHUNK_SECONDS = 8 * 60


def _env(run: dict) -> dict[str, str]:
    from temporalize.settings import harness_env, run_settings, temporal_target

    return {**harness_env(temporal_target(run_settings(run))), "TZ_RUN_ID": run_id_of(run)}


def _workspace(run: dict):
    ws = workspace_path(run)
    if not (ws / "temporal-poc" / "PLAN.md").exists():
        raise WorkspaceError(f"The workspace {ws} is not prepared.")
    return ws


def _out(result) -> dict:
    return {"summary": result.summary, "files_changed": result.files_changed, "turns": result.turns,
            "finished": result.finished, "done": result.done}


def generate_code(llm, run: dict, process: dict, heartbeat: Callable[[str], None]) -> dict:
    """One chunk of the generator's conversation; `done` False means call again to carry on."""
    ws = _workspace(run)
    return _out(run_agent(llm, ws, prompts.GENERATOR_SYSTEM, prompts.generate_task(run, process), heartbeat,
                          max_turns=GENERATE_TURNS, env=_env(run), session="generate",
                          turn_budget=CHUNK_TURNS, time_budget=CHUNK_SECONDS))


def fix_code(llm, run: dict, process: dict, kind: str, details: dict, heartbeat: Callable[[str], None],
             session: str | None = None) -> dict:
    """One chunk of a fix conversation (`session` names it, e.g. fix-review-1); same contract as generate_code."""
    ws = _workspace(run)
    return _out(run_agent(llm, ws, prompts.FIX_SYSTEM, prompts.fix_task(kind, details), heartbeat,
                          max_turns=FIX_TURNS, env=_env(run), session=session or f"fix-{kind}",
                          turn_budget=CHUNK_TURNS, time_budget=CHUNK_SECONDS))
