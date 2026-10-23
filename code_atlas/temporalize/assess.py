"""
Screens 3 to 6: from the Temporal report to a scoped, approvable plan.

    analyze_scope   one model call, right after the report (before the user says what to
                    focus on): Code Atlas's recommendation, the external systems the code
                    really talks to (named from the source, not from a list of libraries),
                    what a crash loses for each process, and every way the code runs each
                    assessable process (variants, read from the code rather than asked)
    draft_plan      the behavior claims come straight from the report's process card
                    (facts already checked against the source; confidence from those
                    checks); one model call writes the plain-language summaries, proposes
                    intentional changes and the test data, and checks the user's notes
                    against the code; then design_poc
    design_poc      the design, by a model that reads Temporal's temporal-developer skill
                    and searches the Temporal Docs MCP (research.py), starting from the
                    report's design; also how the user's design change requests are applied

The model sees source text, the report and the brief. Line references it returns are
kept only when they point at a file of the repository.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, Field

SOURCE_DIR = Path(os.environ.get("SOURCE_DIR", "/data/source"))
REF = re.compile(r"^(?P<path>[^@]+)@L(?P<start>\d+)(?:-(?P<end>\d+))?$")
MAX_FILE_CHARS = 40_000
MAX_SOURCE_CHARS = 220_000

SCOPE_SYSTEM = """You help a Temporal solutions architect scope a proof of concept (POC) from a code assessment.
You get the processes a code report found in a repository (each with a verdict, a fit score and a process card of
facts checked against the source), how the local scan says the code runs today, the libraries and command-line tools
it imports or calls, and source excerpts.

Answer precisely and briefly, in plain language a sales engineer can read aloud. Never invent facts: say only what the
process cards and source excerpts show. Line references must use the form path@Lstart-end and point at text you were given."""

PLAN_SYSTEM = """You prepare a Temporal proof of concept (POC) plan that a solutions architect approves before any code is
written. Both the original code and the Temporal version will run on the test data you write, with every external boundary
(browsers, HTTP APIs, LLM calls, SSH, email, databases) replaced by fakes that serve this data, so the original code becomes
the answer key. Never use real customer data, credentials, hostnames or personal data: everything is synthetic and labeled so."""


# ----------------------------------------------------------------------
# Source text
# ----------------------------------------------------------------------

def _file(run: dict, path: str) -> str | None:
    """A repository file: the index's mirrored copy, else the repository folder."""
    for root in (SOURCE_DIR / run["repo"]["index"], Path(run["repo"]["path"])):
        candidate = (root / path).resolve()
        if root.resolve() in candidate.parents and candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    return None


def numbered(text: str, start: int = 1, end: int | None = None) -> str:
    lines = text.splitlines()
    end = min(end or len(lines), len(lines))
    return "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(max(start, 1), end + 1))


def source_bundle(run: dict, paths: list[str], budget: int = MAX_SOURCE_CHARS) -> str:
    """Numbered source of the given files, within a character budget."""
    parts, used = [], 0
    for path in dict.fromkeys(paths):
        text = _file(run, path)
        if text is None:
            continue
        body = numbered(text)[:MAX_FILE_CHARS]
        if used + len(body) > budget:
            parts.append(f"=== {path} (omitted: over the size budget)")
            continue
        parts.append(f"=== {path}\n{body}")
        used += len(body)
    return "\n\n".join(parts)


def valid_refs(run: dict, refs: list[str]) -> list[str]:
    return [r for r in refs if (m := REF.match(r.strip())) and _file(run, m.group("path")) is not None]


# ----------------------------------------------------------------------
# Processes from the report
# ----------------------------------------------------------------------

def report_processes(report: dict) -> list[dict]:
    """The report's investigated processes, keyed p1, p2, ... in the report's order (best first)."""
    out = []
    for i, p in enumerate((report.get("conversion") or {}).get("processes", []), start=1):
        if p.get("error"):
            continue
        out.append({**p, "key": f"p{i}"})
    return out


