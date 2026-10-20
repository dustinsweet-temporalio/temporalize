"""
Business-impact translation of a stored Temporal report:

    prepare (check the report, key its processes)
      -> translate_process, per process (one model call, checked in code; none for
         processes that are not a fit or whose card shows no consequence)
      -> synthesize (rank, merge questions and assumptions, one summary call)

Orchestration-free: build_business_impact runs the steps in-process (the script,
the tests and the in-process runner); the Temporal workflow runs the same steps
as activities. Every model answer is cached on its inputs (the card, the
verdict, the context, the mapping, the prompts and the model), so a rerun with
unchanged inputs makes no model calls. The cache holds the model's answer; the
checks run again on every read, so their drops are always reproducible.
"""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import structlog

from semantic_ingress.core.llm_client import LLMUsage
from semantic_ingress.report.cache import ReportCache, prompt_hash
from semantic_ingress.report.impact.context import ImpactContext, numbered
from semantic_ingress.report.impact.cost import compute_cost
from semantic_ingress.report.impact.mapping import (
    ImpactMapping,
    ProcessSignals,
    derive_signals,
    eligible_categories,
)
from semantic_ingress.report.impact.models import ImpactSummaryDraft, ProcessImpactDraft
from semantic_ingress.report.impact.prompts import (
    PROCESS_SYSTEM,
    RETRY_NOTE,
    SUMMARY_SYSTEM,
    process_user_prompt,
    summary_user_prompt,
)
from semantic_ingress.report.impact.validate import (
    ImpactGrounding,
    clean_process,
    process_problems,
    summary_problems,
)
from semantic_ingress.report.investigate import card_text

logger = structlog.get_logger()

IMPACT_VERSION = 1      # bump to invalidate every cached impact answer
PROCESS_WORKERS = 4     # as the report (report/run.py)
CASE_RANK = {"strong": 3, "moderate": 2, "weak": 1, "none": 0}
SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
CAPPED = {"strong": "moderate"}

REPORT_MISSING = "Build the Temporal report first."
REPORT_FULL = ("This index already uses Temporal. Business-impact translation of its evaluation findings "
               "isn't supported yet.")
REPORT_EMPTY = "The Temporal report assessed no processes, so there is nothing to translate."
ZERO = LLMUsage(input_tokens=0, output_tokens=0)


class ImpactUnavailable(Exception):
    """The stored report cannot be translated; the message says why, for the user."""


@dataclass
class ProcessInput:
    key: str
    process: dict


@dataclass
class ImpactResult:
    data: dict
    usage: LLMUsage
    calls: int = 0


@dataclass
class ProcessOutcome:
    impact: dict
    usage: LLMUsage = field(default_factory=lambda: ZERO)
    calls: int = 0


def process_key(process: dict) -> str:
    """Two processes can share an entry point, so the key covers the name too."""
    return hashlib.sha1(f"{process['entry']}\0{process['name']}".encode()).hexdigest()[:12]


def source_info(doc: dict) -> dict:
    """What identifies the report an impact was built from (compared later to flag it stale)."""
    return {k: doc.get(k) for k in ("generated_at", "commit_sha", "pack_version", "rubric_version")}


def check_report(doc: dict | None) -> None:
    """Raise ImpactUnavailable unless the stored report has process assessments to translate."""
    if not doc or not doc.get("data"):
        failed = f" The last report attempt failed: {doc['error']}" if doc and doc.get("error") else ""
        raise ImpactUnavailable(REPORT_MISSING + failed)
    data = doc["data"]
    if not data.get("conversion"):
        raise ImpactUnavailable(REPORT_FULL if data.get("status") == "full" else REPORT_EMPTY)
    if not data["conversion"].get("processes"):
        raise ImpactUnavailable(REPORT_EMPTY)


def prepare(doc: dict) -> tuple[list[ProcessInput], list[dict]]:
    """The processes to translate, keyed, and those skipped because the report failed on them."""
    check_report(doc)
    pins, skipped, seen = [], [], set()
    for p in doc["data"]["conversion"]["processes"]:
        if p.get("error"):
            skipped.append({"name": p["name"], "reason": "error",
                            "detail": f"The Temporal report could not assess it: {p['error']}"})
            continue
        key = process_key(p)
        if key in seen:
            raise ValueError(f"two processes share the key {key} ({p['name']})")
        seen.add(key)
        pins.append(ProcessInput(key, p))
    return pins, skipped


