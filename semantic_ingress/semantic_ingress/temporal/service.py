"""
The indexing worker for syncs started from the web UI: `semantic-ingress worker`,
the compose `index-worker` service.

A sync's activities are bound to its repository (IndexActivities reads it from disk)
and each repository has its own task queue, so this service does not poll one fixed
queue. It watches for running sync workflows the UI started (memo origin "ui") and,
for each, runs an embedded worker exactly as `sync` does for the run it starts, until
the workflow closes. After a restart it finds the running ones again and Temporal
resumes them from their history. Runs started by `sync` (index.sh) are left to the
worker inside that command.

The repository is read at the path the UI recorded in the memo. That is the host
path: compose mounts REPOS_ROOT at the same path here and in the app, read-only.

When a run cannot be served (the repository is not visible here, or the preflight
fails), the error is stored in the staging database for the UI to show, and the
service tries again a minute later.
"""
import asyncio
import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import structlog
from temporalio.client import Client

from semantic_ingress.config import FileFilterConfig, IngestionConfig, LLMConfig, MongoConfig, TemporalConfig
from semantic_ingress.core.llm_client import LLMClient
from semantic_ingress.preflight import run_preflight
from semantic_ingress.runtime import apply_saved_settings, index_call_key, install_call_counter
from semantic_ingress.storage import MongoStore
from semantic_ingress.storage.staging_store import STAGING_DATABASE, WORKER_STATUS, StagingStore
from semantic_ingress.temporal.activities import IndexActivities
from semantic_ingress.temporal.constants import ORIGIN_UI, WORKFLOW_NAME, task_queue, workflow_id
from semantic_ingress.temporal.worker import build_worker

logger = structlog.get_logger()

DISCOVERY_SECONDS = 3.0
RETRY_SECONDS = 60.0
CONNECT_RETRY_SECONDS = 5.0


class SyncWorkerService:
    def __init__(self, client: Client, prefix: str) -> None:
        self.client = client
        self.prefix = prefix
        self.temporal = TemporalConfig()
        self.store = MongoStore(MongoConfig(), prefix)
        self.status = self.store.client[STAGING_DATABASE][WORKER_STATUS]
        install_call_counter(self.store.client)
        self._serving: dict[str, asyncio.Task] = {}   # repo_id -> its worker's task
        self._retry_at: dict[str, float] = {}

    async def run(self) -> None:
        logger.info("worker_service_started", temporal=self.temporal.address, namespace=self.temporal.namespace)
        while True:
            try:
                await self._discover()
            except Exception as e:  # the server restarting, for example; try again on the next round
                logger.warning("worker_discovery_failed", error=str(e))
            await asyncio.sleep(DISCOVERY_SECONDS)

    async def _discover(self) -> None:
        for repo_id, task in list(self._serving.items()):
            if task.done():
                del self._serving[repo_id]
        query = f"WorkflowType = '{WORKFLOW_NAME}' AND ExecutionStatus = 'Running'"
        now = asyncio.get_running_loop().time()
        async for execution in self.client.list_workflows(query):
            memo = await execution.memo()
            repo_id, repo_path = memo.get("repo_id"), memo.get("repo_path")
            if memo.get("origin") != ORIGIN_UI or not repo_id or not repo_path:
                continue
            if repo_id in self._serving or now < self._retry_at.get(repo_id, 0):
                continue
            self._serving[repo_id] = asyncio.create_task(self._serve(repo_id, repo_path, memo))

    async def _serve(self, repo_id: str, repo_path: str, memo: dict) -> None:
        """
        Run the repository's worker until its sync workflow closes. Temporalize runs add
        `model` and `exclude_extra` to the memo; every run's model calls are counted.
        """
        log = logger.bind(repo_id=repo_id, repo_path=repo_path)
        try:
            if not Path(repo_path).is_dir():
                raise ValueError(f"{repo_path} is not visible to the indexer worker. It must be under "
                                 "REPOS_ROOT (or the Temporalize clone folder), which compose mounts into "
                                 "the worker and the app.")
            await asyncio.to_thread(apply_saved_settings, self.store.client)
            llm = dataclasses.replace(LLMConfig(), count_key=index_call_key(repo_id))
            if memo.get("model"):
                llm = dataclasses.replace(llm, model_id=memo["model"])
            file_filter = FileFilterConfig()
            if memo.get("exclude_extra"):
                file_filter = dataclasses.replace(
                    file_filter, exclude_patterns=file_filter.exclude_patterns + tuple(memo["exclude_extra"]))
            config = IngestionConfig(repo_path=Path(repo_path), output_dir=Path("/tmp"),
                                     repo_id=repo_id, prefix=self.prefix, llm=llm, file_filter=file_filter)
            preflight = await asyncio.to_thread(run_preflight, config, False)
            staging = StagingStore(self.store.client)
            staging.ensure_indexes()
            activities = IndexActivities(config, preflight, self.store, staging,
                                         LLMClient(config.llm, internal_retries=False))
        except Exception as e:
            log.error("worker_start_failed", error=str(e))
            self._retry_at[repo_id] = asyncio.get_running_loop().time() + RETRY_SECONDS
            self._record_error(repo_id, str(e))
            return
        self._record_error(repo_id, None)

        worker, executor = build_worker(self.client, task_queue(repo_id), activities.all(), self.temporal.window_size)
        log.info("worker_serving_sync", workflow_id=workflow_id(repo_id))
        try:
            async with worker:
                try:
                    await self.client.get_workflow_handle(workflow_id(repo_id)).result()  # follows Continue-As-New
                except Exception as e:  # failed or terminated: the UI reads the outcome from the workflow
                    log.info("sync_closed", outcome=type(e).__name__)
                else:
                    log.info("sync_completed")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _record_error(self, repo_id: str, error: str | None) -> None:
        try:
            if error is None:
                self.status.delete_one({"_id": repo_id})
            else:
                self.status.replace_one(
                    {"_id": repo_id}, {"_id": repo_id, "error": error, "at": datetime.now(timezone.utc)}, upsert=True
                )
        except Exception as e:
            logger.warning("worker_status_write_failed", repo_id=repo_id, error=str(e))


async def serve(prefix: str) -> None:
    """Connect to Temporal (waiting for it if needed) and run the service until the process stops."""
    temporal = TemporalConfig()
    while True:
        try:
            client = await Client.connect(temporal.address, namespace=temporal.namespace)
            break
        except Exception as e:
            logger.warning("temporal_connect_failed", address=temporal.address, error=str(e))
            await asyncio.sleep(CONNECT_RETRY_SECONDS)
    await SyncWorkerService(client, prefix).run()
