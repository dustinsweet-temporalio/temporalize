"""
Names, error types, the retry policy and timeouts of the business-impact workflow.

Safe to import in the workflow sandbox (standard library and temporalio only).
The values are starting points; tune them against real runs.
"""
from datetime import timedelta

from temporalio.common import RetryPolicy

WORKFLOW_NAME = "BusinessImpactWorkflow"
PROGRESS_QUERY = "progress"
DEFAULT_TASK_QUEUE = "code-atlas-impact"   # IMPACT_TASK_QUEUE overrides it (config.py)


def workflow_id(repo_id: str) -> str:
    """One run per index at a time: a second start fails (conflict policy FAIL) instead of attaching."""
    return f"impact-{repo_id}"


# Activity names (explicit, so renaming a method never breaks a running workflow).
PREPARE_IMPACT = "prepare_impact"
TRANSLATE_PROCESS = "translate_process"
SYNTHESIZE_IMPACT = "synthesize_impact"
MARK_IMPACT_STATUS = "mark_impact_status"

# ApplicationError types. LLM_PERMANENT and LLM_TRANSIENT are the indexer's (the same
# classification of model errors); IMPACT_INVALID: the request or its report cannot run.
LLM_PERMANENT = "LLM_PERMANENT"
LLM_TRANSIENT = "LLM_TRANSIENT"
IMPACT_INVALID = "IMPACT_INVALID"

# The shape of the indexer's LLM_RETRY.
IMPACT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=4,
    non_retryable_error_types=[LLM_PERMANENT, IMPACT_INVALID],
)
LOCAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
    non_retryable_error_types=[IMPACT_INVALID],
)

PROCESS_CONCURRENCY = 4                      # as the report (report/run.py PROCESS_WORKERS)
PREPARE_TIMEOUT = timedelta(minutes=1)
PROCESS_TIMEOUT = timedelta(minutes=10)      # one model call plus one retry
SYNTHESIZE_TIMEOUT = timedelta(minutes=5)
MARK_TIMEOUT = timedelta(minutes=1)
HEARTBEAT_TIMEOUT = timedelta(seconds=30)
HEARTBEAT_INTERVAL_SECONDS = 5.0
ERROR_CHARS = 500
