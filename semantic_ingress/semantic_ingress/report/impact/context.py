"""
Customer context for business-impact translation: what the user knows about the
customer (call notes, volumes, pain points), numbered C1..Cn so the model can cite it.

Context is untrusted input. It is sanitized here (control characters and the
prompt's block delimiters removed), wrapped as data in the prompt, and never
logged. Empty context is valid: the output is then qualitative only.
"""
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_ITEMS = 40
ITEM_CHARS = 400
# The prompt wraps context between these markers; context may not contain them.
BLOCK_START = "<<<CUSTOMER_CONTEXT"
BLOCK_END = "CUSTOMER_CONTEXT>>>"
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MARKERS = re.compile(r"<{3,}|>{3,}")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# Labels for the numbered items, in the order they are numbered.
FIELDS = [("industry", "Industry"), ("volumes", "Volumes"), ("pain_points", "Pain points"), ("text", "Notes")]


def sanitize(text: str) -> str:
    text = _CONTROL.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _MARKERS.sub(" ", text)
    return "\n".join(" ".join(line.split()) for line in text.split("\n")).strip()


class ImpactContext(BaseModel):
    """What the user tells us about the customer. 0 for hourly_rate means not given."""

    text: str = Field(default="", max_length=4000, description="Free text: call notes, pain points")
    industry: str = Field(default="", max_length=80)
    volumes: str = Field(default="", max_length=300, description='For example "~40k payouts/day"')
    pain_points: str = Field(default="", max_length=1000)
    audience: Literal["exec", "champion", "discovery"] = Field(
        default="exec", description="The view shown first; every view is always built")
    currency: str = Field(default="USD", pattern=r"^(?:[A-Z]{3}|[$€£¥₹])$")
    hourly_rate: float = Field(default=0.0, ge=0, le=100_000, description="Loaded staff cost per hour; 0 if not given")

    model_config = {"extra": "forbid", "frozen": True}

    @field_validator("text", "industry", "volumes", "pain_points", mode="after")
    @classmethod
    def _clean(cls, value: str) -> str:
        return sanitize(value)

    @property
    def present(self) -> bool:
        return any(getattr(self, name) for name, _ in FIELDS) or self.hourly_rate > 0

    @property
    def hash(self) -> str:
        """Content hash of everything that changes the output (the default view does not)."""
        fields = {**self.model_dump(exclude={"audience"}), "hourly_rate": float(self.hourly_rate)}  # 90 == 90.0
        blob = json.dumps(fields, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def hourly_rate_item(ctx: ImpactContext) -> str:
    rate = f"{ctx.hourly_rate:g}"
    return f"Hourly rate: {rate} {ctx.currency}/hour"


def numbered(ctx: ImpactContext) -> dict[str, str]:
    """Context items as {"C1": "Industry: fintech", ...}: each field split on lines and sentence ends."""
    items: list[str] = [hourly_rate_item(ctx)] if ctx.hourly_rate > 0 else []  # first, so the cap never drops it
    for name, label in FIELDS:
        for line in getattr(ctx, name).split("\n"):
            for part in _SENTENCE_END.split(line):
                part = part.strip()
                if part:
                    items.append(f"{label}: {part[:ITEM_CHARS]}")
    return {f"C{i}": text for i, text in enumerate(items[:MAX_ITEMS], 1)}


def hourly_rate_id(items: dict[str, str], ctx: ImpactContext) -> str:
    """The C id of the structured hourly rate, or "" when it was not given (or did not fit)."""
    target = hourly_rate_item(ctx) if ctx.hourly_rate > 0 else None
    return next((cid for cid, text in items.items() if text == target), "")