def path_titles(doc: dict) -> dict[str, str]:
    return {b["id"]: b["t"] for b in doc["data"]["conversion"].get("paths", [])}


# --- one process -----------------------------------------------------------------

def _prompts_hash() -> str:
    return prompt_hash(PROCESS_SYSTEM, RETRY_NOTE, str(IMPACT_VERSION))


def _cache_parts(p: dict, ctx: ImpactContext, mapping: ImpactMapping, model_id: str) -> tuple:
    design = p.get("design") or {}
    return (p["card"], p["verdict_class"], p["overall"], p["best_path"], p.get("low_confidence", []),
            [design.get("gains", []), design.get("removes", []), design.get("stays", [])],
            ctx.hash, mapping.hash, _prompts_hash(), model_id)


def _base(pin: ProcessInput, titles: dict[str, str]) -> dict:
    p = pin.process
    return {"key": pin.key, "name": p["name"], "entry": p["entry"], "verdict_class": p["verdict_class"],
            "overall": p["overall"], "best_path": p["best_path"],
            "best_path_t": titles.get(p["best_path"], p["best_path"]),
            "low_confidence": list(p.get("low_confidence") or [])}


def _not_a_fit(pin: ProcessInput, titles: dict[str, str]) -> dict:
    """No model call: the report's own first reason and what would change the verdict (D6)."""
    p = pin.process
    reasons = p.get("reasons") or []
    why = reasons[0]["text"] if reasons else (p.get("verdict") or "")
    return {**_base(pin, titles), "status": "not_a_fit", "business_case": "none", "why": why,
            "would_change": list(p.get("would_change") or [])}


def _no_consequence(pin: ProcessInput, titles: dict[str, str]) -> dict:
    return {**_base(pin, titles), "status": "no_consequence", "business_case": "none",
            "why": "The code facts show no crash loss, waits or schedule to build a failure story from.",
            "would_change": []}


def _grounding(pin: ProcessInput, sig: ProcessSignals, eligible: list[str], items: dict[str, str],
               mapping: ImpactMapping) -> ImpactGrounding:
    return ImpactGrounding(pin.process["name"], sig, eligible, items, mapping)


def process_prompt(pin: ProcessInput, sig: ProcessSignals, eligible: list[str], items: dict[str, str],
                   mapping: ImpactMapping, titles: dict[str, str]) -> str:
    p = pin.process
    cats = [(c, mapping.categories[c]["t"], mapping.categories[c]["anchors"]) for c in eligible]
    models = [(m, spec["t"], spec["formula"], [(n, i["unit"]) for n, i in spec["inputs"].items()])
              for m, spec in mapping.cost_models.items() if m in mapping.cost_models_for(eligible)]
    return process_user_prompt(p, titles.get(p["best_path"], p["best_path"]), cats, mapping.stakeholders,
                               sig.crash_stages, models, card_text(p["card"]), p["card"]["scale_assumption"], items)


def _cap(case: str, p: dict) -> tuple[str, str]:
    """V8: borderline and low-confidence processes are at most moderate. (case, why it was capped)."""
    reasons = []
    if p["verdict_class"] == "borderline":
        reasons.append("the Temporal report rates the process borderline")
    if p.get("low_confidence"):
        reasons.append("some facts about the process are low confidence")
    if reasons and case in CAPPED:
        return CAPPED[case], "Capped at moderate: " + " and ".join(reasons) + "."
    return case, ""


def _evidence(fids: list[str], sig: ProcessSignals) -> list[dict]:
    ordered = sorted(dict.fromkeys(f for f in fids if f in sig.facts), key=lambda f: int(f[1:]))
    return [{"id": f, **sig.facts[f]} for f in ordered]


def _missing_questions(name: str, model_id: str, missing: list[str], mapping: ImpactMapping) -> list[dict]:
    if not missing:
        return []
    spec = mapping.cost_models[model_id]
    return [{"question": spec["inputs"][i]["question"], "why": f"Needed to estimate: {spec['t'].lower()}.",
             "fills": i, "ask": spec["inputs"][i]["ask"], "unit": spec["inputs"][i]["unit"],
             "cost_model": model_id, "source": "missing input", "processes": [name]} for i in missing]


