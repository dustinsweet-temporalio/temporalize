"""
Build a report: gather -> verify leads -> per process (card -> score -> write up)
-> evaluate existing Temporal code -> render.

The mode follows the repository lens status: none/adjacent -> suitability;
full -> evaluation; partial -> both (processes that touch Temporal code are
evaluated instead of investigated).
"""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

import structlog

from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.lens.pack import Pack
from semantic_ingress.report.cache import ReportCache, prompt_hash
from semantic_ingress.report.gather import IndexData, load_index
from semantic_ingress.report.grounding import Grounding
from semantic_ingress.report.investigate import Candidate, card_facts, card_text, discover, process_card
from semantic_ingress.report.models import ConversionWriteup, EvaluationWriteup
from semantic_ingress.report.prompts import (
    DESIGN_TASK,
    EVAL_SYSTEM,
    GROUNDING,
    RETRY_NOTE,
    SCORE_SYSTEM,
    UNSUITABLE_TASK,
    WRITEUP_SYSTEM,
)
from semantic_ingress.report.render import render
from semantic_ingress.report.rubric import Rubric
from semantic_ingress.report.score import evaluation_bars, pct_temporalized, records_text, score_process
from semantic_ingress.report.verify import LeadResult, collect_leads, verify_leads
from semantic_ingress.report.writeup import (
    DocsSearch,
    entries_text,
    finding_key,
    moved_files,
    write_evaluation,
    write_process,
)

logger = structlog.get_logger()

CONVERSION_SECTIONS = ["gates", "values", "patterns", "conversion", "limits"]
PROCESS_WORKERS = 4


class ReportProgress:
    """
    What the report build is doing, for a UI to show while it runs (it takes minutes): the phase,
    a line of detail, and each process's state (pending, investigating, scoring, writing, done,
    failed). Sent to `sink` on every change; best effort, never fails the build.
    """

    def __init__(self, sink: Callable[[dict], None] | None) -> None:
        self.sink = sink
        self.lock = threading.Lock()
        self.state: dict = {"phase": "starting", "detail": "", "processes": []}

    def update(self, **fields) -> None:
        with self.lock:
            self.state.update(fields)
            snapshot = copy.deepcopy(self.state)
        self._send(snapshot)

    def process(self, name: str, state: str) -> None:
        with self.lock:
            for p in self.state["processes"]:
                if p["name"] == name:
                    p["state"] = state
            done = sum(1 for p in self.state["processes"] if p["state"] in ("done", "failed"))
            total = len(self.state["processes"])
            self.state["detail"] = f"Investigating processes: {done} of {total} done"
            snapshot = copy.deepcopy(self.state)
        self._send(snapshot)

    def _send(self, snapshot: dict) -> None:
        if self.sink is None:
            return
        try:
            self.sink(snapshot)
        except Exception as e:
            logger.warning("report_progress_failed", error=str(e))


@dataclass
class ReportResult:
    data: dict
    markdown: str
    usage: LLMUsage


def _lead_dict(r: LeadResult) -> dict:
    return {"path": r.lead.path, "lead": r.lead.key, "entry": r.lead.entry, "verdict": r.verdict,
            "line": r.line, "severity": r.severity, "reason": r.reason}


def _warnings(data: IndexData, pack: Pack) -> list[str]:
    warnings = []
    if data.lens_state == "partial":
        warnings.append(f"{data.unlensed} indexed files have no lens record; they are not covered.")
    versions = [v["version"] for v in data.pack_versions if v.get("version")]
    if len(versions) > 1:
        warnings.append(f"The lens was built with several pack versions ({', '.join(versions)}).")
    if versions and pack.version not in versions:
        warnings.append(
            f"The lens was built with pack {', '.join(versions)}; this report uses pack {pack.version}. "
            "Re-index with --refresh-lens for a consistent report."
        )
    return warnings