def process_by_key(report: dict, key: str) -> dict | None:
    return next((p for p in report_processes(report) if p["key"] == key), None)


def _verdict_class(p: dict) -> str:
    return (p.get("verdict_class") or ("suitable" if p.get("suitable") else "not suitable")).lower()


def _card_brief(p: dict) -> str:
    card = p.get("card") or {}
    steps = "; ".join(f"{s['name']}: {s['does']}" for s in card.get("steps", []))
    losses = "; ".join(f"{c['stage']}: {c['loses']}" for c in card.get("crash_loss", []))
    return (f"[{p['key']}] {p['name']} — {_verdict_class(p)}, {p.get('overall')}/10, entry {p.get('entry')}\n"
            f"  trigger: {(card.get('trigger') or {}).get('text', '')} refs {(card.get('trigger') or {}).get('refs', [])}\n"
            f"  unit of work: {(card.get('unit_of_work') or {}).get('text', '')}\n"
            f"  steps: {steps}\n  crash loses: {losses}\n  files: {', '.join(card.get('files', []))}\n"
            f"  verdict text: {p.get('verdict', '')}")


# ----------------------------------------------------------------------
# Scope analysis (screen 5)
# ----------------------------------------------------------------------

class ProcessNote(BaseModel):
    key: str = Field(description="The process key exactly as given, e.g. p1")
    summary: str = Field(description="At most 25 words: what goes wrong today if it crashes mid-run, concretely")
    also_found: str = Field(description="At most 30 words: one notable defect or risk the card or source shows "
                                        "(for example state marked done before the work succeeded); empty if none")

    model_config = {"extra": "forbid"}


class Variant(BaseModel):
    name: str = Field(description="At most 5 words, e.g. 'Weekday mornings', 'Every 15 days', 'On demand'")
    trigger: str = Field(description="At most 20 words: what starts this variant (timer, cron line, CLI flag, endpoint)")
    refs: list[str] = Field(description="path@Lstart-end of the code or unit file that shows it")

    model_config = {"extra": "forbid"}


class Contradiction(BaseModel):
    note: str = Field(description="What the pasted notes say, at most 20 words")
    code: str = Field(description="What the code shows instead, at most 25 words")
    refs: list[str]

    model_config = {"extra": "forbid"}


SystemKind = Literal["web_app", "ai", "email", "spreadsheet", "storage", "database", "chat", "notify", "api",
                     "payments", "cloud", "ssh", "queue", "other"]


class System(BaseModel):
    name: str = Field(description="The product or service the code talks to, as people call it: 'ClockShark', 'Gmail', "
                                  "'Google Sheets', 'Gemini', 'Stripe', 'the company SFTP server'. Not a library name.")
    kind: SystemKind
    via: str = Field(description="At most 8 words: how the code reaches it, e.g. 'Selenium browser login', "
                                 "'gws CLI', 'google-genai SDK', 'REST API with requests'")
    processes: list[str] = Field(description="Keys of the processes that use it")
    refs: list[str] = Field(description="path@Lstart-end of a line that shows the code talking to it")

    model_config = {"extra": "forbid"}


class ProcessVariants(BaseModel):
    key: str = Field(description="The process key exactly as given")
    variants: list[Variant] = Field(description="Every distinct way the code runs this process: schedules, modes, "
                                                "entry points; one when it runs only one way")

    model_config = {"extra": "forbid"}


class ScopeAnalysis(BaseModel):
    recommendation: str = Field(description="At most 45 words, plain language for a sales engineer: which process to "
                                            "build the POC on and why, from the verdicts and what a crash loses")
    recommended: str = Field(description="Key of the recommended process (a suitable one, else the best borderline one)")
    processes: list[ProcessNote]
    systems: list[System] = Field(description="Every external system the code talks to, named as the product it is, "
                                              "once each: one entry per product even when several processes or ways reach it")
    variants: list[ProcessVariants] = Field(description="For every process that is not 'not suitable'")

    model_config = {"extra": "forbid"}


