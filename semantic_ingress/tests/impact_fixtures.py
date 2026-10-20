"""
Offline fakes for the business-impact tests: a handmade stored Temporal report, a
fake store and a scripted fake model (no network, no real model).
"""
import copy
import re

from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.report.impact.models import (
    CostInput,
    DiscoveryQuestion,
    Driver,
    FailureStory,
    ImpactSummaryDraft,
    ProcessImpactDraft,
    Workaround,
)
from semantic_ingress.report.investigate import NOT_ESTABLISHED
from semantic_ingress.report.models import ProcessCard

USAGE = LLMUsage(input_tokens=1000, output_tokens=200)


def fact(text, refs=None):
    return {"text": text, "refs": refs if refs is not None else ["orders/flow.py@L1-2"]}


def card(**over):
    """Order payment: F1 trigger, F2 unit, F3 charge (paid), F4 ship, F5 state, F6 persistence,
    F7 crash after charging, F8 reliability, F9 run length (waits add facts before F9)."""
    base = {
        "process": "Order payment",
        "trigger": fact("An HTTP order request."),
        "unit_of_work": fact("One order."),
        "scales_with": "orders per day",
        "scale_assumption": "Assumes 1,000 orders a day.",
        "steps": [
            {"name": "charge", "does": "Charges the card.", "effects": ["paid_call"], "refs": ["orders/pay.py@L1-2"]},
            {"name": "ship", "does": "Books the shipment and records it.", "effects": ["external_call", "db_write"],
             "refs": ["orders/ship.py@L4-9"]},
        ],
        "state_between_steps": fact("In memory."),
        "persistence": fact("Written at the end."),
        "crash_loss": [{"stage": "after charging", "loses": "The shipment; the charge is not undone.",
                        "refs": ["orders/flow.py@L3-4"]}],
        "reliability": [fact("Retries the charge three times.")],
        "waits": [],
        "run_length": fact(NOT_ESTABLISHED, []),
        "files": ["orders/flow.py", "orders/pay.py", "orders/ship.py"],
    }
    base.update(over)
    return ProcessCard.model_validate(base).model_dump()


def process(name="Order payment", verdict_class="suitable", overall=7.2, best_path="PATH-TRANSACTION", **over):
    p = {
        "name": name, "entry": f"{name.lower().replace(' ', '_')}.py", "origin": "repository lens",
        "card": card(process=name), "low_confidence": [], "verdict_class": verdict_class, "overall": overall,
        "best_path": best_path, "fit": 8,
        "design": {"process": name, "elements": [], "removes": ["orders/flow.py: retry loop"],
                   "stays": ["The web form"], "why": "A crash after charging leaves the order unshipped.",
                   "gains": [{"text": "Charges are never repeated", "cites": ["F3", "F7"]}], "cites": ["P-SAGA"]},
        "verdict": "Suitable: a distributed transaction.", "reasons": [], "would_change": [],
    }
    if verdict_class == "not suitable":
        p["design"] = None
        p["reasons"] = [{"text": "It is a single fast database read.", "evidence": [], "cites": ["G-LATENCY"]}]
        p["would_change"] = ["It started calling paid services."]
    p.update(over)
    return p


def report_doc(processes=None, status="none", generated_at="2026-10-20T10:00:00+00:00", commit="abc1234def"):
    data = {"repo_id": "demo", "status": status, "commit": commit}
    if processes is not None:
        data["conversion"] = {"processes": processes, "paths": [
            {"id": "PATH-PIPELINE", "t": "Durable pipeline"}, {"id": "PATH-TRANSACTION", "t": "Distributed transaction"},
            {"id": "PATH-WAIT", "t": "Long-lived waits"}, {"id": "PATH-SCHEDULE", "t": "Scheduled work"}]}
    return {"_id": "demo::temporal_report", "repo_id": "demo", "status": "ok", "error": None,
            "generated_at": generated_at, "commit_sha": commit, "pack_version": "0.3.0", "rubric_version": "1.0.0",
            "data": data}


