"""
Screens 5 and 6: from the Temporal report to a scoped, approvable plan.

    analyze_scope   one model call: which process matches the brief, the ways the code
                    runs it (variants, read from the code rather than asked), what a crash
                    loses for each process, and anything in the pasted notes the code
                    contradicts
    draft_plan      the behavior claims come straight from the report's process card
                    (facts already checked against the source; confidence from those
                    checks); one model call proposes intentional changes and the test
                    data; the design is the report's, plus a Schedule per included variant

The model sees source text, the report and the brief. Line references it returns are
kept only when they point at a file of the repository.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

SOURCE_DIR = Path(os.environ.get("SOURCE_DIR", "/data/source"))
REF = re.compile(r"^(?P<path>[^@]+)@L(?P<start>\d+)(?:-(?P<end>\d+))?$")
MAX_FILE_CHARS = 40_000
MAX_SOURCE_CHARS = 220_000

SCOPE_SYSTEM = """You help a Temporal solutions architect scope a proof of concept (POC) from a code assessment.
You get the processes a code report found in a repository (each with a verdict, a fit score and a process card of
facts checked against the source), the user's brief, how the local scan says the code runs today, and source excerpts.

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


class ScopeAnalysis(BaseModel):
    matched: str = Field(description="Key of the process the brief names; empty when the brief names none or none matches")
    match_reason: str = Field(description="At most 20 words")
    processes: list[ProcessNote]
    variants: list[Variant] = Field(description="Every distinct way the code runs the matched process (or, without a "
                                                "match, the best-scoring suitable one): schedules, modes, entry points")
    contradictions: list[Contradiction] = Field(description="Only clear contradictions between the notes and the code")

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


def analyze_scope(llm, run: dict, report: dict) -> dict:
    """The scope object for screen 5 (processes, the matched one, variants, contradictions)."""
    procs = report_processes(report)
    brief = run.get("brief") or {}
    if not procs:
        return {"processes": [], "selected": None, "variants": [], "contradictions": [], "in_scope": [],
                "confirmed": False, "borderline_confirmed": False}
    source = source_bundle(run, _runs_today_sources(run) + _trigger_paths(procs), budget=120_000)
    runs_today = ", ".join(i["name"] for i in (run.get("scan") or {}).get("runs_today", [])) or "nothing detected"
    prompt = (
        f"BRIEF\nmode: {'the user named a process' if brief.get('mode') != 'discover' else 'find opportunities'}\n"
        f"process: {brief.get('process') or '(none)'}\nfocus: {', '.join(brief.get('focus') or []) or '(none)'}\n"
        f"notes: {brief.get('notes') or '(none)'}\n\n"
        f"HOW IT RUNS TODAY (local scan): {runs_today}\n\n"
        "PROCESSES\n" + "\n\n".join(_card_brief(p) for p in procs) + "\n\n"
        f"SOURCE EXCERPTS\n{source}\n\n"
        "Return: the matched process (only if the brief names one), a note for every process, the variants of the "
        "matched process (or of the best suitable process when none matches), and contradictions between the notes "
        "and the code (none when there are no notes)."
    )
    a = llm.structured(SCOPE_SYSTEM, prompt, ScopeAnalysis, max_tokens=16000)
    keys = {p["key"] for p in procs}
    notes = {n.key: n for n in a.processes if n.key in keys}
    matched = a.matched if a.matched in keys else ""
    if not matched:
        suitable = [p for p in procs if p.get("suitable")]
        selected = (suitable or procs)[0]["key"]
    else:
        selected = matched
    processes = []
    for p in procs:
        card = p.get("card") or {}
        losses = [c.get("loses", "") for c in card.get("crash_loss") or []]
        loss = next((x for x in losses if x and not x.lower().startswith(("not established", "nothing"))), "")
        n = notes.get(p["key"])
        processes.append({
            "key": p["key"], "name": p["name"], "overall": p.get("overall"), "verdict_class": _verdict_class(p),
            "suitable": bool(p.get("suitable")), "gated": _verdict_class(p) == "not suitable",
            "gate_capped": bool(p.get("gate_capped")),
            "summary": n.summary if n else loss, "crash_loss": loss, "also_found": n.also_found if n else "",
            "matches_brief": p["key"] == matched, "match_reason": a.match_reason if p["key"] == matched else "",
            "entry": p.get("entry"), "files": card.get("files", []),
            "reasons": [r.get("text", "") for r in p.get("reasons", [])] + list(p.get("would_change") or []),
            "verdict": p.get("verdict", ""),
            **scope_items(p, n.also_found if n else ""),
        })
    processes.sort(key=lambda x: (x["key"] != selected, -(x["overall"] or 0)))
    variants = [{"id": f"v{i}", "name": v.name, "trigger": v.trigger, "refs": valid_refs(run, v.refs), "included": True}
                for i, v in enumerate(a.variants, start=1)]
    entry = next((p for p in processes if p["key"] == selected), {}).get("entry") or ""
    in_scope = [{"id": f"scope-{v['id']}", "label": f"{_folder(entry)} {v['name']}".strip()} for v in variants] or \
               [{"id": "scope-entry", "label": entry or "The selected process"}]
    return {
        "processes": processes, "selected": selected, "variants": variants,
        "contradictions": [{"note": c.note, "code": c.code, "refs": valid_refs(run, c.refs)} for c in a.contradictions],
        "in_scope": in_scope, "notes": "", "confirmed": False, "borderline_confirmed": False,
    }


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


