"""
Model calls made by a Temporalize run, counted on the run document (the status strip).

The key and model come from the saved settings, falling back to .env. Errors are the
indexer's classified LLM errors, so activities can use its translated_errors().
"""
from __future__ import annotations

import json
import os
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from temporalize import store

T = TypeVar("T", bound=BaseModel)

DEFAULT_MAX_TOKENS = 32000

MODELS = [
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet"},
    {"id": "claude-opus-5-5", "label": "Claude Opus"},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku"},
]


def default_model() -> str:
    from semantic_ingress.config import LLMConfig

    return LLMConfig().model_id


def api_key(settings: dict | None = None) -> str:
    settings = settings if settings is not None else store.get_settings()
    return (settings.get("anthropic_api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()


class RunLLM:
    def __init__(self, run_id: str, settings: dict | None = None, effort: str | None = "medium") -> None:
        settings = settings if settings is not None else store.get_settings()
        self.run_id = run_id
        self.model = settings.get("model") or default_model()
        self.effort = effort
        self.client = anthropic.Anthropic(api_key=api_key(settings) or None, timeout=900, max_retries=0)

    def create(self, **params: Any):
        """One Messages API call (streamed, so large max_tokens are allowed), counted on the run."""
        from semantic_ingress.core.llm_client import LLMPermanentError, classify_llm_error

        params.setdefault("model", self.model)
        params.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        if self.effort:
            params.setdefault("output_config", {}).setdefault("effort", self.effort)
        try:
            with self.client.messages.stream(**params) as stream:
                message = stream.get_final_message()
        except Exception as e:
            raise classify_llm_error(e) from e
        finally:
            store.add_calls(self.run_id)
        if message.stop_reason == "refusal":
            raise LLMPermanentError("The model declined the request.")
        return message

    def structured(self, system: str, prompt: str, model_cls: type[T], max_tokens: int = DEFAULT_MAX_TOKENS) -> T:
        """One call whose answer is constrained to `model_cls`'s JSON schema."""
        from semantic_ingress.core.llm_client import LLMTransientError
        from semantic_ingress.core.llm_schema import to_llm_schema

        message = self.create(
            system=system, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": to_llm_schema(model_cls)}},
        )
        text = next((b.text for b in message.content if b.type == "text"), None)
        if text is None:
            raise LLMTransientError(f"No answer (stop_reason={message.stop_reason})")
        if message.stop_reason == "max_tokens":
            raise LLMTransientError("The model's answer hit max_tokens before it was complete.")
        try:
            return model_cls.model_validate(json.loads(text))
        except Exception as e:
            raise LLMTransientError(f"The model's answer did not match the expected format: {type(e).__name__}") from e