def standard_processes():
    """One of each kind: a transaction, a borderline schedule, a not-a-fit, no consequence, and a report error."""
    sched = card(process="Nightly export", crash_loss=[
        {"stage": "while exporting", "loses": "The partial export; the next night starts over.", "refs": ["jobs/export.py@L5"]}],
        steps=[{"name": "export", "does": "Writes the export file.", "effects": ["file_write"], "refs": ["jobs/export.py@L5"]}],
        trigger=fact("A cron entry at midnight."))
    quiet = card(process="Lookup", crash_loss=[], steps=[
        {"name": "read", "does": "Reads a row.", "effects": [], "refs": ["lookup.py@L1"]}])
    return [
        process(),
        process("Nightly export", "borderline", 5.4, "PATH-SCHEDULE", card=sched, low_confidence=["F6 unsupported"]),
        process("Report email", "not suitable", 3.1, "PATH-PIPELINE"),
        process("Lookup", "suitable", 6.0, "PATH-PIPELINE", card=quiet),
        {"name": "Broken", "entry": "broken.py", "origin": "entry point", "error": "investigation timed out"},
    ]


class FakeStore:
    """The store methods impact generation uses: the report and the report cache."""

    def __init__(self, doc=None):
        self.doc = doc
        self.cache = {}

    def get_report(self, repo_id):
        return copy.deepcopy(self.doc)

    def get_cached(self, repo_id, key):
        return copy.deepcopy(self.cache.get(key))

    def put_cached(self, repo_id, key, value):
        self.cache[key] = copy.deepcopy({"_id": key, **value})


def story(**over):
    base = dict(scenario="If the server restarts after the card is charged, the order is never shipped and the "
                         "customer has paid for nothing.",
                category="duplicate_or_missed_side_effect", stage="after charging",
                who_feels_it=["customers", "support"], severity="high", cites=["F7", "F3"])
    base.update(over)
    return FailureStory(**base)


def draft(name="Order payment", **over):
    base = dict(
        process=name,
        stories=[story()],
        workaround=Workaround(text="Support staff check payments against shipments by hand.", basis="assumption",
                              cites=[]),
        drivers=[Driver(kind="frequency", text="How often the server restarts mid-order.", basis="assumption",
                        cites=[]),
                 Driver(kind="blast_radius", text="Each crash affects the one order in flight.", basis="code",
                        cites=["F2"])],
        cost_model="duplicate_side_effect",
        cost_inputs=[],
        business_case="strong",
        case_reason="Customers pay without receiving goods, which creates refunds and support work.",
        questions=[DiscoveryQuestion(question="How do you find orders that were charged but never shipped?",
                                     why="Shows how long the problem goes unnoticed.", fills="detection_delay",
                                     ask="support")],
    )
    base.update(over)
    return ProcessImpactDraft(**base)


def summary(names, **over):
    base = dict(headline="Failed orders can charge customers without shipping their goods.",
                exec_summary="Order payment carries the strongest case. Report email is not worth pursuing.",
                mentions=list(names))
    base.update(over)
    return ImpactSummaryDraft(**base)


class FakeLLM:
    """
    Answers by process name from `script` (name -> list of answers, one per call:
    the first answer, then the retry). Unscripted processes get `draft(name)` with
    a story that fits their card. Summaries come from `summaries` (same idea).
    """

    config = type("C", (), {"model_id": "fake-model"})()

    def __init__(self, script=None, summaries=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.summaries = list(summaries or [])
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt, response_model, system=None, enforce_schema=False):
        self.calls += 1
        self.prompts.append(prompt)
        if response_model is ImpactSummaryDraft:
            if self.summaries:
                return self.summaries.pop(0), USAGE
            names = re.findall(r"^\d+\. (.+?) \|", prompt, re.M) + re.findall(r"^- (.+)$", prompt.split(
                "NOT WORTH PURSUING:")[1], re.M)
            return summary(names), USAGE
        assert response_model is ProcessImpactDraft
        name = re.search(r"^PROCESS: (.+)$", prompt, re.M).group(1)
        answers = self.script.get(name)
        if answers:
            return answers.pop(0) if len(answers) > 1 else answers[0], USAGE
        return default_answer(name, prompt), USAGE


def default_answer(name, prompt):
    """A valid answer for the standard processes."""
    if name == "Nightly export":
        return draft(name, stories=[story(
            scenario="If the server restarts during the nightly export, the partial file is lost and the export "
                     "starts again the next night.",
            category="missed_or_overlapping_runs", stage="while exporting", who_feels_it=["data_consumers"],
            severity="medium", cites=["F1", "F6"])], cost_model="", business_case="strong")
    return draft(name)


def cost_input(name="incidents_per_month", low=4, high=4, cite="C2"):
    return CostInput(name=name, low=low, high=high, cite=cite)
