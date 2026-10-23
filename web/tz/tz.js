/*
 * Temporalize UI: eight screens from repository to a validated Temporal POC.
 *
 * Plain JS, no build step. Everything a run shows comes from GET /api/tz/runs/{id}
 * (docs/TEMPORALIZE.md), polled while something runs. Local edits (form fields,
 * selections) live in `ui` until they are sent.
 *
 * Routes: #/ (new run, setup)  ·  #/run/<id>/<step>  (step 1-8)
 */
(function () {
  "use strict";

  const STEPS = ["Setup", "What we found", "Focus", "Success", "Report and scope",
    "Behavior and design", "Generate and validate", "Results"];
  const BUSY = ["scanning", "assessing", "planning", "generating"];
  const EDITABLE_EARLY = ["scanning", "scan_failed", "ready"];   // screens 1-4 editable only then
  const POLL_MS = 1500;
  const MAX_FOCUS = 2;

  const state = {
    settings: null, config: null,
    run: null, runId: null, step: 1,
    pollTimer: null, pollToken: 0,
  };
  // Per-screen local state (reset when the run changes).
  let ui = freshUI();

  function freshUI() {
    return {
      setup: null, exclEdit: false, focus: null, focusFor: null, criteria: null, criteriaFor: null,
      critSaveTimer: null, addCrit: false, newCrit: { text: "", priority: "P1" }, importedCriteria: null,
      scope: null, scopeFor: null, plan: { open: null, editing: null, editText: "", editFile: null, editFileText: "", preview: {} },
      autoNavDone: false, busy: false, error: null,
    };
  }

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k === "class") el.className = v;
        else if (k === "style") el.setAttribute("style", v);
        else if (k === "value") el.value = v;
        else if (k === "checked" || k === "disabled" || k === "hidden" || k === "readOnly" || k === "selected" || k === "open") el[k] = !!v;
        else if (v === true) el.setAttribute(k, "");
        else el.setAttribute(k, v);
      }
    }
    append(el, kids);
    return el;
  }
  function append(el, kids) {
    for (const k of kids) {
      if (k === null || k === undefined || k === false) continue;
      if (Array.isArray(k)) append(el, k);
      else el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
    }
  }

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    let res;
    try { res = await fetch(path, init); }
    catch (e) { throw new Error(`The server is not reachable (${e.message || e}).`); }
    const type = res.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await res.json().catch(() => null) : await res.text();
    if (!res.ok) {
      const msg = (data && typeof data === "object" && (data.error || data.detail)) || (typeof data === "string" && data) || res.statusText;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }
  const asRun = (d) => (d && d.run ? d.run : d);
  const runId = (r) => r.id || r._id;
  const enc = encodeURIComponent;

  function banner(msg) {
    const b = document.getElementById("banner");
    if (!msg) { b.hidden = true; b.textContent = ""; return; }
    b.hidden = false; b.textContent = "";
    append(b, [h("span", null, msg), h("button", { type: "button", "aria-label": "Dismiss", onclick: () => banner(null) }, "×")]);
  }

  const fmtInt = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : (n ?? "—"));
  function approx(n) {
    if (typeof n !== "number") return "—";
    if (n < 1000) return String(n);
    const r = n < 100000 ? Math.round(n / 100) * 100 : Math.round(n / 1000) * 1000;
    return "~" + r.toLocaleString("en-US");
  }
  function plural(n, w, pl) { return `${fmtInt(n)} ${n === 1 ? w : (pl || w + "s")}`; }
  function ago(iso) {
    if (!iso) return "";
    const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (m < 1) return "just now";
    if (m < 60) return `${m} minute${m === 1 ? "" : "s"} ago`;
    const hr = Math.round(m / 60);
    return `${hr} hour${hr === 1 ? "" : "s"} ago`;
  }
  function verdictLabel(p) {
    const vc = (p.verdict_class || (p.suitable ? "suitable" : "")).toLowerCase();
    const name = vc === "not suitable" || vc === "not_suitable" ? "Not suitable" : vc === "borderline" ? "Borderline" : vc ? "Suitable" : "Assessed";
    const score = typeof p.overall === "number" ? `, ${p.overall.toFixed(1).replace(/\.0$/, "")} of 10` : "";
    const cls = name === "Not suitable" ? "notsuitable" : name === "Borderline" ? "borderline" : "";
    return { text: name + score, cls, kind: name };
  }
  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

  // ------------------------------------------------------------------
  // Run state and routing
  // ------------------------------------------------------------------

  function isBusy(run) {
    if (!run) return false;
    if (BUSY.includes(run.status)) return true;
    if (run.plan && run.plan.status === "drafting") return true;
    const groups = [run.checks, run.assessment && run.assessment.stages, run.generate && run.generate.stages];
    if (run.status === "done" || run.status === "failed" || run.status === "stopped") return false;
    return groups.some((g) => (g || []).some((s) => s.state === "running"));
  }
  function earlyEditable(run) { return !run || EDITABLE_EARLY.includes(run.status); }
  function allowedStep(run) {
    if (!run) return 1;
    let s = run.step || 1;
    const st = run.status;
    s = Math.max(s, 2);   // a run exists once setup is done
    if (st === "ready" && run.scan) s = Math.max(s, 3);
    if (run.brief && run.scan) s = Math.max(s, 4);
    if (["assessing", "scoping", "not_suitable"].includes(st)) s = Math.max(s, 5);
    if (["planning", "plan_ready"].includes(st)) s = Math.max(s, 6);
    if (st === "generating" || (run.generate && run.generate.started_at)) s = Math.max(s, 7);
    if (st === "done" && run.results) s = Math.max(s, 8);
    return Math.min(8, s);
  }
  function go(step, id) {
    const target = `#/run/${enc(id || state.runId)}/${step}`;
    if (location.hash === target) render(); else location.hash = target;
  }

  function setRun(run) {
    if (!run) return;
    const id = runId(run);
    if (state.runId && id !== state.runId) ui = freshUI();
    state.run = run; state.runId = id;
  }

  async function route() {
    stopPoll();
    const m = location.hash.match(/^#\/run\/([^/]+)(?:\/(\d))?/);
    if (!m) {
      if (state.runId) ui = freshUI();
      state.run = null; state.runId = null; state.step = 1;
      state.view = location.hash.startsWith("#/new") ? "new" : "home";
      if (state.view === "home") return renderHome();
      render();
      return;
    }
    state.view = "run";
    const id = decodeURIComponent(m[1]);
    const wanted = Math.max(1, Math.min(8, parseInt(m[2] || "0", 10) || 0));
    if (id !== state.runId) { ui = freshUI(); state.run = null; state.runId = id; }
    try {
      setRun(asRun(await api(`/api/tz/runs/${enc(id)}`)));
      banner(null);
    } catch (e) {
      banner(`Could not load run ${id}: ${e.message}`);
      state.run = null;
      renderMissing(id);
      return;
    }
    const allowed = allowedStep(state.run);
    const step = wanted ? Math.min(wanted, allowed) : allowed;
    state.step = step;
    if (step !== wanted) { history.replaceState(null, "", `#/run/${enc(id)}/${step}`); }
    render();
  }

  function stopPoll() { state.pollToken++; clearTimeout(state.pollTimer); state.pollTimer = null; }
  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (!state.run || !isBusy(state.run)) return;
    const token = ++state.pollToken;
    state.pollTimer = setTimeout(async () => {
      if (token !== state.pollToken || !state.runId) return;
      try {
        const run = asRun(await api(`/api/tz/runs/${enc(state.runId)}`));
        if (token !== state.pollToken) return;
        const changed = JSON.stringify(run) !== JSON.stringify(state.run);
        setRun(run);
        banner(null);
        if (changed) render(); else schedulePoll();
      } catch (e) {
        banner(`Lost contact with the server: ${e.message}. Retrying…`);
        if (token === state.pollToken) state.pollTimer = setTimeout(schedulePoll, POLL_MS * 2);
      }
    }, POLL_MS);
  }
  async function refresh() {
    if (!state.runId) return;
    try { setRun(asRun(await api(`/api/tz/runs/${enc(state.runId)}`))); } catch (e) { banner(e.message); }
    render();
  }
  function cellValue(v) {
    // A comparison cell: short values as they are, long ones (a whole report, a JSON dump) folded.
    if (v === null || v === undefined || v === "null") return "—";
    const text = typeof v === "string" ? v : JSON.stringify(v);
    if (text.length <= 80) return text;
    return h("details", { class: "cell-more" }, h("summary", null, text.slice(0, 60).trim() + "…"),
      h("pre", { class: "cell-full" }, text));
  }

  function retryBox(run, fallback) {
    // A failed or stopped run: what went wrong, and a way to carry on from where it got to.
    if (run.status !== "failed" && run.status !== "stopped") return null;
    return h("div", { class: "errbox" },
      run.status === "stopped" ? "This run was stopped." : (run.error || fallback),
      h("div", { style: "margin-top:8px" },
        h("button", { type: "button", class: "btn pri", disabled: ui.busy,
          onclick: () => act(() => api(`/api/tz/runs/${enc(runId(run))}/retry`, { method: "POST", body: {} })) },
          "Try again")));
  }

  async function act(fn) {
    // Run an action that returns a run (or nothing); show errors; re-render.
    if (ui.busy) return;
    ui.busy = true; ui.error = null; render();
    try {
      const out = await fn();
      if (out && (out.id || out._id || out.run)) setRun(asRun(out));
      return true;
    } catch (e) {
      ui.error = e.message;
      return false;
    } finally {
      ui.busy = false;
      render();
    }
  }

  // ------------------------------------------------------------------
  // Frame: header, window, strip, notes
  // ------------------------------------------------------------------

  function render() {
    const app = document.getElementById("app");
    const run = state.run;
    const step = run ? state.step : 1;
    const label = document.getElementById("run-label");
    label.textContent = run ? `${runId(run)} · ${(run.repo && (run.repo.name || run.repo.index)) || sourceName(run.source)}` : "";
    const tui = document.getElementById("tui-link");
    tui.href = (run && run.links && run.links.temporal_ui) || (state.config && state.config.temporal_ui_url) || "http://localhost:8233";
    document.title = run ? `Temporalize · ${runId(run)} · ${STEPS[step - 1]}` : "Temporalize";

    const screen = SCREENS[step - 1];
    let body;
    try { body = screen(run); }
    catch (e) { console.error(e); body = h("div", { class: "errbox" }, `This screen could not be drawn: ${e.message}`); }

    const page = h("div", null,
      h("p", { class: "kicker" }, `Screen ${step} of 8`),
      h("h2", null, STEPS[step - 1]),
      h("p", { class: "lede" }, LEDES[step - 1]),
      h("div", { class: "grid" },
        h("div", { class: "win" }, windowBar(step, run), h("div", { class: "body" }, body), strip(run)),
        notesPanel(step)));
    app.replaceChildren(page);
    schedulePoll();
  }

  // ------------------------------------------------------------------
  // Home: earlier runs
  // ------------------------------------------------------------------

  function shortSource(src) {
    // github.com/acme/backoffice -> acme/backoffice; a folder -> its name
    if (!src) return "";
    const parts = String(src).replace(/\.git$/, "").replace(/\/+$/, "").split("/");
    return /^https?:|^git@/.test(src) ? parts.slice(-2).join("/") : parts.slice(-1)[0];
  }

  async function renderHome() {
    const app = document.getElementById("app");
    document.getElementById("run-label").textContent = "";
    document.title = "Temporalize";
    const list = h("div", { class: "runs-table", "aria-live": "polite" }, h("p", { class: "loading-line" }, h("span", { class: "spin" }, "◌"), "Loading runs…"));
    app.replaceChildren(h("div", null,
      h("p", { class: "kicker" }, "Temporalize"),
      h("div", { class: "home-head" },
        h("div", null, h("h2", null, "Runs"),
          h("p", { class: "lede", style: "margin-bottom:0" }, "Each run takes one repository from setup to a running Temporal POC. Open one to step through its eight screens as they were captured.")),
        h("a", { class: "btn pri", href: "#/new" }, "+ New run")),
      list));
    let runs;
    try { runs = (await api("/api/tz/runs")).runs || []; }
    catch (e) { list.replaceChildren(h("div", { class: "errbox" }, `Could not load runs: ${e.message}`)); return; }
    if (state.view !== "home") return;
    if (!runs.length) {
      list.replaceChildren(h("div", { class: "win empty-home" }, h("div", { class: "body" },
        h("h3", null, "No runs yet"),
        h("p", { class: "hint" }, "Point Temporalize at a repository to see what it does today and what it would look like on Temporal."),
        h("div", { class: "btns" }, h("span"), h("a", { class: "btn pri", href: "#/new" }, "Start your first run")))));
      return;
    }
    list.replaceChildren(...runs.map((r) => {
      const id = runId(r);
      const step = Math.max(1, Math.min(8, r.step || 1));
      const outcomes = (r.outcomes || []).filter((o) => o.ok === true).map((o) => o.label);
      const tone = r.status === "done" ? "ok" : (r.status === "failed" || r.status === "scan_failed") ? "bad" : r.status === "stopped" ? "" : "live";
      return h("a", { class: "run-row", href: `#/run/${enc(id)}/${step}` },
        h("div", { class: "run-main" },
          h("div", { class: "run-title" }, h("b", null, r.repo || shortSource(r.source) || id),
            h("span", { class: "run-id" }, id)),
          h("div", { class: "run-sub" }, r.process ? h("span", null, r.process) : h("span", { class: "hint" }, r.source || ""),
            r.criteria_summary ? h("span", null, r.criteria_summary) : null,
            outcomes.length ? h("span", { class: "ok-text" }, "✓ " + outcomes.join(" · ")) : null,
            r.error && tone === "bad" ? h("span", { class: "bad-text" }, r.error.slice(0, 120)) : null)),
        h("div", { class: "run-progress", title: `Reached step ${step} of 8: ${STEPS[step - 1]}` },
          h("div", { class: "mini-steps" }, STEPS.map((_, k) => h("i", { class: k < step ? "done" : "" }))),
          h("span", null, `Step ${step}: ${STEPS[step - 1]}`)),
        h("div", { class: "run-meta" },
          h("span", { class: "pill " + tone }, statusNow(r)),
          h("span", { class: "hint" }, `${plural(r.calls || 0, "model call")} · ${ago(r.updated_at || r.created_at)}`)));
    }));
  }

  function renderMissing(id) {
    const app = document.getElementById("app");
    app.replaceChildren(h("div", { class: "win", style: "max-width:640px" },
      h("div", { class: "body" }, h("h3", null, `Run ${id} is not available`),
        h("p", { class: "hint" }, "It may have been removed, or the server is not reachable."),
        h("div", { class: "btns" }, h("a", { class: "btn", href: "#/" }, "All runs"),
          h("button", { class: "btn pri", type: "button", onclick: route }, "Try again")))));
  }

  function windowBar(step, run) {
    const allowed = allowedStep(run);
    return h("div", { class: "bar" }, "Temporalize",
      h("span", { class: "where" }, `Step ${step} of 8: ${STEPS[step - 1]}`),
      h("div", { class: "steps", role: "navigation", "aria-label": "Steps" },
        STEPS.map((name, k) => h("button", {
          type: "button", class: k < step - 1 ? "done" : k === step - 1 ? "cur" : "",
          title: `Step ${k + 1}: ${name}`, "aria-label": `Step ${k + 1}: ${name}`,
          "aria-current": k === step - 1 ? "step" : null,
          disabled: !run || k + 1 > allowed,
          onclick: () => go(k + 1),
        }))));
  }

  function strip(run) {
    const s = (run && run.strip) || {};
    const sent = s.sent || [];
    const now = s.now || (run ? statusNow(run) : "Waiting for setup");
    return h("div", { class: "strip", "aria-label": "Run status" },
      h("div", null, h("span", null, "Now"), h("b", { "aria-live": "polite" }, now)),
      h("div", null, h("span", null, "Model calls"), h("b", null, fmtInt(s.calls || 0))),
      h("div", null, h("span", null, "Sent so far"),
        h("b", null, h("i", { class: sent.length ? "dot out" : "dot" }), sent.length ? sent.join(", ") : "Nothing")));
  }
  function statusNow(run) {
    return ({
      scanning: "Scanning locally", scan_failed: "Scan needs attention", ready: "Ready for review",
      assessing: "Assessing", scoping: "Waiting for your scope", not_suitable: "Not suitable",
      planning: "Drafting the plan", plan_ready: "Waiting for your approval", generating: "Building the POC",
      done: "Done", failed: "Failed", stopped: "Stopped",
    })[run.status] || run.status || "";
  }

  function notesPanel(step) {
    const n = NOTES[step - 1];
    return h("aside", { class: "notes", "aria-label": "About this screen" },
      n.map(([title, content]) => h("section", null, h("h4", null, title),
        content === "zero" ? h("span", { class: "calls zero" }, "None")
          : typeof content === "object" && content.calls ? h("span", { class: "calls some" }, content.calls)
          : Array.isArray(content) ? h("ul", null, content.map((t) => h("li", null, t)))
          : h("p", null, content))));
  }

  const LEDES = [
    "Every setting the run needs, entered once before any code is read. The API key comes first, because nothing later works without it.",
    "The results of the local scan, for you to check before anything is sent. Plain code only, a few seconds total.",
    "Pick what the POC should show, not what’s broken. Anything the code can answer, like how often it runs, isn’t asked here.",
    "A short list of criteria picked by simple rules from the scan and the goal. Every suggestion says why it’s there. P0 and P1 match the Mutual Action Plan template.",
    "Code Atlas indexes the repository and builds its Temporal report, then lists the processes it found. The process you named comes first, with every way the code runs it.",
    "The plan the POC is built from, pre-filled from the code: what it does today, fixes worth making, the test data both versions run on, and the design. Change only what looks wrong.",
    "Progress only, nothing to edit. Every loop is visible: how many review passes ran, what each found, and what was fixed.",
    "What this run proved, the side-by-side comparison, and everything an SA needs to review: diff, scorecard, the test data, and limitations.",
  ];

  const NOTES = [
    [["Editable", ["Anthropic API key, checked as soon as it’s pasted, and model", "Temporal target: local, or Cloud address, namespace and API key", "Repository: a local folder or a git URL with branch", "A read-only token, only when the repo is private"]],
      ["Return visits", "Once saved, the key, model and Temporal target fold into one line with “Change”, so only the repository is asked."],
      ["Model calls", "zero"]],
    [["Visible", ["File, line, test and secret counts", "Detected tools and integrations", "How the code runs today, including what wasn’t found"]],
      ["Editable", ["Folders and file patterns to leave out"]],
      ["Model calls", "zero"],
      ["If a step fails", "The line turns red with what went wrong and how to fix it. “Try again” reruns the checklist."]],
    [["Editable", ["Mode: a process in mind, or let Code Atlas find opportunities", "Which process", "One or two focus areas", "Pasted notes", "Import or export a brief file"]],
      ["Model calls", "zero"],
      ["What isn’t asked", "How often it runs, how it’s triggered, and how much it handles all come from the code on screen 5."]],
    [["Editable", ["Each criterion on or off", "P0 or P1 for each", "Custom criteria"]],
      ["Visible", ["Why each one was suggested", "What this run can’t prove"]],
      ["Model calls", "zero"],
      ["The one button", "“Start assessment” is the first point where anything leaves the machine, and it says so right beside it."]],
    [["Editable", ["Which process to build", "Each way it runs, each step, and each failure to address: in or out", "Notes on anything else about scope"]],
      ["Visible", ["Verdict and fit score for every process", "What a crash loses today", "Every way the code runs the process, read from the code", "Anything in your notes the code contradicts"]],
      ["Model calls", { calls: "Counted live in the strip" }],
      ["Gates", "“Not suitable” stops here with reasons. “Borderline” asks you to confirm."]],
    [["Editable", ["Drop or reword a behavior the POC must keep", "Include or skip each fix", "Test data files"]],
      ["Defaults", "Every behavior is kept and every fix included until you say otherwise. The ones the code checks were less sure about come first."],
      ["Model calls", { calls: "Drafting claims and design" }],
      ["Why the test data is here", "Both versions run on exactly this data on screen 7, so the original code becomes the answer key."]],
    [["Visible", ["Each stage as it runs, and how long the run has taken", "Review passes, with findings per pass", "A Temporal UI link once the smoke run starts"]],
      ["Editable", "Nothing. You can leave and come back, since the run is itself a Temporal workflow."],
      ["Model calls", { calls: "Writing and revising" }],
      ["Loop limit", "Review and revise stops at three passes. Anything left over is listed in the results."]],
    [["Visible", ["Which of the three outcomes this run proved", "Side-by-side comparison, with intended fixes and new behavior labeled", "Scorecard, diff, evidence, limitations", "The test data, labeled as synthetic"]],
      ["Actions", ["Export the review packet", "Push the branch to the customer’s GitHub", "Open the workflow in the Temporal UI"]],
      ["Model calls", "zero"]],
  ];

  function sourceName(src) {
    if (!src) return "";
    if (src.kind === "git") return (src.url || "").replace(/^https?:\/\//, "").replace(/\.git$/, "");
    return src.path || "";
  }
  function readOnlyNote(run) {
    return earlyEditable(run) ? null
      : h("p", { class: "ro" }, "The assessment has started, so this screen is read-only. Start a new run to change it.");
  }
  function errorBox() { return ui.error ? h("div", { class: "errbox", role: "alert" }, ui.error) : null; }

  function checklist(lines, opts = {}) {
    const icon = { done: "✓", running: "◌", pending: "○", failed: "✗", skipped: "–" };
    return h("ul", { class: "chk", "aria-live": "polite", style: opts.style || null },
      (lines || []).map((l) => h("li", { class: l.state || "pending" },
        h("span", { class: "ic" + (l.state === "running" ? " spin" : "") , "aria-hidden": "true" }, icon[l.state] || "○"),
        h("span", { class: "lbt" }, l.label || l.id, l.state === "running" ? "…" : ""),
        l.detail ? h("span", { class: "sub" }, l.detail) : null,
        l.state === "failed" && l.error ? h("span", { class: "errl" }, l.error) : null)));
  }

  // ------------------------------------------------------------------
  // Screen 1: setup
  // ------------------------------------------------------------------

  function initSetup() {
    const s = state.settings || {};
    const t = s.temporal || {};
    ui.setup = {
      changing: !s.saved, apiKey: "", keyStatus: null, checkingKey: false,
      model: s.model || ((s.models || [])[0] || {}).id || "",
      tmode: t.mode || "local", taddr: t.mode === "cloud" ? (t.address || "") : "", tns: t.mode === "cloud" ? (t.namespace || "") : "", tkey: "",
      rmode: "git", url: "", branch: "main", token: "", showToken: false,
      path: "", pathInfo: null, lastDir: null, dropNote: null,
    };
  }

  function screenSetup(run) {
    if (run) return setupReadOnly(run);
    if (!state.settings) {
      return h("div", { class: "loading-line" }, h("span", { class: "spin" }, "◌"), "Loading settings…");
    }
    if (!ui.setup) initSetup();
    const u = ui.setup, s = state.settings;

    const credentials = (s.saved && !u.changing) ? h("div", null,
      h("div", { class: "lbl" }, "Saved settings"),
      h("div", { class: "summary-line" },
        h("span", null, `${modelLabel(s.model)} · key ••••${s.anthropic.last4 || "????"} · ${s.temporal.mode === "cloud" ? `Temporal Cloud (${s.temporal.namespace || s.temporal.address})` : `Local dev server (${s.temporal.address})`}`),
        h("button", { type: "button", class: "lnk", onclick: () => { u.changing = true; render(); } }, "Change")))
      : credentialFields(u, s);

    return h("div", null,
      h("h3", null, "Set up your evaluation"),
      credentials,
      h("div", { class: "lbl" }, "Repository"),
      seg([["local", "Local folder"], ["git", "Git URL"]], u.rmode, (v) => { u.rmode = v; render(); }, "Repository source"),
      u.rmode === "git" ? gitFields(u) : localFields(u),
      errorBox(),
      h("div", { class: "btns" }, h("span"),
        h("button", { type: "button", class: "btn pri", disabled: ui.busy, onclick: setupContinue },
          ui.busy ? "Checking…" : "Check and continue")));
  }

  function modelLabel(id) {
    const m = ((state.settings && state.settings.models) || []).find((x) => x.id === id);
    return m ? m.label : (id || "Default model");
  }

  function seg(options, current, onPick, label, disabled) {
    return h("div", { class: "seg", role: "group", "aria-label": label },
      options.map(([v, text]) => h("button", {
        type: "button", class: v === current ? "on" : "", "aria-pressed": String(v === current),
        disabled: !!disabled, onclick: () => onPick(v),
      }, text)));
  }

  function credentialFields(u, s) {
    const a = s.anthropic || {};
    let keyStatus = null;
    if (u.checkingKey) keyStatus = h("span", { class: "hint" }, "Checking…");
    else if (u.keyStatus && u.keyStatus.ok) keyStatus = h("span", { class: "ok" }, "✓ Valid");
    else if (u.keyStatus && !u.keyStatus.ok) keyStatus = h("span", { class: "err" }, u.keyStatus.error || "Not valid");
    else if (!u.apiKey && a.configured) keyStatus = h("span", { class: "ok" }, `✓ ••••${a.last4 || ""} ${a.source === "env" ? "(from .env)" : "(saved)"}`);

    const keyInput = h("input", {
      class: "field", type: "password", autocomplete: "off", spellcheck: "false", id: "tz-key",
      placeholder: a.configured ? `••••${a.last4 || ""} — paste a new key to replace it` : "sk-ant-…",
      value: u.apiKey,
      oninput: (e) => { u.apiKey = e.target.value.trim(); u.keyStatus = null; },
      onpaste: () => setTimeout(() => checkKey(), 0),
      onblur: () => { if (u.apiKey && !u.keyStatus) checkKey(); },
    });
    const modelSel = h("select", { class: "field", id: "tz-model", onchange: (e) => { u.model = e.target.value; if (u.apiKey || a.configured) checkKey(true); } },
      (s.models || []).map((m) => h("option", { value: m.id, selected: m.id === u.model }, m.label)));
    if (!(s.models || []).some((m) => m.id === u.model) && u.model) modelSel.appendChild(h("option", { value: u.model, selected: true }, u.model));

    return h("div", null,
      h("div", { class: "two" },
        h("div", null, h("label", { class: "lbl", for: "tz-key" }, "Anthropic API key"), keyInput,
          h("div", { style: "min-height:18px;margin-top:3px" }, keyStatus)),
        h("div", null, h("label", { class: "lbl", for: "tz-model" }, "Model"), modelSel)),
      h("div", { class: "lbl" }, "Temporal"),
      seg([["local", "Local dev server"], ["cloud", "Temporal Cloud"]], u.tmode, (v) => { u.tmode = v; render(); }, "Temporal target"),
      u.tmode === "cloud" ? h("div", null,
        h("div", { class: "two", style: "margin-top:8px" },
          h("div", null, h("label", { class: "lbl", for: "tz-taddr", style: "margin-top:0" }, "Address"),
            h("input", { class: "field", id: "tz-taddr", placeholder: "my-ns.a1b2c.tmprl.cloud:7233", value: u.taddr, oninput: (e) => { u.taddr = e.target.value.trim(); } })),
          h("div", null, h("label", { class: "lbl", for: "tz-tns", style: "margin-top:0" }, "Namespace"),
            h("input", { class: "field", id: "tz-tns", placeholder: "my-ns.a1b2c", value: u.tns, oninput: (e) => { u.tns = e.target.value.trim(); } }))),
        h("label", { class: "lbl", for: "tz-tkey" }, "Temporal Cloud API key"),
        h("input", { class: "field", id: "tz-tkey", type: "password", autocomplete: "off",
          placeholder: s.temporal && s.temporal.api_key_set ? "Saved — paste a new key to replace it" : "", value: u.tkey,
          oninput: (e) => { u.tkey = e.target.value.trim(); } }))
        : h("p", { class: "hint", style: "margin-top:6px" }, `The POC runs on the dev server in this stack (${(s.temporal && s.temporal.mode === "local" && s.temporal.address) || "localhost:7233"}).`));
  }

  async function checkKey(force) {
    const u = ui.setup;
    if (!u || (!u.apiKey && !force)) return;
    u.checkingKey = true; render();
    try { u.keyStatus = await api("/api/tz/settings/check-key", { method: "POST", body: { api_key: u.apiKey, model: u.model } }); }
    catch (e) { u.keyStatus = { ok: false, error: e.message }; }
    u.checkingKey = false; render();
  }

  function gitFields(u) {
    return h("div", null,
      h("div", { class: "two", style: "margin-top:8px" },
        h("div", null, h("label", { class: "lbl", for: "tz-url", style: "margin-top:0" }, "Repository URL"),
          h("input", { class: "field", id: "tz-url", placeholder: "https://github.com/acme/backoffice", value: u.url, spellcheck: "false",
            oninput: (e) => { u.url = e.target.value.trim(); } })),
        h("div", null, h("label", { class: "lbl", for: "tz-branch", style: "margin-top:0" }, "Branch"),
          h("input", { class: "field", id: "tz-branch", value: u.branch, spellcheck: "false", oninput: (e) => { u.branch = e.target.value.trim(); } }))),
      u.showToken ? h("div", null,
        h("label", { class: "lbl", for: "tz-token" }, "Read-only token"),
        h("input", { class: "field", id: "tz-token", type: "password", autocomplete: "off", value: u.token, oninput: (e) => { u.token = e.target.value.trim(); } }),
        h("p", { class: "hint" }, "It’s used only to clone, never sent to a model."))
        : h("p", { class: "hint", style: "margin-top:8px" },
          h("button", { type: "button", class: "lnk", onclick: () => { u.showToken = true; render(); setTimeout(() => { const t = document.getElementById("tz-token"); if (t) t.focus(); }); } }, "Private repo? Add a read-only token."),
          " It’s used only to clone, never sent to a model."));
  }

  function localFields(u) {
    const info = u.pathInfo;
    let status = null;
    if (info && info.error) status = h("span", { class: "err" }, info.error);
    else if (info && info.git && info.git.error) status = h("span", { class: "err" }, info.git.error);
    else if (info && info.git) status = h("span", { class: "ok" }, `✓ git repository · ${info.git.branch || "detached"} @ ${info.git.short || ""}${info.git.subject ? " · " + info.git.subject : ""}`);
    else if (info && info.path) status = h("span", { class: "err" }, "Not a git repository: pick the folder that contains .git.");
    const drop = h("div", {
      class: "drop",
      ondragover: (e) => { e.preventDefault(); e.currentTarget.classList.add("over"); },
      ondragleave: (e) => e.currentTarget.classList.remove("over"),
      ondrop: (e) => {
        e.preventDefault();
        const item = e.dataTransfer.items && e.dataTransfer.items[0];
        const entry = item && item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
        const name = entry ? entry.name : (e.dataTransfer.files[0] && e.dataTransfer.files[0].name);
        if (!name) return;
        const base = u.lastDir || (u.pathInfo && u.pathInfo.root) || "";
        u.path = base ? `${base.replace(/\/$/, "")}/${name}` : name;
        u.dropNote = "Browsers don’t share a dropped folder’s full path, so check it, or use Browse.";
        u.pathInfo = null;
        render();
        lookupPath();
      },
    }, "Drop a folder here, or type its path");
    return h("div", null,
      h("label", { class: "lbl", for: "tz-path" }, "Folder"),
      h("div", { class: "inrow" },
        h("input", { class: "field", id: "tz-path", placeholder: "~/code/my-service", value: u.path, spellcheck: "false",
          oninput: (e) => { u.path = e.target.value.trim(); u.pathInfo = null; u.dropNote = null; },
          onblur: () => lookupPath() }),
        h("button", { type: "button", class: "btn", onclick: () => openFolderPicker(u.path || u.lastDir || "") }, "Browse…")),
      h("div", { style: "min-height:18px;margin-top:3px" }, status),
      u.dropNote ? h("p", { class: "hint" }, u.dropNote) : null,
      drop,
      h("p", { class: "hint", style: "margin-top:6px" }, "Only committed files at HEAD are assessed. The folder must be under the folder this stack can read (REPOS_ROOT)."));
  }

  async function lookupPath() {
    const u = ui.setup;
    if (!u || !u.path) return;
    try { u.pathInfo = await api(`/api/fs?path=${enc(u.path)}`); }
    catch (e) { u.pathInfo = { error: e.message }; }
    render();
  }

  async function setupContinue() {
    const u = ui.setup, s = state.settings;
    ui.error = null;
    const credsShown = !(s.saved && !u.changing);
    if (credsShown) {
      if (!u.apiKey && !(s.anthropic && s.anthropic.configured)) { ui.error = "Paste an Anthropic API key."; return render(); }
      if (u.tmode === "cloud" && (!u.taddr || !u.tns)) { ui.error = "Temporal Cloud needs an address and a namespace."; return render(); }
      if (u.tmode === "cloud" && !u.tkey && !(s.temporal && s.temporal.mode === "cloud" && s.temporal.api_key_set)) { ui.error = "Temporal Cloud needs an API key."; return render(); }
    }
    let source;
    if (u.rmode === "git") {
      if (!u.url) { ui.error = "Enter the repository URL."; return render(); }
      source = { kind: "git", url: u.url, branch: u.branch || "main" };
      if (u.token) source.token = u.token;
    } else {
      if (!u.path) { ui.error = "Enter or browse to the repository folder."; return render(); }
      source = { kind: "local", path: (u.pathInfo && u.pathInfo.path) || u.path };
    }
    let created = null;
    const ok = await act(async () => {
      if (credsShown) {
        if (u.apiKey && !(u.keyStatus && u.keyStatus.ok)) {
          const k = await api("/api/tz/settings/check-key", { method: "POST", body: { api_key: u.apiKey, model: u.model } });
          u.keyStatus = k;
          if (!k.ok) throw new Error(`Anthropic API key: ${k.error || "not valid"}`);
        }
        const body = { model: u.model, temporal: { mode: u.tmode } };
        if (u.apiKey) body.api_key = u.apiKey;
        if (u.tmode === "cloud") { body.temporal.address = u.taddr; body.temporal.namespace = u.tns; if (u.tkey) body.temporal.api_key = u.tkey; }
        state.settings = await api("/api/tz/settings", { method: "PUT", body });
      }
      created = asRun(await api("/api/tz/runs", { method: "POST", body: { source } }));
      return created;
    });
    if (ok && created) { ui.setup = null; go(2, runId(created)); }
  }

  function setupReadOnly(run) {
    // The settings this run was created with (captured then; later changes never apply to it).
    const src = run.source || {};
    const snap = run.settings;
    const t = (snap && snap.temporal) || {};
    const row = (label, value) => h("div", { class: "kv" }, h("span", null, label), h("span", null, value));
    return h("div", null,
      readOnlyNote(run) || h("p", { class: "ro" }, "This run’s setup is fixed. Start a new run to change it."),
      h("h3", null, "Set up your evaluation"),
      snap ? h("p", { class: "hint" }, `Captured ${ago(snap.captured_at || run.created_at)}, when this run was created.`)
        : h("p", { class: "hint" }, "This run is older than captured settings; it uses the current ones."),
      h("div", { class: "kvs" },
        snap ? row("Anthropic API key", `••••${(snap.anthropic && snap.anthropic.last4) || "????"}${snap.anthropic && snap.anthropic.source === "env" ? " (from .env)" : ""}`) : null,
        snap ? row("Model", modelLabel(snap.model)) : null,
        snap ? row("Temporal", t.mode === "cloud" ? `Temporal Cloud · ${t.display_address || t.address} · ${t.namespace}` : `Local dev server · ${t.display_address || "localhost:7233"}`) : null,
        row("Repository", src.kind === "git" ? h("span", null, h("code", null, sourceName(src)), ` · branch ${src.branch || "main"}${src.token_set ? " · read-only token" : ""}`)
          : h("span", null, "Local folder ", h("code", null, src.path || ""))),
        run.repo && run.repo.commit ? row("Commit", h("code", null, run.repo.commit.slice(0, 12))) : null,
        run.repo && run.repo.index ? row("Code Atlas index", h("code", null, run.repo.index)) : null),
      h("div", { class: "btns" }, h("a", { class: "btn", href: "#/new" }, "Start a new run"),
        h("button", { type: "button", class: "btn pri", onclick: () => go(2) }, "Continue")));
  }

  // Folder picker (GET /api/fs)
  async function openFolderPicker(start) {
    let current = null, error = null;
    const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(); } });
    function close() { back.remove(); document.removeEventListener("keydown", onKey); }
    function onKey(e) { if (e.key === "Escape") close(); }
    document.addEventListener("keydown", onKey);
    async function load(path) {
      try { current = await api(`/api/fs?path=${enc(path || "")}`); error = null; ui.setup.lastDir = current.path; }
      catch (e) { error = e.message; if (!current && path) return load(""); }
      draw();
    }
    function choose(path, git) {
      ui.setup.path = path;
      ui.setup.pathInfo = { path, git };
      ui.setup.dropNote = null;
      close();
      render();
      if (!git) lookupPath();
    }
    function draw() {
      const g = current && current.git;
      const canUse = g && !g.error;
      back.replaceChildren(h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": "Choose a repository folder" },
        h("div", { class: "modal-h" }, h("h3", null, "Choose a repository folder"),
          h("button", { type: "button", class: "close", "aria-label": "Close", onclick: close }, "×")),
        h("div", { class: "modal-b" },
          error ? h("div", { class: "errbox", style: "margin:0 0 8px" }, error) : null,
          current ? h("div", { class: "fs-path" }, current.path) : h("div", { class: "loading-line" }, "Loading…"),
          current && g ? h("p", { class: g.error ? "err" : "ok", style: "margin:0 0 8px" },
            g.error ? g.error : `✓ git repository · ${g.branch || "detached"} @ ${g.short}${g.subject ? " · " + g.subject : ""}`) : null,
          current ? h("ul", { class: "fs-list" },
            current.parent ? h("li", null, h("button", { type: "button", class: "nm", onclick: () => load(current.parent) }, "↑ ..")) : null,
            current.entries.map((e) => h("li", null,
              h("button", { type: "button", class: "nm", onclick: () => load(e.path) }, "📁 " + e.name),
              e.git ? h("span", { class: "gitmark" }, "git") : null,
              e.git ? h("button", { type: "button", class: "btn sm", onclick: () => choose(e.path, null) }, "Choose") : null)),
            !current.entries.length ? h("li", null, h("span", { class: "empty", style: "padding:8px 4px" }, "No subfolders")) : null) : null,
          current && current.truncated ? h("p", { class: "hint" }, "Only the first 500 folders are listed.") : null),
        h("div", { class: "modal-f" },
          h("button", { type: "button", class: "btn", onclick: close }, "Cancel"),
          h("button", { type: "button", class: "btn pri", disabled: !canUse, onclick: () => choose(current.path, g) }, "Use this folder"))));
    }
    document.getElementById("modal-root").appendChild(back);
    draw();
    load(start);
  }

  // ------------------------------------------------------------------
  // Screen 2: what we found
  // ------------------------------------------------------------------

  function screenFound(run) {
    const loading = run.status === "scanning" || run.status === "scan_failed" || !run.scan ||
      (run.checks || []).some((c) => c.state === "running" || c.state === "failed");
    if (loading) {
      const failed = run.status === "scan_failed" || (run.checks || []).some((c) => c.state === "failed");
      return h("div", null,
        h("h3", null, "Checking your setup and scanning locally"),
        h("p", { class: "hint" }, "Nothing leaves your machine during this step."),
        checklist(run.checks),
        failed && run.error ? h("div", { class: "errbox" }, run.error) : null,
        errorBox(),
        h("div", { class: "btns" },
          h("button", { type: "button", class: "btn", onclick: () => go(1) }, "Back"),
          failed && earlyEditable(run) ? h("button", { type: "button", class: "btn pri", disabled: ui.busy,
            onclick: () => act(() => api(`/api/tz/runs/${enc(state.runId)}/rescan`, { method: "POST", body: {} })) }, "Try again") : null));
    }
    const sc = run.scan;
    const editable = earlyEditable(run);
    const filesLabel = sc.python_files ? "Python files" : "Files";
    const secrets = sc.secrets || 0;
    return h("div", null,
      readOnlyNote(run),
      h("h3", null, "Here’s what we found"),
      h("p", { class: "hint" }, "From the local scan. Nothing has been sent to a model."),
      h("div", { class: "mets", style: "margin-top:12px" },
        met(fmtInt(sc.python_files || sc.files || 0), filesLabel),
        met(approx(sc.lines), "Lines"),
        met(fmtInt(sc.tests || 0), "Tests found"),
        met(fmtInt(secrets), "Secrets found", secrets > 0)),
      secrets > 0 ? h("div", { class: "errbox" },
        h("b", null, "Possible secrets in the code. "), "Values are never stored or sent. Remove them, or leave the files out below.",
        h("ul", { class: "plain" }, (sc.secret_findings || []).slice(0, 12).map((f) =>
          h("li", null, h("code", null, `${f.path}:${f.line}`), ` ${f.type || ""}`)))) : null,
      (sc.languages || []).length > 1 ? h("p", { class: "hint", style: "margin-top:6px" },
        sc.languages.map((l) => `${l.name} ${fmtInt(l.files)}`).join(" · ")) : null,
      h("div", { class: "lbl" }, "Tools and integrations"),
      (sc.integrations || []).length
        ? h("div", { class: "tags" }, sc.integrations.map((i) => h("span", { class: "tag", title: (i.files || []).join(", ") }, i.name)))
        : h("p", { class: "empty" }, "None detected from imports."),
      h("div", { class: "lbl" }, "How it runs today"),
      (sc.runs_today || []).length
        ? h("div", { class: "tags" }, sc.runs_today.map((r) => h("span", { class: "tag", title: (r.evidence || []).join(", ") }, r.name)))
        : h("p", { class: "empty" }, "Nothing detected."),
      (sc.not_found || []).length ? h("p", { class: "hint", style: "margin-top:6px" }, `Not found: ${sc.not_found.join(", ")}`) : null,
      h("div", { class: "lbl" }, "Left out of the assessment"),
      ui.exclEdit && editable ? exclusionEditor(sc) : h("div", { class: "field", style: "font-size:13px" },
        h("span", null, (sc.exclusions && sc.exclusions.summary) || ".git, __pycache__, data files, files over 1 MB"),
        editable ? h("button", { type: "button", class: "btn sm", onclick: () => { ui.exclEdit = true; render(); } }, "Edit") : null),
      errorBox(),
      h("div", { class: "btns" },
        h("button", { type: "button", class: "btn", onclick: () => go(1) }, "Back"),
        h("button", { type: "button", class: "btn pri", onclick: () => go(3) }, "Continue")));
  }
  function met(value, label, warn) {
    return h("div", { class: "met" + (warn ? " warn" : "") }, h("b", null, value), h("span", null, label));
  }
  function exclusionEditor(sc) {
    const ex = sc.exclusions || {};
    const folders = new Set(ex.folders || []);
    let patterns = (ex.patterns || []).join("\n");
    const ta = h("textarea", { class: "field code", style: "min-height:110px", "aria-label": "Patterns to leave out, one per line",
      oninput: (e) => { patterns = e.target.value; } }, patterns);
    ta.value = patterns;
    return h("div", { class: "conf" },
      h("div", { class: "lbl", style: "margin-top:0" }, "Patterns, one per line (a trailing / matches a folder anywhere)"),
      ta,
      (sc.top_folders || []).length ? h("div", null,
        h("div", { class: "lbl" }, "Top-level folders to leave out"),
        h("div", { class: "opts" }, sc.top_folders.map((f) => h("label", { class: "checkline", style: "margin-top:0" },
          h("input", { type: "checkbox", checked: folders.has(f.path), onchange: (e) => { if (e.target.checked) folders.add(f.path); else folders.delete(f.path); } }),
          h("span", null, h("code", null, f.path + "/"), ` ${plural(f.files, "file")}`))))) : null,
      h("div", { class: "sec-foot" },
        h("button", { type: "button", class: "btn", onclick: () => { ui.exclEdit = false; render(); } }, "Cancel"),
        h("button", { type: "button", class: "btn pri", disabled: ui.busy, onclick: async () => {
          const body = { exclude: { patterns: patterns.split("\n").map((p) => p.trim()).filter(Boolean), folders: [...folders] } };
          const ok = await act(() => api(`/api/tz/runs/${enc(state.runId)}/rescan`, { method: "POST", body }));
          if (ok) { ui.exclEdit = false; render(); }
        } }, "Apply and scan again")));
  }

  // ------------------------------------------------------------------
  // Screen 3: focus
  // ------------------------------------------------------------------

  function screenFocus(run) {
    if (!ui.focus || ui.focusFor !== runId(run)) {
      const b = run.brief || {};
      ui.focus = { mode: b.mode || "process", process: b.process || "", focus: [...(b.focus || [])], notes: b.notes || "" };
      ui.focusFor = runId(run);
    }
    const f = ui.focus;
    const editable = earlyEditable(run);
    const options = run.focus_options || [];
    return h("div", null,
      readOnlyNote(run),
      h("h3", null, "What should this POC focus on?"),
      h("div", { style: "margin-top:10px" },
        seg([["process", "I have a process in mind"], ["discover", "Find opportunities for me"]], f.mode,
          (v) => { f.mode = v; render(); }, "Mode", !editable)),
      f.mode === "process" ? h("div", null,
        h("label", { class: "lbl", for: "tz-proc" }, "Which process?"),
        h("input", { class: "field", id: "tz-proc", placeholder: "For example: the morning timesheet audit", value: f.process,
          readOnly: !editable, oninput: (e) => { f.process = e.target.value; } }))
        : h("p", { class: "hint", style: "margin-top:8px" }, "Code Atlas ranks every process it finds; the strongest fit comes first on screen 5."),
      h("div", { class: "lbl" }, `Focus, pick one or two`),
      options.length ? h("div", { class: "opts" }, options.map((o) => {
        const on = f.focus.includes(o.id);
        return h("button", {
          type: "button", class: "opt" + (on ? " on" : "") + (o.supported === false ? " unsupported" : ""), "aria-pressed": String(on),
          disabled: !editable,
          onclick: () => {
            if (on) f.focus = f.focus.filter((x) => x !== o.id);
            else { f.focus.push(o.id); if (f.focus.length > MAX_FOCUS) f.focus.shift(); }
            render();
          },
        }, h("b", null, o.title), h("span", null, o.hint || (o.supported === false ? "Not seen in the scan" : "")));
      })) : h("p", { class: "empty" }, "No focus options yet."),
      h("label", { class: "lbl", for: "tz-notes" }, "Notes from the Opportunity or calls, optional"),
      (() => { const t = h("textarea", { class: "field", id: "tz-notes", placeholder: "Paste anything useful", readOnly: !editable,
        oninput: (e) => { f.notes = e.target.value; } }); t.value = f.notes; return t; })(),
      h("p", { class: "hint", style: "margin-top:8px;display:flex;gap:14px;flex-wrap:wrap" },
        h("a", { class: "lnk", href: `/api/tz/runs/${enc(runId(run))}/brief.json`, download: `brief-${runId(run)}.json` }, "Export brief"),
        editable ? h("label", { class: "lnk", style: "cursor:pointer" }, "Import brief…",
          h("input", { type: "file", accept: "application/json,.json", hidden: true, onchange: importBrief })) : null,
        ui.importedCriteria ? h("span", null, `Imported, with ${plural(ui.importedCriteria.length, "criterion", "criteria")}.`) : null),
      errorBox(),
      h("div", { class: "btns" },
        h("button", { type: "button", class: "btn", onclick: () => go(2) }, "Back"),
        h("button", { type: "button", class: "btn pri", disabled: ui.busy || (editable && !f.focus.length), title: f.focus.length ? null : "Pick at least one focus area",
          onclick: focusContinue }, "Continue")));
  }

  async function importBrief(e) {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      const b = data.brief || data;
      ui.focus = { mode: b.mode === "discover" ? "discover" : "process", process: b.process || "", focus: (b.focus || []).slice(0, MAX_FOCUS), notes: b.notes || "" };
      ui.importedCriteria = Array.isArray(data.criteria) ? data.criteria : null;
      ui.error = null;
    } catch (err) { ui.error = `That file is not a brief: ${err.message}`; }
    render();
  }

  async function focusContinue() {
    const run = state.run;
    if (!earlyEditable(run)) return go(4);
    const ok = await act(async () => {
      let out = asRun(await api(`/api/tz/runs/${enc(state.runId)}/brief`, { method: "PUT", body: ui.focus }));
      if (ui.importedCriteria) {
        out = asRun(await api(`/api/tz/runs/${enc(state.runId)}/criteria`, { method: "PUT", body: { criteria: ui.importedCriteria } })) || out;
        ui.importedCriteria = null;
      }
      ui.criteria = null;   // take the refreshed suggestions
      return out;
    });
    if (ok) go(4);
  }

  // ------------------------------------------------------------------
  // Screen 4: success
  // ------------------------------------------------------------------

  const saveCriteriaSoon = debounce(() => saveCriteria().catch((e) => banner(`Could not save the criteria: ${e.message}`)), 500);
  async function saveCriteria() {
    if (!ui.criteria || !earlyEditable(state.run)) return;
    const out = asRun(await api(`/api/tz/runs/${enc(state.runId)}/criteria`, { method: "PUT", body: { criteria: ui.criteria } }));
    if (out && (out.id || out._id)) { state.run = out; }
  }

  function screenSuccess(run) {
    if (!ui.criteria || ui.criteriaFor !== runId(run)) {
      ui.criteria = (run.criteria || []).map((c) => ({ ...c }));
      ui.criteriaFor = runId(run);
    }
    const editable = earlyEditable(run);
    const list = ui.criteria;
    const change = () => { saveCriteriaSoon(); render(); };
    return h("div", null,
      readOnlyNote(run),
      h("h3", null, "What does success look like?"),
      h("p", { class: "hint" }, "P0 is required, P1 is nice to have. Click P0 or P1 to switch."),
      h("div", { style: "margin-top:8px" },
        list.length ? list.map((c) => h("div", { class: "crit" },
          h("input", { type: "checkbox", class: "box", checked: c.on !== false, disabled: !editable, "aria-label": `Include: ${c.text}`,
            onchange: (e) => { c.on = e.target.checked; change(); } }),
          h("span", { class: "txt" }, c.text,
            h("span", { class: "why" }, c.custom ? "Added by you" : (c.why || ""))),
          h("button", { type: "button", class: "pri-tag" + (c.priority === "P1" ? " p1" : ""), disabled: !editable,
            title: "Switch between P0 (required) and P1 (nice to have)",
            onclick: () => { c.priority = c.priority === "P0" ? "P1" : "P0"; change(); } }, `${c.priority || "P1"} ▾`),
          c.custom && editable ? h("button", { type: "button", class: "lnk", "aria-label": "Remove", style: "color:var(--ink-3)",
            onclick: () => { ui.criteria = list.filter((x) => x !== c); change(); } }, "×") : null))
          : h("p", { class: "empty" }, "No criteria suggested yet. Add your own below.")),
      editable ? (ui.addCrit ? h("div", { class: "addrow" },
        h("input", { class: "field", id: "tz-newcrit", placeholder: "For example: a failed run alerts the on-call channel", value: ui.newCrit.text,
          oninput: (e) => { ui.newCrit.text = e.target.value; },
          onkeydown: (e) => { if (e.key === "Enter") addCriterion(); if (e.key === "Escape") { ui.addCrit = false; render(); } } }),
        seg([["P0", "P0"], ["P1", "P1"]], ui.newCrit.priority, (v) => { ui.newCrit.priority = v; render(); }, "Priority"),
        h("button", { type: "button", class: "btn pri", onclick: addCriterion }, "Add"),
        h("button", { type: "button", class: "btn", onclick: () => { ui.addCrit = false; render(); } }, "Cancel"))
        : h("p", { style: "margin:8px 0 0;font-size:13px" },
          h("button", { type: "button", class: "lnk", onclick: () => { ui.addCrit = true; render(); setTimeout(() => { const i = document.getElementById("tz-newcrit"); if (i) i.focus(); }); } }, "+ Add a criterion"))) : null,
      h("p", { class: "hint", style: "margin-top:8px" }, "Not proven by a local run: scale, latency, and cost."),
      errorBox(),
      h("div", { class: "btns" },
        h("button", { type: "button", class: "btn", onclick: () => go(3) }, "Back"),
        editable ? h("span", { class: "r", style: "align-items:center" },
          h("span", { class: "note-go" }, "Next, source files are sent to Anthropic for the assessment."),
          h("button", { type: "button", class: "btn go", disabled: ui.busy || !list.some((c) => c.on !== false),
            onclick: startAssessment }, ui.busy ? "Starting…" : "Start assessment"))
          : h("button", { type: "button", class: "btn pri", onclick: () => go(5) }, "Continue")));
  }
  function addCriterion() {
    const text = ui.newCrit.text.trim();
    if (!text) return;
    ui.criteria.push({ id: `custom-${Date.now().toString(36)}`, text, why: "Added by you", priority: ui.newCrit.priority, on: true, custom: true });
    ui.newCrit = { text: "", priority: "P1" };
    ui.addCrit = false;
    saveCriteriaSoon();
    render();
  }
  async function startAssessment() {
    const ok = await act(async () => {
      await saveCriteria();
      return asRun(await api(`/api/tz/runs/${enc(state.runId)}/start`, { method: "POST", body: {} }));
    });
    if (ok) go(5);
  }

  // ------------------------------------------------------------------
  // Screen 5: report and scope
  // ------------------------------------------------------------------

  const REPORT_STATES = {
    pending: ["○", "Waiting"], investigating: ["◌", "Reading the code"], scoring: ["◌", "Scoring the fit"],
    writing: ["◌", "Writing the design"], done: ["✓", "Done"], failed: ["✗", "Failed"],
  };
  const REPORT_PHASES = {
    starting: "Starting", reading: "Reading the index", leads: "Checking leads against the source",
    processes: "Investigating each process", evaluation: "Reviewing existing Temporal code", rendering: "Writing the report", done: "Done",
  };

  function elapsed(since, until) {
    // A timer that keeps ticking between polls (see the interval in boot).
    if (!since) return null;
    return h("span", { class: "elapsed", "data-since": since, "data-until": until || null }, clock(since, until));
  }
  function clock(since, until) {
    const s = Math.max(0, Math.round(((until ? Date.parse(until) : Date.now()) - Date.parse(since)) / 1000));
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  }

  function reportProgress(run, stages) {
    // The Temporal report takes minutes: what it is doing, per process, with a running clock.
    const st = stages.find((x) => x.id === "report");
    const rp = (run.assessment && run.assessment.report) || null;
    if (!st || st.state === "pending" || !rp) return null;
    const running = st.state === "running";
    const procs = rp.processes || [];
    const done = procs.filter((p) => p.state === "done" || p.state === "failed").length;
    return h("div", { class: "rprog", "aria-live": "polite" },
      h("div", { class: "rprog-h" },
        h("span", null, running ? (REPORT_PHASES[rp.phase] || rp.detail || "Working") : "Report built"),
        procs.length ? h("span", { class: "hint" }, `${done} of ${plural(procs.length, "process", "processes")}`) : null,
        h("span", { class: "hint" }, running ? ["running for ", elapsed(rp.started_at)] : ["took ", elapsed(rp.started_at, rp.finished_at)])),
      procs.length ? h("div", { class: "bar-track" }, h("i", { style: `width:${Math.round(100 * done / procs.length)}%` })) : null,
      running && procs.length ? h("ul", { class: "rprog-list" }, procs.map((p) => {
        const [icon, label] = REPORT_STATES[p.state] || ["○", p.state];
        return h("li", { class: p.state }, h("span", { class: "ic" + (icon === "◌" ? " spin" : "") }, icon),
          h("span", { class: "nm" }, p.name), h("span", { class: "sub" }, label));
      })) : null,
      running ? h("p", { class: "hint", style: "margin:4px 0 0" }, "Each process is read from the source, scored against the rubric, and given a design. A few minutes in all; finished steps are cached, so a re-run is quick.") : null);
  }

  function screenScope(run) {
    const sc = run.scope || {};
    const procs = sc.processes || [];
    const stages = (run.assessment && run.assessment.stages) || [];
    const links = h("p", { class: "hint", style: "margin-top:6px;display:flex;gap:14px;flex-wrap:wrap" },
      run.links && run.links.temporal_ui ? h("a", { class: "lnk", href: run.links.temporal_ui, target: "_blank", rel: "noopener" }, "Watch it in Temporal UI ↗") : null,
      h("a", { class: "lnk", href: (run.links && run.links.atlas) || "/atlas", target: "_blank", rel: "noopener" }, "Open in Code Atlas ↗"));
    const head = h("div", null, checklist(stages, { style: "margin:0 0 6px" }), reportProgress(run, stages), links);

    if (!procs.length) {
      const failed = run.status === "failed" || stages.some((s) => s.state === "failed");
      return h("div", null, head,
        failed ? (retryBox(run, "The assessment failed.") || h("div", { class: "errbox" }, run.error || "The assessment failed.")) :
          (run.assessment && run.assessment.report) ? null : h("div", { class: "loading-line" }, h("span", { class: "spin" }, "◌"),
            run.status === "assessing" ? "Code Atlas is indexing the repository and building its Temporal report. This takes a few minutes." : "Waiting for the assessment…"),
        h("div", { class: "btns" }, h("button", { type: "button", class: "btn", onclick: () => go(4) }, "Back")));
    }

    // What the user has checked in, per process: variants, steps, failures to address; plus scope notes.
    const itemsOf = (p) => {
      const confirmed = p.key === sc.selected && sc.confirmed;
      const variants = p.key === sc.selected ? (sc.variants || []) : (p.variants || []);
      const steps = confirmed && sc.steps ? sc.steps : (p.steps || []);
      const risks = confirmed && sc.risks ? sc.risks : (p.risks || []);
      return { variants, steps, risks };
    };
    const checkedIds = (list) => new Set(list.filter((x) => x.included !== false).map((x) => x.id));
    const pick = (p) => {
      const it = itemsOf(p);
      return { selected: p.key, variants: checkedIds(it.variants), steps: checkedIds(it.steps), risks: checkedIds(it.risks) };
    };
    if (!ui.scope || ui.scopeFor !== runId(run)) {
      const first = procs.find((p) => p.key === sc.selected) || procs[0];
      ui.scope = { ...pick(first), notes: sc.notes || "", borderline: !!sc.borderline_confirmed };
      ui.scopeFor = runId(run);
    }
    const u = ui.scope;
    const locked = !!sc.confirmed || !["scoping", "not_suitable"].includes(run.status);
    const sel = procs.find((p) => p.key === u.selected) || procs[0];
    const others = procs.filter((p) => p !== sel);
    const { variants, steps, risks } = itemsOf(sel);
    const v = verdictLabel(sel);
    const notSuitable = v.kind === "Not suitable" || sel.gated;
    const borderline = v.kind === "Borderline";
    const EFFECT = { paid_call: "paid call", external_call: "external call", db_write: "database write",
      file_write: "file write", message: "message", irreversible: "irreversible" };

    const toggle = (set, id) => (e) => { if (e.target.checked) set.add(id); else set.delete(id); render(); };
    const group = (title, hint, list, set, line) => list.length ? h("fieldset", { class: "scope-group" },
      h("legend", null, title, h("span", { class: "hint" }, ` ${set.size} of ${list.length} in scope`)),
      hint ? h("p", { class: "hint", style: "margin:0 0 4px" }, hint) : null,
      list.map((x) => h("label", { class: "scope-item" + (set.has(x.id) ? "" : " off") },
        h("input", { type: "checkbox", checked: set.has(x.id), disabled: locked, onchange: toggle(set, x.id) }),
        h("span", null, ...line(x),
          (x.refs || []).length ? h("span", { class: "refs" }, x.refs.join(", ")) : null)))) : null;

    const scopeBox = h("div", null,
      group("Ways it runs", variants.length > 1 ? "Every way the code runs this process, read from the code." : null,
        variants, u.variants, (x) => [h("b", null, x.name), x.trigger ? h("span", { class: "sub" }, x.trigger) : null]),
      group("Steps", "Unchecked steps stay as they are in the original code.", steps, u.steps,
        (x) => [h("b", null, x.name), x.does ? h("span", { class: "sub" }, x.does) : null,
          (x.effects || []).length ? h("span", { class: "fx" }, x.effects.map((e) => EFFECT[e] || e).join(" · ")) : null]),
      group("Failures to address", "What the POC should show Temporal handling.", risks, u.risks, (x) => [h("span", null, x.text)]),
      h("div", { class: "lbl" }, h("label", { for: "tz-scope-notes" }, "Anything else about scope"), h("span", { class: "hint" }, " optional")),
      h("textarea", { id: "tz-scope-notes", class: "field area", rows: 3, disabled: locked,
        placeholder: "For example: leave email sending real but stub ClockShark; the payroll run matters most to the customer.",
        value: u.notes, oninput: (e) => { u.notes = e.target.value; } }));

    return h("div", null, head,
      locked ? h("p", { class: "ro" }, sc.confirmed ? "Scope confirmed. The plan is built from it on screen 6." : "Read-only.") : null,
      h("div", { class: "proc main" },
        h("div", { class: "t" }, sel.name, h("span", { class: "verdict " + v.cls }, v.text)),
        sel.matches_brief ? h("span", { class: "match" }, "Matches your brief") : null,
        sel.summary ? h("p", null, sel.summary) : null,
        sel.crash_loss ? h("p", null, h("b", null, "Today: "), sel.crash_loss) : null,
        sel.entry ? h("p", { class: "hint" }, "Entry ", h("code", null, sel.entry)) : null),
      notSuitable ? h("div", { class: "gate" }, h("b", null, "Not suitable, so a POC isn’t built for it."),
        (sel.reasons || []).length ? h("ul", null, sel.reasons.map((r) => h("li", null, typeof r === "string" ? r : r.text))) : null,
        others.some((p) => verdictLabel(p).kind !== "Not suitable") ? h("p", { style: "margin:4px 0 0" }, "Pick another process below.") : null) : null,
      (sc.contradictions || []).length ? h("div", { class: "conf" }, h("b", null, "Your notes and the code disagree"),
        h("ul", null, sc.contradictions.map((c) => h("li", null, `You noted: “${c.note}”. The code: ${c.code}`,
          (c.refs || []).length ? h("span", { class: "hint" }, " ", h("code", null, c.refs.join(", "))) : null)))) : null,
      others.length ? h("div", { class: "lbl" }, "Other opportunities") : null,
      others.map((p) => {
        const pv = verdictLabel(p);
        return h("div", { class: "proc" },
          h("div", { class: "t" }, p.name, h("span", { class: "verdict " + pv.cls }, pv.text)),
          p.summary ? h("p", null, p.summary) : null,
          p.also_found ? h("p", null, h("b", null, "Also found: "), p.also_found) : null,
          !locked ? h("p", { style: "margin-top:6px" }, h("button", { type: "button", class: "btn sm", disabled: pv.kind === "Not suitable",
            onclick: () => { Object.assign(u, pick(p), { borderline: false }); render(); } }, "Build this one instead")) : null);
      }),
      h("div", { class: "lbl", style: "font-size:14px;font-weight:700;color:var(--ink)" }, `In scope for ${sel.name}`),
      scopeBox,
      borderline && !locked ? h("label", { class: "checkline" },
        h("input", { type: "checkbox", checked: u.borderline, onchange: (e) => { u.borderline = e.target.checked; render(); } }),
        h("span", null, "This process is borderline. Build the POC anyway.")) : null,
      errorBox(),
      h("div", { class: "btns" },
        h("button", { type: "button", class: "btn", onclick: () => go(4) }, "Back"),
        locked ? (allowedStep(run) >= 6 ? h("button", { type: "button", class: "btn pri", onclick: () => go(6) }, "Continue") : null)
          : h("button", { type: "button", class: "btn pri", disabled: ui.busy || notSuitable || (borderline && !u.borderline) ||
              (variants.length > 0 && u.variants.size === 0) || (steps.length > 0 && u.steps.size === 0),
            onclick: async () => {
              const body = {
                selected: sel.key,
                variants: variants.filter((x) => u.variants.has(x.id)).map((x) => x.id),
                steps: steps.length ? steps.filter((x) => u.steps.has(x.id)).map((x) => x.id) : null,
                risks: risks.length ? risks.filter((x) => u.risks.has(x.id)).map((x) => x.id) : null,
                notes: u.notes,
                borderline_confirmed: borderline ? u.borderline : false,
              };
              const ok = await act(() => api(`/api/tz/runs/${enc(state.runId)}/scope`, { method: "POST", body }));
              if (ok) go(6);
            } }, "Confirm scope")));
  }

  // ------------------------------------------------------------------
  // Screen 6: behavior and design
  // ------------------------------------------------------------------


  function planPut(body) {
    return act(() => api(`/api/tz/runs/${enc(state.runId)}/plan`, { method: "PUT", body }));
  }

  async function approvePlan() {
    // One action: the plan as it stands (anything undecided is kept or included), then generate.
    const ok = await act(async () => {
      await api(`/api/tz/runs/${enc(state.runId)}/plan`, { method: "PUT", body: { approve_all: true } });
      return api(`/api/tz/runs/${enc(state.runId)}/approve`, { method: "POST", body: {} });
    });
    if (ok) go(7);
  }

  function screenPlan(run) {
    const plan = run.plan;
    const title = (t) => h("h3", null, t);
    if ((run.status === "failed" || run.status === "stopped") && (!plan || plan.status !== "ready")) {
      return h("div", null, title("Review the plan"), retryBox(run, "Drafting the plan failed."),
        h("div", { class: "btns" }, h("button", { type: "button", class: "btn", onclick: () => go(5) }, "Back")));
    }
    if (!plan || plan.status === "drafting") {
      return h("div", null, title("Review the plan"),
        h("div", { class: "loading-line", "aria-live": "polite" }, h("span", { class: "spin" }, "◌"),
          "Drafting the plan from the code: what it does today, fixes worth making, test data and the design. About a minute."),
        h("div", { class: "btns" }, h("button", { type: "button", class: "btn", onclick: () => go(5) }, "Back")));
    }
    if (plan.status === "failed") {
      return h("div", null, title("Review the plan"),
        h("div", { class: "errbox" }, run.error || plan.error || "Drafting the plan failed."),
        h("div", { class: "btns" }, h("button", { type: "button", class: "btn", onclick: () => go(5) }, "Back")));
    }
    const ro = run.status !== "plan_ready";
    const kept = (plan.claims || []).filter((c) => c.status !== "rejected").length;
    const fixes = (plan.changes || []).filter((c) => c.decision !== "decline").length;
    const files = ((plan.testdata && plan.testdata.files) || []).length;
    const summary = [plural(kept, "behavior") + " kept", plural(fixes, "fix", "fixes"), plural(files, "test file"),
      (plan.design && plan.design.summary) || ""].filter(Boolean).join(" · ");
    const approveBtn = () => h("button", { type: "button", class: "btn pri", disabled: ui.busy || kept === 0, onclick: approvePlan },
      ui.busy ? "Approving…" : "Approve plan and generate");
    const part = (n, heading, sub, body) => h("section", { class: "plan-part" },
      h("div", { class: "plan-part-h" }, h("span", { class: "num" }, String(n)), h("div", null, h("b", null, heading), sub ? h("span", { class: "hint" }, sub) : null)),
      body);

    return h("div", null,
      title(ro ? "The approved plan" : "Review the plan"),
      ro ? h("p", { class: "ro" }, run.status === "planning" ? "Drafting…" : "Approved. The POC was generated from this plan.")
        : h("p", { class: "hint" }, "Everything below is pre-filled from the code. Change only what looks wrong, then approve."),
      h("div", { class: "plan-top" }, h("span", null, summary), ro ? null : approveBtn()),
      part(1, "What the code does today", "The POC must preserve these. Read from the code and checked against it.", PLAN_PARTS.claims(plan, ro)),
      part(2, "Fixes on purpose", "Bugs the POC can fix. Included fixes show as “Intended fix” in the results.", PLAN_PARTS.changes(plan, ro)),
      part(3, "Test data", "Synthetic. Both versions run on exactly this, so the original code is the answer key.", SECTION_BODY.testdata(plan, ro)),
      part(4, "Design", plan.design && plan.design.summary, SECTION_BODY.design(plan)),
      errorBox(),
      h("div", { class: "btns" },
        h("button", { type: "button", class: "btn", onclick: () => go(5) }, "Back"),
        ro ? (allowedStep(run) >= 7 ? h("button", { type: "button", class: "btn pri", onclick: () => go(7) }, "Continue") : null) : approveBtn()));
  }

  const PLAN_PARTS = {
    claims(plan, ro) {
      const pu = ui.plan;
      const cs = plan.claims || [];
      if (!cs.length) return h("p", { class: "empty" }, "No behaviors were recorded.");
      const glance = cs.filter((c) => c.confidence && c.confidence !== "high");
      const rest = cs.filter((c) => !glance.includes(c));
      const row = (c) => {
        const editing = pu.editing === c.id;
        const text = c.status === "edited" && c.edited_text ? c.edited_text : c.text;
        const dropped = c.status === "rejected";
        return h("div", { class: "beh" + (dropped ? " dropped" : "") },
          editing ? (() => {
            const t = h("textarea", { class: "field", "aria-label": "Edit the behavior", oninput: (e) => { pu.editText = e.target.value; } });
            t.value = pu.editText;
            return h("div", { class: "beh-edit" }, t, h("div", { class: "sec-foot", style: "margin-top:6px" },
              h("button", { type: "button", class: "btn sm", onclick: () => { pu.editing = null; render(); } }, "Cancel"),
              h("button", { type: "button", class: "btn sm pri", disabled: !pu.editText.trim(), onclick: async () => {
                const ok = await planPut({ claims: [{ id: c.id, status: "edited", edited_text: pu.editText.trim() }] });
                if (ok) { pu.editing = null; render(); }
              } }, "Save")));
          })() : h("div", { class: "beh-txt" }, h("span", null, text),
            h("span", { class: "refs" }, (c.refs || []).join(", "),
              c.status === "edited" ? h("span", { class: "conf-pill" }, "edited") : null,
              dropped ? h("span", { class: "conf-pill" }, "dropped") : null)),
          !ro && !editing ? h("span", { class: "beh-act" },
            dropped ? h("button", { type: "button", class: "lnk", onclick: () => planPut({ claims: [{ id: c.id, status: "accepted" }] }) }, "Keep")
              : [h("button", { type: "button", class: "lnk", onclick: () => { pu.editing = c.id; pu.editText = text; render(); } }, "Edit"),
                h("button", { type: "button", class: "lnk", onclick: () => planPut({ claims: [{ id: c.id, status: "rejected" }] }) }, "Drop")]) : null);
      };
      return h("div", null,
        glance.length ? h("div", { class: "glance" },
          h("div", { class: "glance-h" }, `Worth a glance: ${plural(glance.length, "behavior")} the checks were less sure about`),
          glance.map(row)) : null,
        rest.length ? h("details", { class: "fold", open: !glance.length || null },
          h("summary", null, glance.length ? `The other ${plural(rest.length, "behavior")}, confirmed in the code` : `${plural(rest.length, "behavior")}, confirmed in the code`),
          rest.map(row)) : null);
    },
    changes(plan, ro) {
      const cs = plan.changes || [];
      if (!cs.length) return h("p", { class: "empty" }, "None found. The POC keeps today’s behavior exactly.");
      // Included unless skipped (an undecided fix is included when the plan is approved).
      return h("div", null, cs.map((c) => h("label", { class: "scope-item" + (c.decision !== "decline" ? "" : " off") },
        h("input", { type: "checkbox", checked: c.decision !== "decline", disabled: ro || ui.busy,
          onchange: (e) => planPut({ changes: [{ id: c.id, decision: e.target.checked ? "approve" : "decline" }] }) }),
        h("span", null, h("b", null, c.title), c.detail ? h("span", { class: "sub" }, c.detail) : null,
          (c.refs || []).length ? h("span", { class: "refs" }, c.refs.join(", ")) : null))));
    },
  };

  const SECTION_BODY = {
    testdata(plan, ro) {
      const td = plan.testdata || {};
      const pu = ui.plan;
      const files = td.files || [];
      return h("div", null,
        td.summary ? h("p", { style: "margin:0;font-size:13.5px" }, td.summary) : null,
        h("p", { class: "hint" }, "Synthetic data. Both versions run on exactly these files, so the original code is the answer key."),
        td.frozen_time ? h("p", { style: "margin:6px 0 0;font-size:13px" }, h("b", null, "Clock frozen at "), h("code", null, td.frozen_time)) : null,
        (td.failures || []).length ? h("div", null, h("div", { class: "lbl" }, "Injected failures"),
          h("ul", { class: "plain" }, td.failures.map((f) => h("li", null, h("b", null, f.boundary), ` fails ${plural(f.times, "time")}`, f.detail ? `: ${f.detail}` : "")))) : null,
        h("div", { class: "lbl" }, "Files"),
        files.map((f) => {
          const editing = pu.editFile === f.name;
          const shown = pu.preview[f.name];
          return h("div", { class: "filebox" },
            h("div", { class: "filebox-h" }, h("code", null, f.name), h("span", { class: "d" }, f.description || ""),
              !editing ? h("button", { type: "button", class: "btn sm", "aria-expanded": String(!!shown),
                onclick: () => { pu.preview[f.name] = !shown; render(); } }, shown ? "Hide" : "Preview") : null,
              !ro && !editing ? h("button", { type: "button", class: "btn sm", onclick: () => { pu.editFile = f.name; pu.editFileText = f.content || ""; render(); } }, "Edit") : null),
            editing ? h("div", { style: "padding:0 10px 10px" }, (() => {
              const t = h("textarea", { class: "field code", "aria-label": `Edit ${f.name}`, oninput: (e) => { pu.editFileText = e.target.value; } });
              t.value = pu.editFileText; return t;
            })(), h("div", { class: "sec-foot", style: "margin-top:6px" },
              h("button", { type: "button", class: "btn sm", onclick: () => { pu.editFile = null; render(); } }, "Cancel"),
              h("button", { type: "button", class: "btn sm pri", onclick: async () => {
                if (/\.json$/i.test(f.name)) { try { JSON.parse(pu.editFileText); } catch (e) { ui.error = `${f.name} is not valid JSON: ${e.message}`; return render(); } }
                const all = files.map((x) => ({ name: x.name, content: x.name === f.name ? pu.editFileText : x.content }));
                const ok = await planPut({ testdata_files: all });
                if (ok) { pu.editFile = null; render(); }
              } }, "Save")))
              : shown ? h("pre", { class: "prev" }, f.content || "") : null);
        }));
    },
    design(plan) {
      const d = plan.design || {};
      return h("div", null,
        d.why ? h("p", { style: "margin:0 0 6px;font-size:13.5px" }, d.why) : null,
        (d.elements || []).map((el) => h("div", { class: "crit" },
          h("span", { class: "txt" }, h("span", { class: "kind " + el.kind }, (el.kind || "").replace("_", " ")), h("b", null, el.name),
            el.does ? h("span", { class: "why", style: "color:var(--ink-2)" }, el.does) : null))),
        (d.removes || []).length ? h("div", null, h("div", { class: "lbl" }, "Removes"), h("ul", { class: "plain" }, d.removes.map((x) => h("li", null, x)))) : null,
        (d.stays || []).length ? h("div", null, h("div", { class: "lbl" }, "Stays outside Temporal"), h("ul", { class: "plain" }, d.stays.map((x) => h("li", null, x)))) : null);
    },
  };

  // ------------------------------------------------------------------
  // Screen 7: generate and validate
  // ------------------------------------------------------------------

  function screenGenerate(run) {
    const g = run.generate || {};
    const done = run.status === "done";
    if (done && !ui.autoNavDone && run.results) {
      ui.autoNavDone = true;
      setTimeout(() => { if (state.step === 7 && state.runId === runId(run)) go(8); }, 1200);
    }
    const passes = g.review_passes || [];
    return h("div", null,
      h("h3", null, done ? "Your POC is built" : run.status === "failed" ? "The build stopped" : "Building your POC"),
      g.started_at ? h("p", { class: "hint" }, `Started ${ago(g.started_at)}.${done ? "" : " You can close this and come back."}`)
        : h("p", { class: "hint" }, "Starting…"),
      checklist(g.stages || []),
      passes.length ? h("div", { style: "margin-top:10px" }, h("div", { class: "lbl" }, "Code Atlas review passes"),
        passes.map((p) => h("details", { class: "pass" },
          h("summary", null, `Pass ${p.n}: ${plural((p.findings || []).length, "finding")}${p.fixed !== undefined && (p.findings || []).length ? `, ${p.fixed === (p.findings || []).length ? "all" : p.fixed} fixed` : ""}`),
          (p.findings || []).map((f) => h("div", { class: "finding" },
            h("span", { class: "sev " + (f.severity || "") }, f.severity || ""), h("b", null, f.title), f.file ? [" ", h("code", null, f.file)] : null,
            f.detail ? h("div", null, f.detail) : null))))) : null,
      g.smoke && g.smoke.url ? h("p", { style: "margin-top:10px;font-size:13.5px" },
        h("a", { class: "lnk", href: g.smoke.url, target: "_blank", rel: "noopener" }, "Open the smoke run in Temporal UI ↗")) : null,
      retryBox(run, "The run failed."),
      errorBox(),
      h("div", { class: "btns" },
        h("button", { type: "button", class: "btn", onclick: () => go(6) }, "Back"),
        h("span", { class: "r" },
          run.status === "generating" ? h("button", { type: "button", class: "btn", disabled: ui.busy, onclick: async () => {
            if (!confirm("Stop this run? Its workflow is cancelled; the work so far stays in the workspace.")) return;
            await act(() => api(`/api/tz/runs/${enc(state.runId)}`, { method: "DELETE" }));
            refresh();
          } }, "Stop run") : null,
          done ? h("button", { type: "button", class: "btn pri", onclick: () => go(8) }, "See results") : null)));
  }

  // ------------------------------------------------------------------
  // Screen 8: results
  // ------------------------------------------------------------------

  const RESULT = { match: ["m", "Match"], intended: ["i", "Intended fix"], new: ["n", "New behavior"], mismatch: ["x", "Mismatch"] };

  function screenResults(run) {
    const r = run.results;
    if (!r) return h("div", null, h("p", { class: "empty" }, "Results appear when the run finishes."),
      h("div", { class: "btns" }, h("button", { type: "button", class: "btn", onclick: () => go(7) }, "Back")));
    const id = enc(runId(run));
    const crit = r.criteria || [];
    return h("div", null,
      h("div", { class: "tags" }, (r.outcomes || []).map((o) => h("span", { class: "tag" + (o.ok === true ? " okt" : o.ok === false ? " badt" : "") },
        o.ok === true ? "✓ " : o.ok === false ? "✗ " : "", o.label))),
      h("div", { class: "lbl", style: "display:flex;justify-content:space-between;gap:8px" }, h("span", null, "Same test data, both versions"),
        h("button", { type: "button", class: "lnk", onclick: () => showTestData(run) }, "View test data")),
      (r.comparison || []).length ? h("div", { class: "scroll" }, h("table", { class: "tbl" },
        h("thead", null, h("tr", null, h("th", null, "Check"), h("th", null, "Original"), h("th", null, "Temporal"), h("th", null, "Result"))),
        h("tbody", null, r.comparison.map((c) => {
          const [cls, label] = RESULT[c.result] || ["", c.result || ""];
          return h("tr", { title: c.detail || null }, h("td", null, c.check), h("td", null, cellValue(c.original)), h("td", null, cellValue(c.temporal)), h("td", { class: cls }, label));
        })))) : h("p", { class: "empty" }, "No comparison was recorded."),
      h("div", { class: "lbl" }, "Success criteria"),
      h("p", { style: "margin:0;font-size:13.5px" }, r.criteria_summary || ""),
      crit.length ? h("details", { class: "pass" }, h("summary", null, "Each criterion"),
        crit.map((c) => h("div", { class: "finding" }, h("span", { class: "sev " + (c.passed === false ? "high" : "") }, c.passed === true ? "pass" : c.passed === false ? "fail" : "n/a"),
          h("b", null, `${c.priority || ""} `), c.text, c.evidence ? h("div", null, c.evidence) : null))) : null,
      h("div", { class: "lbl", style: "display:flex;justify-content:space-between;gap:8px" }, h("span", null, "Changes"),
        h("button", { type: "button", class: "lnk", onclick: () => showDiff(run) }, "View diff")),
      h("p", { style: "margin:0;font-size:13.5px" }, r.changes_summary || "",
        r.diff_stats ? h("span", { class: "hint" }, ` ${plural(r.diff_stats.files, "file")}, +${fmtInt(r.diff_stats.insertions)} −${fmtInt(r.diff_stats.deletions)}`) : null),
      typeof r.review_passes === "number" ? h("p", { class: "hint" }, `After ${plural(r.review_passes, "review pass", "review passes")}.`) : null,
      (r.leftover_findings || []).length ? h("div", null, h("div", { class: "lbl" }, "Left over after review"),
        r.leftover_findings.map((f) => h("div", { class: "finding", style: "padding-left:0" }, h("span", { class: "sev " + (f.severity || "") }, f.severity || ""),
          h("b", null, f.title || ""), f.file ? [" ", h("code", null, f.file)] : null, f.detail ? h("div", null, f.detail) : null))) : null,
      (r.limitations || []).length ? h("div", null, h("div", { class: "lbl" }, "Limitations"),
        h("ul", { class: "plain" }, r.limitations.map((x) => h("li", null, x)))) : null,
      errorBox(),
      h("div", { class: "btns" },
        h("span", { style: "display:flex;gap:8px;flex-wrap:wrap" },
          r.temporal_ui_url ? h("a", { class: "btn", href: r.temporal_ui_url, target: "_blank", rel: "noopener" }, "Open in Temporal UI") : null,
          h("button", { type: "button", class: "btn", onclick: () => showPush(run) }, "Push to GitHub")),
        h("a", { class: "btn pri", href: `/api/tz/runs/${id}/packet`, download: `temporalize-${runId(run)}.zip` }, "Export review packet")));
  }

  function modal(title, body, foot, wide) {
    const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(); } });
    function close() { back.remove(); document.removeEventListener("keydown", onKey); }
    function onKey(e) { if (e.key === "Escape") close(); }
    document.addEventListener("keydown", onKey);
    const bodyEl = h("div", { class: "modal-b" }, body);
    const footEl = h("div", { class: "modal-f" }, foot ? foot(close) : h("button", { type: "button", class: "btn", onclick: close }, "Close"));
    back.appendChild(h("div", { class: "modal" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-label": title },
      h("div", { class: "modal-h" }, h("h3", null, title), h("button", { type: "button", class: "close", "aria-label": "Close", onclick: close }, "×")),
      bodyEl, footEl));
    document.getElementById("modal-root").appendChild(back);
    const firstBtn = back.querySelector(".close");
    if (firstBtn) firstBtn.focus();
    return { close, bodyEl, footEl };
  }

  function showTestData(run) {
    const td = (run.plan && run.plan.testdata) || {};
    modal("Test data (synthetic)", h("div", null,
      td.summary ? h("p", { style: "margin-top:0" }, td.summary) : null,
      td.frozen_time ? h("p", { class: "hint" }, "Clock frozen at ", h("code", null, td.frozen_time)) : null,
      (td.files || []).map((f) => h("div", { class: "filebox" },
        h("div", { class: "filebox-h" }, h("code", null, f.name), h("span", { class: "d" }, f.description || "")),
        h("pre", { class: "prev" }, f.content || "")))), null, true);
  }

  async function showDiff(run) {
    const pre = h("pre", { class: "diff" }, "Loading…");
    modal(`Diff: ${(run.generate && run.generate.branch) || "POC branch"}`, pre, null, true);
    try {
      const text = await api(`/api/tz/runs/${enc(runId(run))}/diff`);
      pre.replaceChildren();
      const lines = String(text).split("\n");
      const MAX = 6000;
      for (const line of lines.slice(0, MAX)) {
        const cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ") ? "meta"
          : line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : null;
        pre.appendChild(cls ? h("span", { class: cls }, line) : document.createTextNode(line + "\n"));
      }
      if (lines.length > MAX) pre.appendChild(h("span", { class: "meta" }, `… ${fmtInt(lines.length - MAX)} more lines. The review packet has the full diff.`));
      if (!text) pre.textContent = "No changes.";
    } catch (e) { pre.textContent = `Could not load the diff: ${e.message}`; }
  }

  function showPush(run) {
    const src = run.source || {};
    let url = src.kind === "git" ? (/^https?:\/\//.test(src.url || "") ? src.url : `https://${src.url || ""}`) : "";
    let token = "";
    const status = h("div", { "aria-live": "polite" });
    const branch = (run.generate && run.generate.branch) || `temporalize/${runId(run)}`;
    const urlIn = h("input", { class: "field", id: "tz-push-url", placeholder: "https://github.com/acme/backoffice", value: url, oninput: (e) => { url = e.target.value.trim(); } });
    const tokIn = h("input", { class: "field", id: "tz-push-tok", type: "password", autocomplete: "off", oninput: (e) => { token = e.target.value.trim(); } });
    modal("Push to GitHub", h("div", null,
      h("p", { style: "margin-top:0;font-size:13.5px" }, "Pushes the branch ", h("code", null, branch), " to the repository below. Nothing else is changed there."),
      h("label", { class: "lbl", for: "tz-push-url" }, "Repository URL"), urlIn,
      h("label", { class: "lbl", for: "tz-push-tok" }, "Token with write access"), tokIn,
      h("p", { class: "hint" }, "Used for this push only. It is not stored or sent to a model."),
      status), (close) => [
      h("button", { type: "button", class: "btn", onclick: close }, "Cancel"),
      h("button", { type: "button", class: "btn pri", onclick: async (e) => {
        if (!url || !token) { status.replaceChildren(h("div", { class: "errbox" }, "Enter the repository URL and a token.")); return; }
        e.target.disabled = true;
        status.replaceChildren(h("div", { class: "loading-line" }, h("span", { class: "spin" }, "◌"), "Pushing…"));
        try {
          const out = await api(`/api/tz/runs/${enc(runId(run))}/push`, { method: "POST", body: { remote_url: url, token } });
          const link = out.url || out.branch_url || out.compare_url;
          status.replaceChildren(h("p", { class: "ok", style: "white-space:normal" }, `✓ ${out.message || `Pushed ${out.branch || branch}.`} `,
            link ? h("a", { class: "lnk", href: link, target: "_blank", rel: "noopener" }, "Open on GitHub ↗") : null));
        } catch (err) {
          status.replaceChildren(h("div", { class: "errbox" }, err.message));
          e.target.disabled = false;
        }
      } }, "Push")]);
  }

  // ------------------------------------------------------------------
  // Runs list (header)
  // ------------------------------------------------------------------

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------

  const SCREENS = [screenSetup, screenFound, screenFocus, screenSuccess, screenScope, screenPlan, screenGenerate, screenResults];

  setInterval(() => {
    for (const el of document.querySelectorAll(".elapsed[data-since]")) {
      if (!el.dataset.until) el.textContent = clock(el.dataset.since);
    }
  }, 1000);

  async function boot() {
    document.getElementById("runs-btn").addEventListener("click", () => { location.hash = "#/"; });
    window.addEventListener("hashchange", route);
    api("/api/config").then((c) => {
      state.config = c;
      if (!(state.run && state.run.links && state.run.links.temporal_ui) && c.temporal_ui_url) document.getElementById("tui-link").href = c.temporal_ui_url;
    }).catch(() => {});
    try { state.settings = await api("/api/tz/settings"); }
    catch (e) { banner(`Could not load settings: ${e.message}`); state.settings = { models: [], anthropic: {}, temporal: { mode: "local" }, saved: false }; }
    route();
  }
  boot();
})();
