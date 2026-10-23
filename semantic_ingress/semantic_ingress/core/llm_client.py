"""
LLM client for semantic artifact generation.
Wraps the Anthropic Messages API with retry logic, token counting, and error handling.
"""
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import anthropic
import structlog
from pydantic import BaseModel, ValidationError

from semantic_ingress.config import LLMConfig
from semantic_ingress.core.llm_schema import to_llm_schema

logger = structlog.get_logger()


class LLMError(Exception):
    """Base exception for LLM client errors."""
    pass


class LLMTransientError(LLMError):
    """A failure that may succeed when retried (rate limit, overload, timeout, bad JSON)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMPermanentError(LLMError):
    """A failure that will not succeed when retried (other 4xx, refusal)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(e: Exception) -> float | None:
    """The server's retry-after hint in seconds, if the error carries one."""
    response = getattr(e, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def classify_llm_error(e: Exception) -> LLMError:
    """
    Map an exception from a model call to LLMTransientError or LLMPermanentError.

    Transient: 408, 409, 429, 5xx, connection and timeout errors, invalid JSON or a
    response that fails validation. Permanent: other 4xx and refusals. Anything
    else (an unexpected error) counts as transient.
    """
    if isinstance(e, (LLMTransientError, LLMPermanentError)):
        return e
    if isinstance(e, anthropic.APIStatusError):
        if 400 <= e.status_code < 500 and e.status_code not in (408, 409, 429):
            return LLMPermanentError(f"Anthropic API error ({e.status_code}): {e}")
        return LLMTransientError(f"Anthropic API error ({e.status_code}): {e}", _retry_after(e))
    if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return LLMTransientError(f"Anthropic API not reachable: {e}")
    if isinstance(e, (ValidationError, json.JSONDecodeError)):
        return LLMTransientError(f"Invalid model response: {e}")
    return LLMTransientError(str(e))


# USD per million tokens, used only for cost reporting. Set from config by LLMClient.
_pricing = {"input": 2.00, "output": 10.00, "cache_read": 0.05}


# Called with config.count_key after every Messages API call (semantic_ingress.runtime installs it).
_call_counter: Callable[[str], None] | None = None


def set_call_counter(counter: Callable[[str], None] | None) -> None:
    global _call_counter
    _call_counter = counter


def set_pricing(config: LLMConfig) -> None:
    """Use the configured prices for cost estimates (LLMClient does this; so does the web UI's progress)."""
    _pricing["input"] = config.input_price_per_1m
    _pricing["output"] = config.output_price_per_1m
    _pricing["cache_read"] = config.cache_read_multiplier


# Prompt-cache writes relative to the input price (5-minute cache). Cache reads
# vary by model (0.05x on the 5.5 models, 0.1x on most others) and come from config.
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class LLMUsage:
    """Token usage (and estimated cost) for one or more LLM calls."""
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    @property
    def input_cost_usd(self) -> float:
        """Estimated input cost at the configured price, including prompt-cache reads and writes."""
        cached = (
            self.cache_read_tokens * _pricing["cache_read"]
            + self.cache_write_tokens * CACHE_WRITE_MULTIPLIER
        )
        return ((self.input_tokens + cached) / 1_000_000) * _pricing["input"]

    @property
    def output_cost_usd(self) -> float:
        """Estimated output cost at the configured price."""
        return (self.output_tokens / 1_000_000) * _pricing["output"]

    @property
    def total_cost_usd(self) -> float:
        """Total estimated cost."""
        return self.input_cost_usd + self.output_cost_usd


def _move_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    """
    Keep one cache breakpoint, on the newest user turn of a tool loop, so each call
    reads the conversation so far from cache instead of paying for it again.
    """
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for block in m["content"]:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}


