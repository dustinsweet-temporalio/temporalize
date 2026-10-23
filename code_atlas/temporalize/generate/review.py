"""The Code Atlas review: a structured model review of the POC diff, grounded in the knowledge pack."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field

from temporalize.generate import prompts
from temporalize.generate.workspace import POC_DIR, diff, workspace_path

DIFF_CHARS = 180_000


class Finding(BaseModel):
    severity: Literal["high", "medium", "low"]
    title: str = Field(description="At most 12 words")
    detail: str = Field(description="What is wrong and the concrete fix, at most 60 words")
    file: str = Field(description="Workspace-relative path, or empty")
    line: int = Field(description="Line in the new file, 0 if not applicable")
    cites: list[str] = Field(description="Pack entry ids from the checklist that apply; may be empty")

    model_config = {"extra": "forbid"}


class Review(BaseModel):
    findings: list[Finding]

    model_config = {"extra": "forbid"}


@lru_cache(maxsize=1)
def checklist() -> str:
    """Review items from the knowledge pack (checks for all SDKs and Python-specific entries): id | title."""
    try:
        from semantic_ingress.config import LensConfig
        from semantic_ingress.lens.pack import load_pack

        pack = load_pack(LensConfig().pack_path)
    except Exception:
        return "(knowledge pack unavailable)"
    lines = []
    for entry in pack.data.get("checks") or []:
        sdks = entry.get("sdk") or ["all"]
        if pack.usable(entry) and ("all" in sdks or "python" in sdks):
            lines.append(f"{entry['id']} | {entry['t']}")
    for entry in pack.data.get("sdk_specific") or []:
        if pack.usable(entry) and "python" in (entry.get("sdk") or []):
            lines.append(f"{entry['id']} | {entry['t']}")
    return "\n".join(lines)


def review_code(llm, run: dict, process: dict, n: int) -> dict:
    ws = workspace_path(run)
    plan_path = ws / POC_DIR / "PLAN.md"
    plan_md = plan_path.read_text() if plan_path.exists() else ""
    diff_text = diff(run)
    if len(diff_text) > DIFF_CHARS:
        diff_text = diff_text[:DIFF_CHARS] + "\n… (diff truncated)"
    passes = (run.get("generate") or {}).get("review_passes") or []
    previous = passes[-1].get("findings", []) if passes else []
    review = llm.structured(prompts.REVIEW_SYSTEM,
                            prompts.review_prompt(plan_md, run.get("criteria") or [], diff_text, checklist(), n, previous),
                            Review, max_tokens=24000)
    findings = []
    for i, f in enumerate(review.findings, 1):
        findings.append({"id": f"R{n}.{i}", **f.model_dump()})
    return {"findings": findings}


def needs_fix(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f.get("severity") in ("high", "medium")]
