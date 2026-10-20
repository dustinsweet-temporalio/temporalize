"""
Business impact: the three rendered views (golden files) and runs on stored
requests (report/impact/service.py). Offline: fake store and fake model.

Regenerate the golden files after an intended change with UPDATE_GOLDEN=1, and review the diff.
"""
import os
import threading
from pathlib import Path

import pytest

from semantic_ingress.report.impact.context import ImpactContext
from semantic_ingress.report.impact.mapping import load_mapping
from semantic_ingress.report.impact.render import VIEWS, cost_range, money, render
from semantic_ingress.report.impact.service import (
    CANCELLED,
    REPORT_CHANGED,
    RequestError,
    RunCancelled,
    new_request,
    prepare_request,
    run_request,
    translate_request_process,
)
from semantic_ingress.report.impact.translate import ImpactUnavailable, build_business_impact
from tests.impact_fixtures import (
    FakeLLM,
    FakeStore,
    cost_input,
    draft,
    process,
    report_doc,
    standard_processes,
)
from tests.test_impact import CONTEXT

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge"
GOLDEN = Path(__file__).resolve().parent / "golden" / "impact"


@pytest.fixture(scope="module")
def mapping():
    return load_mapping(KNOWLEDGE / "business-impact.yaml")


def qualitative(mapping):
    return build_business_impact(FakeLLM(), FakeStore(report_doc(standard_processes())), "demo", ImpactContext(),
                                 mapping).data


def with_cost(mapping):
    answer = draft(cost_inputs=[cost_input(cite="C3"), cost_input("value_per_incident", 250, 250, "C5")])
    llm = FakeLLM(script={"Order payment": [answer]})
    return build_business_impact(llm, FakeStore(report_doc(standard_processes())), "demo", CONTEXT, mapping).data


# --- rendering ------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["qualitative", "with_cost"])
@pytest.mark.parametrize("view", VIEWS)
def test_views_match_the_golden_files(mapping, case, view):
    data = {"qualitative": qualitative, "with_cost": with_cost}[case](mapping)
    text = render(data)[view]
    path = GOLDEN / f"{view}_{case}.md"
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert text == path.read_text()


def test_no_currency_figures_without_context(mapping):
    views = render(qualitative(mapping))
    for view in ("exec", "champion"):
        assert "USD" not in views[view] and "$" not in views[view]
    assert "## Missing inputs for cost estimates" in views["discovery"]
    assert "None given. The briefs are qualitative only." in views["discovery"]


def test_cost_shows_formula_inputs_and_defaults(mapping):
    exec_view = render(with_cost(mapping))["exec"]
    assert "**Illustrative cost: 100 USD to 500 USD per month**" in exec_view
    assert "`incidents_per_month * value_per_incident * share_unrecovered`" in exec_view
    assert "| share unrecovered | 0.1 to 0.5 | fraction | default (placeholder) |" in exec_view
    assert "| incidents per month | 4 | incidents/month | context C3 |" in exec_view
    assert "Illustrative estimate from the stated inputs. Not a prediction." in exec_view


def test_money_formatting():
    assert money(1234.5, "USD") == "1,234 USD" and money(2.5, "$") == "$2.5" and money(5.76, "€") == "€5.76"
    assert cost_range({"low": 570, "high": 570, "currency": "USD"}) == "570 USD per month"


def test_model_text_is_kept_on_one_line(mapping):
    data = qualitative(mapping)
    data["processes"][0]["stories"][0]["scenario"] = "Line one\n## Injected heading\nline three"
    assert "\n## Injected heading" not in render(data)["exec"]


# --- runs on stored requests -------------------------------------------------------------

def test_a_request_needs_a_translatable_report():
    with pytest.raises(ImpactUnavailable, match="Build the Temporal report first"):
        new_request(FakeStore(None), "demo", ImpactContext(), False, "inprocess")
    with pytest.raises(ImpactUnavailable, match="already uses Temporal"):
        new_request(FakeStore(report_doc(None, status="full")), "demo", ImpactContext(), False, "inprocess")