def _finish(pin: ProcessInput, d: ProcessImpactDraft, g: ImpactGrounding, sig: ProcessSignals,
            ctx: ImpactContext, mapping: ImpactMapping, titles: dict[str, str]) -> dict:
    """The validated answer plus everything code adds: the cap, cost, evidence and what changes."""
    p = pin.process
    base = _base(pin, titles)
    if not d.stories:
        return {**base, "status": "failed", "business_case": "none",
                "error": "No failure story passed the checks against the code facts.", "dropped": g.dropped}
    case, capped = _cap(d.business_case, p)
    categories = [s.category for s in d.stories]
    model_id = d.cost_model or next(iter(mapping.cost_models_for(categories[:1]) or
                                         mapping.cost_models_for(categories)), "")
    cost, missing = compute_cost(model_id, d.cost_inputs, ctx, mapping) if model_id else (None, [])
    design = p.get("design") or {}
    cited = [c for s in d.stories for c in s.cites] + [
        c for item in [d.workaround, *d.drivers] if item.basis == "code" for c in item.cites]
    assumptions = [f"{p['name']}: {item.text}" for item in [d.workaround, *d.drivers]
                   if item.basis == "assumption" and item.text]
    if cost:
        assumptions += [f"{p['name']}: {r['name'].replace('_', ' ')} {r['low']:g}-{r['high']:g} {r['unit']} (default)"
                        for r in cost["inputs"] if r["basis"] == "default"]
    if p["card"].get("scale_assumption"):
        assumptions.append(f"{p['name']}: {p['card']['scale_assumption']}")
    return {
        **base, "status": "ok",
        "stories": [s.model_dump() for s in d.stories],
        "workaround": d.workaround.model_dump(),
        "drivers": [x.model_dump() for x in d.drivers],
        "business_case": case, "case_reason": d.case_reason, "case_capped": capped,
        "cost_model": model_id, "cost": cost, "missing_inputs": missing,
        # A missing input the model already asks about keeps the model's (tailored) question.
        "questions": [{**q.model_dump(), "source": "model", "processes": [p["name"]]} for q in d.questions]
                     + _missing_questions(p["name"], model_id, [i for i in missing if i not in {q.fills for q in d.questions}],
                                          mapping),
        "what_changes": {"gains": design.get("gains", []), "removes": design.get("removes", []),
                         "stays": design.get("stays", []), "why": design.get("why", "")},
        "verdict": p.get("verdict", ""), "would_change": list(p.get("would_change") or []),
        "evidence": _evidence(cited, sig),
        "crash_loss": p["card"]["crash_loss"],
        "scale_assumption": p["card"].get("scale_assumption", ""),
        "assumptions": assumptions,
        "dropped": g.dropped,
    }


def translate_process(llm, cache: ReportCache, mapping: ImpactMapping, ctx: ImpactContext,
                      pin: ProcessInput, titles: dict[str, str]) -> ProcessOutcome:
    """One process: deterministic for not-a-fit processes; otherwise cached, or one call (plus one retry)."""
    p = pin.process
    if p["verdict_class"] == "not suitable":
        return ProcessOutcome(_not_a_fit(pin, titles))
    sig = derive_signals(p)
    eligible = eligible_categories(sig, mapping)
    if not eligible:
        return ProcessOutcome(_no_consequence(pin, titles))
    items = numbered(ctx)
    g = _grounding(pin, sig, eligible, items, mapping)
    parts = _cache_parts(p, ctx, mapping, llm.config.model_id)
    hit = cache.get("impact_process", *parts)
    usage, calls, problems = ZERO, 0, []
    if hit is not None:
        answer = ProcessImpactDraft.model_validate(hit["answer"])
    else:
        prompt = process_prompt(pin, sig, eligible, items, mapping, titles)
        answer, usage = llm.generate(prompt, ProcessImpactDraft, system=PROCESS_SYSTEM, enforce_schema=True)
        calls = 1
        problems = process_problems(answer, g)
        if problems:
            retry = prompt + RETRY_NOTE.format(previous=answer.model_dump_json(), problems="\n".join(problems))
            answer, more = llm.generate(retry, ProcessImpactDraft, system=PROCESS_SYSTEM, enforce_schema=True)
            usage, calls = usage + more, 2
        cache.put("impact_process", *parts, value={"answer": answer.model_dump(), "retried": calls == 2})
    impact = _finish(pin, clean_process(answer, g), g, sig, ctx, mapping, titles)
    logger.info("impact_process_done", process_key=pin.key, status=impact["status"], cached=hit is not None,
                calls=calls, retry_problems=len(problems), dropped=len(g.dropped))
    return ProcessOutcome(impact, usage, calls)


