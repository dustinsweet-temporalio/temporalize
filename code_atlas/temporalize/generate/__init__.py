"""
Generate and validate (screen 7) and the results (screen 8).

    workspace.py   the POC branch: clone, plan/test data/harness files, diff, file reads, push
    tz_harness.py  the validation harness copied into temporal-poc/ (imported by generated code)
    agent.py       the coding agent's tool loop (files and commands in the workspace)
    prompts.py     the agent's instructions and tasks, and the review prompt
    steps.py       generate_code and fix_code
    checks.py      imports, lint, types, determinism
    review.py      the Code Atlas review (structured model review of the diff)
    scenarios.py   runs temporal-poc/validate.py <scenario>
    results.py     the results object and the review packet

Every function is synchronous and safe to retry: state lives in the workspace on disk.
"""
from temporalize.generate.checks import run_checks
from temporalize.generate.results import build_results, packet
from temporalize.generate.review import needs_fix, review_code
from temporalize.generate.scenarios import SCENARIOS, run_scenario
from temporalize.generate.steps import fix_code, generate_code
from temporalize.generate.workspace import WorkspaceError, diff, prepare_workspace, push, read_file

__all__ = [
    "SCENARIOS", "WorkspaceError", "build_results", "diff", "fix_code", "generate_code", "needs_fix", "packet",
    "prepare_workspace", "push", "read_file", "review_code", "run_checks", "run_scenario",
]