def _runs_today_sources(run: dict) -> list[str]:
    paths = []
    for item in (run.get("scan") or {}).get("runs_today", []):
        paths += [e for e in item.get("evidence", []) if isinstance(e, str) and "@" not in e]
    return paths[:12]


def _trigger_paths(processes: list[dict]) -> list[str]:
    paths = []
    for p in processes:
        card = p.get("card") or {}
        for ref in (card.get("trigger") or {}).get("refs", []):
            if m := REF.match(ref):
                paths.append(m.group("path"))
        if p.get("entry"):
            paths.append(p["entry"])
    return paths


def _hints(run: dict) -> str:
    """The local scan's imports and CLIs: hints for naming the systems, not the answer."""
    rows = [f"{i['name']} ({i.get('category', '')}): {', '.join((i.get('files') or [])[:4])}"
            for i in (run.get("scan") or {}).get("integrations", [])]
    return "\n".join(rows) or "(none)"


def analyze_scope(llm, run: dict, report: dict) -> dict:
    """The scope object for screens 3 to 5 (processes, the recommendation, systems, variants per process)."""
    procs = report_processes(report)
    if not procs:
        return {"processes": [], "selected": None, "variants": [], "systems": [], "recommendation": "",
                "contradictions": [], "in_scope": [], "confirmed": False, "borderline_confirmed": False}
    card_files = [f for p in procs for f in (p.get("card") or {}).get("files", [])]
    source = source_bundle(run, _runs_today_sources(run) + _trigger_paths(procs) + card_files, budget=140_000)
    runs_today = ", ".join(i["name"] for i in (run.get("scan") or {}).get("runs_today", [])) or "nothing detected"
    prompt = (
        f"HOW IT RUNS TODAY (local scan): {runs_today}\n\n"
        f"LIBRARIES AND COMMAND-LINE TOOLS THE CODE USES (local scan; hints only)\n{_hints(run)}\n\n"
        "PROCESSES\n" + "\n\n".join(_card_brief(p) for p in procs) + "\n\n"
        f"SOURCE EXCERPTS\n{source}\n\n"
        "Return: your recommendation, a note for every process, every external system the code talks to (named as "
        "the product or service, with the line that shows it), and the variants of every process that is not "
        "'not suitable'."
    )
    a = llm.structured(SCOPE_SYSTEM, prompt, ScopeAnalysis, max_tokens=16000)
    keys = {p["key"] for p in procs}
    notes = {n.key: n for n in a.processes if n.key in keys}
    by_variants = {pv.key: pv.variants for pv in a.variants if pv.key in keys}
    assessable = [p for p in procs if _verdict_class(p) != "not suitable"]
    suitable = [p for p in procs if p.get("suitable")]
    if a.recommended in {p["key"] for p in assessable}:
        selected = a.recommended
    else:
        selected = (suitable or assessable or procs)[0]["key"]
    processes = []
    for p in procs:
        card = p.get("card") or {}
        losses = [c.get("loses", "") for c in card.get("crash_loss") or []]
        loss = next((x for x in losses if x and not x.lower().startswith(("not established", "nothing"))), "")
        n = notes.get(p["key"])
        variants = [{"id": f"v{i}", "name": v.name, "trigger": v.trigger, "refs": valid_refs(run, v.refs),
                     "included": True} for i, v in enumerate(by_variants.get(p["key"], []), start=1)]
        processes.append({
            "key": p["key"], "name": p["name"], "overall": p.get("overall"), "verdict_class": _verdict_class(p),
            "suitable": bool(p.get("suitable")), "gated": _verdict_class(p) == "not suitable",
            "gate_capped": bool(p.get("gate_capped")), "recommended": p["key"] == selected,
            "summary": n.summary if n else loss, "crash_loss": loss, "also_found": n.also_found if n else "",
            "entry": p.get("entry"), "files": card.get("files", []),
            "reasons": [r.get("text", "") for r in p.get("reasons", [])] + list(p.get("would_change") or []),
            "verdict": p.get("verdict", ""), "variants": variants,
            **scope_items(p, n.also_found if n else ""),
        })
    processes.sort(key=lambda x: (x["key"] != selected, -(x["overall"] or 0)))
    chosen = next(p for p in processes if p["key"] == selected)
    systems = merge_systems([{"name": x.name, "kind": x.kind, "via": x.via, "processes": [k for k in x.processes if k in keys],
                              "refs": valid_refs(run, x.refs)} for x in a.systems])
    return {
        "processes": processes, "selected": selected, "recommended": selected, "recommendation": a.recommendation,
        "systems": systems, "variants": chosen["variants"], "contradictions": [],
        "in_scope": in_scope_tags(chosen), "notes": "", "confirmed": False, "borderline_confirmed": False,
    }


