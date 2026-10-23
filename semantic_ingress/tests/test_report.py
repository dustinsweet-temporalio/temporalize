import copy
import json
import re
from pathlib import Path

import pytest

from semantic_ingress.core.llm_client import LLMUsage, _move_cache_breakpoint
from semantic_ingress.lens.models import FileLensRecord
from semantic_ingress.lens.pack import load_pack
from semantic_ingress.report.gather import IndexData
from semantic_ingress.report.grounding import Grounding
from semantic_ingress.report.investigate import NOT_ESTABLISHED, card_facts, check_refs, discover, verify_card
from semantic_ingress.report.models import (
    BarScore,
    CardVerdicts,
    ConversionScores,
    ConversionWriteup,
    DesignElement,
    EvaluationWriteup,
    FactVerdict,
    FileVerdicts,
    Gain,
    LeadVerdict,
    ProcessCard,
    ProcessDesign,
    Remediation,
)
from semantic_ingress.report.render import bar
from semantic_ingress.report.rubric import load_rubric, validate
from semantic_ingress.report.run import build_report
from semantic_ingress.report.score import evaluation_bars, pct_temporalized
from semantic_ingress.report.verify import Lead, LeadResult
from semantic_ingress.report.writeup import write_process

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge"
ZERO = LLMUsage(input_tokens=0, output_tokens=0)
DOCS_URL = "https://docs.temporal.io/design-patterns/saga-pattern"


@pytest.fixture(scope="module")
def pack():
    return load_pack(KNOWLEDGE / "temporal-pack.yaml")


@pytest.fixture(scope="module")
def rubric(pack):
    return load_rubric(KNOWLEDGE / "temporal-rubric.yaml", pack)


def rec(rel, kind, f=(), sig=(), n="x"):
    return {"rel": rel, "cov": "full", "kind": list(kind), "sig": list(sig), "sym": [], "c": [], "f": list(f),
            "n": n, "mode": "conv", "lang": "python", "pack_version": "0.1.0"}


FILES = {
    "orders/flow.py": rec(3, ["ORCH"], sig=["reliab.retry@L3-9", "effect.payment@L10-12"]),
    "orders/pay.py": rec(2, ["ACT"], sig=["effect.payment@L1-5"]),
    "orders/config.py": rec(0, ["NONE"]),
    "jobs/nightly.py": rec(2, ["ENTRY", "ACT"], sig=["trigger.cron@L1"]),
    "batch/wf.py": rec(3, ["TWF"], f=["E-DET-NONDET@L2", "E-WFO-RETRY@L3"]),
}
SOURCE = {"orders/flow.py": "a\nb\nc\nd\n", "orders/pay.py": "a\nb\n", "batch/wf.py": "x\ny\nz\n",
          "jobs/nightly.py": "n\n"}
REPO_LENS = {
    "status": "partial", "archetype": "service", "detected": [], "n": "x", "cites": [],
    "processes": [
        {"name": "Order payment", "entry": "orders/flow.py", "files": ["orders/flow.py", "orders/pay.py"],
         "signals": [], "concepts": [], "narrative": "Charges then ships."},
        {"name": "Batch", "entry": "batch/wf.py", "files": ["batch/wf.py"], "signals": [], "concepts": [], "narrative": "x"},
    ],
    "anti": [{"id": "G-LATENCY", "evidence": ["orders/pay.py"]}],
}


def index_data(tmp_path) -> IndexData:
    for path, text in SOURCE.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    return IndexData("demo", "abc", "2026-10-16", [{"version": "0.1.0", "files": 5}], REPO_LENS,
                     {p: FileLensRecord.model_validate(r) for p, r in FILES.items()}, 0, None, tmp_path,
                     blobs={p: f"blob-{p}" for p in FILES})


def fact(text, refs):
    return {"text": text, "refs": refs}


