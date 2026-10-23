"""
The assess-first flow and the design (screens 3 to 6), offline: the vendored Temporal skill
and the docs research, the design step and design edits, the scope analysis's systems and
variants, the plan's summaries, the rules from Code Atlas's systems, the live index counts,
the API's pick/focus/criteria/confirm sequence, and the review packet.
"""
import copy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import importlib

from temporalize import api, assess, rules, store
from temporalize.activities import index_counts
from temporalize.research import Research, read_skill, skill_files

pk = importlib.import_module("temporalize.generate.packet")   # the package also exports a packet() function

XSS = '<script>alert(1)</script>"><img src=x onerror=alert(1)>'


# --- the skill and the docs ------------------------------------------------------------

def test_the_skill_is_vendored_with_core_and_python_references():
    files = skill_files()
    assert "SKILL.md" in files and "references/python/python.md" in files and "references/core/patterns.md" in files
    assert not any(f.startswith("references/typescript") for f in files)
    overview = read_skill("SKILL.md")
    assert "Temporal" in overview and "ALWAYS PROMPT USER TO PROVIDE FEEDBACK" not in overview


def test_skill_reads_stay_inside_the_skill():
    for bad in ("../temporal-pack.yaml", "/etc/passwd", "references/../../temporal-rubric.yaml", "LICENSE"):
        assert read_skill(bad).startswith("No such skill file")


def test_research_records_reads_searches_and_urls():
    r = Research(search=lambda q: [{"source_url": "https://docs.temporal.io/schedule", "content": "Schedules..."},
                                   {"source_url": "https://community.temporal.io/x", "content": "..."}])
    assert "Temporal" in r.handle("read_temporal_skill", {"path": "references/python/python.md"})
    r.handle("read_temporal_skill", {"path": "nope.md"})
    out = r.handle("search_temporal_docs", {"query": "How do Schedules work?"})
    assert "SOURCE: https://docs.temporal.io/schedule" in out
    rec = r.record()
    assert rec["skill_read"] == ["references/python/python.md"] and rec["docs_queries"] == ["How do Schedules work?"]
    assert rec["docs_urls"][0] == "https://docs.temporal.io/schedule" and rec["docs_available"]


def test_an_unavailable_docs_search_is_recorded_not_raised():
    def down(q):
        raise RuntimeError("login expired")
    r = Research(search=down)
    assert "unavailable" in r.handle("search_temporal_docs", {"query": "q?"})
    assert r.record()["docs_available"] is False and "login expired" in r.record()["docs_error"]
    r2 = Research(search=lambda q: [], max_searches=1)
    r2.handle("search_temporal_docs", {"query": "a?"})
    assert "limit" in r2.handle("search_temporal_docs", {"query": "b?"})


# --- design -------------------------------------------------------------------------

PROCESS = {
    "key": "p1", "name": "Morning audit", "entry": "audit.py", "verdict_class": "suitable", "overall": 7.4, "suitable": True,
    "card": {"trigger": {"text": "systemd timer", "refs": ["audit.py@L1-2"]}, "unit_of_work": {"text": "One day of shifts"},
             "steps": [{"name": "login", "does": "Logs into ClockShark", "effects": ["external_call"], "refs": ["audit.py@L3-5"]},
                       {"name": "rate", "does": "Rates notes with Gemini", "effects": ["paid_call"], "refs": ["audit.py@L6"]}],
             "crash_loss": [{"stage": "while rating", "loses": "Every rating", "refs": ["audit.py@L6"]}],
             "files": ["audit.py"]},
    "design": {"elements": [{"kind": "workflow", "name": "AuditWorkflow", "files": ["audit.py"], "does": "Runs it"}],
               "removes": ["audit.py: retry loop"], "stays": [], "why": "A crash loses the ratings.", "gains": []},
}