def merge_systems(systems: list[dict]) -> list[dict]:
    """One entry per product: the ways it is reached, its processes and refs, merged."""
    out: dict[str, dict] = {}
    for x in systems:
        key = " ".join(x["name"].lower().split())
        if key not in out:
            out[key] = {**x, "processes": list(x["processes"]), "refs": list(x["refs"])}
            continue
        m = out[key]
        if x["via"] and x["via"].lower() not in m["via"].lower():
            m["via"] = f"{m['via']}; {x['via']}" if m["via"] else x["via"]
        m["processes"] = list(dict.fromkeys(m["processes"] + x["processes"]))
        m["refs"] = list(dict.fromkeys(m["refs"] + x["refs"]))
    return list(out.values())


def in_scope_tags(process: dict) -> list[dict]:
    entry = process.get("entry") or ""
    return [{"id": f"scope-{v['id']}", "label": f"{_folder(entry)} {v['name']}".strip()}
            for v in process.get("variants") or []] or [{"id": "scope-entry", "label": entry or "The selected process"}]


def scope_items(p: dict, also_found: str = "") -> dict:
    """What a user can check in or out for a process: its steps, and the failures the POC should address."""
    card = p.get("card") or {}
    steps = [{"id": f"s{i}", "name": st["name"], "does": st.get("does", ""), "effects": st.get("effects", []),
              "refs": st.get("refs", []), "included": True} for i, st in enumerate(card.get("steps", []), start=1)]
    risks = [{"id": f"r{i}", "text": f"A crash {c['stage'].strip()}: {c['loses'].strip()}", "refs": c.get("refs", []),
              "included": True} for i, c in enumerate(card.get("crash_loss", []), start=1)
             if c.get("loses") and not c["loses"].lower().startswith(("not established", "nothing"))]
    if also_found:
        risks.append({"id": "r-found", "text": also_found, "refs": [], "included": True})
    return {"steps": steps, "risks": risks}


def _folder(path: str) -> str:
    return (path.split("/", 1)[0] + "/") if "/" in path else path


# ----------------------------------------------------------------------
# Plan (screen 6)
# ----------------------------------------------------------------------

CONFIDENCE = {"supported": "high", "unclear": "medium", "unsupported": "low"}


def plan_claims(process: dict, excluded_steps: set[str] | None = None) -> list[dict]:
    """What the code does today: the card's facts (minus steps left out of scope), with source and confidence."""
    card = process.get("card") or {}
    excluded_steps = excluded_steps or set()
    checks = {c.get("text"): c.get("verdict") for c in process.get("card_checks") or []}
    low = set(process.get("low_confidence") or [])
    facts: list[tuple[str, list[str]]] = []
    for name in ("trigger", "unit_of_work"):
        if card.get(name):
            facts.append((card[name]["text"], card[name].get("refs", [])))
    for i, s in enumerate(card.get("steps", []), start=1):
        if f"s{i}" not in excluded_steps:
            facts.append((f"{s['name']}: {s['does']}", s.get("refs", [])))
    for name in ("state_between_steps", "persistence", "run_length"):
        if card.get(name):
            facts.append((card[name]["text"], card[name].get("refs", [])))
    for name in ("reliability", "waits"):
        for f in card.get(name) or []:
            facts.append((f["text"], f.get("refs", [])))
    claims = []
    for text, refs in facts:
        if not text or text.startswith("Not established"):
            continue
        verdict = checks.get(text) or checks.get(text.split(": ", 1)[-1])
        confidence = CONFIDENCE.get(verdict or "", "medium")
        if text in low and confidence == "high":
            confidence = "medium"
        claims.append({"id": f"c{len(claims) + 1}", "text": text, "refs": refs, "confidence": confidence,
                       "status": "accepted", "edited_text": None})   # kept unless the user drops or edits it
    return claims


