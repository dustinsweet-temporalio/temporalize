"""
The impact worker (compose service `impact-worker`): runs BusinessImpactWorkflow and
its activities on one task queue. It reads only the stored report and the request in
MongoDB, so it needs no source mount, Redis or Docs MCP sign-in.
Run with: python -m impact.worker
"""
import asyncio
import logging
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from config import DB_PREFIX, IMPACT_CONCURRENCY, IMPACT_TASK_QUEUE, LOG_FORMAT, LOG_LEVEL, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE
from impact.activities import ImpactActivities
from impact.workflow import BusinessImpactWorkflow

logger = logging.getLogger("code_atlas.impact.worker")


def build_worker(client: Client, activities: ImpactActivities, task_queue: str = IMPACT_TASK_QUEUE,
                 slots: int = IMPACT_CONCURRENCY) -> tuple[Worker, ThreadPoolExecutor]:
    """The activities are synchronous, so they run on a thread pool the caller shuts down."""
    executor = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="impact-activity")
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[BusinessImpactWorkflow],
        activities=activities.all(),
        activity_executor=executor,
        max_concurrent_activities=slots,
        graceful_shutdown_timeout=timedelta(seconds=30),
    )
    return worker, executor


def default_activities() -> ImpactActivities:
    from semantic_ingress.config import LLMConfig, MongoConfig
    from semantic_ingress.core.llm_client import LLMClient, set_pricing
    from semantic_ingress.report.impact.mapping import load_mapping
    from semantic_ingress.storage import MongoStore

    config = LLMConfig()
    set_pricing(config)
    return ImpactActivities(MongoStore(MongoConfig(), DB_PREFIX), LLMClient(config, internal_retries=False),
                            load_mapping())


async def main() -> None:
    logging.basicConfig(level=getattr(logging, LOG_LEVEL), format=LOG_FORMAT)
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    worker, executor = build_worker(client, default_activities())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("impact worker polling %s on %s", IMPACT_TASK_QUEUE, TEMPORAL_ADDRESS)
    try:
        async with worker:
            await stop.wait()
    finally:
        executor.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