def _run(tmp_path, **over):
    (tmp_path / "audit.py").write_text("\n".join(f"line {i}" for i in range(1, 11)))
    run = {"_id": "r-0100", "repo": {"path": str(tmp_path), "index": "no-such-index", "name": "demo"},
           "brief": {"focus": ["recover"], "notes": "It runs at 9."},
           "criteria": [{"id": "restart", "text": "Survives a restart", "priority": "P0", "on": True}],
           "scope": {"selected": "p1", "variants": [{"id": "v1", "name": "Weekday mornings", "trigger": "timer", "included": True}],
                     "systems": [{"name": "ClockShark", "kind": "web_app", "via": "Selenium login", "processes": ["p1"], "refs": []}],
                     "steps": [], "risks": []},
           "scan": {"integrations": [{"name": "Selenium", "category": "browser", "files": ["audit.py"]}], "runs_today": []}}
    run.update(over)
    return run


def _design(**over):
    base = dict(summary="One workflow runs the audit.", elements=[
        assess.DesignElement(kind="workflow", name="AuditWorkflow", does="Runs the audit", files=["audit.py", "ghost.py"]),
        assess.DesignElement(kind="schedule", name="audit-weekday-mornings", does="Mon-Fri 07:00", files=[])],
        decisions=[assess.Decision(decision="Login is its own activity", reason="It fails on its own",
                                   cites=["https://docs.temporal.io/retry", "https://made.up/url", "references/python/patterns.md"])],
        removes=["audit.py: retry loop"], stays=["The Excel layout"], why="Crashes lose ratings.", gains=["Ratings survive a crash"],
        questions=[], change_note="")
    base.update(over)
    return assess.DesignDraft(**base)


class DesignLLM:
    """Calls the tools the way a model would, then answers with a design."""

    def __init__(self, design=None, search=True):
        self.design, self.search, self.prompts, self.systems = design or _design(), search, [], []

    def structured_with_tools(self, system, prompt, model_cls, tools, handle, max_tool_calls=12, max_tokens=0):
        self.systems.append(system)
        self.prompts.append(prompt)
        assert {t["name"] for t in tools} == {"read_temporal_skill", "search_temporal_docs"}
        handle("read_temporal_skill", {"path": "references/python/patterns.md"})
        if self.search:
            handle("search_temporal_docs", {"query": "How do retry policies work?"})
        return self.design

    def structured(self, system, prompt, model_cls, max_tokens=0):
        self.prompts.append(prompt)
        return model_cls(overview="Today the audit stops on a bad login.", today_summary="It logs in, rates, mails.",
                         changes_summary="One fix.", approve_summary="Approving builds the POC and checks it.",
                         contradictions=[assess.Contradiction(note="runs at 9", code="runs at 7", refs=["audit.py@L1-1"])],
                         changes=[assess.Change(title="Fix the start date", detail="Use Thursday", refs=["audit.py@L2-2"])],
                         summary="Fake shifts", frozen_time="2026-10-19T07:00:00",
                         failures=[assess.Failure(boundary="login", times=2, detail="TimeoutException")],
                         files=[assess.DataFile(name="shifts.json", description="ClockShark export", content="[{\"id\": 1}]")])


def test_the_design_reads_the_skill_searches_the_docs_and_keeps_only_real_cites(tmp_path):
    r = Research(search=lambda q: [{"source_url": "https://docs.temporal.io/retry", "content": "..."}])
    llm = DesignLLM()
    design, record = assess.design_poc(llm, _run(tmp_path), PROCESS, {"changes": []}, research=r)
    assert "temporal-developer" in llm.systems[0] or "Temporal" in llm.systems[0]
    assert "CODE ATLAS'S DESIGN" in llm.prompts[0] and "ClockShark (web_app) via Selenium login" in llm.prompts[0]
    assert design["decisions"][0]["cites"] == ["https://docs.temporal.io/retry", "references/python/patterns.md"]
    assert design["elements"][0]["files"] == ["audit.py"]                       # unknown files dropped
    assert design["summary"] == "1 workflow, 1 Schedule" and design["summary_text"] == "One workflow runs the audit."
    assert record["skill_read"] == ["references/python/patterns.md"] and record["docs_queries"]
    assert design["questions"] == []


