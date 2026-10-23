"""
Results of a run (screen 8) and the review packet: built from the scenario JSONs, the
review passes and the POC branch's diff against the original commit.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path

from temporalize.generate.workspace import (
    POC_DIR,
    changed_files,
    commit_all,
    diff,
    diff_stats,
    run_id_of,
    workspace_path,
)

logger = logging.getLogger("code_atlas.temporalize.results")

RESULTS = ("match", "intended", "new", "mismatch")
BASE_LIMITATIONS = [
    "Stub mode: every external system (browser, APIs, LLMs, email, SSH) was replaced by stubs, so the customer's integrations were not exercised.",
    "The test data is synthetic, written for this run and approved on screen 6.",
    "Scale, latency and cost are not proven by a local run.",
]


def _rows(scenarios: dict[str, dict]) -> list[dict]:
    rows = []
    for name in ("side_by_side", "resilience"):
        for c in (scenarios.get(name) or {}).get("checks") or []:
            result = c.get("result") if c.get("result") in RESULTS else "mismatch"
            rows.append({"check": str(c.get("check", "")), "original": str(c.get("original", "")),
                         "temporal": str(c.get("temporal", "")), "result": result,
                         "detail": str(c.get("detail", "")), "scenario": name})
    return rows


def criteria_results(criteria: list[dict], scenarios: dict[str, dict]) -> list[dict]:
    """Each selected criterion with what the harness reported for it (all reports must pass)."""
    reported: dict[str, list[dict]] = {}
    for name, result in scenarios.items():
        for c in (result or {}).get("criteria") or []:
            if c.get("id"):
                reported.setdefault(str(c["id"]), []).append({**c, "scenario": name})
    out = []
    for c in criteria:
        if not c.get("on"):
            continue
        entries = reported.get(str(c.get("id")), [])
        if entries:
            passed = all(bool(e.get("passed")) for e in entries)
            evidence = " ".join(f"{e.get('evidence', '').strip()} ({e['scenario']})" for e in entries).strip()
        else:
            passed, evidence = False, "Not checked by the harness"
        out.append({"id": c.get("id"), "text": c.get("text", ""), "priority": c.get("priority", "P1"),
                    "passed": passed, "evidence": evidence})
    return out


def criteria_summary(criteria: list[dict], results: list[dict]) -> str:
    """'4 of 4 P0 passed. 1 P1 not selected.'"""
    parts = []
    for prio in ("P0", "P1"):
        chosen = [r for r in results if r.get("priority") == prio]
        if chosen:
            passed = sum(1 for r in chosen if r.get("passed"))
            parts.append(f"{passed} of {len(chosen)} {prio} passed.")
    for prio in ("P0", "P1"):
        off = sum(1 for c in criteria if not c.get("on") and c.get("priority") == prio)
        if off:
            parts.append(f"{off} {prio} not selected.")
    return " ".join(parts) or "No criteria were selected."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def changes_summary(rows: list[dict]) -> str:
    poc = f"{POC_DIR}/"
    added = [r for r in rows if r["path"].startswith(poc) and r["status"] in ("A", "R")]
    originals_changed = [r for r in rows if not r["path"].startswith(poc) and r["status"] == "M"]
    originals_moved = [r for r in rows if r["status"] == "R" and not r.get("old_path", "").startswith(poc)]
    originals_deleted = [r for r in rows if not r["path"].startswith(poc) and r["status"] == "D"]
    legacy = [r for r in rows if r["path"].startswith(f"{poc}legacy/")]
    parts = []
    if originals_changed:
        parts.append(f"{_plural(len(originals_changed), 'original file')} changed "
                     f"({', '.join(r['path'] for r in originals_changed[:4])}{', …' if len(originals_changed) > 4 else ''}).")
    else:
        parts.append("Original files left unchanged.")
    if originals_moved or originals_deleted or legacy:
        parts.append(f"Originals moved to {POC_DIR}/legacy/.")
    parts.append(f"{_plural(len(added), 'file')} added in {POC_DIR}/.")
    return " ".join(parts)


def leftover_findings(run: dict) -> list[dict]:
    """High and medium findings of the last review pass, when that pass still had some."""
    passes = (run.get("generate") or {}).get("review_passes") or []
    if not passes:
        return []
    last = passes[-1]
    if last.get("resolved"):
        return []
    return [f for f in last.get("findings") or [] if f.get("severity") in ("high", "medium")]


def build_results(run: dict, scenarios: dict[str, dict], settings: dict, llm=None) -> dict:
    from temporalize.settings import temporal_target, workflow_url

    ws = workspace_path(run)
    commit_all(ws, "Temporalize: POC")
    stats = diff_stats(run)
    files = changed_files(run)
    criteria = run.get("criteria") or []
    crit = criteria_results(criteria, scenarios)
    smoke = scenarios.get("smoke") or {}
    resilience = scenarios.get("resilience") or {}
    smoke_id = (smoke.get("workflow_ids") or [None])[0] or f"tz-{run_id_of(run)}-smoke"
    target = temporal_target(settings)

    limitations = list(BASE_LIMITATIONS)
    for name in ("side_by_side", "resilience", "replay", "smoke"):
        s = scenarios.get(name)
        if s is None:
            limitations.append(f"The {name.replace('_', ' ')} scenario did not run.")
        elif not s.get("ok"):
            limitations.append(f"The {name.replace('_', ' ')} scenario did not pass.")
    leftovers = leftover_findings(run)
    if leftovers:
        limitations.append(f"{_plural(len(leftovers), 'review finding')} left after the last review pass (listed below).")

    results = {
        "outcomes": [
            {"id": "runnable", "label": "Runnable evaluation", "ok": bool(smoke.get("ok"))},
            {"id": "resilience", "label": "Resilience demonstrated", "ok": bool(resilience.get("ok"))},
            {"id": "integration", "label": "Customer integration not tested", "ok": None},
        ],
        "comparison": _rows(scenarios),
        "criteria": crit,
        "criteria_summary": criteria_summary(criteria, crit),
        "changes_summary": changes_summary(files),
        "diff_stats": stats,
        "changed_files": files,
        "limitations": limitations,
        "leftover_findings": leftovers,
        "review_passes": len((run.get("generate") or {}).get("review_passes") or []),
        "scenarios": {k: {"ok": bool(v.get("ok")), "notes": (v.get("notes") or "")[:2000],
                          "workflow_ids": v.get("workflow_ids") or []} for k, v in scenarios.items()},
        "smoke_workflow_id": smoke_id,
        "temporal_ui_url": workflow_url(target, smoke_id),
    }
    results["narrative"] = _narrative(llm, run, results)
    return results


def _narrative(llm, run: dict, results: dict) -> dict:
    """The packet's write-up (packet.py); best effort, so the results never fail on it."""
    from temporalize.generate.packet import fallback_narrative, narrative

    if llm is not None:
        for attempt in (1, 2):
            try:
                return narrative(llm, run, results)
            except Exception as e:
                logger.warning(f"Packet narrative, attempt {attempt}, failed: {type(e).__name__}: {str(e)[:200]}")
    return fallback_narrative(run, results)


