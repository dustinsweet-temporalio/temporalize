"""
The review packet's write-up (screen 8 and packet.html in the export).

    narrative()     one model call at the end of the run: the title and summary, the use case,
                    before and after, and the POC Standard checks judged from the evidence (the
                    static checks, the review passes, the scenarios and the diff). Best effort:
                    without it, a plain write-up from the run document is used.
    packet_view()   everything the packet shows, from the run document and the narrative:
                    run facts, scorecard, side by side, what changed, test data, limits, next steps
    packet_html()   the packet as one self-contained HTML page (printable)

Counts, criteria results and comparisons come from what ran, never from the model.
"""
from __future__ import annotations

import csv
import html
import io
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger("code_atlas.temporalize.packet")

POC_STANDARD = [
    ("boundaries", "Workflow and activity boundaries"),
    ("determinism", "Determinism in workflow code"),
    ("retry_safe", "Activities safe to retry"),
    ("timeouts", "Timeouts and retry policies"),
    ("heartbeats", "Heartbeats on long activities"),
    ("schedules", "Schedules instead of cron"),
    ("ids", "Workflow IDs and reuse policy"),
    ("payloads", "Large data passed by reference"),
    ("tests", "Failure, timer and replay tests"),
    ("external", "External systems behind activities"),
    ("config", "Configuration from the environment"),
    ("versioning", "Versioning approach stated"),
    ("cancellation", "Cancellation"),
    ("messages", "Signals, Updates, Queries"),
    ("compensation", "Compensation"),
    ("continue_as_new", "Continue-as-new"),
]
STANDARD_VERSION = "draft v0"
SAMPLE_ROWS = 6
SAMPLE_COLS = 6


class UseCaseRow(BaseModel):
    value: str = Field(description="At most 20 words")
    source: Literal["brief", "code", "brief, corrected by code"] = Field(
        description="Where it comes from: the brief and notes, the code, or the brief corrected by what the code shows")

    model_config = {"extra": "forbid"}


class BeforeAfter(BaseModel):
    before: str = Field(description="At most 12 words: how the original code does it")
    after: str = Field(description="At most 14 words: how the Temporal version does it")

    model_config = {"extra": "forbid"}


class StandardCheck(BaseModel):
    id: str = Field(description="The check id exactly as given")
    status: Literal["passed", "open", "not_applicable"] = Field(
        description="passed only if the evidence shows it; open when it applies but is not shown or is incomplete; "
                    "not_applicable when this process has no use for it")
    note: str = Field(description="At most 15 words: the evidence, or what is missing")

    model_config = {"extra": "forbid"}


class PacketDraft(BaseModel):
    title: str = Field(description="At most 7 words, e.g. 'Timesheet audit on Temporal'")
    lede: str = Field(description="At most 55 words of plain prose: what now runs on Temporal and what this run "
                                  "showed (only what the results below show)")
    problem: str = Field(description="One sentence, at most 22 words: the problem in the customer's terms, e.g. 'The "
                                     "morning timesheet audit fails when the ClockShark login is flaky, and nobody finds out.'")
    process: UseCaseRow
    runs: UseCaseRow
    volume: UseCaseRow
    systems: UseCaseRow
    when_it_fails: UseCaseRow
    constraints: UseCaseRow
    before_after: list[BeforeAfter] = Field(description="4-8 rows, the most telling first")
    standard: list[StandardCheck] = Field(description="Every check given, once")
    not_proven: list[str] = Field(description="1-4 things this run did not show beyond the fixed ones, each at most 30 words")
    validate_next: str = Field(description="At most 35 words: how to validate against the customer's real systems next")

    model_config = {"extra": "forbid"}


PACKET_SYSTEM = """You write the summary parts of a review packet for a Temporal proof of concept (POC), for a sales team and the customer's engineers. You get the approved plan, the success criteria results, the side-by-side comparison, the static checks, the code review passes, the scenarios that ran and the diff stats. State only what this evidence shows; the tables of results are produced separately from the same evidence, so never contradict them and never claim more. Plain, concrete language; no marketing words."""


