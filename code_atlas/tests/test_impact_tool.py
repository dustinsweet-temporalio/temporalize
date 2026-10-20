"""The chat tool get_business_impact (tools/report/get_business_impact.py) over a stored impact."""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
module = importlib.import_module("tools.report.get_business_impact")  # the package re-exports the tool by this name

DOC = {"status": "ok", "source_report": {"generated_at": "t1", "commit_sha": "abc"},
       "markdown": {"exec": "# Business impact: demo", "champion": "# Champion", "discovery": "# Discovery"},
       "data": {"processes": []}}


def call(view="exec"):
    return module.get_business_impact._tool_func("demo", view)


def test_returns_the_requested_view(monkeypatch):
    monkeypatch.setattr(module, "load_impact", lambda repo: (DOC, {"generated_at": "t1", "commit_sha": "abc"}))
    assert call("discovery") == "# Discovery" and call("nonsense") == "# Business impact: demo"


def test_says_when_stale_or_missing(monkeypatch):
    monkeypatch.setattr(module, "load_impact", lambda repo: (DOC, {"generated_at": "t2", "commit_sha": "abc"}))
    assert call().startswith("Note: the Temporal report was rebuilt")
    monkeypatch.setattr(module, "load_impact", lambda repo: (None, None))
    assert "No business impact has been generated" in call()