def card(**over):
    base = {
        "process": "Order payment",
        "trigger": fact("HTTP order request.", ["orders/flow.py@L1-2"]),
        "unit_of_work": fact("One order.", ["orders/flow.py@L1"]),
        "scales_with": "orders per day", "scale_assumption": "Assumes 1,000 orders a day.",
        "steps": [{"name": "charge", "does": "Charges the card.", "effects": ["paid_call"], "refs": ["orders/pay.py@L1-2"]}],
        "state_between_steps": fact("In memory.", ["orders/flow.py@L3"]),
        "persistence": fact("Written at the end.", ["orders/flow.py@L4"]),
        "crash_loss": [{"stage": "after charging", "loses": "The shipment; the charge is not undone.", "refs": ["orders/flow.py@L3-4"]}],
        "reliability": [fact("Retries the charge three times.", ["orders/flow.py@L2-3"])],
        "waits": [], "run_length": fact(NOT_ESTABLISHED, []),
        "files": ["orders/flow.py", "orders/pay.py"],
    }
    base.update(over)
    return ProcessCard.model_validate(base).model_dump()


def result(path, key, severity="medium"):
    return LeadResult(Lead(path, key, key.split("@")[0], None, 3), "confirmed", "L2", severity, "it does")


# --- rubric ------------------------------------------------------------------------

def test_fit_is_the_best_value_path_and_gates_cap_the_overall(rubric):
    mods = {"B-BURDEN": 6, "B-SEPARABILITY": 6, "B-EFFORT": 6}
    pipeline = rubric.overall({"PATH-PIPELINE": 8, "PATH-WAIT": 1, **mods, "B-GATES": 10})
    assert (pipeline["fit"], pipeline["best_path"], pipeline["overall"], pipeline["suitable"]) == (8, "PATH-PIPELINE", 7.2, True)
    capped = rubric.overall({"PATH-PIPELINE": 8, **mods, "B-GATES": 2})
    assert capped["overall"] == 4.0 and capped["gate_capped"] and capped["verdict_class"] == "not suitable"


def test_scores_near_the_threshold_are_borderline_and_still_designed(rubric):
    close = rubric.overall({"PATH-PIPELINE": 5, "B-BURDEN": 5, "B-SEPARABILITY": 5, "B-EFFORT": 5, "B-GATES": 10})
    assert close["overall"] == 5.0 and close["verdict_class"] == "borderline"
    assert close["design"] and not close["suitable"]


def test_rubric_validation_catches_unknown_codes_and_unmapped_checks(rubric, pack):
    data = copy.deepcopy(rubric.data)
    data["value_paths"][0]["signals"].append("no.such.signal")
    data["evaluate"]["bars"] = data["evaluate"]["bars"][:1]
    problems = validate(data, pack)
    assert any("unknown signal code 'no.such.signal'" in p for p in problems)
    assert any("map to no bar" in p for p in problems)


def test_evaluation_bars_deduct_by_severity(rubric, tmp_path):
    bars, other = evaluation_bars(rubric, index_data(tmp_path), [
        result("batch/wf.py", "E-DET-NONDET@L2", "high"),
        result("batch/wf.py", "E-WFO-RETRY@L3", "low"),
    ])
    by_id = {b.id: b for b in bars}
    assert by_id["B-E-DETERMINISM"].score == 7
    assert by_id["B-E-FAILURE"].score == 9.5  # E-WFO-* now counts against failure handling
    assert other == []
    assert by_id["B-E-COVERAGE"].score == 2  # 1 of 4 durable-relevant files is Temporal code


def test_pct_temporalized_counts_moved_orch_and_act_files(tmp_path):
    pct = pct_temporalized(index_data(tmp_path), {"orders/flow.py", "orders/pay.py", "orders/config.py"})
    assert (pct["files_total"], pct["files_today"], pct["files_after"]) == (4, 1, 3)
    assert pct["moved_files"] == ["orders/flow.py", "orders/pay.py"]


def test_bar_rendering():
    assert bar(7) == "`███████░░░`"
    assert bar(9.5) == "`█████████░`"  # a full bar means 10


def test_cache_breakpoint_moves_to_the_newest_turn():
    messages = [{"role": "user", "content": [{"type": "text", "text": "p"}]}]
    _move_cache_breakpoint(messages)
    messages += [{"role": "assistant", "content": "x"},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "r"}]}]
    _move_cache_breakpoint(messages)
    assert "cache_control" not in messages[0]["content"][0]
    assert messages[2]["content"][-1]["cache_control"] == {"type": "ephemeral"}


