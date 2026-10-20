"""
The business-impact views as Markdown, from the stored data. No model calls.

    exec       for the economic buyer: headline, ranked failure stories, cost ranges
               only where computed (with formula and inputs), what is not worth pursuing
    champion   for the engineering lead: stories with their code evidence, what a crash
               loses, what changes with Temporal, the verdict, the proof-of-concept scope
    discovery  for the Solutions Architect: questions by who to ask, missing inputs,
               the assumptions to confirm, the context used

Model-written text is put on one line; the web UI escapes it (impact.js), and the
Markdown is for download.
"""
VIEWS = ("exec", "champion", "discovery")
CASE_LABEL = {"strong": "Strong case", "moderate": "Moderate case", "weak": "Weak case", "none": "No case"}
SEVERITY_LABEL = {"high": "high impact", "medium": "medium impact", "low": "low impact"}
SYMBOLS = {"$", "€", "£", "¥", "₹"}
FILTERED = "What was filtered out"


def one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def money(value: float, currency: str) -> str:
    amount = f"{value:,.0f}" if value >= 100 else f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{currency}{amount}" if currency in SYMBOLS else f"{amount} {currency}"


def cost_range(cost: dict) -> str:
    low, high, cur = cost["low"], cost["high"], cost["currency"]
    span = money(low, cur) if low == high else f"{money(low, cur)} to {money(high, cur)}"
    return f"{span} per month"


def _num(v: float) -> str:
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"


def cost_table(cost: dict) -> list[str]:
    lines = [f"**{cost['t']}** = `{cost['formula']}`", "", "| Input | Value | Unit | Source |", "|---|---|---|---|"]
    for r in cost["inputs"]:
        value = _num(r["low"]) if r["low"] == r["high"] else f"{_num(r['low'])} to {_num(r['high'])}"
        source = f"context {r['cite']}" if r["basis"] == "context" else "default (placeholder)"
        lines.append(f"| {r['name'].replace('_', ' ')} | {value} | {r['unit']} | {source} |")
    lines += ["", f"_{cost['label']}_"]
    return lines


def _who(story: dict, data: dict) -> str:
    names = data.get("stakeholders") or {}
    return ", ".join(names.get(w, w.replace("_", " ")) for w in story["who_feels_it"])


def _category(story: dict, data: dict) -> str:
    return (data.get("categories") or {}).get(story["category"], story["category"].replace("_", " "))


def footer(data: dict) -> list[str]:
    src, m, ctx = data["source_report"], data["mapping"], data["context"]
    commit = (src.get("commit_sha") or "")[:7] or "unknown"
    context = f"context {ctx['hash']}" if ctx["present"] else "no customer context"
    return ["", "---", "",
            f"_Built from the Temporal report of commit {commit} (generated {src.get('generated_at') or 'unknown'}); "
            f"mapping {m['id']} {m['version']}; model {data['model']}; {context}. "
            "Numbers are illustrative, computed from stated inputs._"]


def _assumptions(data: dict) -> list[str]:
    items = data.get("assumptions") or []
    if not items:
        return []
    return ["## Assumptions", "", "These are not established by the code or the customer context:", ""] + [
        f"- {one_line(a)}" for a in items] + [""]


def _not_worth(data: dict) -> list[str]:
    out = []
    rows = [s for s in data.get("skipped", []) if s["reason"] == "not suitable"]
    if rows:
        out += ["## Not worth pursuing", ""]
        for s in rows:
            out.append(f"- **{one_line(s['name'])}**: {one_line(s.get('detail'))}")
            if s.get("would_change"):
                out.append(f"  - Would change if: {'; '.join(one_line(w).rstrip('.') for w in s['would_change'])}.")
        out.append("")
    quiet = [s for s in data.get("skipped", []) if s["reason"] == "no grounded consequence"]
    if quiet:
        out += ["## No failure story from the code", "",
                "The Temporal report rates these processes a fit, but their code facts show nothing a crash would "
                "lose, so there is no business story to tell yet:", ""]
        out += [f"- **{one_line(s['name'])}**" for s in quiet] + [""]
    return out


