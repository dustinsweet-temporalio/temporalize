"""
Rules for screens 4 and 5: the focus areas the assessment supports, and the success
criteria suggested from it and the brief. No model calls; every suggestion says why it
is there (the finding or the brief answer it came from).

The external systems come from Code Atlas (the scope analysis names the products the
code talks to, such as ClockShark or Gmail); before the assessment, or without it, the
local scan's imports stand in.

P0 is required, P1 is nice to have (the Mutual Action Plan labels).
"""
from __future__ import annotations

FOCUS = [
    {"id": "recover", "title": "Recovering from failures"},
    {"id": "alerts", "title": "Knowing when a run fails"},
    {"id": "resume", "title": "Resuming after a crash"},
    {"id": "schedule", "title": "Scheduling"},
]
FOCUS_TITLES = {f["id"]: f["title"] for f in FOCUS}

NOT_PROVEN = "Not proven by a local run: scale, latency, and cost."

# Integration categories that call out to something that can fail: these get retries.
EXTERNAL = ("browser", "ai", "workspace", "payments", "cloud", "ssh", "messaging", "notify", "email", "http", "queue")
LOGIN = ("browser", "ssh")


# A system kind from the scope analysis -> the integration category the rules use.
SYSTEM_CATEGORY = {"web_app": "browser", "ai": "ai", "email": "email", "spreadsheet": "workspace", "storage": "cloud",
                   "chat": "messaging", "notify": "notify", "api": "http", "payments": "payments", "cloud": "cloud",
                   "ssh": "ssh", "queue": "queue", "database": "database", "other": "http"}
ORDER = ["browser", "ssh", "ai", "payments", "workspace", "email", "messaging", "notify", "http", "cloud", "queue",
         "database"]


def tools_from_systems(systems: list[dict], process_key: str | None = None) -> list[dict]:
    """The systems Code Atlas named (for one process, when given) in the shape the rules read."""
    picked = [x for x in systems if not process_key or not x.get("processes") or process_key in x["processes"]]
    tools = [{"id": x["name"].lower(), "name": x["name"], "category": SYSTEM_CATEGORY.get(x.get("kind"), "http"),
              "files": [r.split("@")[0] for r in x.get("refs") or []]} for x in picked]
    return sorted(tools, key=lambda t: ORDER.index(t["category"]) if t["category"] in ORDER else len(ORDER))


def _tools(scan: dict, systems: list[dict] | None) -> list[dict]:
    return systems if systems is not None else scan.get("integrations", [])


def _external(scan: dict, systems: list[dict] | None = None) -> list[dict]:
    """External integrations, most telling first."""
    return [t for t in _tools(scan, systems) if t.get("category") in EXTERNAL]


def _names(tools: list[dict], article: bool = False) -> str:
    """'Selenium and Gemini SDK' (the first two); with `article`, 'Selenium and the Gemini SDK'."""
    names = [t["name"] for t in tools[:2]]
    if article:
        names = [f"the {n}" if n.endswith(("SDK", "CLI", "API")) else n for n in names]
    return " and ".join(names)


def _runs(scan: dict) -> dict[str, dict]:
    return {r["id"]: r for r in scan.get("runs_today", [])}


# What a Schedule or durable timer would replace: run id -> (phrase, verb), most telling first.
SCHEDULE_FINDINGS = [
    ("polling", "sleep-based polling", "was"),
    ("crontab", "a crontab file", "was"),
    ("scheduler", "a scheduler library", "was"),
    ("celery_beat", "Celery beat", "was"),
    ("k8s_cronjob", "a Kubernetes CronJob", "was"),
    ("gha_schedule", "a scheduled GitHub Action", "was"),
    ("systemd", "systemd units", "were"),
]


def _schedule_finding(scan: dict) -> tuple[str, str] | None:
    runs = _runs(scan)
    for kind, phrase, verb in SCHEDULE_FINDINGS:
        if kind in runs:
            if kind == "systemd" and any(p.endswith(".timer") for p in runs[kind]["evidence"]):
                return "systemd timers", "were"
            return phrase, verb
    return None