# --- discovery and cards --------------------------------------------------------------

def test_discovery_takes_lens_processes_and_uncovered_entry_points(tmp_path):
    cands, over, uncovered = discover(index_data(tmp_path), exclude={"batch/wf.py"})
    assert [(c.name, c.origin) for c in cands] == [("Order payment", "repository lens"),
                                                   ("Entry point jobs/nightly.py", "entry point")]
    assert over == [] and uncovered == []


def test_discovery_falls_back_to_relevance_1_entry_points(tmp_path):
    data = index_data(tmp_path)
    data.repo_lens = {**REPO_LENS, "processes": []}
    data.files = {"app/Main.kt": FileLensRecord.model_validate(rec(1, ["ENTRY"], sig=["trigger.http"]))}
    cands, _, uncovered = discover(data, exclude=set())
    assert [c.entry for c in cands] == ["app/Main.kt"] and uncovered == []


def test_card_references_must_be_indexed_in_range_and_seen(tmp_path):
    data = index_data(tmp_path)
    c = card(reliability=[fact("Retries.", ["orders/flow.py@L2-9"]),  # beyond the file's 4 lines
                          fact("Locks.", ["orders/ghost.py@L1"])])
    seen = {"orders/flow.py": {1, 2, 3, 4}, "orders/pay.py": {1}}  # pay.py L2 never seen
    dropped = check_refs(c, data, seen)
    assert c["reliability"] == []
    assert c["steps"] == []  # its only reference ended on an unseen line
    assert c["run_length"]["text"] == NOT_ESTABLISHED  # kept: says so honestly
    assert c["persistence"]["refs"] == ["orders/flow.py@L4"]
    assert len(dropped) == 6  # three bad references, three facts left without one


class CardLLM:
    def __init__(self, verdicts):
        self.verdicts = verdicts

    def generate(self, prompt, response_model, system=None, enforce_schema=False):
        assert response_model is CardVerdicts and "orders/flow.py L4-4" in prompt
        return CardVerdicts(verdicts=[FactVerdict(fact=f, verdict=v, reason="r") for f, v in self.verdicts.items()]), ZERO


def test_unsupported_facts_are_dropped_and_lower_confidence(tmp_path):
    data, c = index_data(tmp_path), card()
    ids = {label: fid for fid, _, _, label, _, _ in card_facts(c)}
    verdicts, dropped, low, _ = verify_card(CardLLM({ids["persistence"]: "unsupported"}), data, c)
    assert c["persistence"]["text"] == NOT_ESTABLISHED
    assert low == ["when results become durable could not be established"]
    assert any("persistence" in d for d in dropped)
    assert {v["verdict"] for v in verdicts} == {"unclear", "unsupported"}  # unanswered facts are unclear


# --- grounding ----------------------------------------------------------------------

def test_gates_not_confirmed_by_verification_cannot_be_cited(pack):
    g = Grounding.build(pack, set(FILES))
    g.rejected_gates["G-LATENCY"] = "orders/pay.py: no latency requirement"
    assert g.cites(["G-LATENCY", "G-READONLY"], "reason 1") == ["G-READONLY"]
    assert "not confirmed by verification" in g.dropped[0]


class WriteupLLM:
    """Designs with an invented URL and path; returns the same answer on retry."""

    def __init__(self):
        self.calls = 0

    def _answer(self):
        good = DesignElement(kind="activity", name="charge", files=["orders/pay.py", "orders/nope.py"],
                             does="x", cites=["P-SAGA", "https://docs.temporal.io/made-up"])
        bad = DesignElement(kind="workflow", name="flow", files=["orders/flow.py"], does="x",
                            cites=["https://example.com/blog"])
        design = ProcessDesign(process="Order payment", elements=[good, bad], removes=[], stays=[], why="x",
                               gains=[Gain(text="Charges are never repeated", cites=["F1"]),
                                      Gain(text="Faster", cites=["F99", "https://example.com"])],
                               cites=[DOCS_URL])
        return ConversionWriteup(verdict="v", designs=[design], reasons=[], would_change=[])

    def generate_with_tools(self, prompt, response_model, system, tools, tool_handler, max_tool_calls):
        tool_handler("search_temporal_docs", {"query": "What is the saga pattern?"})
        self.calls += 1
        return self._answer(), ZERO, 1

    def generate(self, prompt, response_model, system=None, enforce_schema=False):
        assert "It failed these checks" in prompt
        self.calls += 1
        return self._answer(), ZERO