class Change(BaseModel):
    title: str = Field(description="At most 8 words, imperative, e.g. 'Fix the Monday start date'")
    detail: str = Field(description="At most 40 words: what the code does wrong today and what the POC does instead")
    refs: list[str] = Field(description="path@Lstart-end showing the defect")

    model_config = {"extra": "forbid"}


class Failure(BaseModel):
    boundary: str = Field(description="The external boundary, as a short snake_case name, e.g. 'login', 'gemini_rate'")
    times: int = Field(description="How many consecutive calls fail before it succeeds (1-3)")
    detail: str = Field(description="At most 20 words: how it fails (exception type and message)")

    model_config = {"extra": "forbid"}


class DataFile(BaseModel):
    name: str = Field(description="File name under testdata/, e.g. shifts.json")
    description: str = Field(description="At most 20 words: which boundary serves it and how")
    content: str = Field(description="The file content (JSON or CSV), synthetic, at most about 6000 characters")

    model_config = {"extra": "forbid"}


class PlanDraft(BaseModel):
    overview: str = Field(description="At most 110 words, plain-language prose a non-engineer follows, as a short "
                                      "narrative: what the process does today, what goes wrong when it fails, and what "
                                      "the POC will show. No jargon, no lists.")
    today_summary: str = Field(description="At most 70 words of prose: what the code does today, in plain words (the "
                                           "behaviors listed below, told as a story)")
    changes_summary: str = Field(description="At most 45 words of prose about the fixes proposed (or that none are "
                                             "needed and today's behavior is kept exactly)")
    approve_summary: str = Field(description="One or two sentences, at most 40 words: what approving starts (the POC "
                                             "built from this plan and checked against the original on the test data)")
    contradictions: list[Contradiction] = Field(description="Only clear contradictions between the user's notes and "
                                                            "the code; empty without notes")
    changes: list[Change] = Field(description="Clear defects the POC should fix on purpose (0-3); empty when none is clear")
    summary: str = Field(description="Test data in at most 8 words, e.g. 'Fake shifts, frozen Monday, 2 failed logins'")
    frozen_time: str = Field(description="ISO 8601 local date-time both versions treat as now, chosen to exercise "
                                         "date logic (for example a Monday morning)")
    failures: list[Failure] = Field(description="1-3 injected failures at boundaries the code calls")
    files: list[DataFile] = Field(description="2-6 files: what every boundary returns, enough for one full run")

    model_config = {"extra": "forbid"}


ElementKind = Literal["workflow", "child_workflow", "activity", "schedule", "signal", "query", "update", "timer"]


class DesignElement(BaseModel):
    kind: ElementKind
    name: str = Field(description="Name in the code, e.g. TimesheetAuditWorkflow, download_shifts, audit-weekday-mornings")
    does: str = Field(description="At most 25 words, including its timeout, retry and heartbeat choice where it has one")
    files: list[str] = Field(description="Original files it is built from")

    model_config = {"extra": "forbid"}


class Decision(BaseModel):
    decision: str = Field(description="At most 20 words: a design choice, e.g. 'One activity per shift note rating'")
    reason: str = Field(description="At most 30 words: why, in terms of this code")
    cites: list[str] = Field(description="Docs URLs search_temporal_docs returned, or skill files read "
                                         "(e.g. references/python/patterns.md)")

    model_config = {"extra": "forbid"}


