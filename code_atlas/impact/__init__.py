"""
Business impact on Temporal: one BusinessImpactWorkflow per run, on the impact-worker.

    constants.py   names, error types, retry policy and timeouts (sandbox-safe)
    models.py      payload dataclasses: ids, keys, counts and statuses only
    workflow.py    BusinessImpactWorkflow: prepare -> processes (4 at a time) -> synthesize
    activities.py  ImpactActivities: the steps of semantic_ingress.report.impact.service
    worker.py      the impact-worker service (python -m impact.worker)
    runner.py      how the app starts, follows and cancels runs (impact_api.py)

The customer context stays in MongoDB (the request document); only the repository
and request ids enter workflow history.
"""
