import importlib.util
from pathlib import Path

import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

ROOT = Path(__file__).resolve().parents[3]

# The domain tests' fakes (semantic_ingress/tests), loaded by path: both test trees are named `tests`.
_spec = importlib.util.spec_from_file_location("impact_fixtures", ROOT / "semantic_ingress" / "tests" / "impact_fixtures.py")
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)


@pytest_asyncio.fixture(scope="session")
async def impact_env():
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()