def test_a_run_records_results_and_stores_the_impact(mapping):
    store, llm = FakeStore(report_doc(standard_processes())), FakeLLM()
    request = new_request(store, "demo", CONTEXT, False, "inprocess")
    assert request["context"]["industry"] == "Mid-market fintech" and request["status"] == "running"
    result = run_request(llm, store, "demo", request["_id"], mapping)
    done = store.requests[request["_id"]]
    assert done["status"] == "ok" and done["phase"] == "DONE" and done["total"] == 4
    assert {r["status"] for r in done["results"].values()} == {"ok", "not_a_fit", "no_consequence"}
    impact = store.impact
    assert impact["status"] == "ok" and impact["request_id"] == request["_id"]
    assert set(impact["markdown"]) == set(VIEWS) and impact["data"] == result.data
    assert impact["context_hash"] == CONTEXT.hash and impact["mapping_version"] == "1.0.0"
    assert impact["source_report"]["generated_at"] == "2026-10-20T10:00:00+00:00"
    assert result.data["model_calls"] == 3 and llm.calls == 3


def test_a_failed_run_keeps_the_last_good_impact(mapping, monkeypatch):
    store = FakeStore(report_doc(standard_processes()))
    first = new_request(store, "demo", ImpactContext(), False, "inprocess")
    run_request(FakeLLM(), store, "demo", first["_id"], mapping)
    good = store.impact["data"]

    from semantic_ingress.report.impact import service

    def broken(*a, **k):
        raise RuntimeError("summary store down")

    monkeypatch.setattr(service, "synthesize", broken)
    second = new_request(store, "demo", ImpactContext(), False, "inprocess")
    with pytest.raises(RuntimeError):
        run_request(FakeLLM(), store, "demo", second["_id"], mapping)
    assert store.requests[second["_id"]]["status"] == "failed"
    assert store.impact["status"] == "failed" and store.impact["error"] == "summary store down"
    assert store.impact["data"] == good


def test_a_report_rebuilt_mid_run_fails_the_request(mapping):
    store = FakeStore(report_doc(standard_processes()))
    request = new_request(store, "demo", ImpactContext(), False, "inprocess")
    keys = prepare_request(store, "demo", request["_id"])
    store.doc = report_doc(standard_processes(), generated_at="2026-10-21T09:00:00+00:00")
    with pytest.raises(RequestError, match="rebuilt"):
        translate_request_process(FakeLLM(), store, "demo", request["_id"], keys[0], mapping)
    with pytest.raises(RequestError):
        run_request(FakeLLM(), store, "demo", request["_id"], mapping)
    assert store.requests[request["_id"]]["error"] == REPORT_CHANGED


def test_a_cancelled_run_leaves_the_impact_alone(mapping):
    store = FakeStore(report_doc(standard_processes()))
    request = new_request(store, "demo", ImpactContext(), False, "inprocess")
    stop = threading.Event()
    stop.set()
    llm = FakeLLM()
    with pytest.raises(RunCancelled):
        run_request(llm, store, "demo", request["_id"], mapping, stop=stop)
    assert store.requests[request["_id"]]["status"] == CANCELLED and store.impact is None and llm.calls == 0


def test_customer_context_is_never_logged(mapping, capsys, caplog):
    secret = ImpactContext(text="Acme Payroll loses 7 payroll batches a month; CFO Jane Roe is furious.",
                           pain_points="Board escalation about missed salaries.")
    store = FakeStore(report_doc(standard_processes()))
    request = new_request(store, "demo", secret, False, "inprocess")
    run_request(FakeLLM(), store, "demo", request["_id"], mapping)
    out = capsys.readouterr()
    logged = out.out + out.err + caplog.text
    for fragment in ("Acme Payroll", "Jane Roe", "Board escalation"):
        assert fragment not in logged