class DesignDraft(BaseModel):
    summary: str = Field(description="At most 80 words of plain-language prose: how the process runs on Temporal")
    elements: list[DesignElement] = Field(description="Workflows, activities, Schedules (one per scheduled variant in "
                                                      "scope), signals, queries, updates, timers")
    decisions: list[Decision] = Field(description="2-6 key decisions (boundaries, timeouts and retries, idempotency, "
                                                  "workflow ids, heartbeats, Schedules), each backed by the docs or the skill")
    removes: list[str] = Field(description="Hand-built reliability code the design removes, each 'path: what', at most 15 words")
    stays: list[str] = Field(description="What stays outside Temporal and why, each at most 15 words")
    why: str = Field(description="At most 60 words: what goes wrong today and how the design addresses it")
    gains: list[str] = Field(description="Up to 3 concrete benefits for the team, each at most 12 words")
    questions: list[str] = Field(description="Requirements the code, the skill and the docs could not settle, for the "
                                             "solutions architect to confirm (0-3), each at most 25 words")
    change_note: str = Field(description="When revising: at most 40 words on what changed and why; empty otherwise")

    model_config = {"extra": "forbid"}


def design_summary(design: dict) -> str:
    counts: dict[str, int] = {}
    for el in design.get("elements", []):
        counts[el["kind"]] = counts.get(el["kind"], 0) + 1
    order = [("workflow", "workflow"), ("child_workflow", "child workflow"), ("activity", "activity"),
             ("schedule", "Schedule"), ("signal", "signal"), ("query", "query"), ("update", "update"),
             ("timer", "timer")]
    parts = []
    for kind, word in order:
        n = counts.get(kind)
        if n:
            plural = "activities" if word == "activity" else (word + "s" if n > 1 else word)
            parts.append(f"{n} {plural if n > 1 else word}")
    return ", ".join(parts) or "No design"


DESIGN_SYSTEM = """You are a Temporal solutions architect designing a proof of concept (POC) for one process of a customer's Python repository. A coding agent implements your design next, with the Temporal Python SDK, reusing the original business logic in activities, with every external system stubbed from synthetic test data.

Ground the design in Temporal's temporal-developer skill and the Temporal documentation:
- Read the skill with `read_temporal_skill` before you design: at least references/python/python.md, references/core/patterns.md (or references/python/patterns.md) and references/python/gotchas.md, plus the references for what you use (determinism, error-handling, testing, versioning).
- Use `search_temporal_docs` for requirements the skill does not settle (Schedules and their specs, retry policy and timeout semantics, heartbeats, workflow id policies, payload limits, Cloud specifics). Make at least two searches. If it reports that it is unavailable, design from the skill and add a question saying the docs could not be checked.
- Cite in each decision the docs URLs the search returned or the skill files you read. Cite nothing else.

Start from Code Atlas's design (from its Temporal report), which is already checked against the code; keep what is sound and correct what the skill or the docs say is wrong. Activities wrap each external side effect, fine-grained and safe to retry (an idempotency key for irreversible effects); workflow code stays deterministic; every activity has a start-to-close timeout and a retry policy; long activities heartbeat; scheduled variants become Temporal Schedules. Keep it as small as the POC needs. Plain language in summary, why and gains.

The skill's overview follows.

"""