def test_a_design_without_docs_says_so(tmp_path):
    def down(q):
        raise RuntimeError("no sign-in")
    design, record = assess.design_poc(DesignLLM(), _run(tmp_path), PROCESS, {"changes": []}, research=Research(search=down))
    assert not record["docs_available"] and any("unavailable" in q for q in design["questions"])
    design, _ = assess.design_poc(DesignLLM(search=False), _run(tmp_path), PROCESS, {"changes": []},
                                  research=Research(search=lambda q: []))
    assert any("not checked against the Temporal docs" in q for q in design["questions"])


def test_a_design_edit_sends_the_current_design_and_the_request(tmp_path):
    llm = DesignLLM(_design(change_note="Merged the three Schedules into one."))
    previous = {"summary_text": "Old design", "elements": [], "decisions": []}
    design, _ = assess.design_poc(llm, _run(tmp_path), PROCESS, {"changes": []}, "Use one Schedule", previous,
                                  research=Research(search=lambda q: []))
    assert "THE CURRENT DESIGN (revise it)" in llm.prompts[0] and "Old design" in llm.prompts[0]
    assert "Use one Schedule" in llm.prompts[0] and design["change_note"] == "Merged the three Schedules into one."


def test_the_plan_has_plain_summaries_the_notes_check_and_the_design(tmp_path):
    plan = assess.draft_plan(DesignLLM(), _run(tmp_path), PROCESS, research=Research(search=lambda q: []))
    assert plan["overview"].startswith("Today") and plan["approve_summary"].startswith("Approving")
    assert plan["contradictions"][0]["code"] == "runs at 7" and plan["contradictions"][0]["refs"] == ["audit.py@L1-1"]
    assert plan["testdata"]["included"] is True and plan["design"]["elements"][0]["name"] == "AuditWorkflow"
    assert plan["research"]["skill_read"] == ["references/python/patterns.md"] and plan["design_edits"] == []


# --- scope analysis: recommendation, systems, variants per process ---------------------------

def test_scope_analysis_names_systems_and_finds_variants_per_process(tmp_path):
    report = {"conversion": {"processes": [
        {**PROCESS, "verdict_class": "suitable"},
        {**PROCESS, "name": "Mailer", "entry": "mail.py", "verdict_class": "not suitable", "suitable": False, "overall": 2.0}]}}

    class ScopeLLM:
        def structured(self, system, prompt, model_cls, max_tokens=0):
            assert "LIBRARIES AND COMMAND-LINE TOOLS" in prompt and "BRIEF" not in prompt
            return assess.ScopeAnalysis(
                recommendation="Build the morning audit: a crash loses every rating.", recommended="p2",
                processes=[assess.ProcessNote(key="p1", summary="Ratings are lost", also_found="")],
                systems=[assess.System(name="ClockShark", kind="web_app", via="Selenium login", processes=["p1", "p9"],
                                       refs=["audit.py@L3-5", "nope.py@L1"])],
                variants=[assess.ProcessVariants(key="p1", variants=[
                    assess.Variant(name="Weekday mornings", trigger="systemd timer", refs=["audit.py@L1-2"]),
                    assess.Variant(name="On demand", trigger="--now flag", refs=[])])])

    scope = assess.analyze_scope(ScopeLLM(), _run(tmp_path), report)
    assert scope["selected"] == "p1"            # a not-suitable recommendation falls back to the best suitable
    assert scope["systems"] == [{"name": "ClockShark", "kind": "web_app", "via": "Selenium login", "processes": ["p1"],
                                 "refs": ["audit.py@L3-5"]}]
    p1 = next(p for p in scope["processes"] if p["key"] == "p1")
    assert [v["name"] for v in p1["variants"]] == ["Weekday mornings", "On demand"] and p1["recommended"]
    assert scope["recommendation"].startswith("Build") and scope["variants"] == p1["variants"]


