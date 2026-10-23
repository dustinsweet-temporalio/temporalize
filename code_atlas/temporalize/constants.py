"""
Names, signals, retry policies and timeouts of the Temporalize workflow.

Safe to import in the workflow sandbox (standard library and temporalio only).
"""
from datetime import timedelta

from temporalio.common import RetryPolicy

WORKFLOW_NAME = "TemporalizeWorkflow"
DEFAULT_TASK_QUEUE = "code-atlas-temporalize"   # TZ_TASK_QUEUE overrides it


def workflow_id(run_id: str) -> str:
    return f"tz-{run_id}"


# Signals from the UI (the decision itself is stored on the run document first).
SCOPE_SIGNAL = "scope_confirmed"
PLAN_SIGNAL = "plan_approved"
DESIGN_EDIT_SIGNAL = "design_edit_requested"   # screen 6 "Make edits": the request is on the run document
PROGRESS_QUERY = "progress"

# Activity names (explicit, so renaming a method never breaks a running workflow).
WAIT_FOR_INDEX = "tz_wait_for_index"
ANALYZE_SCOPE = "tz_analyze_scope"
DRAFT_PLAN = "tz_draft_plan"
PREPARE_WORKSPACE = "tz_prepare_workspace"
GENERATE_CODE = "tz_generate_code"
RUN_CHECKS = "tz_run_checks"
REVIEW_CODE = "tz_review_code"
FIX_CODE = "tz_fix_code"
RUN_SCENARIO = "tz_run_scenario"
FINALIZE_RESULTS = "tz_finalize_results"
MARK_RUN = "tz_mark_run"
RUN_STATE = "tz_run_state"
REVISE_DESIGN = "tz_revise_design"
MARK_DESIGN_EDIT = "tz_mark_design_edit"

# ApplicationError types
TZ_INVALID = "TZ_INVALID"           # the run cannot go on (missing report, not suitable, bad input)
LLM_PERMANENT = "LLM_PERMANENT"
LLM_TRANSIENT = "LLM_TRANSIENT"

LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=4,
    non_retryable_error_types=[LLM_PERMANENT, TZ_INVALID],
)
LOCAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
    non_retryable_error_types=[TZ_INVALID],
)

INDEX_TIMEOUT = timedelta(hours=4)
MODEL_STEP_TIMEOUT = timedelta(minutes=20)
AGENT_TIMEOUT = timedelta(minutes=60)        # no longer scheduled: generation runs in chunks (below)
AGENT_CHUNK_TIMEOUT = timedelta(minutes=30)  # one chunk: up to 8 agent turns or ~8 minutes, plus a command in flight
MAX_AGENT_CHUNKS = 15                        # the agent's own turn limit ends it well before this
CHECKS_TIMEOUT = timedelta(minutes=10)
SCENARIO_TIMEOUT = timedelta(minutes=15)
LOCAL_TIMEOUT = timedelta(minutes=5)
HEARTBEAT_TIMEOUT = timedelta(minutes=2)
HEARTBEAT_INTERVAL_SECONDS = 10.0

MAX_REVIEW_PASSES = 3      # review and revise stops here; leftovers are listed in the results
MAX_FIX_ATTEMPTS = 2       # per failing check or scenario
SCENARIOS = ["side_by_side", "resilience", "replay", "smoke"]
ERROR_CHARS = 800
