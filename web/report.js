// Temporal report view: renders the report data stored with an index.
//
// Order: opportunities first (each process that would gain from Temporal, its
// strongest reason as a feature, its score, and what the team gains), then the
// picture at a glance, then details to drill into. Plain DOM, no build step.
(() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  // Inline `code` in model-written text.
  const fmt = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
  const h = (tag, cls, html) => { const el = document.createElement(tag); if (cls) el.className = cls; if (html != null) el.innerHTML = html; return el; };
  const score = (n) => (Math.round(n * 10) / 10).toString();
  const slug = (s) => "rp-" + s.toLowerCase().replace(/[^a-z0-9]+/g, "-");
  const refs = (list, max = 3) => (list || []).slice(0, max).map((r) => `<span class="ref">${esc(r)}</span>`).join(" ")
    + ((list || []).length > max ? ` <span class="muted">+${list.length - max}</span>` : "");
  const NOT_ESTABLISHED = "Not established from the code.";
  // "Entry point src/main/.../Application.kt" reads as "Entry point Application.kt".
  const displayName = (name) => name.replace(/^Entry point (?:.*\/)?/, "Entry point ");

  // Card facts, numbered the way the report numbers them (F1, F2, ...).
  function facts(card) {
    const out = [];
    const add = (label, f) => out.push({ label, text: f.text ?? f.does ?? f.loses, refs: f.refs || [] });
    add("Trigger", card.trigger);
    add("Unit of work", card.unit_of_work);
    card.steps.forEach((s, i) => out.push({ label: `Step ${i + 1}: ${s.name}`, text: s.does + (s.effects.length ? ` (${s.effects.join(", ").replace(/_/g, " ")})` : ""), refs: s.refs }));
    add("State between steps", card.state_between_steps);
    add("Persisted", card.persistence);
    card.crash_loss.forEach((c) => out.push({ label: `Crash ${c.stage}`, text: c.loses, refs: c.refs, crash: true }));
    card.reliability.forEach((f) => add("Reliability today", f));
    card.waits.forEach((f) => add("Wait", f));
    add("Run length", card.run_length);
    return out.map((f, i) => ({ ...f, id: `F${i + 1}` }));
  }

  function ring(value, cls) {
    const el = h("div", `score-ring ${cls}`);
    el.style.setProperty("--p", Math.max(0, Math.min(100, value * 10)));
    el.innerHTML = `<span>${score(value)}</span>`;
    el.setAttribute("aria-label", `${score(value)} out of 10`);
    return el;
  }

  const verdictClass = (v) => (v === "suitable" ? "good" : v === "borderline" ? "warn" : "poor");

  function opportunityCard(p, titles, onDiscuss, onImpact) {
    const card = h("article", `opp ${verdictClass(p.verdict_class)}`);
    const top = h("div", "opp-top");
    const titleBox = h("div", "opp-title");
    titleBox.append(h("span", "feature", esc(titles[p.best_path] || p.best_path)), h("h3", null, esc(displayName(p.name))));
    top.append(titleBox, ring(p.overall, verdictClass(p.verdict_class)));
    card.append(top);
    card.append(h("div", `verdict-chip ${verdictClass(p.verdict_class)}`, esc(p.verdict_class)));

    const gains = p.design?.gains || [];
    if (gains.length) {
      card.append(h("div", "gain-label", "What you gain"));
      const ul = h("ul", "gains");
      gains.forEach((g) => ul.append(h("li", null, fmt(g.text))));
      card.append(ul);
    } else if (p.design?.why) {
      card.append(h("p", "why", fmt(p.design.why)));
    }

    if ((p.moved || []).length) {
      card.append(h("p", "opp-meta", `Moves ${p.moved.length} file${p.moved.length === 1 ? "" : "s"} to Temporal`));
    }
    if ((p.low_confidence || []).length) card.append(h("p", "opp-warn", "Low confidence: " + esc(p.low_confidence.join("; "))));

    const actions = h("div", "opp-actions");
    const more = h("button", "link", "Details");
    more.type = "button";
    more.onclick = () => { const d = document.getElementById(slug(p.name)); if (d) { d.open = true; d.scrollIntoView({ behavior: "smooth", block: "start" }); } };
    const talk = h("button", "link", "Discuss in chat");
    talk.type = "button";
    talk.onclick = () => onDiscuss(p.name);
    actions.append(more, talk);
    if (onImpact) {
      const impact = h("button", "link", "See business impact");
      impact.type = "button";
      impact.onclick = () => onImpact(p.name);
      actions.append(impact);
    }
    card.append(actions);
    return card;
  }

  function groupGates(data) {
    const titles = Object.fromEntries((data.sources?.pack || []).map((e) => [e.id, e.t]));
    const byId = new Map();
    (data.gates || []).forEach((g) => {
      if (!byId.has(g.id)) byId.set(g.id, { id: g.id, title: titles[g.id] || g.id, places: [] });
      byId.get(g.id).places.push(g.path.split("/").pop() + (g.line ? ` ${g.line}` : ""));
    });
    return [...byId.values()];
  }

  function glance(data, procs) {
    const pct = data.pct_temporalized || {};
    const designed = procs.some((p) => p.design);
    const stats = [
      [designed ? `${pct.pct_today}% → ${pct.pct_after}%` : `${pct.pct_today ?? 0}%`,
        designed ? "durable work on Temporal, today → with the designs" : "durable work on Temporal today"],
      [String(procs.length), "processes assessed"],
      [String(groupGates(data).length), "gates verified (reasons against Temporal)"],
    ];
    if (data.evaluation) stats.push([String(data.evaluation.temporal_files), "files already on Temporal"]);
    const strip = h("div", "glance");
    stats.forEach(([v, l]) => { const s = h("div", "stat"); s.append(h("strong", null, esc(v)), h("span", null, esc(l))); strip.append(s); });
    return strip;
  }

  function scoreRows(p) {
    const box = h("div", "score-rows");
    const row = (b, strong) => {
      const r = h("div", "score-row" + (strong ? " strong" : ""));
      const bar = h("div", "mini-bar"); bar.style.setProperty("--p", b.score * 10);
      r.append(h("span", "name", esc(b.t)), bar, h("span", "num", score(b.score)), h("span", "reason", fmt(b.reason)));
      return r;
    };
    box.append(h("div", "rows-label", "Reasons to use Temporal (fit is the strongest)"));
    p.bars.filter((b) => b.kind === "path").forEach((b) => box.append(row(b, b.id === p.best_path)));
    box.append(h("div", "rows-label", "Adjustments"));
    p.bars.filter((b) => b.kind === "modifier").forEach((b) => box.append(row(b, false)));
    box.append(h("p", "muted small", `Overall ${score(p.overall)}/10` + (p.gate_capped ? `, capped by verified gates (weighted ${score(p.weighted)})` : "")));
    return box;
  }

  function processDetails(p, data) {
    const d = h("details", "proc-detail");
    d.id = slug(p.name);
    d.append(h("summary", null, `<span class="dot-v ${verdictClass(p.verdict_class)}"></span>${esc(displayName(p.name))} <span class="muted">${score(p.overall)}/10 · ${esc(p.verdict_class)}</span>`));
    const body = h("div", "detail-body");
    body.append(h("p", "lead", fmt(p.verdict)));
    if (p.design?.why) body.append(h("p", null, `<strong>Why Temporal.</strong> ${fmt(p.design.why)}`));

    body.append(h("h4", null, "How it runs today"));
    const list = h("dl", "facts");
    const all = facts(p.card);
    all.filter((f) => !f.crash && f.text !== NOT_ESTABLISHED).forEach((f) => {
      list.append(h("dt", null, `<span class="fid">${f.id}</span>${esc(f.label)}`), h("dd", null, `${fmt(f.text)} ${refs(f.refs)}`));
      if (f.label === "Unit of work") {
        list.append(h("dt", null, `<span class="fid"></span>Scale assumed`),
          h("dd", null, `${fmt(p.card.scale_assumption)} <span class="muted">Scales with ${fmt(p.card.scales_with)}.</span>`));
      }
    });
    body.append(list);
    const crashes = all.filter((f) => f.crash);
    if (crashes.length) {
      body.append(h("h4", null, "What a crash loses today"));
      const cl = h("dl", "facts crash");
      crashes.forEach((f) => cl.append(h("dt", null, `<span class="fid">${f.id}</span>${esc(f.label.replace(/^Crash /, ""))}`), h("dd", null, `${fmt(f.text)} ${refs(f.refs)}`)));
      body.append(cl);
    }

    body.append(h("h4", null, "Score"));
    body.append(scoreRows(p));

    if (p.design) {
      body.append(h("h4", null, "Design"));
      const t = h("div", "elements");
      p.design.elements.forEach((el) => {
        const e = h("div", "element");
        e.append(h("span", `kind k-${el.kind}`, esc(el.kind.replace(/_/g, " "))), h("strong", null, esc(el.name)),
          h("span", "does", fmt(el.does)), h("span", "from muted small", (el.files || []).map((f) => `<code>${esc(f)}</code>`).join(" ")));
        t.append(e);
      });
      body.append(t);
      if (p.design.removes.length) body.append(h("p", null, `<strong>Removes.</strong> ${p.design.removes.map(fmt).join("; ")}`));
      if (p.design.stays.length) body.append(h("p", null, `<strong>Stays outside Temporal.</strong> ${p.design.stays.map(fmt).join("; ")}`));
    } else {
      if (p.reasons.length) {
        body.append(h("h4", null, "Why it is not a fit"));
        const ul = h("ul");
        p.reasons.forEach((r) => ul.append(h("li", null, fmt(r.text))));
        body.append(ul);
      }
      if (p.would_change.length) body.append(h("p", null, `<strong>What would change the verdict.</strong> ${p.would_change.map(fmt).join("; ")}`));
    }

    const titles = Object.fromEntries((data.sources?.pack || []).map((e) => [e.id, e]));
    const cited = new Set([...p.bars.flatMap((b) => b.cites), ...(p.design?.cites || []), ...(p.design?.elements || []).flatMap((e) => e.cites)]);
    const items = [...cited].map((c) => titles[c]
      ? `<li><code>${esc(c)}</code> ${esc(titles[c].t)}${titles[c].urls.slice(0, 1).map((u) => ` · <a href="${esc(u)}" target="_blank" rel="noopener noreferrer">docs</a>`).join("")}</li>`
      : c.startsWith("http") ? `<li><a href="${esc(c)}" target="_blank" rel="noopener noreferrer">${esc(c.replace(/^https?:\/\//, ""))}</a></li>` : "").join("");
    if (items) { body.append(h("h4", null, "Sources")); body.append(h("ul", "sources", items)); }
    d.append(body);
    return d;
  }

  function evaluation(ev) {
    const box = h("section", "rp-section");
    box.append(h("h2", null, "Existing Temporal code"), h("p", "lead", fmt(ev.verdict)));
    const rows = h("div", "score-rows");
    ev.bars.forEach((b) => {
      const r = h("div", "score-row");
      const bar = h("div", "mini-bar"); bar.style.setProperty("--p", b.score * 10);
      r.append(h("span", "name", esc(b.t)), bar, h("span", "num", score(b.score)), h("span", "reason", esc(b.reason)));
      rows.append(r);
    });
    box.append(rows);
    const order = { high: 0, medium: 1, low: 2 };
    const findings = [...ev.bars.flatMap((b) => b.findings), ...ev.other_findings].sort((a, b) => order[a.severity] - order[b.severity]);
    if (findings.length) {
      box.append(h("h4", null, "Fix first"));
      const ul = h("ul", "findings");
      findings.forEach((f) => ul.append(h("li", null,
        `<span class="sev ${esc(f.severity)}">${esc(f.severity)}</span> <code>${esc(f.entry)}</code> in <code>${esc(f.path)}</code> ${esc(f.line)}: ${fmt(f.reason)}`
        + (f.fix ? `<div class="fix">${fmt(f.fix)}</div>` : ""))));
      box.append(ul);
    }
    return box;
  }

  window.renderTemporalReport = function (container, payload, { onDiscuss, onImpact }) {
    container.innerHTML = "";
    const data = payload.data;
    const conv = data.conversion || {};
    const titles = Object.fromEntries((conv.paths || []).map((p) => [p.id, p.t]));
    const procs = (conv.processes || []).filter((p) => !p.error);
    const opps = procs.filter((p) => p.verdict_class !== "not suitable")
      .sort((a, b) => (a.verdict_class === b.verdict_class ? b.overall - a.overall : a.verdict_class === "suitable" ? -1 : 1));
    const misfits = procs.filter((p) => p.verdict_class === "not suitable");

    const wrap = h("div", "report");
    // 1. Header
    const head = h("header", "rp-head");
    head.append(h("p", "eyebrow", "Temporal report"), h("h1", null, esc(data.repo_id)));
    const when = payload.generated_at ? new Date(payload.generated_at).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
    head.append(h("p", "chips",
      `<span class="chip">Temporal: ${esc(data.status)}</span><span class="chip">${esc(when)} · commit ${esc((payload.commit_sha || "").slice(0, 7))}</span>`
      + `<span class="chip">pack ${esc(data.pack.version)} · rubric ${esc(data.rubric.version)}</span>`));
    if (payload.stale) head.append(h("p", "notice", "The code has changed since this report was built. It will update on the next index run."));
    if (payload.status === "failed") head.append(h("p", "notice", `The latest attempt to rebuild this report failed (${esc(payload.error)}); this is the previous report.`));
    wrap.append(head);

    // 2. Opportunities
    const oppSec = h("section", "rp-section");
    if (data.conversion) {
      const good = opps.filter((p) => p.verdict_class === "suitable").length;
      const close = opps.length - good;
      oppSec.append(h("h2", null, "Opportunities"));
      oppSec.append(h("p", "lead", opps.length
        ? `${good ? `${good} process${good === 1 ? "" : "es"} would clearly gain from Temporal` : "No process clearly gains from Temporal"}`
          + `${close ? `${good ? "; " : ", but "}${close} ${close === 1 ? "is" : "are"} borderline` : ""}.`
        : "No Temporal opportunities: none of the processes found gains enough from durable execution."));
      if (opps.length) {
        const grid = h("div", "opp-grid");
        opps.forEach((p) => grid.append(opportunityCard(p, titles, onDiscuss, onImpact)));
        oppSec.append(grid);
      } else if (misfits.length) {
        const ul = h("ul", "reasons");
        misfits.slice(0, 3).forEach((p) => ul.append(h("li", null, `<strong>${esc(displayName(p.name))}.</strong> ${fmt(p.reasons[0]?.text || p.verdict)}`)));
        oppSec.append(ul);
      }
      wrap.append(oppSec);
    }

    // 3. At a glance
    const glanceSec = h("section", "rp-section");
    glanceSec.append(h("h2", null, "At a glance"), glance(data, procs));
    const gates = groupGates(data);
    if (gates.length) {
      const ul = h("ul", "gates");
      gates.forEach((g) => ul.append(h("li", null,
        `<code>${esc(g.id)}</code> <strong>${esc(g.title)}</strong> <span class="muted">Verified in ${g.places.length} `
        + `place${g.places.length === 1 ? "" : "s"}:</span> ${g.places.map((x) => `<span class="ref">${esc(x)}</span>`).join(", ")}`)));
      glanceSec.append(ul);
    }
    wrap.append(glanceSec);

    // 4. Not a fit
    if (misfits.length) {
      const nf = h("details", "rp-section collapsible");
      nf.append(h("summary", null, `<h2>Not a fit</h2><span class="muted">${misfits.length} process${misfits.length === 1 ? "" : "es"}</span>`));
      const ul = h("ul", "misfits");
      misfits.forEach((p) => ul.append(h("li", null, `<span class="num">${score(p.overall)}</span><strong>${esc(displayName(p.name))}</strong> <span class="muted">${fmt(p.reasons[0]?.text || p.verdict)}</span>`)));
      nf.append(ul);
      wrap.append(nf);
    }

    // 5. Existing Temporal code
    if (data.evaluation) wrap.append(evaluation(data.evaluation));

    // 6. Details
    if (procs.length) {
      const det = h("section", "rp-section");
      det.append(h("h2", null, "Details"), h("p", "muted small", "How each process runs today (facts checked against the cited source lines), what a crash loses, how it scores, and the design."));
      [...opps, ...misfits].forEach((p) => det.append(processDetails(p, data)));
      wrap.append(det);
    }

    // 7. Method
    const m = h("details", "rp-section collapsible");
    m.append(h("summary", null, "<h2>How this report was made</h2>"));
    const leads = data.leads || [];
    m.append(h("ul", "small", [
      `Built from the code index, the mirrored source, the Temporal knowledge pack ${esc(data.pack.version)}, rubric ${esc(data.rubric.version)} and the Temporal Docs MCP, with ${esc(data.model)}. No other Temporal knowledge.`,
      `Each process was investigated in the source; every fact cites the lines it rests on and was checked against them.`,
      `${leads.length} index findings were checked against the source (${leads.filter((l) => l.verdict === "confirmed").length} confirmed).`,
      `Scores come from a fixed rubric: a process's fit is its strongest reason to use Temporal; suitable from ${score(conv.threshold + (conv.margin || 0))}, borderline from ${score(conv.threshold - (conv.margin || 0))}.`,
    ].map((t) => `<li>${t}</li>`).join("")));
    wrap.append(m);

    container.append(wrap);
  };
})();