def test_systems_reached_several_ways_are_listed_once():
    merged = assess.merge_systems([
        {"name": "Gmail", "kind": "email", "via": "gog CLI", "processes": ["p1"], "refs": ["a.py@L1"]},
        {"name": "gmail", "kind": "email", "via": "smtplib", "processes": ["p7"], "refs": ["b.py@L2"]},
        {"name": "Gemini", "kind": "ai", "via": "google-genai SDK", "processes": ["p1"], "refs": []}])
    assert [m["name"] for m in merged] == ["Gmail", "Gemini"]
    assert merged[0]["via"] == "gog CLI; smtplib" and merged[0]["processes"] == ["p1", "p7"]
    assert merged[0]["refs"] == ["a.py@L1", "b.py@L2"]


# --- rules from Code Atlas's systems ------------------------------------------------------

def test_focus_and_criteria_name_the_systems_code_atlas_found():
    systems = rules.tools_from_systems([
        {"name": "Gmail", "kind": "email", "processes": ["p1"], "refs": ["mail.py@L3"]},
        {"name": "ClockShark", "kind": "web_app", "processes": ["p1"], "refs": []},
        {"name": "Stripe", "kind": "payments", "processes": ["p2"], "refs": []}], "p1")
    assert [t["name"] for t in systems] == ["ClockShark", "Gmail"]       # p2's system is left out; browsers first
    opts = {o["id"]: o for o in rules.focus_options({}, systems)}
    assert opts["recover"]["hint"] == "ClockShark and Gmail found" and opts["alerts"]["supported"]
    crit = {c["id"]: c for c in rules.suggest_criteria({}, {"focus": ["recover"]}, [], systems)}
    assert crit["retries"]["text"].startswith("Failed logins") and "ClockShark and Gmail" in crit["retries"]["why"]


# --- live index counts ----------------------------------------------------------------

def _progress(phase, done=0, total=0, levels=()):
    plan = SimpleNamespace(n_files=120, levels=[SimpleNamespace(count=c) for c in levels], mode="full",
                           n_snapshot_files=130, n_unchanged=0)
    return SimpleNamespace(phase=phase, done=done, total=total, plan=plan)


def test_index_counts_follow_the_sync_phases():
    assert index_counts(SimpleNamespace(phase="PLAN", plan=None, done=0, total=0)) is None
    files = index_counts(_progress("FILES", 37, 120))
    assert (files["files_done"], files["files_total"], files["folders_done"], files["folders_total"]) == (37, 120, 0, None)
    folders = index_counts(_progress("FOLDERS", 5, 14, levels=(10, 4)))
    assert (folders["files_done"], folders["folders_done"], folders["folders_total"]) == (120, 5, 14)
    done = index_counts(_progress("REPORT", levels=(10, 4)))
    assert (done["files_done"], done["folders_done"]) == (120, 14)


# --- the API: pick, focus, criteria, confirm; design edits; test data ---------------------------

class MemStore:
    def __init__(self, run):
        self.runs = {run["_id"]: run}

    def get_run(self, run_id):
        return copy.deepcopy(self.runs.get(run_id))

    def update_run(self, run_id, fields, unset=None):
        doc = self.runs[run_id]
        for path, value in fields.items():
            *parents, last = path.split(".")
            target = doc
            for part in parents:
                target = target.setdefault(part, {})
            target[last] = copy.deepcopy(value)
        return copy.deepcopy(doc)


@pytest.fixture
def client(monkeypatch, tmp_path):
    run = _run(tmp_path, status="scoping", step=3, assessment={"started_at": "t"})
    run["scope"]["processes"] = [{**PROCESS, "gated": False, "variants": run["scope"]["variants"],
                                  **assess.scope_items(PROCESS)},
                                 {"key": "p2", "name": "Mailer", "verdict_class": "not suitable", "variants": []}]
    mem = MemStore(run)
    monkeypatch.setattr(store, "get_run", mem.get_run)
    monkeypatch.setattr(store, "update_run", mem.update_run)
    monkeypatch.setattr(store, "index_calls", lambda index: 0)
    monkeypatch.setattr(api, "view", lambda r: r)
    signals = []

    async def signal(run_id, name):
        signals.append(name)
    monkeypatch.setattr(api, "_signal", signal)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app), mem, signals