def _run_process(ctx: dict, cand: Candidate) -> tuple[dict, LLMUsage, int]:
    """Card, scores and write-up for one process. Failures are reported, not raised."""
    data: IndexData = ctx["data"]
    rubric: Rubric = ctx["rubric"]
    zero = LLMUsage(input_tokens=0, output_tokens=0)
    progress: ReportProgress = ctx["progress"]
    try:
        progress.process(cand.name, "investigating")
        cr = process_card(ctx["llm"], ctx["inv_llm"], data, ctx["store"], ctx["repo_id"], cand, ctx["fresh"])
        usage = cr.usage
        paths = {p for p in [cand.entry, *cand.files, *cr.card["files"]] if p in data.blobs}
        gates = {g: [p for p in ps if p in paths] for g, ps in ctx["gates"].items() if any(p in paths for p in ps)}
        # Scores and write-up depend only on the card, the gates and the versions: cached on them.
        parts = (cr.card, gates, rubric.hash, ctx["pack"].version, ctx["llm"].config.model_id, ctx["prompts"])
        hit = ctx["cache"].get("process", *parts)
        progress.process(cand.name, "scoring")
        searches = 0
        if hit is not None:
            bars, notes, dropped, searches = hit["bars"], hit["notes"], hit["dropped"], hit["searches"]
            writeup = ConversionWriteup.model_validate(hit["writeup"])
            result = rubric.overall({b: v["score"] for b, v in bars.items()})
        else:
            grounding = ctx["grounding"].fork()
            bars, u, notes = score_process(ctx["llm"], rubric, ctx["pack"], data, grounding, cr.card,
                                           paths, gates, ctx["pack_text"])
            usage = usage + u
            result = rubric.overall({b: v["score"] for b, v in bars.items()})
            progress.process(cand.name, "writing")
            writeup, u, searches, w_notes = write_process(
                ctx["llm"], ctx["pack"], grounding, ctx["docs_search"], bars, result, gates,
                cand.name, card_text(cr.card), records_text(data, paths, min_rel=0), ctx["pack_text"],
                fact_ids={f[0] for f in card_facts(cr.card)},
            )
            usage = usage + u
            notes, dropped = notes + w_notes, grounding.dropped
            ctx["cache"].put("process", *parts, value={
                "bars": bars, "writeup": writeup.model_dump(), "notes": notes, "dropped": dropped,
                "searches": searches,
            })
    except Exception as e:
        logger.error("process_report_failed", process=cand.name, error=str(e))
        print(f"  ✗ {cand.name}: {e}")
        progress.process(cand.name, "failed")
        return {"name": cand.name, "entry": cand.entry, "origin": cand.origin, "error": str(e)}, zero, 0

    titles = rubric.titles()
    kinds = {b["id"]: "path" for b in rubric.value_paths} | {b["id"]: "modifier" for b in rubric.modifiers}
    label = result["verdict_class"]
    progress.process(cand.name, "done")
    print(f"  • {cand.name}: {result['overall']}/10 {label} | card {'reused' if cr.reused else f'{cr.tool_calls} tool calls'}"
          f", assessment {'reused' if hit is not None else 'new'} | ${usage.total_cost_usd:.4f}")
    return {
        "name": cand.name, "entry": cand.entry, "origin": cand.origin,
        "card": cr.card, "card_reused": cr.reused, "card_checks": cr.verdicts,
        "low_confidence": cr.low_confidence, "card_dropped": cr.dropped,
        "bars": [{"id": b, "t": titles[b], "kind": kinds[b], **v} for b, v in bars.items()],
        **result,
        "gates": [{"id": g, "paths": ps} for g, ps in gates.items()],
        "verdict": writeup.verdict,
        "design": writeup.designs[0].model_dump() if writeup.designs else None,
        "reasons": [r.model_dump() for r in writeup.reasons],
        "would_change": writeup.would_change,
        "notes": notes,
        "dropped": dropped,
        "moved": sorted(moved_files(writeup)) if result["design"] else [],
    }, usage, searches


