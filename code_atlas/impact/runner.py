"""
How the app runs business impact on Temporal (impact_api.py's runner): start
BusinessImpactWorkflow with conflict policy FAIL (a second start while one runs is
refused, not attached: it would come with different context), follow it with the
progress query, and cancel it.
"""
import asyncio
from datetime import timedelta

from config import IMPACT_TASK_QUEUE
from impact import constants as C

QUERY_TIMEOUT = timedelta(seconds=3)   # a progress query with no worker to answer it


class TemporalRunner:
    name = "temporal"

    def workflow_id(self, repo_id: str) -> str:
        return C.workflow_id(repo_id)

    async def _client(self):
        from indexing import temporal_client  # one Temporal client for the app

        return await temporal_client()

    async def _running(self, repo_id: str):
        """The running execution's description, or None."""
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError, RPCStatusCode

        client = await self._client()
        try:
            description = await client.get_workflow_handle(self.workflow_id(repo_id)).describe()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return None
            raise
        return description if description.status == WorkflowExecutionStatus.RUNNING else None

    async def running(self, repo_id: str) -> str | None:
        description = await self._running(repo_id)
        if description is None:
            return None
        return (await description.memo()).get("request_id") or "unknown"

    async def start(self, repo_id: str, request_id: str) -> str:
        from temporalio.common import WorkflowIDConflictPolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from impact.models import ImpactInput
        from impact_api import AlreadyRunning

        client = await self._client()
        try:
            handle = await client.start_workflow(
                C.WORKFLOW_NAME, ImpactInput(repo_id=repo_id, request_id=request_id),
                id=self.workflow_id(repo_id), task_queue=IMPACT_TASK_QUEUE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                memo={"repo_id": repo_id, "request_id": request_id},
            )
        except WorkflowAlreadyStartedError:
            raise AlreadyRunning() from None
        return handle.id

    async def progress(self, repo_id: str) -> dict | None:
        """The workflow's progress query; the request's own record when no worker answers."""
        from impact.models import ImpactProgress
        from impact_api import request_progress, store

        description = await self._running(repo_id)
        if description is None:
            return None
        client = await self._client()
        try:
            p: ImpactProgress = await client.get_workflow_handle(self.workflow_id(repo_id)).query(
                C.PROGRESS_QUERY, result_type=ImpactProgress, rpc_timeout=QUERY_TIMEOUT)
        except Exception:
            request_id = (await description.memo()).get("request_id", "")
            request = await asyncio.to_thread(store().get_impact_request, repo_id, request_id) if request_id else None
            base = request_progress(request) if request else {"request_id": request_id, "phase": None}
            return {**base, "waiting": True,
                    "description": "Waiting for the impact worker (if this lasts: docker compose up -d impact-worker)"}
        return {"request_id": p.request_id, "phase": p.phase, "total": p.total, "done": p.done, "failed": p.failed,
                "processes": [{"key": s.key, "name": s.name, "status": s.status} for s in p.processes]}

    async def cancel(self, repo_id: str) -> bool:
        if await self._running(repo_id) is None:
            return False
        await (await self._client()).get_workflow_handle(self.workflow_id(repo_id)).cancel()
        return True
