"""Temporalize pieces that need no services: sources, plan claims, the design summary, the strip."""
import pytest

from temporalize import api, assess, sources


def test_urls_are_normalized_and_credentials_refused():
    assert sources.normalize_url("github.com/acme/backoffice") == "https://github.com/acme/backoffice"
    assert sources.normalize_url("git@github.com:acme/backoffice.git") == "https://github.com/acme/backoffice.git"
    assert sources.repo_name("https://github.com/acme/backoffice.git") == "backoffice"
    with pytest.raises(sources.SourceError):
        sources.normalize_url("https://user:secret@github.com/acme/x")
    with pytest.raises(sources.SourceError):
        sources.normalize_url("github.com")


def test_clone_folders_are_per_repository_and_branch():
    a = sources.clone_dir("https://github.com/acme/backoffice", "main")
    b = sources.clone_dir("https://github.com/acme/backoffice.git", "dev")
    assert a.name == "acme__backoffice__main" and b.name == "acme__backoffice__dev"


def test_tokens_go_in_a_header_never_the_url():
    args = sources._auth_args("ghp_secret")
    assert args[0] == "-c" and args[1].startswith("http.extraHeader=Authorization: Basic ")
    assert "ghp_secret" not in args[1]


PROCESS = {
    "card": {
        "trigger": {"text": "A systemd timer runs morning_par_audit.py on weekdays.", "refs": ["deploy/audit.timer@L1-5"]},
        "unit_of_work": {"text": "Not established from the code.", "refs": []},
        "steps": [{"name": "Rate notes", "does": "Rates each shift note with one Gemini call.", "effects": ["paid_call"],
                   "refs": ["morning_par_audit.py@L40-60"]}],
        "persistence": {"text": "Writes the report to a JSON file.", "refs": ["morning_par_audit.py@L90-95"]},
        "reliability": [], "waits": [],
    },
    "card_checks": [
        {"text": "A systemd timer runs morning_par_audit.py on weekdays.", "verdict": "supported"},
        {"text": "Rates each shift note with one Gemini call.", "verdict": "unclear"},
    ],
}


def test_claims_come_from_the_card_with_checked_confidence():
    claims = assess.plan_claims(PROCESS)
    assert [c["text"] for c in claims] == [
        "A systemd timer runs morning_par_audit.py on weekdays.",
        "Rate notes: Rates each shift note with one Gemini call.",
        "Writes the report to a JSON file.",
    ]  # the unestablished fact is left out
    assert [c["confidence"] for c in claims] == ["high", "medium", "medium"]
    assert all(c["status"] == "accepted" for c in claims) and claims[0]["refs"] == ["deploy/audit.timer@L1-5"]


def test_design_summary_counts_elements():
    design = {"elements": [{"kind": "workflow"}, {"kind": "activity"}, {"kind": "activity"},
                           {"kind": "schedule"}, {"kind": "schedule"}, {"kind": "schedule"}]}
    assert assess.design_summary(design) == "1 workflow, 2 activities, 3 Schedules"


def test_report_processes_skip_failures_and_keep_order():
    report = {"conversion": {"processes": [{"name": "A", "overall": 7}, {"name": "B", "error": "x"}, {"name": "C"}]}}
    assert [(p["key"], p["name"]) for p in assess.report_processes(report)] == [("p1", "A"), ("p3", "C")]


def test_the_strip_says_what_was_sent_and_when():
    run = {"status": "ready", "step": 2, "brief": {"notes": "call notes"}}
    assert api._now_text(run) == "Ready to start the assessment"
    assert api._sent(run) == []
    run["assessment"] = {"started_at": "now"}
    assert api._sent(run) == ["Source files"]          # the brief comes after Code Atlas's recommendation
    run["scope"] = {"confirmed": True}
    assert api._sent(run) == ["Source files", "brief", "notes"]


def test_the_strip_names_the_review_pass():
    run = {"status": "generating", "generate": {"stages": [
        {"id": "review-1", "state": "done"}, {"id": "review-2", "state": "running", "label": "Code Atlas review, pass 2"}]}}
    assert api._now_text(run) == "Review pass 2 of 3"


def test_scope_items_list_steps_and_failures_to_address():
    card = {**PROCESS["card"], "crash_loss": [
        {"stage": "while notes are rated", "loses": "Every rating already paid for.", "refs": ["a.py@L1-2"]},
        {"stage": "before the email", "loses": "Not established from the code.", "refs": []}]}
    items = assess.scope_items({"card": card}, also_found="The email is sent before state is saved.")
    assert [(s["id"], s["name"], s["included"]) for s in items["steps"]] == [("s1", "Rate notes", True)]
    assert [r["text"] for r in items["risks"]] == [
        "A crash while notes are rated: Every rating already paid for.", "The email is sent before state is saved."]


def test_steps_left_out_of_scope_drop_their_claims():
    claims = assess.plan_claims(PROCESS, excluded_steps={"s1"})
    assert all("Rate notes" not in c["text"] for c in claims) and len(claims) == 2


def test_a_run_keeps_the_settings_it_was_created_with(monkeypatch):
    from temporalize import settings, store

    current = {"model": "claude-opus-5-5", "anthropic_api_key": "sk-today",
               "temporal": {"mode": "cloud", "address": "ns.acct.tmprl.cloud:7233", "namespace": "ns", "api_key": "k"}}
    monkeypatch.setattr(store, "get_settings", lambda: current)
    run = {"settings": {"model": "claude-sonnet-5-5", "temporal": {"mode": "local", "address": "", "namespace": "default"}}}
    used = settings.run_settings(run)
    assert used["model"] == "claude-sonnet-5-5" and used["temporal"]["mode"] == "local"
    assert used["anthropic_api_key"] == "sk-today"            # secrets are today's
    assert settings.run_settings({})["model"] == "claude-opus-5-5"   # runs from before snapshots