def _bullet(rows: list[str]) -> str:
    return "\n".join(f"- {r}" for r in rows) or "- (none)"


def _evidence(run: dict, results: dict) -> str:
    plan, gen, scope = run.get("plan") or {}, run.get("generate") or {}, run.get("scope") or {}
    proc = next((p for p in scope.get("processes") or [] if p.get("key") == scope.get("selected")), {})
    design = plan.get("design") or {}
    checks = (gen.get("checks") or {}).get("items") or []
    findings = [f"pass {p.get('n')}: [{f.get('severity')}] {f.get('title')}" for p in gen.get("review_passes") or []
                for f in p.get("findings") or []]
    brief = run.get("brief") or {}
    return "\n\n".join([
        f"PROCESS: {proc.get('name', '')} (entry {proc.get('entry', '')}); verdict {proc.get('verdict_class', '')}",
        f"WHAT A CRASH LOSES TODAY: {proc.get('crash_loss', '')}",
        "VARIANTS IN SCOPE\n" + _bullet([f"{v['name']}: {v.get('trigger', '')}" for v in scope.get("variants") or [] if v.get("included")]),
        "SYSTEMS\n" + _bullet([f"{x['name']} via {x.get('via', '')}" for x in scope.get("systems") or []
                               if not x.get("processes") or proc.get("key") in x["processes"]]),
        f"BRIEF: focus {', '.join(brief.get('focus') or []) or '(none)'}; notes: {brief.get('notes') or '(none)'}; "
        f"scope notes: {scope.get('notes') or '(none)'}",
        f"PLAN OVERVIEW: {plan.get('overview', '')}",
        "BEHAVIORS KEPT\n" + _bullet([c.get("edited_text") or c["text"] for c in plan.get("claims") or [] if c.get("status") != "rejected"]),
        "APPROVED INTENTIONAL CHANGES\n" + _bullet([f"{c['title']}: {c['detail']}" for c in plan.get("changes") or []
                                                  if c.get("decision") != "decline"]),
        "DESIGN\n" + json.dumps({k: design.get(k) for k in ("summary_text", "elements", "decisions", "removes", "stays")},
                               indent=1, default=str)[:12000],
        "TEST DATA: " + json.dumps({k: (plan.get("testdata") or {}).get(k) for k in ("summary", "frozen_time", "failures")}, default=str),
        "CRITERIA RESULTS\n" + _bullet([f"[{c['priority']}] {c['text']}: {'passed' if c.get('passed') else 'not passed'} "
                                       f"({c.get('evidence', '')})" for c in results.get("criteria") or []]),
        "SIDE BY SIDE\n" + _bullet([f"{r['check']}: original {r['original']} / temporal {r['temporal']} -> {r['result']}"
                                   for r in results.get("comparison") or []]),
        "STATIC CHECKS\n" + _bullet([f"{i.get('label')}: {'ok' if i.get('ok') else 'failed'}" for i in checks]),
        "SCENARIOS\n" + _bullet([f"{k}: {'ok' if v.get('ok') else 'failed'}; {v.get('notes', '')[:300]}"
                                for k, v in (results.get("scenarios") or {}).items()]),
        "REVIEW FINDINGS (all passes)\n" + _bullet(findings[:40]),
        "LEFT OVER AFTER REVIEW\n" + _bullet([f"[{f.get('severity')}] {f.get('title')}" for f in results.get("leftover_findings") or []]),
        f"DIFF: {json.dumps(results.get('diff_stats') or {})}; {results.get('changes_summary', '')}",
        f"GENERATOR SUMMARY: {(gen.get('summary') or '')[:3000]}",
    ])


