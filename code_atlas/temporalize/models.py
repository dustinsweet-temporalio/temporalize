"""
Payloads of the Temporalize workflow: ids, names and counts only. Everything a step
produces (scope, plan, findings, scenario results) is written to the run document in
MongoDB, so no source code or model answer enters workflow history.
"""
from dataclasses import dataclass


@dataclass
class RunInput:
    run_id: str
    resume: bool = False    # a retry: skip what the run document says is already done


@dataclass
class RunState:
    scoped: bool = False             # the report is in and the scope analyzed
    scope_confirmed: bool = False
    planned: bool = False
    approved: bool = False


@dataclass
class StepInput:
    run_id: str
    n: int = 0              # attempt or review pass
    kind: str = ""          # fix: checks | review | scenario
    scenario: str = ""


@dataclass
class StepResult:
    ok: bool = True
    count: int = 0          # review: findings that need a fix
    detail: str = ""


@dataclass
class MarkInput:
    run_id: str
    status: str             # failed | stopped
    error: str = ""