def focus_options(scan: dict, systems: list[dict] | None = None) -> list[dict]:
    """The four focus areas, each with what the assessment found for it and whether it has evidence."""
    external = _external(scan, systems)
    runs = _runs(scan)
    notify = [t for t in _tools(scan, systems) if t.get("category") in ("notify", "email", "messaging")]
    schedule = _schedule_finding(scan)
    options = {
        "recover": (f"{_names(external)} found", True) if external
        else ("Retries for calls that fail", False),
        "alerts": (f"Alerts and run history; {_names(notify)} found" if notify else "Alerts and run history",
                   bool(notify or runs)),
        "resume": ("Finished steps aren't redone", bool(external or "state_files" in runs)),
        "schedule": (f"{schedule[0][0].upper()}{schedule[0][1:]} found", True) if schedule
        else ("Schedules and durable timers", False),
    }
    return [{"id": f["id"], "title": f["title"], "hint": options[f["id"]][0], "supported": options[f["id"]][1]}
            for f in FOCUS]


def _criterion(id: str, text: str, why: str, priority: str = "P0", on: bool = True) -> dict:
    return {"id": id, "text": text, "why": why, "priority": priority, "on": on, "custom": False}


def suggest_criteria(scan: dict, brief: dict, existing: list[dict] | None = None,
                     systems: list[dict] | None = None) -> list[dict]:
    """
    The suggested criteria for this assessment and brief. With `existing`, the user's choices are
    kept: on/off, priority and edited text for criteria that still apply, and custom criteria.
    """
    focus = set(brief.get("focus") or [])
    external = _external(scan, systems)
    out = [_criterion("restart", "Survives a worker restart without redoing finished work", "Always included")]
    if external:
        login = any(t["category"] in LOGIN for t in external)
        text = ("Failed logins and API calls retry automatically, visible in the Temporal UI" if login
                else "Failed API calls retry automatically, visible in the Temporal UI")
        out.append(_criterion("retries", text, f"Because {_names(external, article=True)} were found"))
    elif "recover" in focus:
        out.append(_criterion("retries", "Failed steps retry automatically, visible in the Temporal UI",
                              f'Because you picked "{FOCUS_TITLES["recover"]}"'))
    if not scan.get("tests"):
        out.append(_criterion("same_output", "Same test data produces the same report as the original",
                              "Because no tests were found"))
    else:
        n = scan["tests"]
        out.append(_criterion("existing_tests", "Existing tests pass against the Temporal version",
                              f"Because {n} test file{'' if n == 1 else 's'} {'was' if n == 1 else 'were'} found"))
    if "alerts" in focus:
        out.append(_criterion("alerts", "Failures send an alert instead of going silent",
                              f'Because you picked "{FOCUS_TITLES["alerts"]}"'))
    if "resume" in focus:
        out.append(_criterion("resume", "A crash mid-run resumes from the last finished step",
                              f'Because you picked "{FOCUS_TITLES["resume"]}"'))
    schedule = _schedule_finding(scan)
    if schedule:
        phrase, verb = schedule
        text = ("Polling loops replaced by Schedules or durable timers" if phrase == "sleep-based polling"
                else "Scheduled runs move to Temporal Schedules")
        out.append(_criterion("schedule", text, f"Because {phrase} {verb} found", priority="P1",
                              on="schedule" in focus))
    elif "schedule" in focus:
        out.append(_criterion("schedule", "Runs start from a Temporal Schedule",
                              f'Because you picked "{FOCUS_TITLES["schedule"]}"', priority="P1"))

    if not existing:
        return out
    previous = {c.get("id"): c for c in existing}
    for c in out:
        if c["id"] in previous:
            p = previous[c["id"]]
            c.update({k: p[k] for k in ("on", "priority", "text") if k in p})
    return out + [c for c in existing if c.get("custom")]