def narrative(llm, run: dict, results: dict) -> dict:
    """The packet's prose and the POC Standard, from one model call."""
    checks = "\n".join(f"{cid} | {label}" for cid, label in POC_STANDARD)
    prompt = (_evidence(run, results) + f"\n\nPOC STANDARD ({STANDARD_VERSION}) CHECKS (id | check)\n{checks}\n\n"
              "Write the packet's summary parts and judge every POC Standard check from the evidence.")
    d = llm.structured(PACKET_SYSTEM, prompt, PacketDraft, max_tokens=16000)
    known = dict(POC_STANDARD)
    judged = {c.id: c for c in d.standard if c.id in known}
    standard = [{"id": cid, "check": label,
                 "status": judged[cid].status if cid in judged else "open",
                 "note": judged[cid].note if cid in judged else "Not judged"} for cid, label in POC_STANDARD]
    return {
        "title": d.title, "lede": d.lede, "problem": d.problem,
        "use_case": [{"label": label, **getattr(d, key).model_dump()} for key, label in
                     (("process", "Process"), ("runs", "Runs"), ("volume", "Volume"), ("systems", "Systems"),
                      ("when_it_fails", "When it fails"), ("constraints", "Constraints"))],
        "before_after": [x.model_dump() for x in d.before_after[:8]],
        "standard": standard, "standard_version": STANDARD_VERSION,
        "not_proven": d.not_proven[:4], "validate_next": d.validate_next, "source": "model",
    }


def fallback_narrative(run: dict, results: dict) -> dict:
    """A plain write-up when the narrative call is not available."""
    scope = run.get("scope") or {}
    proc = next((p for p in scope.get("processes") or [] if p.get("key") == scope.get("selected")), {})
    name = proc.get("name") or "The process"
    outcomes = [o["label"] for o in results.get("outcomes") or [] if o.get("ok")]
    systems = ", ".join(x["name"] for x in scope.get("systems") or [] if not x.get("processes") or proc.get("key") in x["processes"])
    return {
        "title": f"{name} on Temporal", "lede": f"{name} runs as a Temporal workflow in this POC. "
        + (f"This run showed: {', '.join(outcomes).lower()}." if outcomes else "See the results below."),
        "problem": proc.get("crash_loss") or proc.get("summary") or "",
        "use_case": [{"label": "Process", "value": name, "source": "code"},
                     {"label": "Systems", "value": systems or "(none named)", "source": "code"},
                     {"label": "When it fails", "value": proc.get("crash_loss") or "", "source": "code"}],
        "before_after": [], "standard": [{"id": cid, "check": label, "status": "open", "note": "Not judged"}
                                         for cid, label in POC_STANDARD],
        "standard_version": STANDARD_VERSION, "not_proven": [], "validate_next": "", "source": "fallback",
    }


# ----------------------------------------------------------------------
# What the packet shows (screen 8 renders the same object)
# ----------------------------------------------------------------------

def _rows_from(content: str, name: str) -> tuple[list[str], list[list[str]], int]:
    """Columns, the first rows, and the row count of a JSON list of objects or a CSV file."""
    try:
        if name.lower().endswith(".csv"):
            rows = list(csv.DictReader(io.StringIO(content)))
        else:
            data = json.loads(content)
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [])
            rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    except (ValueError, csv.Error):
        return [], [], 0
    if not rows:
        return [], [], 0
    cols = [c for c in rows[0].keys()][:SAMPLE_COLS]
    sample = [[str(r.get(c, ""))[:80] for c in cols] for r in rows[:SAMPLE_ROWS]]
    return cols, sample, len(rows)