def failed_process(pin: ProcessInput, titles: dict[str, str], error: str) -> dict:
    """A process whose translation raised (a model or store error), so the run can still finish."""
    return {**_base(pin, titles), "status": "failed", "business_case": "none", "error": error[:500]}


# --- the whole run ----------------------------------------------------------------

def rank_key(impact: dict) -> tuple:
    top = max((SEVERITY_RANK[s["severity"]] for s in impact.get("stories", [])), default=0)
    return (-CASE_RANK[impact["business_case"]], -top, -impact["overall"], impact["name"])


def _norm(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text.lower()).split())


def merge_questions(ranked: list[dict]) -> list[dict]:
    """Every process's questions, near-duplicates merged; the top process's missing inputs first."""
    merged: dict[str, dict] = {}
    for rank, impact in enumerate(ranked):
        for q in impact.get("questions", []):
            key = _norm(q["question"])
            if key in merged:
                merged[key]["processes"] = list(dict.fromkeys(merged[key]["processes"] + q["processes"]))
                continue
            merged[key] = {**q, "processes": list(q["processes"]), "_order": (rank, q["source"] != "missing input",
                                                                                len(merged))}
    out = sorted(merged.values(), key=lambda q: q["_order"])
    for q in out:
        q.pop("_order")
    return out


def template_summary(ranked: list[dict], not_worth: list[dict], mapping: ImpactMapping) -> ImpactSummaryDraft:
    """The deterministic summary, when there is nothing to summarize or the model's fails its checks."""
    if not ranked:
        names = [s["name"] for s in not_worth]
        return ImpactSummaryDraft(
            headline="The code shows no process with a business case for durable execution.",
            exec_summary=("None of the assessed processes shows a failure consequence worth pursuing from the code "
                          "alone. " + ("Not worth pursuing: " + "; ".join(names) + "." if names else "")).strip(),
            mentions=names)
    top = ranked[0]
    cat = mapping.categories[top["stories"][0]["category"]]["t"].lower()
    cases = "; ".join(f"{p['name']} ({p['business_case']} case)" for p in ranked)
    return ImpactSummaryDraft(
        headline=f"{top['name']}: {cat} is the main business risk the code shows.",
        exec_summary=f"Ranked by business case: {cases}. " + (
            "Not worth pursuing: " + "; ".join(s["name"] for s in not_worth) + "." if not_worth else ""),
        mentions=[p["name"] for p in ranked] + [s["name"] for s in not_worth])


def summarize(llm, cache: ReportCache, mapping: ImpactMapping, ranked: list[dict],
              not_worth: list[dict]) -> tuple[ImpactSummaryDraft, str, LLMUsage, int]:
    """(summary, its source: "model" | "template", usage, calls)."""
    if not ranked:
        return template_summary(ranked, not_worth, mapping), "template", ZERO, 0
    prompt = summary_user_prompt([(p["name"], p["business_case"], p["stories"][0]["scenario"]) for p in ranked],
                                 [s["name"] for s in not_worth])
    names = {p["name"] for p in ranked} | {s["name"] for s in not_worth}
    parts = (prompt, prompt_hash(SUMMARY_SYSTEM, RETRY_NOTE, str(IMPACT_VERSION)), mapping.hash, llm.config.model_id)
    hit = cache.get("impact_summary", *parts)
    usage, calls = ZERO, 0
    if hit is not None:
        answer = ImpactSummaryDraft.model_validate(hit["answer"])
    else:
        answer, usage = llm.generate(prompt, ImpactSummaryDraft, system=SUMMARY_SYSTEM, enforce_schema=True)
        calls = 1
        problems = summary_problems(answer, names)
        if problems:
            retry = prompt + RETRY_NOTE.format(previous=answer.model_dump_json(), problems="\n".join(problems))
            answer, more = llm.generate(retry, ImpactSummaryDraft, system=SUMMARY_SYSTEM, enforce_schema=True)
            usage, calls = usage + more, 2
        cache.put("impact_summary", *parts, value={"answer": answer.model_dump()})
    if summary_problems(answer, names):
        return template_summary(ranked, not_worth, mapping), "template", usage, calls
    return answer, "model", usage, calls


