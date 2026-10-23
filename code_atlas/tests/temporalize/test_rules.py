"""Focus options and suggested success criteria (screens 3 and 4)."""
from temporalize import rules

SCAN = {
    "tests": 0,
    "integrations": [
        {"id": "selenium", "name": "Selenium", "category": "browser", "files": ["a.py"]},
        {"id": "gemini", "name": "Gemini SDK", "category": "ai", "files": ["a.py"]},
        {"id": "ntfy", "name": "ntfy", "category": "notify", "files": ["a.py"]},
        {"id": "subprocess", "name": "subprocess", "category": "process", "files": ["a.py"]},
    ],
    "runs_today": [
        {"id": "systemd", "name": "systemd units", "evidence": ["deploy/audit.service"]},
        {"id": "polling", "name": "Sleep-based polling", "evidence": ["a.py"]},
        {"id": "state_files", "name": "JSON state files", "evidence": ["a.py"]},
    ],
}
QUIET = {"tests": 3, "integrations": [{"id": "pandas", "name": "pandas", "category": "data", "files": []}],
         "runs_today": []}


def test_focus_options_cite_the_scan():
    options = {o["id"]: o for o in rules.focus_options(SCAN)}
    assert [o["id"] for o in rules.focus_options(SCAN)] == ["recover", "alerts", "resume", "schedule"]
    assert options["recover"] == {"id": "recover", "title": "Recovering from failures",
                                  "hint": "Selenium and Gemini SDK found", "supported": True}
    assert options["schedule"]["hint"] == "Sleep-based polling found"
    assert options["resume"]["hint"] == "Finished steps aren't redone"
    assert options["alerts"]["hint"].startswith("Alerts and run history")


def test_focus_options_without_evidence():
    options = {o["id"]: o for o in rules.focus_options(QUIET)}
    assert not options["recover"]["supported"] and not options["schedule"]["supported"]
    assert options["alerts"]["hint"] == "Alerts and run history"


def test_criteria_rules_say_why():
    crit = {c["id"]: c for c in rules.suggest_criteria(SCAN, {"focus": ["recover", "alerts"]})}
    assert list(crit) == ["restart", "retries", "same_output", "alerts", "schedule"]
    assert crit["restart"]["why"] == "Always included" and crit["restart"]["priority"] == "P0"
    assert crit["retries"]["text"] == "Failed logins and API calls retry automatically, visible in the Temporal UI"
    assert crit["retries"]["why"] == "Because Selenium and the Gemini SDK were found"
    assert crit["same_output"]["why"] == "Because no tests were found"
    assert crit["alerts"]["why"] == 'Because you picked "Knowing when a run fails"'
    assert crit["schedule"] == {"id": "schedule", "text": "Polling loops replaced by Schedules or durable timers",
                                "why": "Because sleep-based polling was found", "priority": "P1", "on": False,
                                "custom": False}
    assert all(not c["custom"] for c in crit.values())


def test_criteria_follow_the_brief_and_tests():
    crit = {c["id"]: c for c in rules.suggest_criteria(QUIET, {"focus": ["resume", "schedule"]})}
    assert "retries" not in crit and "same_output" not in crit
    assert crit["existing_tests"]["why"] == "Because 3 test files were found"
    assert crit["resume"]["priority"] == "P0"
    assert crit["schedule"]["why"] == 'Because you picked "Scheduling"' and crit["schedule"]["on"]


def test_criteria_keep_the_users_choices():
    first = rules.suggest_criteria(SCAN, {"focus": ["alerts"]})
    edited = [{**c, "priority": "P1", "on": False} if c["id"] == "restart" else c for c in first]
    edited = [{**c, "text": "Same rows as the original"} if c["id"] == "same_output" else c for c in edited]
    edited.append({"id": "custom-1", "text": "Runs in under 5 minutes", "why": "Added by you", "priority": "P1",
                   "on": True, "custom": True})
    again = {c["id"]: c for c in rules.suggest_criteria(SCAN, {"focus": []}, existing=edited)}
    assert again["restart"]["priority"] == "P1" and not again["restart"]["on"]
    assert again["same_output"]["text"] == "Same rows as the original"
    assert "alerts" not in again                    # no longer picked
    assert again["custom-1"]["custom"]
    assert list(again)[-1] == "custom-1"
