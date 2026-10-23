"""
Settings and counters the indexer shares with the Temporalize UI, in MongoDB.

- Model calls: an LLMConfig with a `count_key` reports every Messages API call to the
  counter installed with `install_call_counter` (the index-worker counts per index in
  `code_atlas_ingest.model_calls`), so the UI can show how many calls a run made.
- The Anthropic key and model saved in the UI (`code_atlas_temporalize.settings`) take
  precedence over .env for UI-started syncs: `apply_saved_settings` copies them into
  this process's environment, where the SDK and LLMConfig read them.
"""
import os

from semantic_ingress.core import llm_client
from semantic_ingress.storage.staging_store import STAGING_DATABASE

CALLS_COLLECTION = "model_calls"
SETTINGS_DATABASE = "code_atlas_temporalize"
SETTINGS_ID = "settings"


def index_call_key(repo_id: str) -> str:
    return f"index:{repo_id}"


def install_call_counter(mongo_client) -> None:
    calls = mongo_client[STAGING_DATABASE][CALLS_COLLECTION]

    def count(key: str) -> None:
        calls.update_one({"_id": key}, {"$inc": {"n": 1}}, upsert=True)

    llm_client.set_call_counter(count)


def read_call_count(mongo_client, key: str) -> int:
    doc = mongo_client[STAGING_DATABASE][CALLS_COLLECTION].find_one({"_id": key})
    return int(doc["n"]) if doc else 0


def apply_saved_settings(mongo_client) -> None:
    """The UI-saved Anthropic key, if any, replaces ANTHROPIC_API_KEY in this process."""
    doc = mongo_client[SETTINGS_DATABASE]["settings"].find_one({"_id": SETTINGS_ID}) or {}
    key = (doc.get("anthropic_api_key") or "").strip()
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