def synthesize(llm, cache: ReportCache, mapping: ImpactMapping, ctx: ImpactContext, doc: dict,
               impacts: list[dict], skipped: list[dict], usage: LLMUsage = ZERO, calls: int = 0) -> ImpactResult:
    """Rank the processes, merge questions and assumptions, write the summary: the stored `data`."""
    ranked = sorted((i for i in impacts if i["status"] == "ok"), key=rank_key)
    others = sorted((i for i in impacts if i["status"] != "ok"), key=lambda i: (-i["overall"], i["name"]))
    reasons = {"not_a_fit": "not suitable", "no_consequence": "no grounded consequence", "failed": "failed"}
    skipped = skipped + [{
        "name": i["name"], "reason": reasons[i["status"]], "detail": i.get("why") or i.get("error", ""),
        "overall": i["overall"], "verdict_class": i["verdict_class"], "would_change": i.get("would_change", []),
    } for i in others]
    not_worth = [s for s in skipped if s["reason"] == "not suitable"]
    summary, source, u, n = summarize(llm, cache, mapping, ranked, not_worth)
    usage, calls = usage + u, calls + n
    dropped = [d for i in impacts for d in i.get("dropped", [])]
    if source == "template" and ranked:
        dropped.append("summary: the model's summary failed its checks; a template summary is used")
    data = {
        "repo_id": doc.get("repo_id") or doc["data"].get("repo_id"),
        "generated": date.today().isoformat(),
        "source_report": source_info(doc),
        "mapping": {"id": mapping.id, "version": mapping.version, "hash": mapping.hash},
        "categories": {c: spec["t"] for c, spec in mapping.categories.items()},
        "stakeholders": dict(mapping.stakeholders),
        "context": {"present": ctx.present, "hash": ctx.hash, "currency": ctx.currency, "audience": ctx.audience,
                    "items": numbered(ctx)},
        "model": llm.config.model_id,
        "headline": summary.headline, "exec_summary": summary.exec_summary, "summary_source": source,
        "processes": ranked,
        "skipped": skipped,
        "questions": merge_questions(ranked),
        "assumptions": list(dict.fromkeys(a for i in ranked for a in i.get("assumptions", []))),
        "dropped": dropped,
        "model_calls": calls,
        "cost_usd": round(usage.total_cost_usd, 4),
    }
    return ImpactResult(data, usage, calls)


def build_business_impact(llm, store, repo_id: str, ctx: ImpactContext, mapping: ImpactMapping,
                          fresh: bool = False, on_progress=None) -> ImpactResult:
    """The whole run in-process (raises ImpactUnavailable when the report cannot be translated)."""
    doc = store.get_report(repo_id)
    pins, skipped = prepare(doc)
    titles = path_titles(doc)
    cache = ReportCache(store, repo_id, fresh)

    def one(pin: ProcessInput) -> ProcessOutcome:
        try:
            out = translate_process(llm, cache, mapping, ctx, pin, titles)
        except Exception as e:
            logger.error("impact_process_failed", process_key=pin.key, error=str(e)[:300])
            out = ProcessOutcome(failed_process(pin, titles, str(e)))
        if on_progress:
            on_progress(out.impact)
        return out

    with ThreadPoolExecutor(max_workers=PROCESS_WORKERS) as pool:
        outcomes = list(pool.map(one, pins))
    usage = sum((o.usage for o in outcomes), ZERO)
    calls = sum(o.calls for o in outcomes)
    result = synthesize(llm, cache, mapping, ctx, doc, [o.impact for o in outcomes], skipped, usage, calls)
    logger.info("impact_built", repo_id=repo_id, processes=len(pins), calls=result.calls,
                cost_usd=result.data["cost_usd"], cache_hits=cache.hits, cache_misses=cache.misses)
    return result