def _not_assessed(data: dict) -> list[str]:
    rows = [s for s in data.get("skipped", []) if s["reason"] in ("error", "failed")]
    if not rows:
        return []
    return ["## Not assessed", ""] + [f"- **{one_line(s['name'])}**: {one_line(s.get('detail'))}" for s in rows] + [""]


def first_step(data: dict) -> str:
    procs = data.get("processes") or []
    if not procs:
        return "None recommended: no assessed process shows a business case from the code."
    top = procs[0]
    if top.get("cost"):
        return (f"Confirm the inputs behind the {top['name']} estimate with the customer, then scope a proof of "
                f"concept on {top['name']}.")
    return (f"Ask the discovery questions for {top['name']} to size the problem, then scope a proof of concept "
            f"on it.")


def render_exec(data: dict) -> str:
    out = [f"# Business impact: {data['repo_id']}", "", f"**{one_line(data['headline'])}**", "",
           one_line(data["exec_summary"]), ""]
    procs = data.get("processes") or []
    if procs:
        out += ["## Where failures would cost the business", ""]
    for i, p in enumerate(procs, 1):
        out += [f"### {i}. {one_line(p['name'])}: {CASE_LABEL[p['business_case']]}", ""]
        for s in p["stories"]:
            out.append(f"- **{_category(s, data)}** ({SEVERITY_LABEL[s['severity']]}; felt by {_who(s, data)}): "
                       f"{one_line(s['scenario'])}")
        out.append("")
        if p.get("case_reason"):
            out += [f"Why it matters: {one_line(p['case_reason'])}", ""]
        if p.get("case_capped"):
            out += [f"_{p['case_capped']}_", ""]
        if p.get("cost"):
            out += [f"**Illustrative cost: {cost_range(p['cost'])}**", ""] + cost_table(p["cost"]) + [""]
        elif p.get("missing_inputs"):
            out += ["No cost estimate yet: the customer has not stated the inputs. See the discovery sheet.", ""]
    out += _not_worth(data)
    out += ["## Recommended first step", "", first_step(data), ""]
    out += _assumptions(data)
    return "\n".join(out + footer(data)) + "\n"


def _evidence(p: dict) -> dict[str, dict]:
    return {e["id"]: e for e in p.get("evidence", [])}


