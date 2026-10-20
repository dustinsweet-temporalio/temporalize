// Code Atlas chat UI. No build step; libraries are vendored under vendor/.
//
// Every conversation is about one index. The page opens on the home view (the list of
// indexes, where they are added, synced and deleted); the chat appears once an index is
// selected, and a conversation is saved when its first question is sent. The index is
// sent with every message, so the first message is the user's own question, with the
// repository overview already in context.
//
// Conversations live in IndexedDB (store.js), the record of every transcript. Each
// conversation is a session workflow on the server, which keeps only the recent
// context; each message carries the newest messages within the context budget, and
// `resync` replaces the server's copy when it may differ (after a stop or a failure).
(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    messages: $("messages"), input: $("input"), form: $("composer"), send: $("send"), stop: $("stop"),
    repoSelect: $("repo-select"), repoMeta: $("repo-meta"), convs: $("conversations"), newChat: $("new-chat"),
    status: $("status"), banner: $("banner"), docsStatus: $("docs-status"), docsStatusText: $("docs-status-text"), header: $("chat-header"), chatRepo: $("chat-repo"),
    chatRepoMeta: $("chat-repo-meta"), reportToggle: $("report-toggle"), reportView: $("report-view"),
    reportActions: $("report-actions"), reportSummary: $("report-summary"), reportDetailed: $("report-detailed"), reportImpact: $("report-impact"),
    reportDownload: $("report-download"), compassOpen: $("compass-open"), temporalLink: $("temporal-ui-link"),
    home: $("home"), homeView: $("home-view"), addIndex: $("add-index"), repoActions: $("repo-actions"),
    syncIndex: $("sync-index"), deleteIndex: $("delete-index"), navHome: $("nav-home"), backHome: $("back-home"),
  };

  // ---- storage ----
  // Conversations are in IndexedDB (ChatStore); localStorage is read once for
  // conversations saved by earlier versions (LEGACY_KEY).
  const LEGACY_KEY = "code-atlas:conversations:v1";

  const state = {
    conversations: [], activeId: null, streaming: false, controller: null, turnId: null,
    // selected: the index the main view is about ("" on the home view). Not restored on load.
    repos: new Map(), reposLoaded: false, repoIds: "", reposJSON: "", selected: "", view: "chat", config: {},
    // The loaded report: {repo, payload, markdown (fetched on first use), mode: "summary" | "detailed"}.
    report: null,
    storageOk: true, storageWarning: "", chatContextChars: 240000, reattaching: new Set(),
  };

  const showStorageWarning = (text) => {
    state.storageWarning = text;
    els.banner.hidden = false; els.banner.textContent = text;
  };
  // Saves never throw: a full or blocked store shows the banner and the chat goes on.
  const persist = (c) => (state.storageOk ? ChatStore.put(c) : Promise.resolve())
    .catch(() => showStorageWarning("Could not save this conversation (browser storage full or blocked)."));

  // Ids are UUIDs (the server's session and turn ids). crypto.randomUUID needs a secure
  // context (https or localhost); getRandomValues works over plain http too.
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
    : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (ch) =>
      (ch ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (ch / 4)))).toString(16)));

  // ---- clipboard ----
  // navigator.clipboard needs a secure context (https or localhost); the fallback covers
  // the app opened over plain http on another host.
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* fall back */ }
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (!ok) throw new Error("copy failed");
  }
  const copyButton = (btn, getText, label = "Copy") => {
    btn.onclick = () => copyText(getText())
      .then(() => { btn.textContent = "Copied"; }, () => { btn.textContent = "Copy failed"; })
      .finally(() => setTimeout(() => (btn.textContent = label), 1200));
  };

  // ---- markdown ----
  marked.setOptions({ gfm: true, breaks: false });
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    // In-page links (the detailed report's process table) stay in the page.
    if (node.tagName === "A" && !(node.getAttribute("href") || "").startsWith("#")) { node.setAttribute("target", "_blank"); node.setAttribute("rel", "noopener noreferrer"); }
  });
  const render = (el, text) => {
    el.innerHTML = DOMPurify.sanitize(marked.parse(text));
    el.querySelectorAll("pre").forEach((pre) => {
      const code = pre.querySelector("code");
      if (code) hljs.highlightElement(code);
      const btn = document.createElement("button");
      btn.className = "copy"; btn.type = "button"; btn.textContent = "Copy";
      copyButton(btn, () => (code ? code.innerText : pre.innerText));
      pre.appendChild(btn);
    });
  };

  // ---- theme for code blocks ----
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  const applyTheme = () => { $("hljs-theme").href = dark.matches ? "vendor/github-dark.min.css" : "vendor/github.min.css"; };
  applyTheme(); dark.addEventListener("change", applyTheme);

  // ---- conversations ----
  const active = () => state.conversations.find((c) => c.id === state.activeId);
  // The index the chat is about: the open conversation's, or the selected one for a new chat.
  const currentRepo = () => { const c = active(); return c ? c.repo : state.selected || null; };
  const isHome = () => !active() && !state.selected;
  const conversationsAbout = (repo) => state.conversations.filter((c) => c.repo === repo && c.messages.length).length;
  const shorten = (text) => (text.length > 48 ? text.slice(0, 45) + "…" : text);
  const firstQuestion = (c) => c.messages.find((m) => m.role === "user")?.content.trim();
  const retitle = (c) => { const q = firstQuestion(c); c.title = q ? shorten(q) : "New chat"; };

  const newConversation = (repo) => {
    const c = { id: uuid(), v: 2, repo, title: "New chat", messages: [], updated: Date.now() };
    state.conversations.unshift(c); state.activeId = c.id; persist(c); return c;
  };

  // Conversations saved in localStorage by earlier versions: copied into IndexedDB once,
  // with new UUID ids (the old ids were timestamps), then the old key is removed. Data
  // that cannot be parsed or copied stays where it is.
  function legacyConversations() {
    let raw = null;
    try { raw = localStorage.getItem(LEGACY_KEY); } catch { return null; }
    if (!raw) return null;
    try {
      const list = JSON.parse(raw);
      return Array.isArray(list) ? list.map((c) => ({ ...c, id: uuid(), updated: c.updated || Number(c.id) || Date.now() })) : null;
    } catch { return null; }
  }

  async function importLegacy() {
    const list = legacyConversations();
    if (!list) return;
    if ((await ChatStore.loadAll()).length > 0) return;  // never overwrite; the key stays
    await ChatStore.putAll(list);
    try { localStorage.removeItem(LEGACY_KEY); } catch { /* ignore */ }  // only after the copy succeeded
  }

  // Conversations saved before indexes were chosen up front opened with a bare
  // index name as their first message: recover the index from it and drop it,
  // with the model's reply to it, so the title comes from the first real question.
  // Runs once per conversation (v: 2 marks conversations in the current shape).
  function migrateShapes() {
    state.conversations.forEach((c) => {
      if (c.v === 2) return;
      const first = c.messages[0];
      if (first && first.role === "user" && /^[A-Za-z0-9_-]{1,64}$/.test(first.content.trim())) {
        c.repo = first.content.trim();
        c.messages = c.messages.slice(c.messages[1]?.role === "assistant" ? 2 : 1);
      } else if (!c.repo) {
        c.repo = null;  // unknown: it can be read but not continued
      }
      delete c.titled;
      c.v = 2;
      retitle(c);
      persist(c);
    });
  }

  function renderSidebar() {
    els.convs.innerHTML = "";
    state.conversations.forEach((c) => {
      const li = document.createElement("li");
      li.className = "item conv" + (c.id === state.activeId ? " active" : "");
      li.title = c.repo ? `${c.title}\n${c.repo}` : c.title;
      const text = document.createElement("div"); text.className = "text";
      const title = document.createElement("span"); title.className = "title"; title.textContent = c.title;
      const sub = document.createElement("span"); sub.className = "sub"; sub.textContent = c.repo || "no index";
      text.append(title, sub);
      const del = document.createElement("button"); del.className = "del"; del.textContent = "✕"; del.title = "Delete";
      del.onclick = (e) => {
        e.stopPropagation();
        if (state.streaming) return;
        state.conversations = state.conversations.filter((x) => x.id !== c.id);
        if (state.activeId === c.id) state.activeId = null;  // back to a new chat about the same index
        if (state.storageOk) ChatStore.remove(c.id).catch(() => {});
        // End the server-side session (fire and forget; it also ends on its own when idle).
        if (c.repo) fetch(`/api/chat/${encodeURIComponent(c.repo)}/${encodeURIComponent(c.id)}`, { method: "DELETE" }).catch(() => {});
        renderAll();
      };
      li.append(text, del);
      li.onclick = () => {
        if (state.streaming) return;
        state.activeId = c.id;
        state.selected = c.repo && state.repos.has(c.repo) ? c.repo : "";
        renderRepoSelect(); renderAll();
      };
      els.convs.appendChild(li);
    });
  }

  function renderHeader() {
    if (isHome()) return;  // layout() hides the header
    const repo = currentRepo();
    els.chatRepo.textContent = repo || "none recorded";
    els.reportToggle.hidden = !repo;
    els.compassOpen.hidden = !repo;
    const r = repo && state.repos.get(repo);
    els.chatRepoMeta.textContent = r ? repoMeta(r)
      : repo ? (state.reposLoaded ? "no longer indexed" : "")
      : "This conversation was started before indexes were chosen up front. Start a new chat to continue.";
  }

  // A message row. Its Copy button copies the message as written (Markdown source for
  // answers), read from bubble.source so a streamed answer can set it when it finishes.
  function addRow(role, text) {
    const row = document.createElement("div"); row.className = "row " + role;
    const bubble = document.createElement("div"); bubble.className = "bubble";
    if (role === "assistant") render(bubble, text); else bubble.textContent = text;
    bubble.source = text;
    const actions = document.createElement("div"); actions.className = "msg-actions";
    const copy = document.createElement("button");
    copy.type = "button"; copy.className = "msg-copy"; copy.textContent = "Copy";
    copy.title = role === "user" ? "Copy this prompt" : "Copy this response as Markdown";
    copyButton(copy, () => bubble.source);
    actions.appendChild(copy);
    row.append(bubble, actions); els.messages.appendChild(row);
    return bubble;
  }

  function renderMessages() {
    els.messages.innerHTML = "";
    if (isHome()) return;
    const c = active();
    if (!c || c.messages.length === 0) {
      const repo = currentRepo();
      const empty = document.createElement("div"); empty.className = "empty";
      empty.innerHTML = "<h1></h1><p>Ask anything about this repository: what it does, where something lives, " +
        "how a part works. Answers are built from the semantic index and the mirrored source.</p>";
      empty.querySelector("h1").textContent = repo ? `Ask about ${repo}` : "No index recorded";
      els.messages.appendChild(empty);
      return;
    }
    c.messages.forEach((m) => addRow(m.role, m.content));
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function renderComposer() {
    const c = active();
    const repo = currentRepo();
    const indexed = !!repo && (!state.reposLoaded || state.repos.has(repo));
    const usable = indexed && !state.streaming && !(c && state.reattaching.has(c.id));
    els.input.disabled = !usable; els.send.disabled = !usable;
    els.input.placeholder = !repo ? (c ? "Start a new chat to continue" : "Select an index")
      : !indexed ? `${repo} is no longer indexed. Start a new chat with another index.` : `Ask about ${repo}…`;
    els.newChat.disabled = !state.selected || state.streaming;
    els.newChat.title = state.selected ? `New chat about ${state.selected}` : "Select an index first";
  }

  // Which parts of the main area show: the home view, or the chat (or report) about an index.
  function layout() {
    const home = isHome();
    els.homeView.hidden = !home;
    els.header.hidden = home;
    els.reportView.hidden = home || state.view !== "report";
    els.messages.hidden = home || state.view === "report";
    els.form.hidden = home || state.view === "report";
  }

  const renderAll = () => { showChat(); renderSidebar(); renderNav(); renderHeader(); renderMessages(); renderComposer(); renderHome(); };

  // The ways back to the home view: marked current while on it, unavailable while an answer streams.
  function renderNav() {
    const home = isHome();
    els.navHome.classList.toggle("active", home);
    if (home) els.navHome.setAttribute("aria-current", "page"); else els.navHome.removeAttribute("aria-current");
    for (const b of [els.navHome, els.backHome, els.home]) {
      b.disabled = state.streaming;
      b.title = state.streaming ? "Available when the answer has finished" : "All indexes";
    }
  }

  // ---- home view: the indexes, with their actions ----
  const ago = (iso) => {
    const t = Date.parse(iso);
    if (!t) return "";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 90) return "just now";
    if (s < 5400) return `${Math.round(s / 60)} minutes ago`;
    if (s < 129600) return `${Math.round(s / 3600)} hours ago`;
    return `${Math.round(s / 86400)} days ago`;
  };
  const elt = (tag, className, text) => {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text != null) n.textContent = text;
    return n;
  };
  const button = (text, className, onClick, title) => {
    const b = elt("button", className, text); b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", onClick);
    return b;
  };

  function renderHome() {
    if (!isHome()) { els.homeView.innerHTML = ""; return; }
    const v = els.homeView;
    v.innerHTML = "";
    const wrap = elt("div", "home");
    const head = elt("div", "home-head");
    const titles = elt("div");
    titles.append(elt("h1", null, "Your indexes"),
      elt("p", "muted", "Choose an index to ask about its code, or add a git repository to index it."));
    const canIndex = state.config.indexing_enabled !== false;
    if (canIndex) head.append(titles, button("+ Add index", "primary", () => indexing.openAdd()));
    else head.append(titles);
    wrap.appendChild(head);

    if (!state.reposLoaded) {
      wrap.appendChild(elt("p", "muted", "Loading the indexes…"));
    } else if (!state.repos.size) {
      const empty = elt("div", "home-empty");
      empty.append(elt("h2", null, "No indexes yet"),
        elt("p", "muted", canIndex
          ? "Add a git repository: Code Atlas summarizes its files and folders, then answers questions about it."
          : "Indexing from the UI is off (set REPOS_ROOT in .env). Index a repository with ./scripts/index.sh."));
      if (canIndex) empty.appendChild(button("+ Add index", "primary", () => indexing.openAdd()));
      wrap.appendChild(empty);
    } else {
      const grid = elt("div", "index-grid");
      for (const [id, r] of state.repos) {
        const job = indexing.running(id);
        const card = elt("article", "index-card");
        const top = elt("div", "index-top");
        top.appendChild(button(id, "index-name", () => openChat(id), `Chat about ${id}`));
        if (job) top.appendChild(elt("span", "badge running", "syncing"));  // progress is in the panel above
        card.appendChild(top);
        const meta = elt("p", "muted index-meta", repoMeta(r) + (r.indexed_at ? ` · indexed ${ago(r.indexed_at)}` : ""));
        meta.title = lensTooltip(r);
        card.appendChild(meta);
        const path = elt("p", "index-path", r.repo_path || "Folder not recorded (indexed by an older version)");
        path.title = r.repo_path || "";
        card.appendChild(path);
        const actions = elt("div", "index-actions");
        actions.appendChild(button("Chat", "primary small", () => openChat(id)));
        const busy = job ? "A sync is running" : "";
        const sync = button("Sync…", "ghost", () => indexing.openSync(id), busy || "Check for changes and update the index");
        sync.hidden = !canIndex;
        const del = button("Delete…", "ghost danger", () => indexing.openDelete(id), busy || "Delete this index");
        sync.disabled = del.disabled = !!job;
        actions.append(sync, del);
        card.appendChild(actions);
        grid.appendChild(card);
      }
      wrap.appendChild(grid);
    }
    v.appendChild(wrap);
  }

  // Open the chat about an index (a new conversation, saved when its first question is sent).
  function openChat(repo) {
    if (state.streaming) return;
    state.selected = repo; state.activeId = null;
    renderRepoSelect(); renderAll();
    els.input.focus();
  }

  function goHome() {
    if (state.streaming) return;
    state.selected = ""; state.activeId = null;
    renderRepoSelect(); renderAll();
  }

  // ---- Temporal report view (the conversation's index) ----
  // Three views of one report: the summary (rendered from the report data), the
  // detailed report (the full Markdown document the indexer writes), and the business
  // impact (generated on demand from the report; impact.js). Each downloads as what it
  // is: the summary and the impact brief as standalone HTML pages, the detailed report as .md.
  const closeImpact = () => { state.report?.impact?.destroy(); if (state.report) state.report.impact = null; };

  function showChat() {
    closeImpact();
    state.view = "chat";
    els.reportActions.hidden = true;
    els.reportToggle.textContent = "Temporal report";
    layout();
  }

  const reportMessage = (text) => {
    els.reportView.innerHTML = "";
    const p = document.createElement("p"); p.className = "muted report"; p.textContent = text;
    els.reportView.appendChild(p);
  };
  // The view still shows this report in this mode (the user may have moved on during a fetch).
  const reportCurrent = (r, mode) => state.view === "report" && state.report === r && r.mode === mode && currentRepo() === r.repo;

  async function showReport() {
    const repo = currentRepo();
    if (!repo) return;
    const c = active();
    closeImpact();
    state.view = "report"; state.report = null;
    layout();
    els.reportActions.hidden = true;
    els.reportToggle.textContent = "Back to chat";
    els.reportView.innerHTML = '<div class="report"><p class="muted">Loading the report…</p></div>';
    try {
      const hasImpact = fetch(`/api/impact/${encodeURIComponent(repo)}`).then((r) => r.ok).catch(() => false);
      const res = await fetch(`/api/report/${encodeURIComponent(repo)}`);
      const body = await res.json();
      if (state.view !== "report" || active() !== c || currentRepo() !== repo) return;  // the user moved on
      if (!res.ok) {
        els.reportView.innerHTML = "";
        const empty = document.createElement("div"); empty.className = "empty";
        empty.innerHTML = "<h1>No Temporal report yet</h1><p></p>";
        empty.querySelector("p").textContent = `${body.error || ""} It is built when the index is updated with the Temporal lens on, or with ./scripts/report.sh ${repo}.`;
        els.reportView.appendChild(empty);
        return;
      }
      state.report = { repo, payload: body, markdown: null, mode: "summary", hasImpact: await hasImpact };
      els.reportActions.hidden = false;
      showReportMode("summary");
    } catch (e) {
      reportMessage(`Could not load the report: ${e.message}`);
    }
  }

  const discuss = (text) => {
    showChat(); renderMessages(); renderComposer();
    els.input.value = text;
    autosize(); els.input.focus();
  };

  async function showReportMode(mode, { focus } = {}) {
    const r = state.report;
    if (!r) return;
    closeImpact();
    r.mode = mode;
    els.reportSummary.setAttribute("aria-pressed", String(mode === "summary"));
    els.reportDetailed.setAttribute("aria-pressed", String(mode === "detailed"));
    els.reportImpact.setAttribute("aria-pressed", String(mode === "impact"));
    els.reportDownload.title = { summary: "Download the summary as an HTML page", detailed: "Download the detailed report as Markdown",
      impact: "Download the brief you are viewing as an HTML page" }[mode];
    els.reportDownload.disabled = mode === "detailed" && !r.markdown;
    if (mode === "summary") {
      window.renderTemporalReport(els.reportView, r.payload, {
        onDiscuss: (name) => discuss(`Tell me about the Temporal opportunity for "${name}": what would change, and what would we gain?`),
        onImpact: r.hasImpact ? (name) => showReportMode("impact", { focus: name }) : null,
      });
      els.reportView.scrollTop = 0;
      return;
    }
    if (mode === "impact") {
      r.impact = window.renderBusinessImpact(els.reportView, { repo: r.repo }, {
        focus,
        isCurrent: () => reportCurrent(r, "impact"),
        onDiscuss: (name) => discuss(`What's the business case for "${name}"? Which failure stories matter most, and what should I ask the customer?`),
      });
      els.reportView.scrollTop = 0;
      return;
    }
    if (!r.markdown) {
      els.reportView.innerHTML = '<div class="report"><p class="muted">Loading the detailed report…</p></div>';
      try {
        const res = await fetch(`/api/report/${encodeURIComponent(r.repo)}/markdown`);
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          if (reportCurrent(r, "detailed")) reportMessage(`Could not load the detailed report: ${body.error || res.statusText}`);
          return;
        }
        r.markdown = await res.text();
      } catch (e) {
        if (reportCurrent(r, "detailed")) reportMessage(`Could not load the detailed report: ${e.message}`);
        return;
      }
      if (!reportCurrent(r, "detailed")) return;
      els.reportDownload.disabled = false;
    }
    renderDetailedReport(r);
  }

  // Heading anchors the way the report renderer writes its links (render.py _anchor).
  const anchor = (text) => [...text.toLowerCase().replace(/ /g, "-")].filter((ch) => /[\p{L}\p{N}-]/u.test(ch)).join("");

  function renderDetailedReport(r) {
    els.reportView.innerHTML = "";
    const wrap = document.createElement("div"); wrap.className = "report md-report";
    if (r.payload.stale) {
      const n = document.createElement("p"); n.className = "notice";
      n.textContent = "The code has changed since this report was built. It will update on the next index run.";
      wrap.appendChild(n);
    }
    const doc = document.createElement("div");
    render(doc, r.markdown);
    // Anchors as data attributes, not ids, so a process name cannot collide with an id in the page.
    doc.querySelectorAll("h1, h2, h3, h4").forEach((hd) => { hd.dataset.anchor = anchor(hd.textContent); });
    doc.addEventListener("click", (e) => {
      const a = e.target.closest('a[href^="#"]');
      if (!a) return;
      e.preventDefault();
      const target = doc.querySelector(`[data-anchor="${CSS.escape(decodeURIComponent(a.getAttribute("href").slice(1)))}"]`);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    wrap.appendChild(doc);
    els.reportView.appendChild(wrap);
    els.reportView.scrollTop = 0;
  }

  const saveFile = (name, type, text) => {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = document.createElement("a"); a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  async function downloadReport() {
    const r = state.report;
    if (!r) return;
    if (r.mode === "impact") { r.impact?.download(); return; }
    if (r.mode === "detailed") {
      if (r.markdown) saveFile(`${r.repo}-temporal-report-detailed.md`, "text/markdown;charset=utf-8", r.markdown);
      return;
    }
    // The summary as one self-contained page: the rendered report with the app's styles inlined.
    const report = els.reportView.querySelector(".report");
    if (!report) return;
    let css = "";
    try { css = await (await fetch("style.css")).text(); } catch { /* the page still reads without styles */ }
    const clone = report.cloneNode(true);
    clone.querySelectorAll(".opp-actions").forEach((el) => el.remove());  // buttons that only work in the app
    const title = `Temporal report: ${r.repo}`.replace(/[&<>]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[ch]);
    saveFile(`${r.repo}-temporal-report.html`, "text/html;charset=utf-8", `<!doctype html>
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
${clone.outerHTML}
</body>
</html>
`);
  }

  // ---- Compass Web (the chat's index in the compose `compass` service) ----
  // The tab is opened on the click itself (so it is not blocked as a popup), then sent to
  // the URL the server looks up; if the lookup fails, the tab says why.
  const compassTitle = els.compassOpen.title;
  async function openCompass() {
    const repo = currentRepo();
    if (!repo) return;
    els.compassOpen.title = compassTitle;
    const tab = window.open("", "_blank");
    if (tab) { tab.opener = null; tab.document.title = "Opening Compass…"; tab.document.body.textContent = "Opening Compass…"; }
    let error;
    try {
      const res = await fetch(`/api/compass/${encodeURIComponent(repo)}`);
      const body = await res.json();
      if (res.ok) {
        if (tab) tab.location.replace(body.url); else window.open(body.url, "_blank", "noopener");
        return;
      }
      error = body.error || `Server returned ${res.status}`;
    } catch (e) {
      error = e.message;
    }
    if (tab) { tab.document.title = "Compass unavailable"; tab.document.body.textContent = `Could not open Compass: ${error}`; }
    els.compassOpen.title = `Could not open Compass: ${error}`;
  }

  // ---- chat ----
  function setStreaming(on) {
    state.streaming = on;
    els.send.hidden = on; els.stop.hidden = !on;
    renderComposer(); renderNav();
    if (!on && !els.input.disabled) els.input.focus();
  }

  // The newest messages within the context budget, starting on a user message: the
  // same rule as the server's trim_to_budget (chat/context.py).
  const takeTail = (messages, budget) => {
    const kept = []; let used = 0;
    for (let i = messages.length - 1; i >= 0; i--) {
      const n = messages[i].content.length;
      if (kept.length && used + n > budget) break;
      kept.unshift(messages[i]); used += n;
    }
    while (kept.length > 1 && kept[0].role !== "user") kept.shift();
    return kept.map(({ role, content }) => ({ role, content }));
  };

  // Reads a chat SSE response, calling onEvent for each data frame (a text chunk, or
  // {reset} / {stopped} / {error}); `: keepalive` comments and [DONE] are skipped.
  async function readEvents(res, onEvent) {
    const reader = res.body.getReader(); const decoder = new TextDecoder(); let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const frameText = buf.slice(0, idx); buf = buf.slice(idx + 2);
        if (!frameText.startsWith("data: ")) continue;
        const data = frameText.slice(6);
        if (data !== "[DONE]") onEvent(JSON.parse(data));
      }
    }
  }

  async function ask(text) {
    text = text.trim();
    const repo = currentRepo();
    if (!text || state.streaming || !repo) return;
    const c = active() || newConversation(repo);  // a new chat is saved with its first question
    const turnId = uuid();
    const prior = c.messages.slice();  // before the new message
    c.messages.push({ role: "user", content: text });
    c.pending = { turn_id: turnId, at: Date.now() };
    retitle(c);
    await persist(c);
    renderAll(); els.input.value = ""; autosize();
    const tail = takeTail(prior, state.chatContextChars);

    const bubble = addRow("assistant", "");
    bubble.classList.add("typing");
    let answer = ""; let frame = null;
    let stopped = false, failed = false, aborted = false;
    const flush = () => { frame = null; render(bubble, answer); els.messages.scrollTop = els.messages.scrollHeight; };

    setStreaming(true);
    state.controller = new AbortController(); state.turnId = turnId;
    try {
      const res = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repo_id: c.repo, session_id: c.id, turn_id: turnId, prior_count: prior.length, resync: !!c.resync,
          messages: [...tail, { role: "user", content: text }],
        }),
        signal: state.controller.signal,
      });
      if (!res.ok) {
        let detail = `Server returned ${res.status}`;
        try { const body = await res.json(); detail = body.error || JSON.stringify(body.detail) || detail; } catch { /* not JSON */ }
        throw new Error(detail);
      }

      await readEvents(res, (chunk) => {
        if (typeof chunk === "string") answer += chunk;
        else if (chunk && chunk.reset) answer = "";  // a retry restarted the answer
        else if (chunk && chunk.stopped) stopped = true;
        else if (chunk && chunk.error) { answer += `\n\n**Error:** ${chunk.error}`; failed = true; }
        if (!frame) frame = setTimeout(flush, 80);
      });
    } catch (e) {
      if (e.name === "AbortError") aborted = true;
      else { answer += `\n\n**Error:** ${e.message}`; failed = true; }
    } finally {
      if (frame) clearTimeout(frame);
      let final = answer;
      if (stopped || aborted) final = (answer ? `${answer}\n\n` : "") + "_(stopped)_";
      if (!final) { final = "(no response)"; failed = true; }
      bubble.classList.remove("typing"); render(bubble, final);
      bubble.source = final;
      c.messages.push({ role: "assistant", content: final });
      c.pending = null;
      // The server's context differs after a stop or failure: the next message replaces it.
      c.resync = stopped || failed || aborted;
      await persist(c);
      state.turnId = null;
      setStreaming(false); renderSidebar();
      els.messages.scrollTop = els.messages.scrollHeight;
    }
  }

  // Reattach: a conversation saved with a turn in flight (the page was reloaded or
  // closed mid-answer) gets that answer from the server, or a note that it is gone.
  async function reattach(c) {
    const p = c.pending;
    if (!p || !c.repo) return;
    state.reattaching.add(c.id); renderComposer();
    const live = active() === c && state.view === "chat";
    const bubble = live ? addRow("assistant", "") : null;
    if (bubble) bubble.classList.add("typing");
    let answer = "", stopped = false, missing = false, frame = null;
    const flush = () => { frame = null; if (bubble) { render(bubble, answer); els.messages.scrollTop = els.messages.scrollHeight; } };
    try {
      const res = await fetch(`/api/chat/${encodeURIComponent(c.repo)}/${encodeURIComponent(c.id)}/turns/${encodeURIComponent(p.turn_id)}/stream`);
      if (res.status === 404) missing = true;
      else if (!res.ok) throw new Error(`Server returned ${res.status}`);
      else await readEvents(res, (chunk) => {
        if (typeof chunk === "string") answer += chunk;
        else if (chunk && chunk.reset) answer = "";
        else if (chunk && chunk.stopped) stopped = true;
        else if (chunk && chunk.error) answer += `\n\n**Error:** ${chunk.error}`;
        if (!frame) frame = setTimeout(flush, 80);
      });
    } catch (e) {
      answer += `\n\n**Error:** ${e.message}`;
    }
    if (frame) clearTimeout(frame);
    let final = missing ? "_(response unavailable)_" : answer;
    if (stopped) final = (answer ? `${answer}\n\n` : "") + "_(stopped)_";
    c.messages.push({ role: "assistant", content: final || "(no response)" });
    c.pending = null;
    c.resync = true;  // the server's context may differ from what was recovered
    await persist(c);
    state.reattaching.delete(c.id);
    if (active() === c && state.view === "chat") renderMessages();
    renderComposer();
  }

  // Stop asks the server to cancel the turn; its stream then ends with {stopped}. The
  // request is aborted only if the stop itself fails.
  async function stopTurn() {
    const c = active();
    if (!state.streaming || !c || !state.turnId) return;
    try {
      const res = await fetch(`/api/chat/${encodeURIComponent(c.repo)}/${encodeURIComponent(c.id)}/stop`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ turn_id: state.turnId }),
      });
      if (!res.ok) throw new Error(`stop returned ${res.status}`);
    } catch {
      if (state.controller) state.controller.abort();
    }
  }

  // ---- indexes + health ----
  // Index details: file count plus the knowledge pack the Temporal lens was built with.
  function repoMeta(r) {
    const files = `${Number(r.files).toLocaleString()} files`;
    const mixed = (r.prompt_versions || []).length > 1 ? " · mixed summaries" : "";
    let versions = (r.pack_versions || []).map((v) => v.version);
    if (versions.length === 0 && r.pack_version) versions = [r.pack_version];
    if (!r.lens || r.lens === "off" || versions.length === 0) return files + mixed;
    const pack = `pack ${versions.join(" + ")}`;
    return (r.lens === "partial" ? `${files} · ${pack} (partial)` : `${files} · ${pack}`) + mixed;
  }

  function lensTooltip(r) {
    const prompts = r.prompt_versions || [];
    if (prompts.length > 1) {
      const counts = prompts.map((v) => `${v.version == null ? "older" : `version ${v.version}`}: ${v.files}`).join(", ");
      return `Summaries were written by different prompt versions (${counts}). Re-index with --force to regenerate them all.`;
    }
    if (r.lens === "mixed" || r.lens === "partial") {
      const counts = (r.pack_versions || [])
        .map((v) => `pack ${v.version}: ${v.files} ${v.files === 1 ? "file" : "files"}`).join(", ");
      const why = r.lens === "mixed" ? "Files use different pack versions" : "Some files have no Temporal lens yet";
      return `${why}${counts ? ` (${counts})` : ""}. Re-index with --refresh-lens to update every file.`;
    }
    if (r.lens === "on") return `Temporal lens built with knowledge pack ${r.pack_version}`;
    return "";
  }

  function renderRepoSelect() {
    const sel = els.repoSelect;
    sel.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = !state.reposLoaded ? "Loading…" : state.repos.size ? "Select an index…" : "No indexes yet";
    sel.appendChild(placeholder);
    for (const id of state.repos.keys()) {
      const opt = document.createElement("option"); opt.value = id; opt.textContent = id;
      sel.appendChild(opt);
    }
    if (state.reposLoaded && !state.repos.has(state.selected)) state.selected = "";
    sel.value = state.selected;
    const r = state.repos.get(state.selected);
    els.repoMeta.textContent = r ? repoMeta(r)
      : state.reposLoaded && !state.repos.size ? "Add a repository with + Add index." : "";
    els.repoMeta.title = r ? lensTooltip(r) : "";
    renderRepoActions();
    renderTemporalLink();
  }

  // Sync and Delete for the selected index (not while a sync of it runs).
  function renderRepoActions() {
    const id = state.selected;
    els.repoActions.hidden = !id;
    if (!id) return;
    const job = indexing.running(id);
    els.syncIndex.disabled = els.deleteIndex.disabled = !!job;
    els.syncIndex.title = job ? "A sync is running" : `Check ${id} for changes and update its index`;
    els.deleteIndex.title = job ? "A sync is running" : `Delete the index ${id}`;
  }

  async function loadRepos() {
    let reposJSON;
    try {
      const { repos } = await (await fetch("/api/repos")).json();
      state.repos = new Map((repos || []).map((r) => [r.repo_id, r]));
      reposJSON = JSON.stringify(repos);
    } catch {
      els.repoMeta.textContent = "Could not load indexes.";
      return;
    }
    // Rebuild the options only when the list of indexes changed (including the first
    // load), so the 30-second refresh never disturbs an open dropdown.
    const ids = [...state.repos.keys()].join("\n");
    const changed = !state.reposLoaded || ids !== state.repoIds;
    state.reposLoaded = true; state.repoIds = ids;
    if (changed) renderRepoSelect();
    else {
      const r = state.repos.get(state.selected);
      if (r) { els.repoMeta.textContent = repoMeta(r); els.repoMeta.title = lensTooltip(r); }
    }
    renderHeader(); renderComposer();
    // The home view is rebuilt only when something on it changed (a rebuild loses keyboard focus).
    if (changed || reposJSON !== state.reposJSON) renderHome();
    state.reposJSON = reposJSON;
    indexing.render();  // a finished sync's "Open chat" needs the new index
  }

  // Temporal UI link: the sync workflows of the selected index, or the UI's home page.
  // The static href in index.html is the fallback when /api/config is unavailable.
  const temporal = { url: els.temporalLink.href.replace(/\/$/, ""), namespace: "default" };
  function renderTemporalLink() {
    const base = `${temporal.url}/namespaces/${encodeURIComponent(temporal.namespace)}/workflows`;
    els.temporalLink.href = state.selected
      ? `${base}?query=${encodeURIComponent(`WorkflowId="sync-${state.selected}"`)}`
      : temporal.url;
  }
  async function loadConfig() {
    try {
      const c = await (await fetch("/api/config")).json();
      state.config = c;
      if (c.temporal_ui_url) temporal.url = c.temporal_ui_url.replace(/\/$/, "");
      if (c.temporal_namespace) temporal.namespace = c.temporal_namespace;
      if (c.chat_context_chars) state.chatContextChars = c.chat_context_chars;
      els.addIndex.hidden = els.syncIndex.hidden = c.indexing_enabled === false;
      renderHome();
    } catch { /* keep the fallback link */ }
    renderTemporalLink();
  }

  // Temporal Docs MCP light: green when a real docs search works, red with the fix otherwise.
  const DOCS_LABEL = { connected: "connected", unavailable: "unavailable", not_configured: "not signed in" };
  function renderDocsStatus(d) {
    const s = (d && d.status) || "unavailable";
    els.docsStatus.className = `docs-status ${s === "not_configured" ? "" : s}`;
    els.docsStatusText.textContent = `Temporal Docs MCP${s === "connected" ? "" : ` · ${DOCS_LABEL[s] || s}`}`;
    const when = d && d.checked_at ? `\nChecked ${new Date(d.checked_at).toLocaleTimeString()}. Click to check again.` : "";
    els.docsStatus.title = d ? [d.detail, d.fix].filter(Boolean).join("\n") + when : "Could not check the Temporal Docs MCP.";
  }

  let healthSeq = 0;  // only the latest check renders (a slow earlier one must not overwrite it)
  async function loadHealth(recheck = false) {
    const seq = ++healthSeq;
    try {
      const res = await fetch(`/api/health${recheck ? "?recheck=true" : ""}`); const h = await res.json();
      if (seq !== healthSeq) return;
      renderDocsStatus(h.temporal_docs);
      els.status.textContent = `Model: ${h.model}`;
      const problems = [];
      if (!h.api_key_configured) problems.push("ANTHROPIC_API_KEY is not set in .env");
      if (h.database !== "ok") problems.push(`Database ${h.database}`);
      if (h.temporal && h.temporal !== "ok") problems.push(`Temporal ${h.temporal}`);
      if (h.redis && h.redis !== "ok") problems.push(`Redis ${h.redis}`);
      if (state.storageWarning) problems.push(state.storageWarning);
      els.banner.hidden = problems.length === 0; els.banner.textContent = problems.join(". ");
    } catch {
      if (seq !== healthSeq) return;
      els.banner.hidden = false; els.banner.textContent = "Cannot reach the server.";
      renderDocsStatus(null);
    }
  }

  // ---- input ----
  const autosize = () => { els.input.style.height = "auto"; els.input.style.height = Math.min(els.input.scrollHeight, 200) + "px"; };
  els.input.addEventListener("input", autosize);
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); els.form.requestSubmit(); }
  });
  els.form.addEventListener("submit", (e) => { e.preventDefault(); ask(els.input.value); });
  els.stop.addEventListener("click", stopTurn);
  els.reportToggle.addEventListener("click", () => {
    if (state.view === "report") { showChat(); renderMessages(); renderComposer(); }
    else if (!state.streaming) showReport();
  });
  els.reportSummary.addEventListener("click", () => { if (state.report?.mode !== "summary") showReportMode("summary"); });
  els.reportDetailed.addEventListener("click", () => { if (state.report?.mode !== "detailed") showReportMode("detailed"); });
  els.reportImpact.addEventListener("click", () => { if (state.report?.mode !== "impact") showReportMode("impact"); });
  els.reportDownload.addEventListener("click", downloadReport);
  els.compassOpen.addEventListener("click", openCompass);

  // Selecting an index opens a new chat about it; the empty choice goes back to the home view.
  els.repoSelect.addEventListener("change", () => {
    if (state.streaming) { els.repoSelect.value = state.selected; return; }
    if (els.repoSelect.value) openChat(els.repoSelect.value); else goHome();
  });
  els.newChat.addEventListener("click", () => { if (state.selected) openChat(state.selected); });
  for (const b of [els.home, els.navHome, els.backHome]) b.addEventListener("click", goHome);
  els.addIndex.addEventListener("click", () => indexing.openAdd());
  els.syncIndex.addEventListener("click", () => { if (state.selected) indexing.openSync(state.selected); });
  els.deleteIndex.addEventListener("click", () => { if (state.selected) indexing.openDelete(state.selected); });

  const indexing = window.createIndexing({
    repos: () => state.repos,
    config: () => state.config,
    conversationsAbout,
    onSyncsChanged: () => { renderRepoActions(); renderHome(); },
    onIndexesChanged: loadRepos,
    // Deleting the index on screen goes home; its conversations stay in the sidebar.
    onDeleted: (repo) => { if (currentRepo() === repo) goHome(); },
    openChat,
  });

  els.docsStatus.addEventListener("click", () => {
    els.docsStatus.className = "docs-status checking";
    loadHealth(true);
  });

  (async () => {
    state.storageOk = await ChatStore.available();
    if (state.storageOk) {
      await ChatStore.persist();
      try { await importLegacy(); } catch { /* the legacy key stays for the next load */ }
      try { state.conversations = await ChatStore.loadAll(); } catch { state.storageOk = false; }
    }
    if (!state.storageOk) {
      // No IndexedDB (or it failed): work in memory, showing older conversations read-only from localStorage.
      state.conversations = legacyConversations() || [];
      showStorageWarning("Conversations will not be saved in this browser session.");
    }
    migrateShapes();
    // Opens on the home view: no conversation and no index selected.
    renderAll(); renderRepoSelect(); loadRepos(); loadHealth(); loadConfig(); indexing.start();
    setInterval(loadRepos, 30000);
    setInterval(loadHealth, 5 * 60 * 1000);
    for (const c of state.conversations.filter((x) => x.pending)) reattach(c);  // in parallel
  })();
})();