def test_the_user_picks_after_the_recommendation_then_focuses_then_confirms(client):
    c, mem, signals = client
    assert c.put("/api/tz/runs/r-0100/selection", json={"selected": "p2", "variants": []}).status_code == 409
    assert c.post("/api/tz/runs/r-0100/scope").status_code == 400                               # nothing picked yet
    r = c.put("/api/tz/runs/r-0100/selection", json={"selected": "p1", "variants": ["v1"], "notes": "Stub email"})
    assert r.status_code == 200, r.text
    run = mem.runs["r-0100"]
    assert run["step"] == 4 and run["scope"]["picked"] and run["scope"]["notes"] == "Stub email"
    assert next(o for o in run["focus_options"] if o["id"] == "recover")["hint"] == "ClockShark found"
    c.put("/api/tz/runs/r-0100/brief", json={"focus": ["recover", "alerts"], "notes": "Managers get no report"})
    assert mem.runs["r-0100"]["step"] == 5 and any(x["id"] == "alerts" for x in mem.runs["r-0100"]["criteria"])
    r = c.post("/api/tz/runs/r-0100/scope")
    assert r.status_code == 200, r.text
    run = mem.runs["r-0100"]
    assert run["scope"]["confirmed"] and run["status"] == "planning" and signals == ["scope_confirmed"]
    assert c.put("/api/tz/runs/r-0100/brief", json={"focus": ["recover"]}).status_code == 409   # locked once confirmed


def test_design_edits_and_the_test_data_checkbox(client):
    c, mem, signals = client
    run = mem.runs["r-0100"]
    run.update(status="plan_ready", plan={"status": "ready", "claims": [{"id": "c1", "text": "x", "status": "accepted"}],
                                          "changes": [], "testdata": {"included": True, "files": []},
                                          "design": {"elements": []}, "approved": {}})
    r = c.post("/api/tz/runs/r-0100/design-edit", json={"request": "Use one Schedule"})
    assert r.status_code == 200 and signals == ["design_edit_requested"]
    c.post("/api/tz/runs/r-0100/design-edit", json={"request": "Alert through Slack"})
    assert mem.runs["r-0100"]["plan"]["design_edit"]["request"] == "Use one Schedule\n\nAlert through Slack"
    assert c.put("/api/tz/runs/r-0100/plan", json={"approve_all": True}).status_code == 409    # still revising
    mem.runs["r-0100"]["plan"]["design_edit"] = {"status": "done"}
    c.put("/api/tz/runs/r-0100/plan", json={"testdata_included": False})
    r = c.put("/api/tz/runs/r-0100/plan", json={"approve_all": True})
    assert r.status_code == 400 and "test data" in r.json()["error"]
    c.put("/api/tz/runs/r-0100/plan", json={"testdata_included": True})
    assert c.put("/api/tz/runs/r-0100/plan", json={"approve_all": True}).status_code == 200


# --- the review packet ------------------------------------------------------------------