def test_data_view(run: dict) -> dict:
    plan = run.get("plan") or {}
    td = plan.get("testdata") or {}
    files = td.get("files") or []
    sample = None
    for f in files:
        cols, rows, n = _rows_from(f.get("content") or "", f.get("name") or "")
        if rows:
            sample = {"file": f["name"], "columns": cols, "rows": rows, "total": n}
            break
    scope = run.get("scope") or {}
    proc_key = scope.get("selected")
    scenario = []
    if td.get("frozen_time"):
        scenario.append({"label": "Date", "value": f"Frozen to {td['frozen_time']}"})
    for x in scope.get("systems") or []:
        if x.get("processes") and proc_key not in x["processes"]:
            continue
        fail = next((f for f in td.get("failures") or [] if f.get("boundary", "").lower() in x["name"].lower()
                     or x["name"].lower().split()[0] in f.get("boundary", "").lower()), None)
        value = "Served from the test data, never contacted"
        if fail:
            value += f"; fails {fail.get('times', 1)} time{'' if fail.get('times', 1) == 1 else 's'} first"
        scenario.append({"label": x["name"], "value": value})
    for f in td.get("failures") or []:
        if not any(f.get("boundary", "") and f["boundary"] in s["value"] for s in scenario):
            scenario.append({"label": f"Failure: {f.get('boundary')}", "value": f"Fails {f.get('times', 1)} time(s): {f.get('detail', '')}"})
    scenario.append({"label": "Worker", "value": "Stopped mid-run and restarted (resilience scenario)"})
    return {"summary": td.get("summary", ""), "files": [{"name": f["name"], "description": f.get("description", "")} for f in files],
            "sample": sample, "scenario": scenario}


def _new_tests(changed: list[dict]) -> int:
    return sum(1 for r in changed if r.get("status") == "A" and (r["path"].rsplit("/", 1)[-1].startswith("test_")
                                                               or "/tests/" in r["path"]))


def packet_view(run: dict) -> dict:
    """The review packet's content: run facts, the narrative, and the evidence tables."""
    from temporalize.generate.workspace import POC_DIR
    from temporalize.llm import MODELS
    from temporalize.settings import temporal_target

    res = run.get("results") or {}
    gen = run.get("generate") or {}
    plan = run.get("plan") or {}
    scope = run.get("scope") or {}
    settings = run.get("settings") or {}       # the run's snapshot: model and Temporal target, no secrets needed
    target = temporal_target(settings)
    nar = res.get("narrative") or fallback_narrative(run, res)
    stats = res.get("diff_stats") or {}
    changed = res.get("changed_files") or []
    entry_points = sum(1 for r in changed if r.get("status") == "M" and not r["path"].startswith(f"{POC_DIR}/"))
    model = settings.get("model") or ""
    label = next((m["label"] for m in MODELS if m["id"] == model), model)
    others = [p for p in scope.get("processes") or [] if p.get("key") != scope.get("selected")
              and p.get("verdict_class") in ("suitable", "borderline")][:2]
    owner = ""
    src = (run.get("source") or {}).get("url") or ""
    if src:
        parts = src.rstrip("/").replace(".git", "").split("/")
        owner = parts[-2] if len(parts) >= 2 else ""
    return {
        "for": owner or (run.get("repo") or {}).get("name", ""),
        "narrative": nar,
        "outcomes": res.get("outcomes") or [],
        "facts": [
            ("Run", run.get("_id") or run.get("id")),
            ("Date", (gen.get("finished_at") or run.get("updated_at") or "")[:10]),
            ("Branch", gen.get("branch", "")),
            ("Prepared with", "Temporalize"),
            ("Model", label),
            ("Model calls", str(int(run.get("calls") or 0))),
            ("Review passes", f"{res.get('review_passes', 0)} of 3"),
            ("Ran against", "Stubs, " + ("Temporal Cloud" if target.get("mode") == "cloud" else "local dev server")),
        ],
        "criteria": res.get("criteria") or [],
        "criteria_off": [c for c in run.get("criteria") or [] if not c.get("on")],
        "comparison": res.get("comparison") or [],
        "intended": [c for c in plan.get("changes") or [] if c.get("decision") != "decline"],
        "changed": {"entry_points": entry_points, "removed": stats.get("deletions", 0), "added": stats.get("insertions", 0),
                    "tests": _new_tests(changed), "summary": res.get("changes_summary", ""),
                    "before_after": nar.get("before_after") or [], "files": stats.get("files", 0)},
        "standard": nar.get("standard") or [], "standard_version": nar.get("standard_version", STANDARD_VERSION),
        "test_data": test_data_view(run),
        "not_proven": list(res.get("limitations") or []) + list(nar.get("not_proven") or [])
        + [f"Left after review: [{f.get('severity')}] {f.get('title')}" for f in res.get("leftover_findings") or []],
        "next_steps": ([{"title": "Validate against real systems", "text": nar.get("validate_next")
                         or "Run the same workflow against test accounts of the real systems, with email still going to outbox/.",
                         "verdict": ""}]
                       + [{"title": p["name"], "text": p.get("summary") or p.get("crash_loss") or "",
                           "verdict": p.get("verdict_class", "").title()} for p in others]),
        "run_it": [f"git checkout {gen.get('branch', '')}", f"cd {POC_DIR}",
                   "temporal server start-dev            # Temporal UI at localhost:8233",
                   "python worker.py", "python starter.py   # stub mode by default",
                   "python validate.py side_by_side      # or resilience, replay, smoke"],
        "research": {"design": plan.get("research") or {}, "generate": gen.get("research") or {}},
        "contents": ["packet.html", "README.md", "results.json", "PLAN.md", "diff.patch", "testdata/", "scenarios/",
                     "temporal-poc/"],
    }


