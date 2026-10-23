"""
Temporalize: from a repository to a running Temporal POC with evidence, on Code Atlas.

    api.py         /api/tz/* (screens 1-8), mounted by app.py
    settings.py    the Anthropic key and model, the Temporal target (stored in MongoDB)
    sources.py     the repository: a git clone (in the tz-data volume) or a local folder
    scan.py        the local scan (screen 2): no model calls
    rules.py       focus options and success criteria from the scan (screens 3-4)
    assess.py      scope analysis and the plan draft (screens 5-6): model calls
    generate/      workspace, coding agent, checks, review, scenarios, results (screens 7-8)
    workflow.py    TemporalizeWorkflow (tz-<run>): screens 5-7 as one durable run
    activities.py  its activities; worker.py runs them (python -m temporalize.worker)
    store.py       runs and settings in MongoDB (code_atlas_temporalize)

See docs/TEMPORALIZE.md.
"""
