# Temporalize

Temporalize takes an AE or SA from a repository to a running Temporal proof of concept (POC) with evidence, in eight screens. It is built on Code Atlas: Code Atlas indexes the repository and writes the Temporal report, and Temporalize turns the report into a scoped plan, generated code, validation runs and a review packet. The Temporalize UI is served at `/`. The original Code Atlas UI is at `/atlas`, linked from the Temporalize header.

| Screen | Job | Model calls |
| --- | --- | --- |
| 1 Setup | Anthropic key and model, Temporal target, repository (git URL + branch [+ token], or a local folder) | none |
| 2 What we found | Clone, check the setup and scan locally (checklist while it runs), then review the findings and exclusions | none |
| 3 Focus | Process in mind or "find opportunities", one or two focus areas, pasted notes; brief import/export | none |
| 4 Success | Criteria picked by rules, P0/P1, custom criteria. **Start assessment** is the first point where data leaves the machine | none |
| 5 Report and scope | Code Atlas indexes and builds the Temporal report; the processes, verdicts, variants; confirm scope | yes |
| 6 Behavior and design | Approve behavior claims, intentional changes, test data and the design | yes |
| 7 Generate and validate | Copy, generate, checks, up to 3 review passes, side-by-side, resilience, replay, smoke | yes |
| 8 Results | Outcomes, comparison, criteria, diff, review packet, push to GitHub, Temporal UI | none |