def test_process_writeup_drops_ungrounded_claims_after_one_retry(pack):
    g = Grounding.build(pack, set(FILES))
    llm = WriteupLLM()
    result = {"overall": 7.0, "suitable": True, "design": True, "verdict_class": "suitable", "gate_capped": False,
              "fit": 8, "best_path": "PATH-PIPELINE"}
    answer, _, searches, notes = write_process(
        llm, pack, g, lambda q: [{"source_url": DOCS_URL, "content": "saga"}],
        {}, result, {}, "Order payment", "(card)", "(records)", "(pack)", fact_ids={"F1", "F2"},
    )
    assert llm.calls == 2 and searches == 1 and notes == []
    design = answer.designs[0]
    assert [el.name for el in design.elements] == ["charge"]  # "flow" had no valid citation
    assert design.elements[0].files == ["orders/pay.py"] and design.elements[0].cites == ["P-SAGA"]
    assert design.cites == [DOCS_URL]  # returned by the search in this run
    assert [(x.text, x.cites) for x in design.gains] == [("Charges are never repeated", ["F1"])]


# --- end to end (fake index, store and model) ---------------------------------------------

class FakeStore:
    def __init__(self):
        self.cards = {}
        self.cache = {}

    def get_index_meta(self, repo_id):
        return {"commit_sha": "abc1234", "indexed_at": "2026-10-16T10:00:00", "lens_pack_versions": [{"version": "0.1.0", "files": 5}]}

    def query_documents(self, repo_id, collection, filter_query=None, projection=None, limit=None):
        if collection == "repo_summaries":
            return [{"lens": REPO_LENS}]
        if collection == "file_summaries":
            return [{"path": p, "lens": r, "blob_oid": f"blob-{p}"} for p, r in FILES.items()]
        return []

    def get_process_card(self, repo_id, key):
        return self.cards.get(key)

    def put_process_card(self, repo_id, doc):
        self.cards[doc["_id"]] = copy.deepcopy(doc)

    def text_search_repo(self, repo_id, collection, text, limit=8):
        return []

    def get_cached(self, repo_id, key):
        return copy.deepcopy(self.cache.get(key))

    def put_cached(self, repo_id, key, value):
        self.cache[key] = copy.deepcopy({"_id": key, **value})