def _design_prompt(run: dict, process: dict, plan: dict, request: str = "", previous: dict | None = None) -> str:
    scope = run.get("scope") or {}
    variants = [v for v in scope.get("variants", []) if v.get("included")]
    out_steps = [st for st in scope.get("steps") or [] if not st.get("included", True)]
    risks = [r for r in scope.get("risks") or [] if r.get("included")]
    report_design = process.get("design") or {}
    criteria = [c for c in run.get("criteria", []) if c.get("on")]
    fixes = [c for c in plan.get("changes", []) if c.get("decision") != "decline"]
    design_files = [f for e in report_design.get("elements", []) for f in e.get("files", [])]
    files = list(dict.fromkeys([*(process.get("card") or {}).get("files", []), *design_files]))
    systems = [x for x in scope.get("systems") or [] if not x.get("processes") or process.get("key") in x["processes"]]
    parts = [
        _card_brief({**process, "key": process.get("key", "p")}),
        "VARIANTS IN SCOPE\n" + ("\n".join(f"- {v['name']}: {v['trigger']}" for v in variants) or "- (one way)"),
        "EXTERNAL SYSTEMS\n" + ("\n".join(f"- {x['name']} ({x['kind']}) via {x['via']}" for x in systems) or "- (none named)"),
    ]
    if out_steps:
        parts.append("STEPS OUT OF SCOPE (they stay as in the original)\n"
                     + "\n".join(f"- {st['name']}: {st['does']}" for st in out_steps))
    if risks:
        parts.append("FAILURES THE POC MUST ADDRESS\n" + "\n".join(f"- {r['text']}" for r in risks))
    if fixes:
        parts.append("INTENTIONAL CHANGES (the Temporal version makes them)\n"
                     + "\n".join(f"- {c['title']}: {c['detail']}" for c in fixes))
    if scope.get("notes"):
        parts.append(f"SCOPE NOTES FROM THE SOLUTIONS ARCHITECT (follow them)\n{scope['notes']}")
    parts.append("SUCCESS CRITERIA\n" + "\n".join(f"- [{c['priority']}] {c['text']}" for c in criteria))
    parts.append("CODE ATLAS'S DESIGN (from the Temporal report)\n" + json.dumps(
        {k: report_design.get(k) for k in ("elements", "removes", "stays", "why", "gains")}, indent=1, default=str))
    if previous:
        parts.append("THE CURRENT DESIGN (revise it)\n" + json.dumps(
            {k: previous.get(k) for k in ("summary_text", "elements", "decisions", "removes", "stays", "why", "gains",
                                          "questions")}, indent=1, default=str))
    if request:
        parts.append(f"CHANGES THE SOLUTIONS ARCHITECT ASKED FOR (apply them; if one conflicts with Temporal practice or "
                     f"the code, apply the closest sound version and say so in change_note)\n{request}")
    parts.append(f"SOURCE\n{source_bundle(run, files, budget=90_000)}")
    parts.append("Read the skill, search the docs, then return the design." if not request else
                 "Read what you need from the skill and the docs for these changes, then return the revised design.")
    return "\n\n".join(parts)


def design_poc(llm, run: dict, process: dict, plan: dict, request: str = "", previous: dict | None = None,
               research=None) -> tuple[dict, dict]:
    """(the design, the research record). The model reads the skill and searches the docs as tools."""
    from temporalize.research import Research, skill_files, skill_overview

    research = research or Research()
    system = DESIGN_SYSTEM + skill_overview()
    d = llm.structured_with_tools(system, _design_prompt(run, process, plan, request, previous), DesignDraft,
                                  research.tools, research.handle, max_tool_calls=16, max_tokens=32000)
    allowed = set(research.urls) | set(research.skill_read) | set(skill_files())
    elements = [{"kind": e.kind, "name": e.name, "does": e.does,
                 "files": [f for f in e.files if _file(run, f) is not None]} for e in d.elements]
    design = {
        "summary_text": d.summary, "elements": elements,
        "decisions": [{"decision": x.decision, "reason": x.reason, "cites": [c for c in x.cites if c in allowed]}
                      for x in d.decisions],
        "removes": d.removes, "stays": d.stays, "why": d.why, "gains": d.gains[:3], "questions": d.questions[:3],
        "change_note": d.change_note if request else "",
    }
    record = research.record()
    if record["docs_available"] and not record["docs_queries"]:
        design["questions"].append("The design was not checked against the Temporal docs (no search was made).")
    if not record["docs_available"]:
        design["questions"].append(f"The Temporal Docs search was unavailable ({record['docs_error']}); the design "
                                   "rests on the skill alone.")
    design["summary"] = design_summary(design)
    return design, record