def render_champion(data: dict) -> str:
    out = [f"# Business impact for engineering: {data['repo_id']}", "", one_line(data["headline"]), ""]
    for p in data.get("processes") or []:
        ev = _evidence(p)
        out += [f"## {one_line(p['name'])}", "",
                f"{CASE_LABEL[p['business_case']]}. Temporal report: {p['verdict_class']}, {p['overall']}/10; "
                f"strongest reason: {p['best_path_t']}. Entry point: `{p['entry']}`.", ""]
        if p.get("case_capped"):
            out += [f"_{p['case_capped']}_", ""]
        if p.get("low_confidence"):
            out += [f"> **Low confidence:** {one_line('; '.join(p['low_confidence']))}", ""]
        out += ["### Failure stories", ""]
        for s in p["stories"]:
            out.append(f"- **{_category(s, data)}** ({SEVERITY_LABEL[s['severity']]}"
                       + (f", stage: {one_line(s['stage'])}" if s["stage"] else "") + f"): {one_line(s['scenario'])}")
            for c in s["cites"]:
                if c in ev:
                    e = ev[c]
                    refs = f" [{', '.join(e['refs'])}]" if e["refs"] else ""
                    out.append(f"  - {c} {e['label']}: {one_line(e['text'])}{refs}")
                elif c.startswith("C"):
                    out.append(f"  - {c} (customer context): {one_line(data['context']['items'].get(c, ''))}")
        out.append("")
        if p.get("crash_loss"):
            out += ["### What a crash loses today", ""] + [
                f"- **{one_line(c['stage'])}**: {one_line(c['loses'])}"
                + (f" [{', '.join(c['refs'])}]" if c.get("refs") else "") for c in p["crash_loss"]] + [""]
        wc = p.get("what_changes") or {}
        if any(wc.get(k) for k in ("gains", "removes", "stays")):
            out += ["### What changes with Temporal", ""]
            if wc.get("gains"):
                out += ["**Gains:**"] + [f"- {one_line(g['text'])} ({', '.join(g['cites'])})" for g in wc["gains"]]
            if wc.get("removes"):
                out += ["", "**Removes:**"] + [f"- {one_line(r)}" for r in wc["removes"]]
            if wc.get("stays"):
                out += ["", "**Stays outside Temporal:**"] + [f"- {one_line(r)}" for r in wc["stays"]]
            out.append("")
        w = p.get("workaround") or {}
        if w.get("text"):
            out += [f"**Workaround today** ({w['basis']}): {one_line(w['text'])}", ""]
        if p.get("drivers"):
            out += ["**What decides the size of the problem:**"] + [
                f"- {d['kind'].replace('_', ' ')} ({d['basis']}): {one_line(d['text'])}" for d in p["drivers"]] + [""]
        if p.get("verdict"):
            out += ["### Verdict", "", one_line(p["verdict"]), ""]
        if p.get("would_change"):
            out += ["**What would change the verdict:**"] + [f"- {one_line(w)}" for w in p["would_change"]] + [""]
        gains = [one_line(g["text"]) for g in wc.get("gains", [])][:2]
        out += ["### Proof-of-concept scope", "",
                f"Start at `{p['entry']}`." + (f" Show: {'; '.join(gains)}." if gains else ""), ""]
    out += _not_worth(data) + _not_assessed(data)
    if data.get("dropped"):
        out += [f"## {FILTERED}", "", "Checks against the code facts and the context removed these:", ""] + [
            f"- {one_line(d)}" for d in data["dropped"]] + [""]
    return "\n".join(out + footer(data)) + "\n"


def render_discovery(data: dict) -> str:
    out = [f"# Discovery sheet: {data['repo_id']}", "",
           "Questions that turn assumptions into facts, most valuable first, grouped by who to ask.", ""]
    names = data.get("stakeholders") or {}
    groups: dict[str, list[dict]] = {}
    for q in data.get("questions") or []:
        groups.setdefault(q["ask"], []).append(q)
    if not groups:
        out += ["No open questions.", ""]
    for ask, qs in groups.items():
        out += [f"## Ask {names.get(ask, ask.replace('_', ' '))}", ""]
        for q in qs:
            fills = q["fills"].replace("_", " ")
            tag = f"cost input: {fills} ({q['unit']})" if q["source"] == "missing input" else f"resolves: {fills}"
            out.append(f"1. {one_line(q['question'])}")
            out.append(f"   - Why: {one_line(q['why'])} ({tag}; for {', '.join(q['processes'])})")
        out.append("")
    missing = [(p["name"], i) for p in data.get("processes") or [] for i in p.get("missing_inputs", [])]
    if missing:
        out += ["## Missing inputs for cost estimates", ""] + [
            f"- {one_line(n)}: {i.replace('_', ' ')}" for n, i in missing] + [""]
    out += _assumptions(data)
    items = data["context"]["items"]
    out += ["## Customer context used", ""] + ([f"- {cid}: {one_line(t)}" for cid, t in items.items()]
                                               or ["None given. The briefs are qualitative only."]) + [""]
    return "\n".join(out + footer(data)) + "\n"


def render(data: dict) -> dict[str, str]:
    """Every view, by name."""
    return {"exec": render_exec(data), "champion": render_champion(data), "discovery": render_discovery(data)}