class LLMClient:
    """Client for interacting with the Anthropic API."""
    
    def __init__(self, config: LLMConfig, internal_retries: bool = True) -> None:
        """
        Initialize LLM client.
        
        Args:
            config: LLM configuration
            internal_retries: Retry inside the client (SDK retries and the retry loop in
                generate). False for Temporal activities, where the activity retry
                policy owns retries: one attempt per call, failures raised as
                LLMTransientError or LLMPermanentError.
        """
        self.config = config
        self.internal_retries = internal_retries
        # Stop reason of this thread's latest single-prompt call (the client is shared across threads).
        self._last = threading.local()
        self.log = logger.bind(component="llm_client", model=config.model_id)
        
        # ANTHROPIC_API_KEY is read from the environment by the SDK.
        self.client = anthropic.Anthropic(
            timeout=config.timeout_seconds,
            max_retries=config.max_retries if internal_retries else 0,
        )
        set_pricing(config)
        self.total_usage = LLMUsage(input_tokens=0, output_tokens=0)

        self.log.info("llm_client_initialized")
    
    def generate(
        self,
        prompt: str,
        response_model: type[BaseModel],
        max_retries: int = 3,
        system: str | None = None,
        enforce_schema: bool = False,
    ) -> tuple[BaseModel, LLMUsage]:
        """
        Generate a response from the LLM and validate against a Pydantic model.

        Args:
            prompt: The prompt to send to the LLM (the user message)
            response_model: Pydantic model to validate response against
            max_retries: Maximum number of retry attempts
            system: Optional system prompt, sent with a prompt-cache breakpoint so
                repeated calls with the same system text read it from cache
            enforce_schema: Constrain the response to the model's JSON schema
                (structured output); the response is then parsed directly

        Returns:
            Tuple of (validated Pydantic model instance, usage stats)

        Raises:
            LLMError: If generation fails after retries or validation fails
        """
        self.log.info(
            "generating_llm_response",
            prompt_length=len(prompt),
            response_model=response_model.__name__,
            cached_system=system is not None,
            enforce_schema=enforce_schema,
        )
        schema = to_llm_schema(response_model) if enforce_schema else None

        if not self.internal_retries:
            return self._generate_once(prompt, response_model, system, schema)

        for attempt in range(1, max_retries + 1):
            try:
                response_text, usage = self._invoke_model(prompt, system=system, schema=schema)
                if schema is not None:
                    validated_response = response_model.model_validate(json.loads(response_text))
                else:
                    validated_response = self._validate_response(response_text, response_model)

                # Accumulate total usage
                self.total_usage = self.total_usage + usage

                self.log.info(
                    "llm_response_generated",
                    attempt=attempt,
                    input_tokens=usage.input_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=f"${usage.total_cost_usd:.4f}",
                )
                
                return validated_response, usage
            
            except (ValidationError, json.JSONDecodeError) as e:
                self.log.warning(
                    "llm_response_validation_failed",
                    attempt=attempt,
                    error=str(e),
                    response_text=response_text[:500] if 'response_text' in locals() else None,
                )
                
                if attempt == max_retries:
                    raise LLMError(f"Validation failed after {max_retries} attempts: {e}") from e
                
                # Exponential backoff
                time.sleep(2 ** attempt)
            
            except anthropic.APIStatusError as e:
                self.log.error(
                    "anthropic_api_error",
                    attempt=attempt,
                    status_code=e.status_code,
                    error_message=str(e),
                )

                # Client errors other than rate limiting will not succeed on retry
                if 400 <= e.status_code < 500 and e.status_code not in (408, 409, 429):
                    raise LLMError(f"Anthropic API error ({e.status_code}): {e}") from e

                if attempt == max_retries:
                    raise LLMError(f"Anthropic API error after {max_retries} attempts: {e}") from e

                # Exponential backoff
                time.sleep(2 ** attempt)

            except Exception as e:
                self.log.error("unexpected_error", attempt=attempt, error=str(e))
                if attempt == max_retries:
                    raise LLMError(f"Unexpected error after {max_retries} attempts: {e}") from e
                
                time.sleep(2 ** attempt)
        
        raise LLMError(f"Failed to generate response after {max_retries} attempts")

    def _generate_once(
        self,
        prompt: str,
        response_model: type[BaseModel],
        system: str | None,
        schema: dict[str, Any] | None,
    ) -> tuple[BaseModel, LLMUsage]:
        """One attempt of generate(); failures are classified for the caller's retry policy."""
        self._last.stop_reason = None
        try:
            response_text, usage = self._invoke_model(prompt, system=system, schema=schema)
            try:
                if schema is not None:
                    validated_response = response_model.model_validate(json.loads(response_text))
                else:
                    validated_response = self._validate_response(response_text, response_model)
            except Exception as e:
                if getattr(self._last, "stop_reason", None) != "max_tokens":
                    raise
                # Say why the JSON is incomplete, rather than only where the parser gave up.
                raise LLMTransientError(
                    f"Response hit max_tokens ({self.config.max_tokens}) before the JSON was complete "
                    f"({e}); raise INDEX_MAX_TOKENS or lower INDEX_EFFORT if this repeats"
                ) from e
        except Exception as e:
            error = classify_llm_error(e)
            self.log.warning("llm_call_failed", error=str(e), retryable=isinstance(error, LLMTransientError))
            if error is e:
                raise
            raise error from e
        self.total_usage = self.total_usage + usage
        self.log.info(
            "llm_response_generated",
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=f"${usage.total_cost_usd:.4f}",
        )
        return validated_response, usage
    
    def _call(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ):
        """
        One Messages API call. Streams the response (so large INDEX_MAX_TOKENS values
        never hit the SDK's non-streaming size guard) and returns the final message
        and its usage.

        Raises:
            anthropic.APIError: If the API call fails
            LLMError: If the model refused
        """
        params: dict[str, Any] = {
            "model": self.config.model_id,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
        }
        if system is not None:
            params["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        if tools:
            params["tools"] = tools
        # Optional parameters: only sent when configured, since model support differs.
        output_config: dict[str, Any] = {}
        if self.config.temperature is not None:
            params["temperature"] = self.config.temperature
        if self.config.effort:
            output_config["effort"] = self.config.effort
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            params["output_config"] = output_config
        if self.config.thinking:
            params["thinking"] = {"type": self.config.thinking}

        try:
            with self.client.messages.stream(**params) as stream:
                message = stream.get_final_message()
        finally:
            if self.config.count_key and _call_counter is not None:
                try:
                    _call_counter(self.config.count_key)
                except Exception as e:  # counting is best effort; never fail the call over it
                    self.log.warning("call_count_failed", error=str(e))

        if message.stop_reason == "refusal":
            raise LLMPermanentError(f"Model refused the request: {getattr(message, 'stop_details', None)}")
        if message.stop_reason == "max_tokens":
            self.log.warning(
                "response_hit_max_tokens",
                max_tokens=self.config.max_tokens,
                hint="raise INDEX_MAX_TOKENS or lower INDEX_EFFORT",
            )

        usage = LLMUsage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=message.usage.cache_read_input_tokens or 0,
            cache_write_tokens=message.usage.cache_creation_input_tokens or 0,
        )
        return message, usage

    def _invoke_model(
        self,
        prompt: str,
        system: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        """
        Invoke the model with a single user prompt and return the response text.

        Args:
            prompt: The prompt to send
            system: Optional system prompt, cached (ephemeral cache breakpoint)
            schema: Optional JSON schema for structured output

        Returns:
            Tuple of (response text, usage stats)
        """
        message, usage = self._call([{"role": "user", "content": prompt}], system=system, schema=schema)
        self._last.stop_reason = message.stop_reason
        text_content = next((b.text for b in message.content if b.type == "text"), None)
        if text_content is None:
            raise LLMError(f"No text content in response (stop_reason={message.stop_reason})")
        return text_content, usage

    def generate_with_tools(
        self,
        prompt: str,
        response_model: type[BaseModel],
        system: str,
        tools: list[dict[str, Any]],
        tool_handler: Callable[[str, dict[str, Any]], str],
        max_tool_calls: int,
        min_tool_calls: int = 0,
        nudge: str = "",
    ) -> tuple[BaseModel, LLMUsage, int]:
        """
        Run a bounded tool loop, then return the schema-enforced final answer.

        The model may call `tools` (each handled by `tool_handler(name, input) -> text`)
        up to `max_tool_calls` times; further calls are answered with a note to finish.
        If it answers after fewer than `min_tool_calls`, it is sent back once with
        `nudge`. The assistant turns are appended unchanged so thinking blocks stay valid.

        Returns:
            (validated response, total usage, number of tool calls made)
        """
        schema = to_llm_schema(response_model)
        messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        total = LLMUsage(input_tokens=0, output_tokens=0)
        calls = 0
        nudged = False
        for _ in range(max_tool_calls + 4):
            _move_cache_breakpoint(messages)
            message, usage = self._call(messages, system=system, schema=schema, tools=tools)
            total = total + usage
            if message.stop_reason != "tool_use":
                text = next((b.text for b in message.content if b.type == "text"), None)
                if text is None:
                    raise LLMError(f"No final answer (stop_reason={message.stop_reason})")
                if calls < min_tool_calls and not nudged:
                    nudged = True
                    self.log.info("tool_loop_nudged", tool_calls=calls, min_tool_calls=min_tool_calls)
                    messages.append({"role": "assistant", "content": message.content})
                    messages.append({"role": "user", "content": [{"type": "text", "text": nudge.format(calls=calls)}]})
                    continue
                self.total_usage = self.total_usage + total
                return response_model.model_validate(json.loads(text)), total, calls
            messages.append({"role": "assistant", "content": message.content})
            results = []
            for block in message.content:
                if block.type != "tool_use":
                    continue
                if calls >= max_tool_calls:
                    output = "Search limit reached. Answer now with what you have."
                else:
                    calls += 1
                    try:
                        output = tool_handler(block.name, block.input)
                    except Exception as e:  # report the failure to the model, keep going
                        self.log.warning("tool_call_failed", tool=block.name, error=str(e))
                        output = f"Tool error: {e}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
            messages.append({"role": "user", "content": results})
        raise LLMError("Tool loop did not finish")

    def _validate_response(self, response_text: str, model: type[BaseModel]) -> BaseModel:
        """
        Parse and validate LLM response against a Pydantic model.
        
        Args:
            response_text: Raw response text from LLM
            model: Pydantic model to validate against
            
        Returns:
            Validated model instance
            
        Raises:
            ValidationError: If response doesn't match schema
            LLMError: If response is not valid JSON
        """
        # Try to extract JSON from response (handle cases where LLM adds extra text)
        response_text = response_text.strip()
        
        # Find JSON object boundaries
        start_idx = response_text.find("{")
        end_idx = response_text.rfind("}") + 1
        
        if start_idx == -1 or end_idx == 0:
            raise LLMError(f"No JSON object found in response: {response_text[:200]}")
        
        json_text = response_text[start_idx:end_idx]
        
        # ✅ NEW: Check for truncated response (max_tokens limit hit)
        json_text = self._repair_truncated_json(json_text)
        
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as e:
            # Try aggressive repair
            self.log.warning("json_decode_failed_attempting_repair", error=str(e), position=e.pos)
            
            import re
            repaired = json_text
            
            # 1. Remove trailing commas before closing braces/brackets
            repaired = re.sub(r',(\s*[}\]])', r'\1', repaired)
            
            # 2. Add missing commas between objects in arrays
            repaired = re.sub(r'\}(\s*\n\s*)\{', r'},\1{', repaired)
            
            # 3. Add missing commas between array elements
            repaired = re.sub(r'\](\s+)\[', r'],\1[', repaired)
            
            # 4. Fix missing commas after closing quotes
            repaired = re.sub(r'\"(\s*\n\s*)\"', r'",\1"', repaired)
            
            # 5. Try parsing repaired JSON
            try:
                parsed = json.loads(repaired)
                self.log.info("json_repaired_successfully", repairs_applied=True)
            except json.JSONDecodeError as e2:
                # Still failing - write debug files and try partial recovery
                self.log.error(
                    "json_repair_failed", 
                    original_error=str(e), 
                    repair_error=str(e2),
                    line=e.lineno,
                    column=e.colno,
                )
                
                # Log context around the error
                error_context_start = max(0, e.pos - 200)
                error_context_end = min(len(json_text), e.pos + 200)
                error_context = json_text[error_context_start:error_context_end]
                error_pointer = " " * min(100, e.pos - error_context_start) + "^"
                
                self.log.error(
                    "json_error_context",
                    position=e.pos,
                    context=f"\n{error_context}\n{error_pointer}",
                )
                
                # Write debug files
                import tempfile
                
                with tempfile.NamedTemporaryFile(mode='w', suffix='_original.json', delete=False, dir='/tmp') as f:
                    f.write(json_text)
                    original_path = f.name
                
                with tempfile.NamedTemporaryFile(mode='w', suffix='_repaired.json', delete=False, dir='/tmp') as f:
                    f.write(repaired)
                    repaired_path = f.name
                
                self.log.error(
                    "json_debug_files_written",
                    original=original_path,
                    repaired=repaired_path,
                )
                
                # Try partial recovery
                try:
                    partial_parsed = self._partial_recovery(json_text, e)
                    self.log.warning(
                        "partial_json_recovery_successful",
                        recovered_items=len(partial_parsed.get("files", [])),
                    )
                    parsed = partial_parsed
                except Exception as e3:
                    self.log.error("partial_recovery_also_failed", error=str(e3))
                    raise LLMError(
                        f"Invalid JSON in response (all repair attempts failed):\n"
                        f"Line {e.lineno}, Column {e.colno}\n"
                        f"Error: {e}\n"
                        f"Context: ...{error_context}...\n"
                        f"Debug files: {original_path}, {repaired_path}"
                    ) from e
        
        return model.model_validate(parsed)

    def _repair_truncated_json(self, json_text: str) -> str:
        """
        Detect and repair JSON truncated by max_tokens limit.
        
        Common pattern: LLM hits token limit mid-array, leaving unclosed structures.
        Example: {"files": [{"name": "x.java"} <- missing ]} at end
        
        Args:
            json_text: Raw JSON string (possibly truncated)
        
        Returns:
            Repaired JSON string with proper closing brackets/braces
        """
        # Count open/close brackets and braces
        open_braces = json_text.count('{')
        close_braces = json_text.count('}')
        open_brackets = json_text.count('[')
        close_brackets = json_text.count(']')
        
        # Check if truncated
        if open_braces == close_braces and open_brackets == close_brackets:
            return json_text  # Already balanced
        
        self.log.warning(
            "detected_truncated_json",
            open_braces=open_braces,
            close_braces=close_braces,
            open_brackets=open_brackets,
            close_brackets=close_brackets,
        )
        
        # Strategy: Add missing closures at the end
        repaired = json_text.rstrip()
        
        # Remove trailing comma if present (common before truncation)
        if repaired.endswith(','):
            repaired = repaired[:-1]
        
        # Close arrays first (inner to outer)
        missing_brackets = open_brackets - close_brackets
        if missing_brackets > 0:
            repaired += '\n' + ('  ]' * missing_brackets)
        
        # Then close objects
        missing_braces = open_braces - close_braces
        if missing_braces > 0:
            repaired += '\n' + ('}' * missing_braces)
        
        self.log.info(
            "repaired_truncated_json",
            added_brackets=missing_brackets,
            added_braces=missing_braces,
        )
        
        return repaired

    def _partial_recovery(self, json_text: str, error: json.JSONDecodeError) -> dict:
        """
        Attempt partial recovery of truncated JSON.
        Extracts valid items before the error position.
        
        Args:
            json_text: Raw JSON string with error
            error: The JSONDecodeError with position info
        
        Returns:
            Partially recovered dictionary
        
        Raises:
            Exception: If recovery not possible
        """
        import re
        
        # Try to find the array we're parsing (usually "files" for folder summaries)
        files_match = re.search(r'"files"\s*:\s*\[', json_text)
        if not files_match:
            raise Exception("Cannot locate 'files' array for partial recovery")
        
        # Truncate at error position
        truncated = json_text[:error.pos].rstrip(',\n\r\t ')
        
        # Count unclosed structures
        open_braces = truncated.count('{') - truncated.count('}')
        open_brackets = truncated.count('[') - truncated.count(']')
        
        # Close them
        truncated += '\n' + ('}' * open_braces) + (']' * open_brackets)
        
        return json.loads(truncated)

    
    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for a given text.
        Uses a simple heuristic: ~4 characters per token for English text.
        
        Args:
            text: Text to estimate tokens for
        
        Returns:
            Estimated token count
        """
        return len(text) // 4
    
    def get_total_usage(self) -> LLMUsage:
        """Get cumulative usage across all calls."""
        return self.total_usage
    
    def reset_usage(self) -> None:
        """Reset usage tracking."""
        self.total_usage = LLMUsage(input_tokens=0, output_tokens=0)