def draft_plan(llm, run: dict, process: dict, research=None) -> dict:
    scope = run.get("scope") or {}
    variants = [v for v in scope.get("variants", []) if v.get("included")]
    card = process.get("card") or {}
    steps, risks = scope.get("steps") or [], scope.get("risks") or []
    out_steps = [st for st in steps if not st.get("included", True)]
    claims = plan_claims(process, {st["id"] for st in out_steps})
    design_files = [f for e in (process.get("design") or {}).get("elements", []) for f in e.get("files", [])]
    files = list(dict.fromkeys([*card.get("files", []), *design_files]))
    criteria = [c for c in run.get("criteria", []) if c.get("on")]
    brief = run.get("brief") or {}
    systems = ", ".join(x["name"] for x in scope.get("systems") or []) or \
        ", ".join(i["name"] for i in (run.get("scan") or {}).get("integrations", []))
    prompt = (
        f"PROCESS: {process['name']} (entry {process.get('entry')})\n\n"
        "WHAT THE CODE DOES TODAY (checked facts)\n" + "\n".join(f"- {c['text']} [{', '.join(c['refs'])}]" for c in claims)
        + "\n\nVARIANTS IN SCOPE\n" + ("\n".join(f"- {v['name']}: {v['trigger']}" for v in variants) or "- (one way)")
        + ("\n\nSTEPS OUT OF SCOPE (they stay as in the original; do not plan changes or test data for them)\n"
           + "\n".join(f"- {st['name']}: {st['does']}" for st in out_steps) if out_steps else "")
        + ("\n\nFAILURES THE POC MUST ADDRESS\n" + "\n".join(f"- {r['text']}" for r in risks if r.get("included"))
           if any(r.get("included") for r in risks) else "")
        + (f"\n\nSCOPE NOTES FROM THE SOLUTIONS ARCHITECT (follow them)\n{scope['notes']}" if scope.get("notes") else "")
        + "\n\nFOCUS: " + (", ".join(brief.get("focus") or []) or "(none)")
        + "\n\nSUCCESS CRITERIA\n" + "\n".join(f"- [{c['priority']}] {c['text']}" for c in criteria)
        + f"\n\nEXTERNAL SYSTEMS: {systems or '(none named)'}"
        + f"\n\nNOTES FROM THE OPPORTUNITY OR CALLS: {brief.get('notes') or '(none)'}"
        + f"\n\nSOURCE\n{source_bundle(run, files)}\n\n"
        "Write the plain-language summaries, check the notes against the code, then propose: intentional changes "
        "(only clear defects visible in this source), the frozen time, injected failures that exercise the retry and "
        "alert criteria, and the synthetic test data files every boundary needs for one full run of the variants in "
        "scope."
    )
    d = llm.structured(PLAN_SYSTEM, prompt, PlanDraft, max_tokens=32000)
    plan = {
        "status": "ready",
        "overview": d.overview, "today_summary": d.today_summary, "changes_summary": d.changes_summary,
        "approve_summary": d.approve_summary,
        "contradictions": [{"note": c.note, "code": c.code, "refs": valid_refs(run, c.refs)} for c in d.contradictions],
        "claims": claims,
        "changes": [{"id": f"x{i}", "title": c.title, "detail": c.detail, "refs": valid_refs(run, c.refs),
                     "decision": "approve"} for i, c in enumerate(d.changes, start=1)],   # included unless skipped
        "testdata": {"summary": d.summary, "frozen_time": d.frozen_time, "included": True,
                     "failures": [f.model_dump() for f in d.failures],
                     "files": [{"name": _safe_name(f.name), "description": f.description, "content": f.content}
                               for f in d.files]},
        "approved": {"claims": False, "changes": not d.changes, "testdata": False, "design": False},
    }
    plan["design"], plan["research"] = design_poc(llm, run, process, plan, research=research)
    plan["design_edits"] = []
    return plan


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name.strip().split("/")[-1]) or "data.json"
