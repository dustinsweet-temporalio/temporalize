"""
The Temporalize worker (compose service `tz-worker`): runs TemporalizeWorkflow and its
activities. It reads the repositories (clones in the tz-data volume, local folders under
REPOS_ROOT) and the mirrored source, writes workspaces to the tz-data volume, and runs
the generated POC, which connects to the Temporal target from the settings.
Run with: python -m temporalize.worker
"""
import asyncio
import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from config import LOG_FORMAT, LOG_LEVEL, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE
from temporalize import constants as C
from temporalize.activities import TemporalizeActivities
from temporalize.workflow import TemporalizeWorkflow

logger = logging.getLogger("code_atlas.temporalize.worker")

TASK_QUEUE = os.environ.get("TZ_TASK_QUEUE", "").strip() or C.DEFAULT_TASK_QUEUE
SLOTS = int(os.environ.get("TZ_CONCURRENCY", "").strip() or "6")


def build_worker(client: Client, task_queue: str = TASK_QUEUE, slots: int = SLOTS) -> tuple[Worker, ThreadPoolExecutor]:
    executor = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="tz-activity")
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[TemporalizeWorkflow],
        activities=TemporalizeActivities(client).all(),
        activity_executor=executor,
        max_concurrent_activities=slots,
        graceful_shutdown_timeout=timedelta(seconds=30),
    )
    return worker, executor


async def main() -> None:
    logging.basicConfig(level=getattr(logging, LOG_LEVEL), format=LOG_FORMAT)
    while True:
        try:
            client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
            break
        except Exception as e:
            logger.warning("Temporal not reachable at %s (%s); retrying", TEMPORAL_ADDRESS, e)
            await asyncio.sleep(5)
    worker, executor = build_worker(client)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("temporalize worker polling %s on %s", TASK_QUEUE, TEMPORAL_ADDRESS)
    try:
        async with worker:
            await stop.wait()
    finally:
        executor.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
