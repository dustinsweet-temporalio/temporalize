"""
The chat worker (compose service `chat-worker`): runs ChatSessionWorkflow and its
turn activity on one shared task queue. Run with: python -m chat.worker
"""
import asyncio
import logging
import signal
from datetime import timedelta

import redis.asyncio as aioredis
from temporalio.client import Client
from temporalio.worker import Worker

from chat import constants as C
from chat.activities import ChatActivities
from chat.workflow import ChatSessionWorkflow
from config import (
    CHAT_MAX_CONCURRENT_TURNS,
    CHAT_STREAM_TTL_SECONDS,
    CHAT_TASK_QUEUE,
    LOG_FORMAT,
    LOG_LEVEL,
    REDIS_URL,
    TEMPORAL_ADDRESS,
    TEMPORAL_NAMESPACE,
)

logger = logging.getLogger("code_atlas.chat.worker")


def build_worker(client: Client, redis, task_queue: str = CHAT_TASK_QUEUE) -> Worker:
    """The activity is async, so no thread pool is needed (unlike the indexer's)."""
    activities = ChatActivities(redis, CHAT_STREAM_TTL_SECONDS)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[ChatSessionWorkflow],
        activities=activities.all(),
        max_concurrent_activities=CHAT_MAX_CONCURRENT_TURNS,
        max_heartbeat_throttle_interval=C.HEARTBEAT_THROTTLE,
        graceful_shutdown_timeout=timedelta(seconds=30),
    )



def _apply_saved_key() -> None:
    """An Anthropic key saved in the Temporalize setup screen takes precedence over .env (read at start)."""
    try:
        from semantic_ingress.runtime import apply_saved_settings
        from utils.mongo import get_mongo_client

        apply_saved_settings(get_mongo_client())
    except Exception as e:
        logger.warning("Could not read the saved settings: %s", e)

async def main() -> None:
    logging.basicConfig(level=getattr(logging, LOG_LEVEL), format=LOG_FORMAT)
    _apply_saved_key()
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    worker = build_worker(client, redis)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("chat worker polling %s on %s", CHAT_TASK_QUEUE, TEMPORAL_ADDRESS)
    async with worker:
        await stop.wait()
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
