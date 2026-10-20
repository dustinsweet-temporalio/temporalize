"""
Read the business impact generated from an index's Temporal report (on demand, in the UI).
"""
import logging

from strands import tool

from config import REPORTS_COLLECTION
from utils import construct_database_name, get_mongo_client, validate_repo_id

logger = logging.getLogger(__name__)

VIEWS = ("exec", "champion", "discovery")


def load_impact(repo_id: str) -> tuple[dict | None, dict | None]:
    """(stored business-impact document or None, the Temporal report document or None)."""
    db = get_mongo_client()[construct_database_name(repo_id)]
    impact = db[REPORTS_COLLECTION].find_one({"_id": f"{repo_id}::business_impact"})
    report = db[REPORTS_COLLECTION].find_one({"_id": f"{repo_id}::temporal_report"},
                                             {"generated_at": 1, "commit_sha": 1}) if impact else None
    return impact, report


@tool
def get_business_impact(repo_id: str, view: str = "exec") -> str:
    """
    Read the business impact generated from the index's Temporal report: what a failure
    of each process would mean for the business, who feels it, illustrative cost ranges
    (only from numbers the customer stated, with the formula), what is not worth pursuing,
    and the questions to ask the customer. Every claim cites a code fact (F ids) or a
    customer-context item (C ids), or is labeled an assumption.

    Use it for questions about the business case, cost or risk of a process, who is
    affected, or what to ask a customer.

    Args:
        repo_id: Repository identifier
        view: "exec" (the brief for the economic buyer), "champion" (stories with their
              code evidence, what changes with Temporal, the proof-of-concept scope) or
              "discovery" (questions by who to ask, missing inputs, assumptions)

    Returns:
        The requested view as Markdown
    """
    is_valid, error = validate_repo_id(repo_id)
    if not is_valid:
        raise ValueError(error)
    if view not in VIEWS:
        view = "exec"
    impact, report = load_impact(repo_id)
    if not impact or not impact.get("data"):
        return (f"No business impact has been generated for {repo_id}. It is generated on demand: in the "
                "Temporal report view, choose Business impact, then Generate.")
    logger.info(f"Business impact for {repo_id}: view={view}")
    notes = []
    src = impact.get("source_report") or {}
    if report and any(report.get(k) != src.get(k) for k in ("generated_at", "commit_sha")):
        notes.append("Note: the Temporal report was rebuilt after this business impact was generated; it may be "
                     "out of date.")
    if impact.get("status") == "failed":
        notes.append(f"Note: the latest generation failed ({impact.get('error')}); this is the previous result.")
    markdown = (impact.get("markdown") or {}).get(view)
    if not markdown:
        from semantic_ingress.report.impact.render import render

        markdown = render(impact["data"])[view]
    return "\n".join(notes + [markdown])
