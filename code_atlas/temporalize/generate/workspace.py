"""
The POC workspace: a local clone of the repository on branch temporalize/<run>, with the
approved plan, the test data and the validation harness written into temporal-poc/.

Also: path-safe file access, the diff against the original commit, and pushing the branch.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

POC_DIR = "temporal-poc"
HARNESS_SOURCE = Path(__file__).resolve().parent / "tz_harness.py"
GIT_TIMEOUT = 300


class WorkspaceError(Exception):
    pass


def default_root() -> Path:
    return Path(os.environ.get("TZ_DATA_DIR", "/data/tz")) / "workspaces"


def branch_name(run_id: str) -> str:
    return f"temporalize/{run_id}"


def run_id_of(run: dict) -> str:
    return run.get("_id") or run["id"]


def workspace_path(run: dict) -> Path:
    ws = (run.get("generate") or {}).get("workspace")
    return Path(ws) if ws else default_root() / run_id_of(run)


def git(cwd: Path, *args: str, check: bool = True, timeout: int = GIT_TIMEOUT, env: dict | None = None) -> str:
    """Run git in `cwd`; returns stdout. Never prompts for credentials."""
    # safe.directory=*: the source repository is owned by the host user, not the worker's.
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*", **(env or {})}
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                          env=full_env)
    if check and proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[:800]}")
    return proc.stdout


def safe_path(root: Path, rel: str) -> Path:
    """`rel` resolved inside `root`; raises on absolute paths or traversal."""
    if not rel or rel.strip() in (".", "./"):
        return root.resolve()
    if os.path.isabs(rel):
        raise WorkspaceError(f"Use a path relative to the workspace, not {rel}.")
    target = (root / rel).resolve()
    base = root.resolve()
    if target != base and base not in target.parents:
        raise WorkspaceError(f"{rel} is outside the workspace.")
    return target


# ----------------------------------------------------------------------
# The approved plan as Markdown (the generator's brief)
# ----------------------------------------------------------------------

def _claim_text(c: dict) -> str:
    return (c.get("edited_text") or c.get("text") or "").strip() if c.get("status") == "edited" else c.get("text", "")


def plan_markdown(run: dict, process: dict) -> str:
    plan = run.get("plan") or {}
    scope = run.get("scope") or {}
    lines = [f"# Temporalize plan for {run_id_of(run)}", "",
             f"Process: **{process.get('name', scope.get('selected', ''))}** (entry `{process.get('entry', '')}`)", ""]
    brief = run.get("brief") or {}
    if brief.get("focus"):
        lines += [f"Focus: {', '.join(brief['focus'])}", ""]

    lines += ["## Behaviors to preserve (approved)", "",
              "Each must hold in the Temporal version; check each one in side_by_side as a `match` row.", ""]
    for c in plan.get("claims") or []:
        if c.get("status") in ("accepted", "edited"):
            refs = ", ".join(c.get("refs") or [])
            lines.append(f"- [{c.get('id')}] {_claim_text(c)}" + (f" ({refs})" if refs else ""))
    lines.append("")

    lines += ["## Intentional changes (approved)", "",
              "Apply these only in the Temporal version and report each as an `intended` row.", ""]
    approved = [x for x in plan.get("changes") or [] if x.get("decision") == "approve"]
    for x in approved:
        refs = ", ".join(x.get("refs") or [])
        lines.append(f"- [{x.get('id')}] {x.get('title', '')}: {x.get('detail', '')}" + (f" ({refs})" if refs else ""))
    if not approved:
        lines.append("- None. Behavior must match the original exactly.")
    declined = [x for x in plan.get("changes") or [] if x.get("decision") == "decline"]
    if declined:
        lines += ["", "Declined (keep the original behavior, even if it looks wrong):", ""]
        lines += [f"- [{x.get('id')}] {x.get('title', '')}" for x in declined]
    lines.append("")

    lines += ["## Success criteria", "", "Report each in the scenarios' `criteria` list by its id.", ""]
    for c in run.get("criteria") or []:
        if c.get("on"):
            lines.append(f"- [{c.get('id')}] {c.get('priority', 'P1')}: {c.get('text', '')}")
    lines.append("")

    variants = [v for v in scope.get("variants") or [] if v.get("included")]
    if variants:
        lines += ["## Variants in scope (ways the code runs this process)", ""]
        for v in variants:
            refs = ", ".join(v.get("refs") or [])
            lines.append(f"- [{v.get('id')}] {v.get('name', '')}: {v.get('trigger', '')}" + (f" ({refs})" if refs else ""))
        lines.append("")
    in_scope = scope.get("in_scope") or []
    if in_scope:
        lines += ["In scope: " + ", ".join(i.get("label", i.get("id", "")) for i in in_scope), ""]
    out_steps = [st for st in scope.get("steps") or [] if not st.get("included", True)]
    if out_steps:
        lines += ["## Steps out of scope", "",
                  "Leave these as they are in the original code: do not convert them, and do not report on them.", ""]
        lines += [f"- {st['name']}: {st.get('does', '')}" for st in out_steps] + [""]
    risks = [r for r in scope.get("risks") or [] if r.get("included")]
    if risks:
        lines += ["## Failures the POC must address", ""] + [f"- {r['text']}" for r in risks] + [""]
    if scope.get("notes"):
        lines += ["## Scope notes from the solutions architect (follow them)", "", scope["notes"], ""]

    design = plan.get("design") or process.get("design") or {}
    lines += ["## Approved design", ""]
    if design.get("summary_text"):
        lines += [design["summary_text"], ""]
    elif design.get("summary"):
        lines += [design["summary"], ""]
    for el in design.get("elements") or []:
        files = ", ".join(el.get("files") or [])
        lines.append(f"- {el.get('kind')}: **{el.get('name')}**: {el.get('does', '')}" + (f" (from {files})" if files else ""))
    if design.get("removes"):
        lines += ["", "Removes:", ""] + [f"- {r}" for r in design["removes"]]
    if design.get("stays"):
        lines += ["", "Stays outside Temporal:", ""] + [f"- {s}" for s in design["stays"]]
    if design.get("why"):
        lines += ["", f"Why: {design['why']}"]
    if design.get("decisions"):
        lines += ["", "Design decisions (follow them; the cites are where they come from):", ""]
        lines += [f"- {d.get('decision')}: {d.get('reason', '')}" + (f" [{', '.join(d['cites'])}]" if d.get("cites") else "")
                  for d in design["decisions"]]
    if design.get("questions"):
        lines += ["", "Open questions (make a sensible choice, note it in temporal-poc/README.md):", ""]
        lines += [f"- {q}" for q in design["questions"]]
    research = plan.get("research") or {}
    if research:
        lines += ["", "The design was made with Temporal's temporal-developer skill"
                  + (f" ({', '.join(research.get('skill_read') or [])})" if research.get("skill_read") else "")
                  + " and the Temporal Docs MCP. You have the same tools: `read_temporal_skill` and "
                  "`search_temporal_docs`."]
    lines.append("")

    td = plan.get("testdata") or {}
    lines += ["## Test data (synthetic)", "", td.get("summary", ""), ""]
    if td.get("frozen_time"):
        lines.append(f"Frozen time: {td['frozen_time']} (use tz_harness.frozen_now()).")
    for f in td.get("failures") or []:
        lines.append(f"Injected failure: boundary `{f.get('boundary')}` fails {f.get('times', 1)} time(s). {f.get('detail', '')}")
    for f in td.get("files") or []:
        lines.append(f"- `testdata/{f.get('name')}`: {f.get('description', '')}")
    lines.append("")
    return "\n".join(lines)


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "").strip()
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "data.json"


def write_plan_files(ws: Path, run: dict, process: dict) -> None:
    poc = ws / POC_DIR
    (poc / "testdata").mkdir(parents=True, exist_ok=True)
    (poc / ".tz").mkdir(exist_ok=True)
    shutil.copyfile(HARNESS_SOURCE, poc / "tz_harness.py")
    (poc / "PLAN.md").write_text(plan_markdown(run, process))
    td = (run.get("plan") or {}).get("testdata") or {}
    for f in td.get("files") or []:
        (poc / "testdata" / _safe_name(f.get("name", ""))).write_text(f.get("content") or "")
    meta = {"run_id": run_id_of(run), "frozen_time": td.get("frozen_time"), "failures": td.get("failures") or []}
    (poc / "testdata" / "failures.json").write_text(json.dumps(meta, indent=2))
    (poc / ".gitignore").write_text(".tz/\n__pycache__/\n")
    card = {"name": process.get("name"), "entry": process.get("entry"), "card": process.get("card"),
            "design": process.get("design")}
    (poc / ".tz" / "process.json").write_text(json.dumps(card, indent=2, default=str))


# ----------------------------------------------------------------------
# Public
# ----------------------------------------------------------------------

def prepare_workspace(run: dict, process: dict, ws_root: Path | None = None) -> dict:
    """A fresh clone on temporalize/<run>, with the plan, test data and harness committed."""
    run_id = run_id_of(run)
    repo = run.get("repo") or {}
    source = Path(repo.get("path") or "")
    if not (source / ".git").exists():
        raise WorkspaceError(f"{source} is not a git repository visible to the worker.")
    root = Path(ws_root) if ws_root else default_root()
    root.mkdir(parents=True, exist_ok=True)
    ws = root / run_id
    if ws.exists():
        shutil.rmtree(ws)
    git(root, "clone", "--quiet", "--local", "--no-hardlinks", str(source), str(ws))
    git(ws, "config", "user.name", "Temporalize")
    git(ws, "config", "user.email", "temporalize@localhost")
    commit = repo.get("commit") or ""
    if commit:
        try:
            git(ws, "cat-file", "-e", f"{commit}^{{commit}}")
        except WorkspaceError:
            commit = ""
    base = commit or git(ws, "rev-parse", "HEAD").strip()
    branch = branch_name(run_id)
    git(ws, "checkout", "--quiet", "-B", branch, base)
    write_plan_files(ws, run, process)
    git(ws, "add", "-A")
    git(ws, "commit", "--quiet", "--allow-empty", "-m", "Temporalize: plan and test data")
    return {"workspace": str(ws), "branch": branch, "base_commit": base}


def base_commit(run: dict) -> str:
    return (run.get("generate") or {}).get("base_commit") or ""


def commit_all(ws: Path, message: str) -> bool:
    """Commit everything in the workspace (outside .tz); False when there was nothing to commit."""
    git(ws, "add", "-A")
    if not git(ws, "status", "--porcelain").strip():
        return False
    git(ws, "commit", "--quiet", "-m", message)
    return True


def diff(run: dict) -> str:
    ws = workspace_path(run)
    if not ws.exists():
        return ""
    base = base_commit(run) or "HEAD"
    # Working tree against the original commit: committed and uncommitted changes together.
    git(ws, "add", "--intent-to-add", ".", check=False)
    return git(ws, "diff", "--no-color", base, "--", ".", ":(exclude)temporal-poc/.tz", check=False)


def diff_stats(run: dict) -> dict:
    ws = workspace_path(run)
    base = base_commit(run) or "HEAD"
    out = git(ws, "diff", "--numstat", base, "--", ".", ":(exclude)temporal-poc/.tz", check=False)
    files = ins = dels = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            files += 1
            ins += int(parts[0]) if parts[0].isdigit() else 0
            dels += int(parts[1]) if parts[1].isdigit() else 0
    return {"files": files, "insertions": ins, "deletions": dels}


def changed_files(run: dict) -> list[dict]:
    """[{status: A|M|D|R, path, old_path?}] against the original commit."""
    ws = workspace_path(run)
    base = base_commit(run) or "HEAD"
    out = git(ws, "diff", "--name-status", "-M", base, "--", ".", ":(exclude)temporal-poc/.tz", check=False)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        status = parts[0][0]
        if status == "R" and len(parts) >= 3:
            rows.append({"status": "R", "old_path": parts[1], "path": parts[2]})
        elif len(parts) >= 2:
            rows.append({"status": status, "path": parts[1]})
    return rows


def read_file(run: dict, path: str) -> str:
    ws = workspace_path(run)
    target = safe_path(ws, path)
    if not target.is_file():
        raise WorkspaceError(f"There is no file {path} in the workspace.")
    if target.stat().st_size > 2_000_000:
        raise WorkspaceError(f"{path} is larger than 2 MB.")
    return target.read_text(errors="replace")


def _auth_header(token: str) -> str:
    return "Authorization: Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _https_url(url: str) -> str:
    url = url.strip()
    m = re.match(r"^git@([^:]+):(.+)$", url)
    if m:
        url = f"https://{m.group(1)}/{m.group(2)}"
    if not re.match(r"^https?://", url):
        url = "https://" + url
    return url


def push(run: dict, remote_url: str, token: str) -> dict:
    """Push temporalize/<run> over HTTPS; the token goes in a one-off header, never into .git/config."""
    ws = workspace_path(run)
    if not ws.exists():
        return {"ok": False, "url": None, "error": "The workspace does not exist yet."}
    remote = (remote_url or "").strip() or ((run.get("source") or {}).get("url") or "")
    if not remote:
        return {"ok": False, "url": None, "error": "No remote URL: give the repository's URL."}
    if not token:
        return {"ok": False, "url": None, "error": "A token with write access is needed to push."}
    url = _https_url(remote)
    branch = branch_name(run_id_of(run))
    try:
        git(ws, "-c", f"http.extraHeader={_auth_header(token)}", "push", url, f"HEAD:refs/heads/{branch}",
            "--force")  # the branch belongs to this run
    except WorkspaceError as e:
        return {"ok": False, "url": None, "error": str(e).replace(token, "***")}
    except subprocess.TimeoutExpired:
        return {"ok": False, "url": None, "error": "The push timed out."}
    web = url[:-4] if url.endswith(".git") else url
    branch_url = f"{web}/tree/{branch}" if "github.com" in web else web
    return {"ok": True, "url": branch_url, "error": None}