# ----------------------------------------------------------------------
# packet.html
# ----------------------------------------------------------------------

CSS = """
:root{--ink:#18212D;--ink2:#4A5666;--ink3:#7A8594;--line:#D5DBE2;--soft:#F5F7F9;--ok:#2F7D54;--warn:#B4661A;--new:#2D5FA8;--bad:#B42318}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);font:15px/1.5 "Public Sans","Helvetica Neue",Arial,sans-serif}
main{max-width:860px;margin:0 auto;padding:40px 24px 60px}h1{font-size:34px;line-height:1.15;margin:4px 0 12px;letter-spacing:-.01em}
.for{color:var(--ink2);font-size:14px;margin:0}.lede{font-size:18px;line-height:1.5;margin:0 0 16px}
.badges{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}.badge{border:1px solid var(--line);border-radius:6px;padding:5px 12px;font-size:14px}
.badge.ok{border-color:var(--ok);color:var(--ok)}.badge.bad{border-color:var(--bad);color:var(--bad)}
.facts{display:grid;grid-template-columns:repeat(4,1fr);gap:12px 16px;border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:0 0 12px}
.facts span{display:block;font-size:12.5px;color:var(--ink3)}.facts b{font-weight:500}
.synthetic{font-size:13.5px;color:var(--ink2);display:flex;gap:12px}.synthetic b{color:var(--ok)}
hr{border:0;border-top:2px solid var(--ink);margin:24px 0}
section{border-top:1px solid var(--line);padding-top:18px;margin-top:22px}section h2{font-size:24px;margin:0 0 4px}
section h2 .n{font-size:14px;color:var(--ink3);font-weight:400;margin-right:10px}.sub{color:var(--ink2);margin:0 0 12px}
blockquote{border-left:3px solid var(--ink);margin:12px 0;padding:4px 14px;font-size:17px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;font-weight:500;color:var(--ink2);font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:8px 8px 8px 0;vertical-align:top}.src{color:var(--ink3);font-size:12.5px;margin-left:6px}
.pill{border:1px solid var(--line);border-radius:4px;padding:0 6px;font-size:12px}.passed,.match{color:var(--ok);font-weight:600}
.open,.intended,.notsel{color:var(--warn);font-weight:600}.new{color:var(--new);font-weight:600}.failed,.mismatch{color:var(--bad);font-weight:600}
.na{color:var(--ink3)}.callout{border:1px solid var(--warn);border-radius:8px;padding:10px 14px;margin-top:12px}.callout b{display:block}
.stats{display:flex;gap:36px;margin:10px 0 14px}.stats b{display:block;font-size:26px}.stats span{color:var(--ink2);font-size:13px}
.std{display:grid;grid-template-columns:1fr 1fr;gap:0 28px}.std div{display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding:8px 0;font-size:14px}
.card{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-top:10px}.card h3{margin:0 0 4px;font-size:16px;display:flex;justify-content:space-between}
pre{background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:12px 14px;font:13px "IBM Plex Mono",Menlo,monospace;white-space:pre-wrap}
.files{columns:2;font:13px "IBM Plex Mono",Menlo,monospace;padding:0;list-style:none}.foot{color:var(--ink3);font-size:12.5px;margin-top:28px}
@media print{main{padding:0}section{break-inside:avoid-page}}@media(max-width:640px){.facts{grid-template-columns:1fr 1fr}.std{grid-template-columns:1fr}}
"""