class ScheduleElement(BaseModel):
    variant: str = Field(description="The variant name exactly as given")
    name: str = Field(description="Schedule id, e.g. timesheet-audit-weekday-mornings")
    spec: str = Field(description="Calendar or interval spec in words, e.g. 'Mon-Fri 07:00 America/Los_Angeles'")

    model_config = {"extra": "forbid"}


class PlanDraft(BaseModel):
    changes: list[Change] = Field(description="Clear defects the POC should fix on purpose (0-3); empty when none is clear")
    summary: str = Field(description="Test data in at most 8 words, e.g. 'Fake shifts, frozen Monday, 2 failed logins'")
    frozen_time: str = Field(description="ISO 8601 local date-time both versions treat as now, chosen to exercise "
                                         "date logic (for example a Monday morning)")
    failures: list[Failure] = Field(description="1-3 injected failures at boundaries the code calls")
    files: list[DataFile] = Field(description="2-6 files: what every boundary returns, enough for one full run")
    schedules: list[ScheduleElement] = Field(description="One per included variant that runs on a timer; empty "
                                                         "when the process is not scheduled")

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


def draft_plan(llm, run: dict, process: dict) -> dict:
    scope = run.get("scope") or {}
    variants = [v for v in scope.get("variants", []) if v.get("included")]
    card = process.get("card") or {}
    steps, risks = scope.get("steps") or [], scope.get("risks") or []
    out_steps = [st for st in steps if not st.get("included", True)]
    claims = plan_claims(process, {st["id"] for st in out_steps})
    design_files = [f for e in (process.get("design") or {}).get("elements", []) for f in e.get("files", [])]
    files = list(dict.fromkeys([*card.get("files", []), *design_files]))
    criteria = [c for c in run.get("criteria", []) if c.get("on")]
    prompt = (
        f"PROCESS: {process['name']} (entry {process.get('entry')})\n\n"
        "WHAT THE CODE DOES TODAY (checked facts)\n" + "\n".join(f"- {c['text']} [{', '.join(c['refs'])}]" for c in claims)
        + "\n\nVARIANTS IN SCOPE\n" + ("\n".join(f"- {v['name']}: {v['trigger']}" for v in variants) or "- (one way)")
        + ("\n\nSTEPS OUT OF SCOPE (they stay as in the original; do not plan changes or test data for them)\n"
           + "\n".join(f"- {st['name']}: {st['does']}" for st in out_steps) if out_steps else "")
        + ("\n\nFAILURES THE POC MUST ADDRESS\n" + "\n".join(f"- {r['text']}" for r in risks if r.get("included"))
           if any(r.get("included") for r in risks) else "")
        + (f"\n\nSCOPE NOTES FROM THE SOLUTIONS ARCHITECT (follow them)\n{scope['notes']}" if scope.get("notes") else "")
        + "\n\nSUCCESS CRITERIA\n" + "\n".join(f"- [{c['priority']}] {c['text']}" for c in criteria)
        + f"\n\nINTEGRATIONS FOUND: {', '.join(i['name'] for i in (run.get('scan') or {}).get('integrations', []))}"
        + f"\n\nBRIEF NOTES: {(run.get('brief') or {}).get('notes') or '(none)'}"
        + f"\n\nSOURCE\n{source_bundle(run, files)}\n\n"
        "Propose: intentional changes (only clear defects visible in this source), the frozen time, injected failures "
        "that exercise the retry and alert criteria, the synthetic test data files every boundary needs for one full "
        "run of the variants in scope, and a Schedule per scheduled variant."
    )
    d = llm.structured(PLAN_SYSTEM, prompt, PlanDraft, max_tokens=32000)
    design_src = process.get("design") or {}
    elements = [{"kind": e["kind"], "name": e["name"], "does": e["does"], "files": e.get("files", [])}
                for e in design_src.get("elements", [])
                if not (d.schedules and e["kind"] == "schedule")]   # one Schedule per variant replaces the report's
    elements += [{"kind": "schedule", "name": s.name, "does": f"{s.variant}: {s.spec}", "files": []} for s in d.schedules]
    stays = list(design_src.get("stays", [])) + [f"{st['name']}: out of scope for this POC, stays as in the original"
                                                 for st in out_steps]
    design = {"elements": elements, "removes": design_src.get("removes", []), "stays": stays,
              "why": design_src.get("why", ""), "gains": [g.get("text", "") for g in design_src.get("gains", [])]}
    design["summary"] = design_summary(design)
    return {
        "status": "ready",
        "claims": claims,
        "changes": [{"id": f"x{i}", "title": c.title, "detail": c.detail, "refs": valid_refs(run, c.refs),
                     "decision": "approve"} for i, c in enumerate(d.changes, start=1)],   # included unless skipped
        "testdata": {"summary": d.summary, "frozen_time": d.frozen_time,
                     "failures": [f.model_dump() for f in d.failures],
                     "files": [{"name": _safe_name(f.name), "description": f.description, "content": f.content}
                               for f in d.files]},
        "design": design,
        "approved": {"claims": False, "changes": not d.changes, "testdata": False, "design": False},
    }


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name.strip().split("/")[-1]) or "data.json"