The status strip on every screen shows what is running now, how many model calls the run has made (indexing calls counted by the indexer, plus the run's own), and what has been sent (nothing until **Start assessment**; then source files and the brief).

## How it runs

```
browser (web/tz) ──► app /api/tz/*  ──► MongoDB code_atlas_temporalize (settings, runs)
                        │  clone + local scan (screens 1-4, in the app, no model calls)
                        │  Start assessment:
                        ├──► SyncWorkflow sync-<index>   (index-worker; memo carries model, exclusions)
                        └──► TemporalizeWorkflow tz-<run> (tz-worker)
                                wait for index + report ─► analyze scope (model)
                                ─► wait for signal scope_confirmed ─► draft plan (model)
                                ─► wait for signal plan_approved
                                ─► prepare workspace ─► generate (agent) ─► checks (+fix)
                                ─► review passes ≤3 (+fix) ─► scenarios (+fix) ─► results
```

A run that fails (a model step that keeps failing, an index that does not finish) can be tried again from the screen it stopped on: the workflow restarts with `resume`, skips what the run document says is done, and waits for the same decisions only if they were not made yet.

Clones live in the `tz-data` volume at `/data/tz/clones/<owner>__<repo>__<branch>`; workspaces (the POC branch `temporalize/<run>`) at `/data/tz/workspaces/<run>`. Both paths are the same in the app, the index-worker and the tz-worker.

## Run document (GET /api/tz/runs/{id})

The UI renders only from this document; it polls it while something runs. Fields the UI does not need (the git token, the API key) are never returned.

```jsonc
{
  "id": "r-0001", "created_at": "...", "updated_at": "...",
  "step": 2,                       // furthest screen reached (1-8)
  "status": "scanning",            // scanning | scan_failed | ready | assessing | scoping | not_suitable
                                   // | planning | plan_ready | generating | done | failed | stopped
  "error": null,
  "source": {"kind": "git", "url": "https://github.com/acme/backoffice", "branch": "main", "token_set": true}
         // or {"kind": "local", "path": "/Users/me/code/backoffice"}
  "repo": {"path": "/data/tz/clones/acme__backoffice__main", "name": "backoffice", "index": "backoffice",
           "commit": "abc1234…", "branch": "main", "index_exists": false},
  "checks": [ {"id": "temporal", "label": "Connected to Temporal dev server", "detail": "localhost:7233",
               "state": "done", "error": null}, ... ],          // screen 2 loading checklist
  "scan": { ... see "Scan result" ... },
  "focus_options": [ {"id": "recover", "title": "Recovering from failures", "hint": "Selenium and Gemini SDK found",
                      "supported": true} ],
  "brief": {"mode": "process", "process": "The morning timesheet audit", "focus": ["recover", "alerts"], "notes": ""},
  "criteria": [ {"id": "restart", "text": "...", "why": "Always included", "priority": "P0", "on": true, "custom": false} ],
  "assessment": {"started_at": "...", "workflow_id": "tz-r-0001", "sync_workflow_id": "sync-backoffice",
                 "calls_base": 0,
                 "stages": [ {"id": "index", "label": "Indexed files", "detail": "19 of 19", "state": "done"},
                             {"id": "report", "label": "Built the Temporal report", "detail": "3 processes found", "state": "done"},
                             {"id": "variants", "label": "Found how it runs", "detail": "3 variants", "state": "done"} ]},
  "scope": {
    "processes": [ {"key": "p1", "name": "...", "overall": 7.6, "verdict_class": "suitable", "suitable": true,
                    "summary": "...", "crash_loss": "...", "also_found": "...", "matches_brief": true,
                    "entry": "path.py", "files": ["..."], "reasons": ["..."], "gated": false} ],
    "selected": "p1",
    "variants": [ {"id": "v1", "name": "Weekday mornings", "trigger": "...", "refs": ["path@L1-9"], "included": true} ],
    "contradictions": [ {"note": "...", "code": "...", "refs": []} ],
    "in_scope": [ {"id": "...", "label": "..."} ],
    "borderline_confirmed": false, "confirmed": false
  },
  "plan": {
    "status": "drafting",            // drafting | ready | failed
    "claims":  [ {"id": "c1", "text": "...", "refs": ["a.py@L10-20"], "confidence": "high", "status": "pending",
                  "edited_text": null} ],                   // status: pending | accepted | edited | rejected
    "changes": [ {"id": "x1", "title": "Fix the Monday start date", "detail": "...", "refs": [], "decision": "pending"} ],
                                                            // decision: pending | approve | decline
    "testdata": {"summary": "Fake shifts, frozen Monday, 2 failed logins", "frozen_time": "2026-10-19T07:00:00",
                 "failures": [ {"boundary": "login", "times": 2, "detail": "..."} ],
                 "files": [ {"name": "shifts.json", "description": "...", "content": "..."} ]},
    "design":  {"summary": "1 workflow, 3 Schedules", "elements": [ {"kind": "workflow", "name": "...", "does": "..."} ],
                "removes": ["..."], "stays": ["..."], "why": "..."},
    "approved": {"claims": false, "changes": false, "testdata": false, "design": false}
  },
  "generate": {
    "branch": "temporalize/r-0001", "workspace": "/data/tz/workspaces/r-0001", "started_at": "...",
    "stages": [ {"id": "copy", "label": "Copied repo to temporalize/r-0001", "state": "done", "detail": ""}, ... ],
    "review_passes": [ {"n": 1, "findings": [ {"severity": "high", "title": "...", "detail": "...", "file": "..."} ],
                        "fixed": 4} ],
    "smoke": {"workflow_id": "...", "url": "..."}
  },
  "results": {
    "outcomes": [ {"id": "runnable", "label": "Runnable evaluation", "ok": true},
                  {"id": "resilience", "label": "Resilience demonstrated", "ok": true},
                  {"id": "integration", "label": "Customer integration not tested", "ok": null} ],
    "comparison": [ {"check": "Report rows", "original": "42", "temporal": "42", "result": "match"} ],
                  // result: match | intended | new | mismatch
    "criteria": [ {"id": "...", "text": "...", "priority": "P0", "passed": true, "evidence": "..."} ],
    "criteria_summary": "4 of 4 P0 passed. 1 P1 not selected.",
    "changes_summary": "...", "diff_stats": {"files": 7, "insertions": 400, "deletions": 20},
    "limitations": ["..."], "leftover_findings": [ ... ], "review_passes": 2,
    "temporal_ui_url": "http://localhost:8233/namespaces/default/workflows/..."
  },
  "strip": {"now": "Scanning locally", "calls": 0, "sent": []},   // sent [] shows "Nothing"
  "links": {"atlas": "/atlas", "temporal_ui": "http://localhost:8233/namespaces/default/workflows/tz-r-0001"}
}
```

## API

| Method and path | Body | Does |
| --- | --- | --- |
| GET `/api/tz/settings` | | `{anthropic: {configured, source: "saved"\|"env"\|null, last4}, model, models: [{id,label}], temporal: {mode, address, namespace, api_key_set, ui_url}, saved}` |
| POST `/api/tz/settings/check-key` | `{api_key?, model}` | Checks the key (token counting, free). Blank key: checks the saved or .env key. `{ok, error?, last4?}` |
| PUT `/api/tz/settings` | `{api_key?, model, temporal: {mode, address?, namespace?, api_key?}}` | Saves; returns the settings view |
| GET `/api/tz/runs` | | Recent runs `{runs: [...]}` |
| POST `/api/tz/runs` | `{source: {kind: "git", url, branch, token?} \| {kind: "local", path}}` | Creates a run and starts the clone and local scan in the background; returns the run |
| GET `/api/tz/runs/{id}` | | The run document above |
| POST `/api/tz/runs/{id}/rescan` | `{exclude?: {patterns: [...], folders: [...]}}` | Runs the checklist and scan again (after a failure or an exclusion edit) |
| PUT `/api/tz/runs/{id}/brief` | `{mode, process, focus, notes}` | Saves the brief and refreshes the suggested criteria (keeps the user's choices) |
| PUT `/api/tz/runs/{id}/criteria` | `{criteria: [...]}` | Saves the criteria |
| GET `/api/tz/runs/{id}/brief.json` | | The brief and criteria as a file (export) |
| POST `/api/tz/runs/{id}/start` | | Start assessment: starts the index sync and the run workflow |
| POST `/api/tz/runs/{id}/scope` | `{selected, variants: [ids included], in_scope: [ids], borderline_confirmed}` | Confirms scope (signal) |
| PUT `/api/tz/runs/{id}/plan` | `{claims?: [{id, status, edited_text?}], changes?: [{id, decision}], testdata_files?: [{name, content}], approve?: "claims"\|"changes"\|"testdata"\|"design", reopen?: section}` | Saves one part's edits / approval |
| POST `/api/tz/runs/{id}/approve` | | All four parts approved: generate (signal) |
| GET `/api/tz/runs/{id}/diff` | | Unified diff of the POC branch (text) |
| GET `/api/tz/runs/{id}/file?path=` | | One file of the workspace (text) |
| GET `/api/tz/runs/{id}/packet` | | The review packet (zip) |
| POST `/api/tz/runs/{id}/push` | `{remote_url?, token}` | Pushes `temporalize/<run>` to GitHub |
| POST `/api/tz/runs/{id}/retry` | | Tries a failed or stopped run again from where it got to: decisions and a drafted plan are kept; generation starts over from a fresh workspace |
| DELETE `/api/tz/runs/{id}` | | Stops the run (cancels its workflow) |
| GET `/api/fs?path=` | | The existing folder picker (local folder mode) |

## Scan result (`run.scan`)

```jsonc
{
  "files": 19, "python_files": 19, "lines": 5012, "tests": 0, "secrets": 0,
  "secret_findings": [ {"path": "a.py", "line": 12, "type": "AWS Access Key"} ],   // values never stored
  "languages": [ {"name": "Python", "files": 19} ],
  "integrations": [ {"id": "selenium", "name": "Selenium", "category": "browser", "files": ["a.py"]} ],
  "runs_today": [ {"id": "systemd", "name": "systemd units", "evidence": ["deploy/audit.service"]} ],
  "not_found": ["a crontab file"],
  "top_folders": [ {"path": "clockshark_audit", "files": 7} ],
  "entry_points": [ {"path": "morning_par_audit.py", "kind": "__main__"} ],
  "exclusions": {"patterns": ["*.csv", ...], "folders": [], "max_file_bytes": 1000000,
                 "summary": ".git, __pycache__, data files, files over 1 MB"}
}
```

## Validation harness

The generated POC lives in `temporal-poc/` of the workspace. Temporalize writes `temporal-poc/tz_harness.py` (fixed), `temporal-poc/testdata/` (the approved test data) and `temporal-poc/PLAN.md` (the approved plan); the generator writes the rest, including `temporal-poc/validate.py`, which runs one scenario per call:

```
python temporal-poc/validate.py <side_by_side|resilience|replay|smoke> --out <file.json>
```

Each scenario writes `{"ok": bool, "checks": [{"check", "original", "temporal", "result", "detail"}], "criteria": [{"id", "passed", "evidence"}], "workflow_ids": [...], "notes": "..."}`. The workflows run on the Temporal target from the settings (`TZ_TEMPORAL_ADDRESS`, `TZ_TEMPORAL_NAMESPACE`, `TZ_TEMPORAL_API_KEY`), with every external boundary stubbed from the test data (stub mode), so nothing reaches the customer's systems.
