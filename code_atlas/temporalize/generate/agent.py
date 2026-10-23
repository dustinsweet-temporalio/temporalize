"""
The coding agent: a bounded tool loop over the Messages API that reads and writes files in
the workspace and runs commands there. Every call is counted on the run (RunLLM).

Resumable: with a `session`, the conversation is saved after every turn (next to the workspace,
outside its git tree), and a call stops after `turn_budget` turns or `time_budget` seconds. The
workflow runs the agent as a series of short activities; a retried or next one carries on from
the last saved turn instead of starting over.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from temporalize.generate.checks import poc_env
from temporalize.generate.workspace import WorkspaceError, git, safe_path
from temporalize.research import READ_SKILL_TOOL, SEARCH_DOCS_TOOL, Research

OUTPUT_CHARS = 8000
READ_CHARS = 60_000
CONTEXT_BUDGET_CHARS = 600_000      # tool output kept in the conversation before older results are dropped
MAX_COMMAND_SECONDS = 300
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tz", ".mypy_cache", ".ruff_cache"}

TOOLS: list[dict[str, Any]] = [
    {"name": "list_files", "description": "List files under a folder of the workspace (relative path; '.' for the root).",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "depth": {"type": "integer", "description": "Folder levels to show (default 3)"}},
         "required": ["path"]}},
    {"name": "read_file", "description": "Read a text file of the workspace, optionally a line range (1-based, inclusive). Lines are numbered.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
         "required": ["path"]}},
    {"name": "write_file", "description": "Create or overwrite a file of the workspace with the full content given.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "replace_in_file", "description": "Replace one exact, unique occurrence of `old` with `new` in a workspace file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
         "required": ["path", "old", "new"]}},
    {"name": "run_command", "description": (
        "Run a shell command in the workspace root (cwd) and get its exit code and output (truncated). "
        "PYTHONPATH includes temporal-poc/ and the workspace root; the TZ_TEMPORAL_* variables point at the "
        "Temporal server. No interactive input. Do not install packages."),
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string"}, "timeout_seconds": {"type": "integer", "description": "At most 300 (default 120)"}},
         "required": ["command"]}},
    READ_SKILL_TOOL,
    SEARCH_DOCS_TOOL,
    {"name": "finish", "description": "Stop: the work is done (or cannot go further). Summarize what you built and what was verified.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
]


@dataclass
class AgentResult:
    summary: str
    turns: int
    finished: bool                  # the model called finish
    files_changed: list[str] = field(default_factory=list)
    done: bool = True               # False: paused at the call's budget; call again to carry on
    research: dict = field(default_factory=dict)   # skill files read and docs searches made (research.py)


def sessions_dir(workspace: Path) -> Path:
    """Saved conversations: beside the workspace, so they are never committed or diffed."""
    return workspace.parent / f"{workspace.name}.agent"


def reset_sessions(workspace: Path) -> None:
    """A fresh workspace starts every conversation over."""
    shutil.rmtree(sessions_dir(workspace), ignore_errors=True)


def _load(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None   # a torn write: start this session over


def _save(path: Path | None, state: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def _blocks(content) -> list[dict[str, Any]]:
    """Response blocks as plain JSON (thinking blocks keep their signatures, so they stay valid)."""
    return [b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else b for b in content]


class Tools:
    def __init__(self, workspace: Path, env: dict[str, str], research: Research | None = None) -> None:
        self.ws = workspace.resolve()
        self.env = env
        self.written: list[str] = []
        self.research = research or Research(max_searches=12)   # the temporal-developer skill and the Docs MCP

    def handle(self, name: str, args: dict[str, Any]) -> str:
        if self.research.handles(name):
            return self.research.handle(name, args)
        try:
            if name == "list_files":
                return self.list_files(args.get("path") or ".", int(args.get("depth") or 3))
            if name == "read_file":
                return self.read_file(args["path"], args.get("start_line"), args.get("end_line"))
            if name == "write_file":
                return self.write_file(args["path"], args.get("content", ""))
            if name == "replace_in_file":
                return self.replace_in_file(args["path"], args["old"], args.get("new", ""))
            if name == "run_command":
                return self.run_command(args["command"], int(args.get("timeout_seconds") or 120))
            return f"Unknown tool {name}."
        except (WorkspaceError, KeyError, ValueError, OSError) as e:
            return f"Error: {e}"

    def list_files(self, rel: str, depth: int) -> str:
        root = safe_path(self.ws, rel)
        if not root.is_dir():
            return f"Error: {rel} is not a folder."
        lines: list[str] = []
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            level = len(Path(dirpath).parts) - base_depth
            if level >= depth:
                dirnames[:] = []
            for f in sorted(filenames):
                p = Path(dirpath) / f
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                lines.append(f"{p.relative_to(self.ws)}  ({size:,} bytes)")
                if len(lines) >= 800:
                    return "\n".join(lines + ["… (more files not shown)"])
        return "\n".join(lines) or "(empty)"

    def read_file(self, rel: str, start: int | None, end: int | None) -> str:
        path = safe_path(self.ws, rel)
        if not path.is_file():
            return f"Error: there is no file {rel}."
        lines = path.read_text(errors="replace").splitlines()
        s = max(1, int(start or 1))
        e = min(len(lines), int(end or len(lines)))
        out = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(s, e + 1))
        if len(out) > READ_CHARS:
            out = out[:READ_CHARS] + f"\n… (truncated; {len(lines)} lines in all, read a range)"
        return out or "(empty file)"

    def write_file(self, rel: str, content: str) -> str:
        path = safe_path(self.ws, rel)
        if ".git" in path.relative_to(self.ws).parts:
            return "Error: do not write inside .git."
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self._note(rel)
        return f"Wrote {rel} ({len(content.splitlines())} lines)."

    def replace_in_file(self, rel: str, old: str, new: str) -> str:
        path = safe_path(self.ws, rel)
        if not path.is_file():
            return f"Error: there is no file {rel}."
        text = path.read_text(errors="replace")
        n = text.count(old)
        if n == 0:
            return "Error: `old` was not found; read the file and copy the text exactly."
        if n > 1:
            return f"Error: `old` occurs {n} times; include more context so it is unique."
        path.write_text(text.replace(old, new, 1))
        self._note(rel)
        return f"Replaced in {rel}."

    def run_command(self, command: str, timeout: int) -> str:
        timeout = max(5, min(MAX_COMMAND_SECONDS, timeout))
        try:
            proc = subprocess.run(command, shell=True, cwd=str(self.ws), capture_output=True, text=True,
                                  timeout=timeout, env={**os.environ, **self.env}, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            return f"Timed out after {timeout}s.\n{_tail(out)}"
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return f"exit code {proc.returncode}\n{_tail(out.strip())}"

    def _note(self, rel: str) -> None:
        if rel not in self.written:
            self.written.append(rel)


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_CHARS else f"… ({len(text) - OUTPUT_CHARS:,} chars cut)\n" + text[-OUTPUT_CHARS:]


def _move_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    """One cache breakpoint, on the newest user turn, so each call reads the conversation so far from cache."""
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for block in m["content"]:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    last = messages[-1]["content"]
    if isinstance(last, list) and last and isinstance(last[-1], dict):
        last[-1]["cache_control"] = {"type": "ephemeral"}


def _trim_context(messages: list[dict[str, Any]]) -> None:
    """Drop the oldest tool outputs once the conversation carries too much of them."""
    results = [b for m in messages if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for b in results)
    for b in results[:-6]:   # keep the latest few intact
        if total <= CONTEXT_BUDGET_CHARS:
            break
        size = len(str(b.get("content", "")))
        if size > 200:
            b["content"] = "[truncated: older tool output removed to save space]"
            total -= size


def run_agent(llm, workspace: Path, system: str, task: str, heartbeat: Callable[[str], None],
              max_turns: int = 80, env: dict[str, str] | None = None, session: str | None = None,
              turn_budget: int | None = None, time_budget: float | None = None) -> AgentResult:
    """
    Run the tool loop until the model calls finish, stops calling tools, or runs out of turns.
    With a session, resume the saved conversation and pause (done=False) once this call has used
    `turn_budget` turns or `time_budget` seconds.
    """
    tools = Tools(workspace, {**poc_env(workspace), **(env or {})})
    sys_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    path = sessions_dir(workspace) / f"{session}.json" if session else None
    state = _load(path) or {"messages": [{"role": "user", "content": [{"type": "text", "text": task}]}],
                            "turn": 0, "summary": "", "finished": False, "over": False, "written": []}
    messages: list[dict[str, Any]] = state["messages"]
    tools.written = list(state.get("written") or [])
    used_before = state.get("research") or {}
    tools.research.skill_read = list(used_before.get("skill_read") or [])
    tools.research.queries = list(used_before.get("docs_queries") or [])
    tools.research.urls = list(used_before.get("docs_urls") or [])
    started, used = time.monotonic(), 0
    while not (state["finished"] or state["over"]) and state["turn"] < max_turns:
        if (turn_budget and used >= turn_budget) or (time_budget and time.monotonic() - started >= time_budget):
            break
        state["turn"] += 1
        used += 1
        turn = state["turn"]
        heartbeat(f"turn {turn}")
        _trim_context(messages)
        _move_cache_breakpoint(messages)
        message = llm.create(system=sys_blocks, messages=messages, tools=TOOLS, max_tokens=32000)
        content = _blocks(message.content)
        messages.append({"role": "assistant", "content": content})   # unchanged: thinking blocks stay valid
        calls = [b for b in content if b.get("type") == "tool_use"]
        if not calls:
            state["summary"] = next((b["text"] for b in content if b.get("type") == "text"), "") or state["summary"]
            if message.stop_reason == "max_tokens":
                messages.append({"role": "user", "content": [{"type": "text", "text":
                                 "Your answer was cut off at the token limit. Continue with smaller steps "
                                 "(write large files in parts with replace_in_file)."}]})
            else:
                state["over"] = True
            _save(path, {**state, "written": tools.written, "research": tools.research.record()})
            continue
        results = []
        for block in calls:
            if block["name"] == "finish":
                state["summary"], state["finished"] = (block.get("input") or {}).get("summary", ""), True
                output = "Finished."
            else:
                output = tools.handle(block["name"], block.get("input") or {})
            results.append({"type": "tool_result", "tool_use_id": block["id"], "content": output})
        if turn == max_turns - 3 and not state["finished"]:
            results.append({"type": "text", "text": "Three turns left: get to a working state and call finish."})
        messages.append({"role": "user", "content": results})
        _save(path, {**state, "written": tools.written, "research": tools.research.record()})
    done = state["finished"] or state["over"] or state["turn"] >= max_turns
    changed = []
    try:
        out = git(workspace, "status", "--porcelain", check=False)
        changed = [line[3:] for line in out.splitlines() if line[3:] and not line[3:].startswith("temporal-poc/.tz")]
    except Exception:
        pass
    changed = changed or tools.written
    return AgentResult(summary=state["summary"] or ("The agent stopped without a summary." if done else ""),
                       turns=state["turn"], finished=state["finished"], files_changed=changed, done=done,
                       research=tools.research.record())
