// Business impact view: buyer-readable briefs generated on demand from the Temporal report.
//
// Three views of one stored result (exec brief, champion brief, discovery sheet), plus
// the states around it: nothing yet (the generate form with a cost check), running
// (polled progress), done (stale and failed banners, regenerate, downloads). Every
// model-written string goes through esc() or fmt(), as in report.js. Plain DOM, no build.
(() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  // Inline `code` in model-written text.
  const fmt = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
  const h = (tag, cls, html) => { const el = document.createElement(tag); if (cls) el.className = cls; if (html != null) el.innerHTML = html; return el; };
  const button = (label, cls, onclick, title) => { const b = h("button", cls, esc(label)); b.type = "button"; b.onclick = onclick; if (title) b.title = title; return b; };
  const displayName = (name) => String(name).replace(/^Entry point (?:.*\/)?/, "Entry point ");
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const VIEWS = [["exec", "Exec brief"], ["champion", "Champion brief"], ["discovery", "Discovery sheet"]];
  const CASE = { strong: ["Strong case", "good"], moderate: ["Moderate case", "warn"], weak: ["Weak case", "poor"], none: ["No case", "poor"] };
  const PHASES = { PREPARE: "Reading the Temporal report", PROCESSES: "Translating each process", SYNTHESIZE: "Ranking and summarizing", DONE: "Finishing" };
  const SYMBOLS = new Set(["$", "€", "£", "¥", "₹"]);

  const money = (v, cur) => {
    const amount = v >= 100 ? Math.round(v).toLocaleString("en-US") : String(Math.round(v * 100) / 100);
    return SYMBOLS.has(cur) ? `${cur}${amount}` : `${amount} ${cur}`;
  };
  const num = (v) => (Number.isInteger(v) ? v.toLocaleString("en-US") : String(Math.round(v * 100) / 100));
  const span = (lo, hi, f) => (lo === hi ? f(lo) : `${f(lo)} to ${f(hi)}`);
  const label = (map, id) => (map || {})[id] || String(id).replace(/_/g, " ");

  // ---- pieces shared by the views ----

  function caseChip(p) {
    const [text, cls] = CASE[p.business_case] || CASE.none;
    return h("span", `verdict-chip ${cls}`, esc(text));
  }

  function evidence(story, p, data) {
    const byId = Object.fromEntries((p.evidence || []).map((e) => [e.id, e]));
    const box = h("details", "imp-evidence");
    box.append(h("summary", null, "Why we say this"));
    const dl = h("dl", "facts");
    story.cites.forEach((c) => {
      if (byId[c]) {
        const e = byId[c];
        dl.append(h("dt", null, `<span class="fid">${esc(c)}</span>${esc(e.label)}`),
          h("dd", null, `${fmt(e.text)} ${(e.refs || []).map((r) => `<span class="ref">${esc(r)}</span>`).join(" ")}`));
      } else if (c.startsWith("C") && data.context.items[c] !== undefined) {
        dl.append(h("dt", null, `<span class="fid">${esc(c)}</span>Customer context`), h("dd", null, fmt(data.context.items[c])));
      }
    });
    box.append(dl);
    return box;
  }

  function storyItem(s, p, data, withEvidence) {
    const li = h("li", "imp-story");
    const top = h("div", "imp-story-top");
    top.append(h("span", "feature", esc(label(data.categories, s.category))), h("span", `sev ${esc(s.severity)}`, esc(s.severity)));
    s.who_feels_it.forEach((w) => top.append(h("span", "chip", esc(label(data.stakeholders, w)))));
    li.append(top, h("p", null, fmt(s.scenario)));
    if (withEvidence) li.append(evidence(s, p, data));
    return li;
  }

  function costBox(cost) {
    const box = h("div", "imp-cost");
    box.append(h("div", "imp-cost-range", `<strong>${esc(span(cost.low, cost.high, (v) => money(v, cost.currency)))}</strong> per month <span class="muted">(illustrative)</span>`));
    box.append(h("p", "small", `${esc(cost.t)} = <code>${esc(cost.formula)}</code>`));
    const table = h("table", "imp-inputs");
    table.append(h("thead", null, "<tr><th>Input</th><th>Value</th><th>Unit</th><th>Source</th></tr>"));
    const body = h("tbody");
    cost.inputs.forEach((r) => body.append(h("tr", null,
      `<td>${esc(r.name.replace(/_/g, " "))}</td><td>${esc(span(r.low, r.high, num))}</td><td>${esc(r.unit)}</td>`
      + `<td>${r.basis === "context" ? `<span class="tag ctx">context ${esc(r.cite)}</span>` : '<span class="tag default">default</span>'}</td>`)));
    table.append(body);
    box.append(table, h("p", "muted small", esc(cost.label) + (cost.inputs.some((r) => r.basis === "default") ? " Defaults are placeholders, not customer data." : "")));
    return box;
  }

  function list(cls, items) {
    const ul = h("ul", cls);
    items.forEach((html) => ul.append(h("li", null, html)));
    return ul;
  }

  function assumptions(data) {
    const sec = h("section", "rp-section");
    sec.append(h("h2", null, "Assumptions"));
    if (!(data.assumptions || []).length) { sec.append(h("p", "muted", "None.")); return sec; }
    sec.append(h("p", "muted small", "Not established by the code or the customer context. Confirm or replace them with the discovery questions."));
    sec.append(list("imp-list", data.assumptions.map(fmt)));
    return sec;
  }

  function notWorth(data) {
    const out = [];
    const misfits = (data.skipped || []).filter((s) => s.reason === "not suitable");
    if (misfits.length) {
      const sec = h("section", "rp-section");
      sec.append(h("h2", null, "Not worth pursuing"));
      sec.append(list("misfits", misfits.map((s) => `<strong>${esc(displayName(s.name))}</strong> <span class="muted">${fmt(s.detail)}</span>`
        + ((s.would_change || []).length ? `<div class="small muted">Would change if: ${s.would_change.map(fmt).join("; ")}</div>` : ""))));
      out.push(sec);
    }
    const quiet = (data.skipped || []).filter((s) => s.reason === "no grounded consequence");
    if (quiet.length) {
      const sec = h("section", "rp-section");
      sec.append(h("h2", null, "No failure story from the code"));
      sec.append(h("p", "muted small", "The Temporal report rates these a fit, but their code facts show nothing a crash would lose."));
      sec.append(list("misfits", quiet.map((s) => `<strong>${esc(displayName(s.name))}</strong>`)));
      out.push(sec);
    }
    const failed = (data.skipped || []).filter((s) => s.reason === "error" || s.reason === "failed");
    if (failed.length) {
      const sec = h("section", "rp-section");
      sec.append(h("h2", null, "Not assessed"));
      sec.append(list("misfits", failed.map((s) => `<strong>${esc(displayName(s.name))}</strong> <span class="muted">${fmt(s.detail)}</span>`)));
      out.push(sec);
    }
    return out;
  }

  function filteredOut(data) {
    const d = h("details", "rp-section collapsible");
    d.append(h("summary", null, `<h2>What was filtered out</h2><span class="muted">${plural((data.dropped || []).length, "item")}</span>`));
    d.append((data.dropped || []).length
      ? list("imp-list small", data.dropped.map(fmt))
      : h("p", "muted small", "Nothing: every statement passed the checks against the code facts and the context."));
    return d;
  }

  function footer(data) {
    const src = data.source_report || {};
    const when = src.generated_at ? new Date(src.generated_at).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "unknown";
    return h("p", "muted small imp-foot",
      `Built from the Temporal report of commit ${esc((src.commit_sha || "").slice(0, 7) || "unknown")} (${esc(when)}); `
      + `mapping ${esc(data.mapping.id)} ${esc(data.mapping.version)}; model ${esc(data.model)}; `
      + `${data.context.present ? `context ${esc(data.context.hash)}` : "no customer context"}. Numbers are illustrative, computed from stated inputs.`);
  }

  // ---- the three views ----

  function execView(data, onDiscuss) {
    const v = h("div", "imp-view");
    const lead = h("section", "rp-section");
    lead.append(h("p", "imp-headline", fmt(data.headline)), h("p", "lead", fmt(data.exec_summary)));
    v.append(lead);
    const procs = data.processes || [];
    if (procs.length) {
      const sec = h("section", "rp-section");
      sec.append(h("h2", null, "Where failures would cost the business"));
      procs.forEach((p, i) => {
        const card = h("article", `opp imp-proc ${(CASE[p.business_case] || CASE.none)[1]}`);
        card.dataset.process = p.name;
        const top = h("div", "opp-top");
        top.append(h("h3", null, `${i + 1}. ${esc(displayName(p.name))}`), caseChip(p));
        card.append(top);
        const ul = h("ul", "imp-stories");
        p.stories.forEach((s) => ul.append(storyItem(s, p, data, true)));
        card.append(ul);
        if (p.case_reason) card.append(h("p", "why", `<strong>Why it matters.</strong> ${fmt(p.case_reason)}`));
        if (p.case_capped) card.append(h("p", "opp-warn", esc(p.case_capped)));
        if (p.cost) card.append(costBox(p.cost));
        else if ((p.missing_inputs || []).length) {
          card.append(h("p", "opp-meta", `No cost estimate yet: the customer has not stated ${esc(p.missing_inputs.map((m) => m.replace(/_/g, " ")).join(", "))}. See the discovery sheet.`));
        }
        if (onDiscuss) {
          const actions = h("div", "opp-actions");
          actions.append(button("Discuss in chat", "link", () => onDiscuss(p.name)));
          card.append(actions);
        }
        sec.append(card);
      });
      v.append(sec);
    }
    notWorth(data).forEach((s) => v.append(s));
    const step = h("section", "rp-section");
    step.append(h("h2", null, "Recommended first step"), h("p", null, fmt(data.first_step || "")));
    v.append(step, assumptions(data));
    return v;
  }

  function championView(data) {
    const v = h("div", "imp-view");
    v.append(h("p", "lead", fmt(data.headline)));
    (data.processes || []).forEach((p) => {
      const d = h("details", "proc-detail");
      d.open = true;
      d.dataset.process = p.name;
      d.append(h("summary", null, `<span class="dot-v ${(CASE[p.business_case] || CASE.none)[1]}"></span>${esc(displayName(p.name))} `
        + `<span class="muted">${esc((CASE[p.business_case] || CASE.none)[0])} · report ${esc(p.verdict_class)} ${esc(p.overall)}/10</span>`));
      const body = h("div", "detail-body");
      body.append(h("p", "small muted", `Strongest reason: ${esc(p.best_path_t)}. Entry point <code>${esc(p.entry)}</code>.`));
      if (p.case_capped) body.append(h("p", "opp-warn", esc(p.case_capped)));
      if ((p.low_confidence || []).length) body.append(h("p", "opp-warn", "Low confidence: " + esc(p.low_confidence.join("; "))));
      body.append(h("h4", null, "Failure stories, with the code facts behind them"));
      const ul = h("ul", "imp-stories");
      p.stories.forEach((s) => { const li = storyItem(s, p, data, true); li.querySelector("details").open = true; ul.append(li); });
      body.append(ul);
      if ((p.crash_loss || []).length) {
        body.append(h("h4", null, "What a crash loses today"));
        const dl = h("dl", "facts crash");
        p.crash_loss.forEach((c) => dl.append(h("dt", null, esc(c.stage)), h("dd", null, `${fmt(c.loses)} ${(c.refs || []).map((r) => `<span class="ref">${esc(r)}</span>`).join(" ")}`)));
        body.append(dl);
      }
      const wc = p.what_changes || {};
      if ((wc.gains || []).length || (wc.removes || []).length || (wc.stays || []).length) {
        body.append(h("h4", null, "What changes with Temporal"));
        if ((wc.gains || []).length) body.append(list("gains", wc.gains.map((g) => `${fmt(g.text)} <span class="ref">${esc(g.cites.join(", "))}</span>`)));
        if ((wc.removes || []).length) body.append(h("p", null, `<strong>Removes.</strong> ${wc.removes.map(fmt).join("; ")}`));
        if ((wc.stays || []).length) body.append(h("p", null, `<strong>Stays outside Temporal.</strong> ${wc.stays.map(fmt).join("; ")}`));
      }
      if (p.workaround?.text) body.append(h("p", null, `<strong>Workaround today</strong> <span class="tag">${esc(p.workaround.basis)}</span> ${fmt(p.workaround.text)}`));
      if ((p.drivers || []).length) {
        body.append(h("h4", null, "What decides the size of the problem"));
        body.append(list("imp-list", p.drivers.map((x) => `<span class="tag">${esc(x.basis)}</span> <strong>${esc(x.kind.replace(/_/g, " "))}.</strong> ${fmt(x.text)}`)));
      }
      if (p.verdict) { body.append(h("h4", null, "Verdict"), h("p", null, fmt(p.verdict))); }
      if ((p.would_change || []).length) body.append(h("p", null, `<strong>What would change the verdict.</strong> ${p.would_change.map(fmt).join("; ")}`));
      const gains = (wc.gains || []).slice(0, 2).map((g) => fmt(g.text));
      body.append(h("h4", null, "Proof-of-concept scope"), h("p", null, `Start at <code>${esc(p.entry)}</code>.${gains.length ? ` Show: ${gains.join("; ")}.` : ""}`));
      d.append(body);
      v.append(d);
    });
    notWorth(data).forEach((s) => v.append(s));
    return v;
  }

  function discoveryView(data) {
    const v = h("div", "imp-view");
    v.append(h("p", "lead", "Questions that turn assumptions into facts, most valuable first, grouped by who to ask."));
    const groups = new Map();
    (data.questions || []).forEach((q) => { if (!groups.has(q.ask)) groups.set(q.ask, []); groups.get(q.ask).push(q); });
    if (!groups.size) v.append(h("p", "muted", "No open questions."));
    groups.forEach((qs, ask) => {
      const sec = h("section", "rp-section");
      sec.append(h("h2", null, `Ask ${esc(label(data.stakeholders, ask))}`));
      const ol = h("ol", "imp-questions");
      qs.forEach((q) => ol.append(h("li", null, `<strong>${fmt(q.question)}</strong>`
        + `<div class="small muted">${fmt(q.why)} · ${q.source === "missing input" ? `<span class="tag default">cost input</span> ${esc(q.fills.replace(/_/g, " "))} (${esc(q.unit)})` : `resolves ${esc(q.fills.replace(/_/g, " "))}`}`
        + ` · for ${esc(q.processes.map(displayName).join(", "))}</div>`)));
      sec.append(ol);
      v.append(sec);
    });
    const missing = (data.processes || []).flatMap((p) => (p.missing_inputs || []).map((m) => `<strong>${esc(displayName(p.name))}:</strong> ${esc(m.replace(/_/g, " "))}`));
    if (missing.length) {
      const sec = h("section", "rp-section");
      sec.append(h("h2", null, "Missing inputs for cost estimates"), list("imp-list", missing));
      v.append(sec);
    }
    v.append(assumptions(data));
    const ctx = h("section", "rp-section");
    ctx.append(h("h2", null, "Customer context used"));
    const items = Object.entries(data.context.items || {});
    ctx.append(items.length ? list("imp-list", items.map(([id, t]) => `<span class="fid">${esc(id)}</span>${fmt(t)}`))
      : h("p", "muted", "None given. The briefs are qualitative only."));
    v.append(ctx);
    return v;
  }

  // The views as DOM, from the stored data only (no fetches): also what the XSS test drives.
  function renderViews(container, data, view, { onDiscuss } = {}) {
    container.innerHTML = "";
    const fn = { exec: execView, champion: championView, discovery: discoveryView }[view] || execView;
    container.append(fn(data, onDiscuss), filteredOut(data), footer(data));
  }

  // ---- the stateful view: fetch, generate, poll ----

  const CONTEXT_FIELDS = [
    ["text", "Call notes", "textarea", "What the customer said: incidents, how they recover, what it costs them. Numbers you type here can be used in cost ranges.", 4000],
    ["pain_points", "Pain points", "textarea", "", 1000],
    ["industry", "Industry", "input", "For example: mid-market fintech", 80],
    ["volumes", "Volumes", "input", "For example: ~40k payouts/day", 300],
  ];

  window.renderBusinessImpact = function (container, { repo }, { onDiscuss, focus, isCurrent = () => true } = {}) {
    let alive = true, timer = null, checkTimer = null;
    // The user may have moved on (another view or index) while a request was in flight.
    const live = () => { if (alive && !isCurrent()) alive = false; return alive; };
    const state = { payload: null, status: null, view: null };
    const api = (path, opts) => fetch(`/api/impact/${encodeURIComponent(repo)}${path}`, opts);
    const errorText = async (res) => { const b = await res.json().catch(() => ({})); return b.error || (b.detail ? JSON.stringify(b.detail) : res.statusText); };

    const shell = (eyebrowRight) => {
      container.innerHTML = "";
      const wrap = h("div", "report impact");
      const head = h("header", "rp-head");
      head.append(h("p", "eyebrow", "Business impact"), h("h1", null, esc(repo)));
      if (eyebrowRight) head.append(eyebrowRight);
      wrap.append(head);
      container.append(wrap);
      return wrap;
    };

    async function load() {
      clearTimeout(timer);
      try {
        const [sRes, iRes] = await Promise.all([api("/status"), api("")]);
        if (!live()) return;
        state.status = sRes.ok ? await sRes.json() : { status: "none" };
        state.payload = iRes.ok ? await iRes.json() : null;
      } catch (e) {
        if (live()) shell().append(h("p", "notice", `Could not load the business impact: ${esc(e.message)}`));
        return;
      }
      if (!live()) return;
      if (state.status.status === "running") return showRunning();
      if (state.payload?.data) return showDone();
      return showForm();
    }

    // ---- nothing yet, or new context: the generate form ----
    function showForm(prefill, { fresh = false } = {}) {
      const wrap = shell();
      const s = state.status || {};
      if (["failed", "interrupted"].includes(s.status)) wrap.append(h("p", "notice", `The last run did not finish: ${esc(s.error || s.status)}`));
      const sec = h("section", "rp-section imp-form");
      sec.append(h("h2", null, state.payload?.data ? "Generate again" : "Generate business impact"));
      sec.append(h("p", "lead", "Turns the verified facts in the Temporal report into an exec brief, a champion brief and a discovery sheet. "
        + "Every claim cites a code fact or your context; anything else is labeled an assumption."));
      const ctx = { ...(prefill || state.payload?.context || {}) };
      const details = h("details", "imp-context");
      details.open = Boolean(prefill) || Object.values(ctx).some((v) => v && v !== "USD" && v !== "exec");
      details.append(h("summary", null, "Customer context <span class=\"muted\">(optional)</span>"));
      const grid = h("div", "imp-fields");
      const inputs = {};
      CONTEXT_FIELDS.forEach(([name, text, kind, hint, max]) => {
        const f = h("div", "field" + (kind === "textarea" ? " wide" : ""));
        const id = `imp-${name}`;
        const lab = h("label", null, esc(text)); lab.htmlFor = id;
        const input = document.createElement(kind); input.id = id; input.maxLength = max; input.value = ctx[name] || "";
        if (kind === "input") input.type = "text";
        if (kind === "textarea") input.rows = name === "text" ? 4 : 2;
        if (hint) input.placeholder = hint;
        f.append(lab, input);
        grid.append(f);
        inputs[name] = input;
      });
      const costField = h("div", "field");
      costField.append(h("label", null, "Currency and hourly staff cost"));
      const row = h("div", "path-row");
      const cur = document.createElement("select"); cur.id = "imp-currency";
      ["USD", "EUR", "GBP", "CAD", "AUD", "JPY", "INR"].forEach((c) => { const o = document.createElement("option"); o.value = c; o.textContent = c; cur.append(o); });
      if (ctx.currency && ![...cur.options].some((o) => o.value === ctx.currency)) { const o = document.createElement("option"); o.value = ctx.currency; o.textContent = ctx.currency; cur.append(o); }
      cur.value = ctx.currency || "USD";
      const rate = document.createElement("input"); rate.type = "number"; rate.min = "0"; rate.step = "1"; rate.id = "imp-rate";
      rate.placeholder = "per hour (optional)"; rate.value = ctx.hourly_rate ? String(ctx.hourly_rate) : "";
      row.append(cur, rate); costField.append(row);
      grid.append(costField);
      details.append(grid);
      details.append(h("p", "muted small", "Context is sent to the model provider, like your code summaries, and stored locally with the result."));
      sec.append(details);

      const read = () => ({
        text: inputs.text.value, pain_points: inputs.pain_points.value, industry: inputs.industry.value,
        volumes: inputs.volumes.value, currency: cur.value, hourly_rate: Number(rate.value) || 0, audience: "exec",
      });
      const check = h("p", "check-result", "Checking what a run would do…");
      const err = h("p", "form-error"); err.hidden = true;
      const go = button(fresh ? "Regenerate from scratch" : "Generate business impact", "primary", async () => {
        go.disabled = true; err.hidden = true;
        try {
          const res = await api("", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ context: read(), fresh }) });
          if (!res.ok) { err.textContent = await errorText(res); err.hidden = false; go.disabled = false; return; }
          state.status = { status: "running", phase: "PREPARE" };
          showRunning();
        } catch (e) { err.textContent = e.message; err.hidden = false; go.disabled = false; }
      });
      const runCheck = async () => {
        try {
          const res = await api("/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ context: read(), fresh }) });
          if (!alive) return;
          const body = await res.json().catch(() => ({}));
          if (!live()) return;
          check.className = "check-result " + (res.ok ? "good" : "bad");
          check.textContent = res.ok ? body.message : (body.error || (body.detail ? "Check the context fields (too long, or an invalid value)." : res.statusText));
          go.disabled = !res.ok;  // fixing the fields runs the check again
        } catch (e) { check.className = "check-result bad"; check.textContent = `Could not check: ${e.message}`; }
      };
      [...Object.values(inputs), cur, rate].forEach((el) => el.addEventListener("input", () => { clearTimeout(checkTimer); checkTimer = setTimeout(runCheck, 600); }));
      const actions = h("div", "check");
      actions.append(check, go);
      if (state.payload?.data) actions.append(button("Back to the current result", "link", showDone));
      sec.append(actions, err);
      wrap.append(sec);
      runCheck();
    }

    // ---- running: poll the status ----
    function showRunning() {
      const s = state.status || {};
      const wrap = shell();
      const sec = h("section", "rp-section sync-job");
      const total = s.total || 0, done = s.done || 0;
      sec.append(h("div", "sync-line", `<span class="sync-name"><strong>${esc(PHASES[s.phase] || "Starting")}</strong></span>`
        + `<span class="sync-desc">${total ? `${done} of ${plural(total, "process")}` : "Preparing"}${s.failed ? `, ${s.failed} failed` : ""}</span>`));
      const bar = h("div", "bar" + (total ? "" : " indeterminate"));
      const fill = h("span"); fill.style.width = `${total ? Math.round((100 * done) / total) : 30}%`;
      bar.append(fill); sec.append(bar);
      if ((s.processes || []).length) {
        sec.append(list("imp-dots", s.processes.map((p) => `<span class="imp-dot ${esc(p.status)}" title="${esc(p.status.replace(/_/g, " "))}"></span>${esc(displayName(p.name || p.key))}`)));
      }
      const cancel = button(state.cancelling ? "Cancelling…" : "Cancel", "ghost danger", async () => {
        state.cancelling = true; cancel.disabled = true; cancel.textContent = "Cancelling…";
        await api("", { method: "DELETE" }).catch(() => {});
      });
      cancel.disabled = Boolean(state.cancelling);
      sec.append(h("p", "muted small", "Not-a-fit processes are skipped without a model call. You can leave this page; the run continues."));
      sec.append(cancel);
      wrap.append(sec);
      timer = setTimeout(async () => {
        if (!live()) return;
        try {
          const res = await api("/status");
          state.status = res.ok ? await res.json() : state.status;
        } catch { /* keep polling */ }
        if (!live()) return;
        if (state.status.status === "running") showRunning(); else { state.cancelling = false; load(); }
      }, 2000);
    }

    // ---- done: the views ----
    function showDone() {
      const p = state.payload, data = p.data, s = state.status || {};
      state.view = state.view || data.context.audience || "exec";
      const chips = h("p", "chips",
        `<span class="chip">${esc(new Date(p.generated_at).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }))}</span>`
        + `<span class="chip">${data.context.present ? "with customer context" : "no customer context (qualitative)"}</span>`
        + `<span class="chip">${plural(data.model_calls || 0, "model call")} · $${esc((data.cost_usd || 0).toFixed(2))}</span>`);
      const wrap = shell(chips);
      const head = wrap.querySelector(".rp-head");
      if (p.stale) {
        const n = h("div", "notice imp-banner", "The Temporal report was rebuilt since this was generated, so it may be out of date. ");
        n.append(button("Regenerate", "link", () => start(p.context || {}, false)));
        head.append(n);
      }
      if (["failed", "interrupted"].includes(s.status) || p.status === "failed") {
        head.append(h("p", "notice", `The latest run did not finish (${esc(s.error || p.error || s.status)}). This is the last good result.`));
      }
      const bar = h("div", "imp-toolbar");
      const seg = h("span", "seg"); seg.setAttribute("role", "group"); seg.setAttribute("aria-label", "Business impact view");
      VIEWS.forEach(([id, text]) => {
        const b = button(text, "ghost", () => { state.view = id; showDone(); });
        b.setAttribute("aria-pressed", String(state.view === id));
        seg.append(b);
      });
      bar.append(seg, h("span", "spacer"),
        button("New context…", "ghost", () => showForm(p.context || {}), "Generate again with different customer context"),
        button("Regenerate", "ghost", () => start(p.context || {}, true), "Run every model call again with the same context"),
        button("Download HTML", "ghost", () => download("html")),
        button("Download .md", "ghost", () => download("md")));
      head.append(bar);
      const body = h("div", "imp-body");
      renderViews(body, data, state.view, { onDiscuss });
      wrap.append(body);
      if (focus) {
        const target = [...body.querySelectorAll("[data-process]")].find((el) => el.dataset.process === focus);
        if (target) target.scrollIntoView({ block: "start" });
        focus = null;
      }
    }

    async function start(context, fresh) {
      try {
        const res = await api("", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ context, fresh }) });
        if (!res.ok && res.status !== 409) { const wrap = container.querySelector(".rp-head") || shell(); wrap.append(h("p", "form-error", esc(await errorText(res)))); return; }
        state.status = { status: "running", phase: "PREPARE" };
        showRunning();
      } catch (e) { (container.querySelector(".rp-head") || shell()).append(h("p", "form-error", esc(e.message))); }
    }

    const saveFile = (name, type, text) => {
      const url = URL.createObjectURL(new Blob([text], { type }));
      const a = document.createElement("a"); a.href = url; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    };

    async function download(kind) {
      const view = state.view || "exec";
      if (!state.payload?.data) return;
      if (kind === "md") {
        const res = await api(`/markdown?view=${view}`);
        if (res.ok) saveFile(`${repo}-business-impact-${view}.md`, "text/markdown;charset=utf-8", await res.text());
        return;
      }
      const body = container.querySelector(".imp-body");
      if (!body) return;
      let css = "";
      try { css = await (await fetch("style.css")).text(); } catch { /* the page still reads without styles */ }
      const clone = body.cloneNode(true);
      clone.querySelectorAll(".opp-actions").forEach((el) => el.remove());
      clone.querySelectorAll("details").forEach((d) => d.setAttribute("open", ""));
      const title = esc(`Business impact (${VIEWS.find(([id]) => id === view)[1]}): ${repo}`);
      saveFile(`${repo}-business-impact-${view}.html`, "text/html;charset=utf-8", `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>${css}
body { display: block; height: auto; }
</style>
</head>
<body>
<div class="report impact"><header class="rp-head"><p class="eyebrow">Business impact</p><h1>${esc(repo)}</h1></header>${clone.outerHTML}</div>
</body>
</html>
`);
    }

    container.innerHTML = '<div class="report"><p class="muted">Loading the business impact…</p></div>';
    load();
    return {
      destroy() { alive = false; clearTimeout(timer); clearTimeout(checkTimer); },
      download: () => download("html"),
    };
  };

  window.BusinessImpact = { renderViews, esc };
})();