class FakeLLM:
    config = type("C", (), {"model_id": "fake", "effort": "medium"})()

    def __init__(self):
        self.investigations = 0
        self.calls = 0

    def generate(self, prompt, response_model, system=None, enforce_schema=False):
        self.calls += 1
        if response_model is FileVerdicts:
            keys = [line.split(" | ")[0] for line in prompt.split("LEADS")[1].split("SOURCE:")[0].splitlines()[1:] if line]
            return FileVerdicts(verdicts=[
                LeadVerdict(lead=k, verdict="rejected" if k == "G-LATENCY" else "confirmed", line="L2",
                            severity="high", reason="r") for k in keys
            ]), ZERO
        if response_model is CardVerdicts:
            ids = re.findall(r"^(F\d+) \(", prompt, re.M)
            return CardVerdicts(verdicts=[FactVerdict(fact=i, verdict="supported", reason="r") for i in ids]), ZERO
        if response_model is ConversionScores:
            ids = re.findall(r"^((?:PATH|B)-[A-Z]+) \|", prompt, re.M)
            order = "PROCESS CARD: Order payment" in prompt

            def score(i):  # the order process is a transaction; the nightly job fits nothing
                if i.startswith("PATH-"):
                    return 8 if order and i == "PATH-TRANSACTION" else 3 if order else 2
                return 6

            return ConversionScores(bars=[
                BarScore(id=i, score=score(i), reason="r", evidence=["F1"], signals=[], cites=["X-RETRY-HAND"])
                for i in ids
            ]), ZERO
        raise AssertionError(response_model)

    def generate_with_tools(self, prompt, response_model, system, tools, tool_handler, max_tool_calls, **depth):
        self.calls += 1
        if response_model is ProcessCard:
            assert depth["min_tool_calls"] > 0 and "{calls}" in depth["nudge"]
            self.investigations += 1
            for path in ("orders/flow.py", "orders/pay.py", "jobs/nightly.py"):
                tool_handler("read_source", {"path": path, "start_line": 1, "end_line": 10})
            name = re.search(r"PROCESS: (.+)", prompt).group(1)
            if name == "Order payment":
                return ProcessCard.model_validate(card()), ZERO, 3
            c = card(process=name, files=["jobs/nightly.py"], steps=[], crash_loss=[], reliability=[],
                     trigger=fact("Cron.", ["jobs/nightly.py@L1"]), unit_of_work=fact("One job.", ["jobs/nightly.py@L1"]),
                     state_between_steps=fact(NOT_ESTABLISHED, []), persistence=fact(NOT_ESTABLISHED, []))
            return ProcessCard.model_validate(c), ZERO, 3
        tool_handler("search_temporal_docs", {"query": "q?"})
        if response_model is ConversionWriteup:
            name = re.search(r"PROCESS: (.+)", prompt).group(1)
            if "is suitable" in system:
                el = DesignElement(kind="workflow", name="OrderWorkflow", files=["orders/flow.py"], does="d", cites=[DOCS_URL])
                design = ProcessDesign(process=name, elements=[el], removes=["orders/flow.py: retry loop"],
                                       stays=[], why="w", gains=[Gain(text="Payments are not repeated", cites=["F1"])],
                                       cites=["P-SAGA"])
                return ConversionWriteup(verdict="Suitable.", designs=[design], reasons=[], would_change=[]), ZERO, 1
            return ConversionWriteup(verdict="Not a fit.", designs=[], reasons=[], would_change=["x"]), ZERO, 1
        keys = re.findall(r"^(\S+#\S+) \|", prompt, re.M)
        return EvaluationWriteup(verdict="Fix determinism.", remediations=[
            Remediation(finding=k, fix="f", cites=[]) for k in keys
        ]), ZERO, 1


def test_partial_repository_report_end_to_end(pack, rubric, tmp_path):
    index_data(tmp_path)  # writes the mirrored sources
    store, llm = FakeStore(), FakeLLM()
    search = lambda q: [{"source_url": DOCS_URL, "content": "c"}]
    out = build_report(llm, llm, pack, rubric, store, "demo", search, tmp_path)
    data, md = out.data, out.markdown
    json.dumps(data)

    procs = {p["name"]: p for p in data["conversion"]["processes"]}
    assert set(procs) == {"Order payment", "Entry point jobs/nightly.py"}  # the Temporal process is evaluated instead
    order = procs["Order payment"]
    assert (order["best_path"], order["fit"], order["overall"], order["suitable"]) == ("PATH-TRANSACTION", 8, 7.2, True)
    assert {b["id"]: b["score"] for b in order["bars"]}["B-GATES"] == 10  # G-LATENCY was rejected
    nightly = procs["Entry point jobs/nightly.py"]
    assert not nightly["suitable"] and nightly["low_confidence"]
    assert data["evaluation"]["other_findings"] == []
    assert data["pct_temporalized"]["moved_files"] == ["orders/flow.py"]

    for heading in ["## Verdict", "## Suitability for Temporal", "### Order payment", "**What a crash loses:**",
                    "## Existing Temporal code", "## Share of durable work on Temporal", "## Sources"]:
        assert heading in md
    assert "**F1** HTTP order request." in md and "> **Low confidence:**" in md

    assert order["design"]["gains"] == [{"text": "Payments are not repeated", "cites": ["F1"]}]
    assert "**What you gain:**" in md

    # A second report on the unchanged index reuses every step: no model calls, same report.
    calls = llm.calls
    again = build_report(llm, llm, pack, rubric, store, "demo", search, tmp_path)
    assert llm.calls == calls and llm.investigations == 2
    assert all(p["card_reused"] for p in again.data["conversion"]["processes"])

    def comparable(d):
        d = copy.deepcopy(d)
        d.pop("generated"); d.pop("cost_usd")
        for p in d["conversion"]["processes"]:
            p.pop("card_reused")
        return d

    assert comparable(again.data) == comparable(data)

    # --fresh runs every step again.
    build_report(llm, llm, pack, rubric, store, "demo", search, tmp_path, fresh=True)
    assert llm.investigations == 4 and llm.calls > calls


