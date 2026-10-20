"""
web/impact.js renders model-written text escaped: its views are driven in node with a
minimal DOM that records every innerHTML assignment, over an impact whose model text,
process names and context carry script and onerror payloads. Skipped without node.
"""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
XSS = '<script>alert(1)</script> "><img src=x onerror=alert(1)>'

_spec = importlib.util.spec_from_file_location("impact_fixtures", ROOT / "semantic_ingress" / "tests" / "impact_fixtures.py")
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)

SHIM = r"""
const fs = require("fs");
const recorded = [];
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this._html = ""; this.className = "";
    this.dataset = {}; this.style = { setProperty() {} }; this.attrs = {}; }
  set innerHTML(v) { recorded.push(String(v)); this._html = String(v); this.children = []; }
  get innerHTML() { return this._html + this.children.map((c) => (c instanceof El ? c.outerHTML : esc(c))).join(""); }
  set textContent(v) { this._html = esc(v); this.children = []; }
  get outerHTML() { return `<${this.tagName.toLowerCase()}>${this.innerHTML}</${this.tagName.toLowerCase()}>`; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(n) { this.children.push(n); return n; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  scrollIntoView() {}
  *walk() { for (const c of this.children) if (c instanceof El) { yield c; yield* c.walk(); } }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const match = sel.startsWith(".") ? (e) => e.className.split(" ").includes(sel.slice(1)) : (e) => e.tagName === sel.toUpperCase();
    return [...this.walk()].filter(match);
  }
}
global.document = { createElement: (t) => new El(t), body: new El("body") };
global.window = global;
eval(fs.readFileSync(process.argv[2], "utf8"));
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
for (const view of ["exec", "champion", "discovery"]) {
  const root = new El("div");
  window.BusinessImpact.renderViews(root, data, view, { onDiscuss() {} });
  out[view] = root.innerHTML;
}
console.log(JSON.stringify({ recorded, out }));
"""


def hostile_impact() -> dict:
    from semantic_ingress.report.impact.context import ImpactContext
    from semantic_ingress.report.impact.mapping import load_mapping
    from semantic_ingress.report.impact.models import DiscoveryQuestion, Workaround
    from semantic_ingress.report.impact.translate import build_business_impact

    name = f"Order payment {XSS}"
    p = fx.process(name)
    p["card"]["crash_loss"][0]["loses"] = f"The shipment {XSS}"
    p["design"]["gains"][0]["text"] = f"Gain {XSS}"
    answer = fx.draft(
        name,
        stories=[fx.story(scenario=f"If the server restarts {XSS} the order is lost.")],
        workaround=Workaround(text=f"By hand {XSS}", basis="assumption", cites=[]),
        case_reason=f"Customers pay twice {XSS}",
        questions=[DiscoveryQuestion(question=f"Who notices {XSS}?", why=f"Speed {XSS}", fills="detection_delay",
                                     ask="support")],
    )
    llm = fx.FakeLLM(script={name: [answer]},
                     summaries=[fx.summary([name], headline=f"Headline {XSS}", exec_summary=f"Summary {XSS}")])
    store = fx.FakeStore(fx.report_doc([p, fx.process(f"Mailer {XSS}", "not suitable", 3.0)]))
    data = build_business_impact(llm, store, "demo", ImpactContext(text=f"Notes {XSS}"),
                                 load_mapping(ROOT / "knowledge" / "business-impact.yaml")).data
    data["dropped"].append(f"Dropped {XSS}")
    return data


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_model_text_is_escaped_in_every_view(tmp_path):
    data = hostile_impact()
    assert XSS in data["processes"][0]["stories"][0]["scenario"]  # the payload got through to the data
    (tmp_path / "shim.js").write_text(SHIM)
    (tmp_path / "data.json").write_text(json.dumps(data))
    run = subprocess.run(["node", str(tmp_path / "shim.js"), str(ROOT / "web" / "impact.js"), str(tmp_path / "data.json")],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    for html in result["recorded"]:
        assert "<script" not in html.lower() and "<img" not in html.lower(), html
    for view, html in result["out"].items():
        assert "<script" not in html.lower() and "<img" not in html.lower(), view
        assert "&lt;script&gt;" in html, f"{view} did not render the payload at all"