def _done_run(tmp_path):
    run = _run(tmp_path, status="done", calls=83, settings={"model": "claude-sonnet-5-5"},
               source={"kind": "git", "url": "https://github.com/acme/backoffice"})
    run["scope"]["processes"] = [{**PROCESS}, {"key": "p2", "name": f"Coast pipeline {XSS}", "verdict_class": "suitable",
                                               "summary": "Polls Gmail"}]
    run["plan"] = {"changes": [{"title": "Fix the Monday start date", "detail": "Thursday", "decision": "approve"}],
                   "testdata": {"frozen_time": "2026-10-19T07:00:00", "failures": [{"boundary": "clockshark_login", "times": 2}],
                                "files": [{"name": "shifts.json", "content": '[{"date": "Oct 15", "employee": "J. Ortiz", "note": "%s"}]' % XSS.replace('"', "'")}]},
                   "research": {"skill_read": ["references/python/patterns.md"], "docs_queries": ["q"], "docs_available": True}}
    run["generate"] = {"branch": "temporalize/r-0100", "finished_at": "2026-10-19T09:00:00"}
    run["results"] = {
        "outcomes": [{"label": "Runnable evaluation", "ok": True}, {"label": "Customer integration not tested", "ok": None}],
        "criteria": [{"text": "Survives a restart", "priority": "P0", "passed": True, "evidence": "Resumed at rating 19"}],
        "comparison": [{"check": "Report rows", "original": "42", "temporal": "42", "result": "match"},
                       {"check": "Start date", "original": "Saturday", "temporal": "Thursday", "result": "intended"}],
        "diff_stats": {"files": 7, "insertions": 430, "deletions": 610}, "review_passes": 2,
        "changed_files": [{"path": "audit.py", "status": "M"}, {"path": "temporal-poc/tests/test_audit.py", "status": "A"}],
        "limitations": ["Stub mode."], "changes_summary": "1 original file changed.",
        "narrative": {"title": f"Timesheet audit on Temporal {XSS}", "lede": "It now runs on Temporal.", "problem": "It fails silently.",
                      "use_case": [{"label": "Process", "value": "Timesheet audit", "source": "brief"}],
                      "before_after": [{"before": "Cron", "after": "Schedules"}],
                      "standard": [{"id": "cancellation", "check": "Cancellation", "status": "open", "note": "Not shown"}],
                      "standard_version": "draft v0", "not_proven": ["Scale"], "validate_next": "Use a ClockShark test account."},
    }
    return run


def test_the_packet_view_has_every_section_from_the_run(tmp_path):
    v = pk.packet_view(_done_run(tmp_path))
    facts = dict(v["facts"])
    assert v["for"] == "acme" and facts["Model calls"] == "83" and facts["Review passes"] == "2 of 3"
    assert facts["Ran against"] == "Stubs, local dev server" and facts["Model"] == "Claude Sonnet"
    assert (v["changed"]["entry_points"], v["changed"]["removed"], v["changed"]["added"], v["changed"]["tests"]) == (1, 610, 430, 1)
    assert v["test_data"]["sample"]["columns"] == ["date", "employee", "note"] and v["test_data"]["sample"]["total"] == 1
    scenario = {s["label"]: s["value"] for s in v["test_data"]["scenario"]}
    assert "fails 2 times first" in scenario["ClockShark"] and scenario["Date"].startswith("Frozen to")
    assert v["next_steps"][0]["text"] == "Use a ClockShark test account." and v["next_steps"][1]["verdict"] == "Suitable"
    assert v["intended"][0]["title"] == "Fix the Monday start date" and "Scale" in v["not_proven"]


def test_the_packet_page_escapes_everything_it_shows(tmp_path):
    page = pk.packet_html(_done_run(tmp_path))
    assert "<script>" not in page and "<img" not in page and "&lt;script&gt;" in page
    for section in ("The use case", "Scorecard", "Side by side", "What changed", "POC Standard", "Test data",
                    "Limits and what’s next", "Run it yourself"):
        assert section in page
    assert "Intended fix, approved before generation" in page and "temporal-developer skill" in page


def test_without_a_narrative_the_packet_falls_back_to_the_run(tmp_path):
    run = _done_run(tmp_path)
    run["results"].pop("narrative")
    v = pk.packet_view(run)
    assert v["narrative"]["title"] == "Morning audit on Temporal" and v["narrative"]["source"] == "fallback"
    assert all(s["status"] == "open" for s in v["standard"]) and len(v["standard"]) == len(pk.POC_STANDARD)