# --- the stored report (sync and report.sh) ---------------------------------------------

class ReportStore:
    def __init__(self, existing=None):
        self.doc = existing

    def get_report(self, repo_id):
        return self.doc

    def put_report(self, repo_id, doc):
        self.doc = doc


def _checks(pack, rubric):
    from semantic_ingress.preflight import PreflightResult
    return PreflightResult(pack=pack, rubric=rubric)


def test_report_is_stored_and_a_failure_keeps_the_last_good_one(pack, rubric, tmp_path, monkeypatch):
    from semantic_ingress.config import IngestionConfig
    from semantic_ingress.report import service
    from semantic_ingress.report.run import ReportResult

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    config = IngestionConfig(repo_path=tmp_path, output_dir=tmp_path, prefix="semv2", repo_id="demo")
    store = ReportStore()
    data = {"commit": "abc", "cost_usd": 0.5}
    monkeypatch.setattr(service, "build_report", lambda *a, **k: ReportResult(data, "# md", ZERO))
    service.build_and_store_report(config, _checks(pack, rubric), store, "demo")
    assert store.doc["status"] == "ok" and store.doc["data"] == data and store.doc["commit_sha"] == "abc"
    assert store.doc["rubric_version"] == rubric.version and store.doc["markdown"] == "# md"

    def fail(*a, **k):
        raise RuntimeError("docs search down")

    monkeypatch.setattr(service, "build_report", fail)
    with pytest.raises(RuntimeError):
        service.build_and_store_report(config, _checks(pack, rubric), store, "demo")
    assert store.doc["status"] == "failed" and store.doc["error"] == "docs search down"
    assert store.doc["data"] == data  # the last good report is kept


def test_cards_follow_the_entry_point_through_renames():
    from semantic_ingress.report.investigate import Candidate
    a = Candidate("Lead intake", "dags/intake.py", [], "n", "repository lens")
    renamed = Candidate("Lead intake and scoring", "dags/intake.py", [], "n", "repository lens")
    assert a.key == renamed.key
    a.shares_entry = renamed.shares_entry = True  # two processes from one entry point stay apart
    assert a.key != renamed.key


def test_incremental_repository_pass_sees_previous_processes():
    from semantic_ingress.lens.prompts import repo_user_prompt
    prompt = repo_user_prompt("t0", "rollup", "records", 0,
                              previous=[{"name": "Lead intake", "entry": "dags/intake.py", "files": ["dags/intake.py"]}],
                              changed=["dags/store.py"])
    assert "PREVIOUS PROCESSES (keep stable):\n- Lead intake | entry dags/intake.py" in prompt
    assert prompt.endswith("CHANGED FILES:\ndags/store.py")
    assert "PREVIOUS PROCESSES" not in repo_user_prompt("t0", "rollup", "records", 0)


def test_report_progress_tracks_each_process():
    from semantic_ingress.report.run import ReportProgress

    seen = []
    progress = ReportProgress(seen.append)
    progress.update(phase="processes", processes=[{"name": "A", "state": "pending"}, {"name": "B", "state": "pending"}])
    progress.process("A", "investigating")
    progress.process("A", "done")
    assert seen[-1]["phase"] == "processes" and seen[-1]["detail"] == "Investigating processes: 1 of 2 done"
    assert [p["state"] for p in seen[-1]["processes"]] == ["done", "pending"]
    assert seen[1]["processes"][0]["state"] == "investigating"   # each update is a snapshot, not a shared dict

    def broken(_state):
        raise RuntimeError("mongo down")

    ReportProgress(broken).update(phase="leads")   # a failing sink never fails the build