RESULT_LABEL = {"match": "Match", "intended": "Intended fix", "new": "New behavior", "mismatch": "Mismatch"}
STATUS_LABEL = {"passed": ("Passed", "passed"), "open": ("Open", "open"), "not_applicable": ("Not applicable", "na")}


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def packet_html(run: dict) -> str:
    v = packet_view(run)
    n = v["narrative"]
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{_e(n.get('title'))}</title><style>{CSS}</style></head><body><main>",
           f"<p class='for'>Temporalize review packet{(' for ' + _e(v['for'])) if v['for'] else ''}</p>",
           f"<h1>{_e(n.get('title'))}</h1><p class='lede'>{_e(n.get('lede'))}</p><div class='badges'>"]
    for o in v["outcomes"]:
        cls = "ok" if o.get("ok") is True else "bad" if o.get("ok") is False else ""
        out.append(f"<span class='badge {cls}'>{'✓ ' if o.get('ok') else ''}{_e(o.get('label'))}</span>")
    out.append("</div><div class='facts'>" + "".join(f"<div><span>{_e(k)}</span><b>{_e(val)}</b></div>" for k, val in v["facts"])
               + "</div><p class='synthetic'><b>Synthetic data only.</b> No real customer data was used to produce these results.</p><hr>")
    # 1 Use case
    out.append("<section><h2><span class='n'>1</span>The use case</h2><p class='sub'>From the brief confirmed at the start "
               "of the run. Anything the code contradicted is noted.</p>")
    if n.get("problem"):
        out.append(f"<blockquote>{_e(n['problem'])}</blockquote>")
    out.append("<table>" + "".join(f"<tr><td style='width:28%'>{_e(r['label'])}</td><td>{_e(r['value'])}"
                                   f"<span class='src'>{_e(r.get('source', ''))}</span></td></tr>" for r in n.get("use_case") or [])
               + "</table></section>")
    # 2 Scorecard
    out.append("<section><h2><span class='n'>2</span>Scorecard</h2><p class='sub'>The success criteria agreed before any "
               "code was written.</p><table><tr><th>Criterion</th><th>Priority</th><th>Result</th><th>Evidence</th></tr>")
    for c in v["criteria"]:
        out.append(f"<tr><td>{_e(c['text'])}</td><td><span class='pill'>{_e(c['priority'])}</span></td>"
                   f"<td class='{'passed' if c.get('passed') else 'failed'}'>{'Passed' if c.get('passed') else 'Not passed'}</td>"
                   f"<td>{_e(c.get('evidence'))}</td></tr>")
    for c in v["criteria_off"]:
        out.append(f"<tr><td>{_e(c['text'])}</td><td><span class='pill'>{_e(c.get('priority'))}</span></td>"
                   "<td class='na'>Not selected</td><td class='na'>None</td></tr>")
    out.append("</table></section>")
    # 3 Side by side
    out.append("<section><h2><span class='n'>3</span>Side by side</h2><p class='sub'>The original code and the Temporal "
               "version ran on the same test data. The original code is the answer key.</p>"
               "<table><tr><th>Check</th><th>Original</th><th>Temporal</th><th>Result</th></tr>")
    for r in v["comparison"]:
        out.append(f"<tr><td>{_e(r['check'])}</td><td>{_e(r['original'])}</td><td>{_e(r['temporal'])}</td>"
                   f"<td class='{_e(r['result'])}'>{_e(RESULT_LABEL.get(r['result'], r['result']))}</td></tr>")
    out.append("</table>")
    for c in v["intended"]:
        out.append(f"<div class='callout'><b>Intended fix, approved before generation: {_e(c['title'])}</b>{_e(c.get('detail'))}</div>")
    out.append("</section>")
    # 4 What changed
    ch = v["changed"]
    out.append(f"<section><h2><span class='n'>4</span>What changed</h2><p class='sub'>{_e(ch['summary'])}</p><div class='stats'>"
               f"<div><b>{ch['entry_points']}</b><span>original files changed</span></div><div><b>−{ch['removed']:,}</b><span>lines removed</span></div>"
               f"<div><b>+{ch['added']:,}</b><span>lines added</span></div><div><b>{ch['tests']}</b><span>new tests</span></div></div>")
    if ch["before_after"]:
        out.append("<table><tr><th>Before</th><th>After</th></tr>" + "".join(
            f"<tr><td>{_e(r['before'])}</td><td>{_e(r['after'])}</td></tr>" for r in ch["before_after"]) + "</table>")
    out.append("<p class='sub' style='margin-top:10px'>Full diff: diff.patch.</p></section>")
    # 5 POC Standard
    out.append(f"<section><h2><span class='n'>5</span>POC Standard</h2><p class='sub'>Checked against {_e(v['standard_version'])}. "
               "Checks that don’t apply to this process are marked so rather than counted against it.</p><div class='std'>")
    for s in v["standard"]:
        label, cls = STATUS_LABEL.get(s["status"], (s["status"], ""))
        out.append(f"<div title='{_e(s.get('note'))}'><span>{_e(s['check'])}</span><span class='{cls}'>{label}</span></div>")
    out.append("</div></section>")
    # 6 Test data
    td = v["test_data"]
    out.append("<section><h2><span class='n'>6</span>Test data</h2><p class='sub'>Everything both versions ran on. All of it "
               "is made up and lives in temporal-poc/testdata/ on the branch.</p>")
    if td["sample"]:
        smp = td["sample"]
        out.append(f"<p><b>{_e(smp['file'])}, first {len(smp['rows'])} of {smp['total']} rows</b></p><table><tr>"
                   + "".join(f"<th>{_e(c)}</th>" for c in smp["columns"]) + "</tr>"
                   + "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in smp["rows"]) + "</table>")
    out.append("<p style='margin-top:14px'><b>Scenario</b></p><table>" + "".join(
        f"<tr><td style='width:28%'>{_e(r['label'])}</td><td>{_e(r['value'])}</td></tr>" for r in td["scenario"]) + "</table></section>")
    # 7 Limits
    out.append("<section><h2><span class='n'>7</span>Limits and what’s next</h2><p class='sub'>What this run doesn’t show, and "
               "where to go from here.</p><p><b>Not proven by this run</b></p><ul>"
               + "".join(f"<li>{_e(x)}</li>" for x in v["not_proven"]) + "</ul><p><b>Next steps</b></p>")
    for c in v["next_steps"]:
        out.append(f"<div class='card'><h3><span>{_e(c['title'])}</span><span class='passed'>{_e(c.get('verdict'))}</span></h3>{_e(c['text'])}</div>")
    out.append("</section>")
    # 8 Run it
    out.append("<section><h2><span class='n'>8</span>Run it yourself</h2><p class='sub'>Everything runs locally against a "
               f"Temporal dev server, in stub mode by default.</p><pre>{_e(chr(10).join(v['run_it']))}</pre>"
               "<p><b>In this packet</b></p><ul class='files'>" + "".join(f"<li>{_e(x)}</li>" for x in v["contents"]) + "</ul>")
    research = v["research"]["design"]
    if research:
        out.append(f"<p class='foot'>Designed with Temporal’s temporal-developer skill ({_e(', '.join(research.get('skill_read') or []) or 'overview')}) "
                   f"and the Temporal Docs MCP ({len(research.get('docs_queries') or [])} searches"
                   + ("" if research.get("docs_available", True) else ", unavailable") + ").</p>")
    out.append("</section></main></body></html>")
    return "".join(out)