def build_report(llm, inv_llm, pack: Pack, rubric: Rubric, store, repo_id: str,
                 docs_search: DocsSearch | None, source_root: Path | None, fresh: bool = False,
                 progress_sink: Callable[[dict], None] | None = None) -> ReportResult:
    progress = ReportProgress(progress_sink)
    progress.update(phase="reading", detail="Reading the index")
    data = load_index(store, repo_id, source_root)
    lens = data.repo_lens
    status = lens["status"]
    do_conversion = status in ("none", "adjacent", "partial")
    do_evaluation = status in ("partial", "full")
    grounding = Grounding.build(pack, set(data.blobs))
    cache = ReportCache(store, repo_id, fresh)
    usage = LLMUsage(input_tokens=0, output_tokens=0)
    searches = 0

    # 1. Verify leads (gates always; review findings when there is Temporal code).
    print(f"Verifying leads for {repo_id} (status {status})...")
    leads, skipped = collect_leads(data, include_findings=do_evaluation)
    progress.update(phase="leads", detail=f"Checking {len(leads)} lead{'' if len(leads) == 1 else 's'} against the source")
    results, u = verify_leads(llm, pack, data, leads, cache)
    usage = usage + u
    confirmed = [r for r in results if r.verdict == "confirmed"]
    confirmed_gates: dict[str, list[str]] = {}
    for r in confirmed:
        if r.lead.is_gate:
            confirmed_gates.setdefault(r.lead.entry, []).append(r.lead.path)
    findings = [r for r in confirmed if not r.lead.is_gate]
    for r in results:
        if r.lead.is_gate and r.lead.entry not in confirmed_gates:
            grounding.rejected_gates.setdefault(r.lead.entry, f"{r.lead.path}: {r.reason}")
    print(f"  {len(results)} leads: {len(confirmed)} confirmed"
          + (f", {skipped} over the cap" if skipped else "") + f" | ${u.total_cost_usd:.4f}")

    report: dict = {
        "repo_id": repo_id,
        "commit": data.commit_sha,
        "indexed_at": data.indexed_at,
        "generated": date.today().isoformat(),
        "pack": {"id": pack.id, "version": pack.version, "hash": pack.hash},
        "rubric": {"id": rubric.id, "version": rubric.version, "hash": rubric.hash},
        "lens_state": data.lens_state,
        "status": status,
        "detected": lens.get("detected", []),
        "archetype": lens.get("archetype"),
        "warnings": _warnings(data, pack),
        "gates": [
            {"id": r.lead.entry, "path": r.lead.path, "line": r.line, "reason": r.reason}
            for r in confirmed if r.lead.is_gate
        ],
        "leads": [_lead_dict(r) for r in results],
        "leads_skipped": skipped,
        "model": llm.config.model_id,
        "investigate_effort": inv_llm.config.effort,
    }

    temporal = data.temporal_files
    moved: set[str] = set()

    # 2. Suitability, per process.
    if do_conversion:
        cands, over, uncovered = discover(data, exclude=temporal if status == "partial" else set())
        progress.update(phase="processes", detail=f"Investigating processes: 0 of {len(cands)} done",
                        processes=[{"name": c.name, "state": "pending"} for c in cands])
        print(f"Investigating {len(cands)} processes" + (f" ({len(over)} over the limit)" if over else "") + "...")
        ctx = {"llm": llm, "inv_llm": inv_llm, "pack": pack, "rubric": rubric, "data": data, "store": store,
               "repo_id": repo_id, "fresh": fresh, "gates": confirmed_gates, "grounding": grounding,
               "docs_search": docs_search, "pack_text": entries_text(pack, sections=CONVERSION_SECTIONS),
               "cache": cache, "progress": progress, "prompts": prompt_hash(SCORE_SYSTEM, WRITEUP_SYSTEM, DESIGN_TASK, UNSUITABLE_TASK,
                                                      GROUNDING, RETRY_NOTE)}
        with ThreadPoolExecutor(max_workers=PROCESS_WORKERS) as pool:
            outcomes = list(pool.map(lambda c: _run_process(ctx, c), cands))
        processes = []
        for proc, u, n in outcomes:
            usage = usage + u
            searches += n
            processes.append(proc)
            moved.update(proc.get("moved", []))
            grounding.dropped.extend(proc.pop("dropped", []))
        processes.sort(key=lambda p: (-p.get("overall", -1), p["name"]))
        report["conversion"] = {
            "processes": processes,
            "not_investigated": [{"name": c.name, "entry": c.entry} for c in over],
            "uncovered_entries": uncovered,
            "suitable": any(p.get("suitable") for p in processes),
            "threshold": rubric.suitable_at,
            "margin": rubric.margin,
            "weights": rubric.data["overall"]["weights"],
            "cap": rubric.data["overall"]["cap"],
            "paths": [{"id": b["id"], "t": b["t"]} for b in rubric.value_paths],
        }

    # 3. Evaluation of existing Temporal code: bars in code, then remediations.
    if do_evaluation:
        ebars, other = evaluation_bars(rubric, data, findings)
        print("Writing the evaluation...")
        progress.update(phase="evaluation", detail="Reviewing the existing Temporal code")
        eval_parts = ([(finding_key(f), f.severity, f.line, f.reason) for f in findings], len(temporal),
                      pack.version, llm.config.model_id, prompt_hash(EVAL_SYSTEM, GROUNDING, RETRY_NOTE))
        hit = cache.get("evaluation", *eval_parts)
        if hit is not None:
            ewrite = EvaluationWriteup.model_validate(hit["writeup"])
            grounding.dropped.extend(hit["dropped"])
            searches += hit["searches"]
        else:
            eval_grounding = grounding.fork()
            ewrite, u, n = write_evaluation(llm, pack, eval_grounding, docs_search, findings, len(temporal))
            usage = usage + u
            searches += n
            cache.put("evaluation", *eval_parts, value={"writeup": ewrite.model_dump(), "dropped": eval_grounding.dropped,
                                                        "searches": n})
            grounding.dropped.extend(eval_grounding.dropped)
        fixes = {r.finding: r for r in ewrite.remediations}

        def finding_dict(r: LeadResult) -> dict:
            fix = fixes.get(finding_key(r))
            return {**_lead_dict(r), "fix": fix.fix if fix else "",
                    "cites": list(dict.fromkeys([r.lead.entry] + (fix.cites if fix else [])))}

        report["evaluation"] = {
            "verdict": ewrite.verdict,
            "temporal_files": len(temporal),
            "bars": [{"id": b.id, "t": b.t, "score": b.score, "reason": b.reason,
                      "findings": [finding_dict(f) for f in b.findings]} for b in ebars],
            "other_findings": [finding_dict(f) for f in other],
        }

    report["pct_temporalized"] = pct_temporalized(data, moved)
    report["questions"] = [{"id": q["id"], "t": q["t"]} for q in pack.data.get("questions", [])]
    report["docs_searches"] = searches
    report["dropped"] = grounding.dropped
    report["notes"] = [n for p in (report.get("conversion") or {}).get("processes", []) for n in p.get("notes", [])]
    report["cost_usd"] = round(usage.total_cost_usd, 4)
    report["sources"] = _sources(report, pack)

    progress.update(phase="rendering", detail="Writing the report")
    print(f"  cache: {cache.hits} steps reused, {cache.misses} run")
    logger.info("report_built", repo_id=repo_id, cost_usd=report["cost_usd"], searches=searches,
                cache_hits=cache.hits, cache_misses=cache.misses)
    return ReportResult(report, render(report), usage)


def _sources(report: dict, pack: Pack) -> dict:
    """Every citation the report uses: pack entries (with their docs URLs) and other docs URLs."""
    refs: list[str] = [g["id"] for g in report["gates"]]
    for p in (report.get("conversion") or {}).get("processes", []):
        for bar in p.get("bars", []):
            refs += bar["cites"]
        if p.get("design"):
            d = p["design"]
            refs += d["cites"] + [c for el in d["elements"] for c in el["cites"]]
            refs += [c for g in d.get("gains", []) for c in g["cites"] if c in pack.entries]
        for r in p.get("reasons", []):
            refs += r["cites"]
    ev = report.get("evaluation") or {}
    for f in [f for b in ev.get("bars", []) for f in b["findings"]] + ev.get("other_findings", []):
        refs += f["cites"]
    refs = list(dict.fromkeys(refs))
    entries = [
        {"id": r, "t": pack.entries[r]["t"], "urls": pack.entries[r].get("url") or []}
        for r in refs if r in pack.entries
    ]
    urls = [r for r in refs if r not in pack.entries]
    return {"pack": entries, "urls": urls}