# ----------------------------------------------------------------------
# Review packet
# ----------------------------------------------------------------------

def _md_table(rows: list[dict]) -> list[str]:
    if not rows:
        return ["(none)"]
    out = ["| Check | Original | Temporal | Result |", "| --- | --- | --- | --- |"]
    for r in rows:
        cells = [str(r.get(k, "")).replace("|", "\\|").replace("\n", " ") for k in ("check", "original", "temporal", "result")]
        out.append("| " + " | ".join(cells) + " |")
    return out


def packet_readme(run: dict) -> str:
    rid = run_id_of(run)
    res = run.get("results") or {}
    gen = run.get("generate") or {}
    repo = run.get("repo") or {}
    sel = (run.get("scope") or {}).get("selected")
    proc = next((p for p in (run.get("scope") or {}).get("processes") or [] if p.get("key") == sel), {})
    lines = [f"# Temporalize review packet: {rid}", "",
             f"Repository: {repo.get('name', '')} at `{(repo.get('commit') or '')[:12]}`  ",
             f"Process: {proc.get('name', sel or '')}  ",
             f"Branch: `{gen.get('branch', '')}`", "", "## Outcomes", ""]
    for o in res.get("outcomes") or []:
        mark = "✓" if o.get("ok") else ("–" if o.get("ok") is None else "✗")
        lines.append(f"- {mark} {o.get('label')}")
    lines += ["", "## Same test data, both versions", ""] + _md_table(res.get("comparison") or [])
    lines += ["", "## Success criteria", "", res.get("criteria_summary", ""), ""]
    for c in res.get("criteria") or []:
        lines.append(f"- {'✓' if c.get('passed') else '✗'} {c.get('priority')} {c.get('text')}: {c.get('evidence')}")
    lines += ["", "## Changes", "", res.get("changes_summary", "")]
    stats = res.get("diff_stats") or {}
    if stats:
        lines.append(f"{stats.get('files', 0)} files, +{stats.get('insertions', 0)} −{stats.get('deletions', 0)} (see diff.patch).")
    lines += ["", "## Review passes", ""]
    for p in gen.get("review_passes") or []:
        lines.append(f"- Pass {p.get('n')}: {len(p.get('findings') or [])} findings, {p.get('fixed', 0)} fixed")
        for f in p.get("findings") or []:
            lines.append(f"  - [{f.get('severity')}] {f.get('title')}: {f.get('detail')}")
    if res.get("leftover_findings"):
        lines += ["", "Left after the last pass:", ""]
        lines += [f"- [{f.get('severity')}] {f.get('title')}: {f.get('detail')}" for f in res["leftover_findings"]]
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in res.get("limitations") or []]
    lines += ["", "## Run the POC", "",
              "```sh",
              f"git checkout {gen.get('branch', 'temporalize/' + rid)}",
              "pip install temporalio",
              "temporal server start-dev            # or set TZ_TEMPORAL_ADDRESS / _NAMESPACE / _API_KEY",
              "python temporal-poc/worker.py        # in one terminal",
              "python temporal-poc/starter.py       # in another",
              "python temporal-poc/validate.py side_by_side   # or resilience, replay, smoke",
              "```", "",
              "The test data in testdata/ is synthetic. In stub mode no external system is contacted.", ""]
    return "\n".join(lines)


def packet(run: dict) -> bytes:
    ws = workspace_path(run)
    poc = ws / POC_DIR
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        from temporalize.generate.packet import packet_html

        z.writestr("packet.html", packet_html(run))
        z.writestr("README.md", packet_readme(run))
        z.writestr("diff.patch", diff(run) if ws.exists() else "")
        z.writestr("results.json", json.dumps(run.get("results") or {}, indent=2, default=str))
        if (poc / "PLAN.md").exists():
            z.write(poc / "PLAN.md", "PLAN.md")
        for sub, target in (("testdata", "testdata"), (".tz", "scenarios")):
            folder = poc / sub
            if folder.is_dir():
                for p in sorted(folder.glob("*.json" if sub == ".tz" else "*")):
                    if p.is_file():
                        z.write(p, f"{target}/{p.name}")
        if poc.is_dir():
            for p in sorted(poc.rglob("*")):
                rel = p.relative_to(poc)
                if p.is_file() and ".tz" not in rel.parts and "__pycache__" not in rel.parts and p.stat().st_size < 2_000_000:
                    z.write(p, str(Path("temporal-poc") / rel))
    return buf.getvalue()
