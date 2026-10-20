"""
Payloads of the business-impact workflow: dataclasses of primitives, safe in the
workflow sandbox. They carry ids, keys, process names, counts and statuses only;
the context and every model answer stay in MongoDB.
"""
from dataclasses import dataclass, field


@dataclass
class ImpactInput:
    repo_id: str
    request_id: str


@dataclass
class PrepareResult:
    keys: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)   # key -> process name (from the code report)


@dataclass
class ProcessRef:
    repo_id: str
    request_id: str
    key: str


@dataclass
class ProcessResult:
    key: str
    status: str                  # ok | not_a_fit | no_consequence | failed
    calls: int = 0
    cost_usd: float = 0.0


@dataclass
class SynthesizeInput:
    repo_id: str
    request_id: str
    calls: int = 0
    cost_usd: float = 0.0
    failures: dict[str, str] = field(default_factory=dict)  # key -> error, for processes whose activity failed


@dataclass
class SynthesizeResult:
    processes: int = 0           # translated (status ok)
    calls: int = 0
    cost_usd: float = 0.0


@dataclass
class MarkInput:
    repo_id: str
    request_id: str
    status: str                  # failed | cancelled
    error: str = ""


@dataclass
class ProcessState:
    key: str
    name: str = ""
    status: str = "pending"


@dataclass
class ImpactProgress:
    request_id: str = ""
    phase: str = "STARTING"      # STARTING, PREPARE, PROCESSES, SYNTHESIZE, DONE
    total: int = 0
    done: int = 0
    failed: int = 0
    processes: list[ProcessState] = field(default_factory=list)


@dataclass
class ImpactOutcome:
    status: str                  # ok
    processes: int = 0
    calls: int = 0
    cost_usd: float = 0.0
