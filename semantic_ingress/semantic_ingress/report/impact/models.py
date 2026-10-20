"""
Response models for the business-impact calls.

As in report/models.py: no Optional or Union (empty strings and lists are the
sentinels), and length and range limits are stated in the descriptions and
enforced in validate.py, because structured outputs do not support them.
"""
from typing import Literal

from pydantic import BaseModel, Field

ImpactCategory = Literal[
    "duplicate_or_missed_side_effect", "lost_or_repeated_work", "wasted_spend", "stuck_or_stale_work",
    "inconsistent_state", "manual_recovery", "missed_or_overlapping_runs",
]
Stakeholder = Literal["customers", "finance", "operations", "on_call_engineering", "compliance", "data_consumers",
                      "support"]
Basis = Literal["code", "context", "assumption"]
Level = Literal["high", "medium", "low"]
DriverKind = Literal["frequency", "blast_radius", "recovery_effort", "detection_delay", "unit_cost"]
BusinessCase = Literal["strong", "moderate", "weak"]


class FailureStory(BaseModel):
    scenario: str = Field(description="At most 45 words: a concrete scenario in plain language; no figures, no currency")
    category: ImpactCategory = Field(description="One of the allowed categories for this process")
    stage: str = Field(description="One of the listed crash stages, exactly as written; empty only if none applies")
    who_feels_it: list[Stakeholder] = Field(description="1 to 3 stakeholders")
    severity: Level
    cites: list[str] = Field(description="Card fact ids (F1, ...) that show it, plus context ids (C1, ...) if used")

    model_config = {"extra": "forbid"}


class Workaround(BaseModel):
    text: str = Field(description="At most 25 words: how people cope today, or empty if not known")
    basis: Basis
    cites: list[str] = Field(description="F ids if basis is code, C ids if context, empty if assumption")

    model_config = {"extra": "forbid"}


class Driver(BaseModel):
    kind: DriverKind
    text: str = Field(description="At most 25 words; no figures")
    basis: Basis
    cites: list[str] = Field(description="F ids if basis is code, C ids if context, empty if assumption")

    model_config = {"extra": "forbid"}


class CostInput(BaseModel):
    name: str = Field(description="An input of the chosen cost model")
    low: float
    high: float
    cite: str = Field(description="The context id (C1, ...) that states this number")

    model_config = {"extra": "forbid"}


class DiscoveryQuestion(BaseModel):
    question: str = Field(description="At most 30 words, phrased for a customer call")
    why: str = Field(description="At most 20 words: what the answer changes")
    fills: str = Field(description="The driver kind or cost input name the answer resolves")
    ask: Stakeholder

    model_config = {"extra": "forbid"}


class ProcessImpactDraft(BaseModel):
    process: str = Field(description="The process name exactly as given")
    stories: list[FailureStory] = Field(description="1 to 3")
    workaround: Workaround
    drivers: list[Driver] = Field(description="0 to 5")
    cost_model: str = Field(description="A listed cost model id, or empty if none applies")
    cost_inputs: list[CostInput] = Field(description="Only numbers the context states; empty otherwise")
    business_case: BusinessCase
    case_reason: str = Field(description="At most 40 words; no figures")
    questions: list[DiscoveryQuestion] = Field(description="0 to 5")

    model_config = {"extra": "forbid"}


class ImpactSummaryDraft(BaseModel):
    headline: str = Field(description="At most 40 words")
    exec_summary: str = Field(description="At most 120 words, plain language, no figures")
    mentions: list[str] = Field(description="Process names the summary refers to, exactly as given")

    model_config = {"extra": "forbid"}
