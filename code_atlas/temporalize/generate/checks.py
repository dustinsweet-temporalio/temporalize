"""
Static checks of the generated POC: imports, lint, types and workflow determinism.

Each check is lenient where a tool is missing (the item passes and says what ran instead);
determinism is an AST scan of @workflow.defn classes.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

from temporalize.generate.workspace import POC_DIR, workspace_path

OUTPUT_CHARS = 4000
SKIP_MODULES = {"tz_harness", "validate"}   # imported by the scenarios, not by the worker
# Type errors here do not fail the check: the harness is ours, and stubs build modules dynamically.
TYPES_LENIENT = {"tz_harness.py", "stubs.py", "validate.py"}


def _counts_as_type_error(line: str) -> bool:
    path = line.split(":", 1)[0]
    if ": error:" not in line or not path.startswith(f"{POC_DIR}/"):
        return False
    rel = path[len(POC_DIR) + 1:]
    return rel not in TYPES_LENIENT and not rel.startswith("legacy/")


# Calls that differ between the original run and a replay, or do I/O, inside workflow code.
NONDETERMINISTIC = {
    "datetime.now": "use workflow.now()",
    "datetime.utcnow": "use workflow.now()",
    "datetime.datetime.now": "use workflow.now()",
    "datetime.datetime.utcnow": "use workflow.now()",
    "date.today": "use workflow.now().date()",
    "datetime.date.today": "use workflow.now().date()",
    "time.time": "use workflow.now().timestamp()",
    "time.sleep": "use asyncio.sleep() or workflow.sleep()",
    "time.monotonic": "use workflow.now()",
    "uuid.uuid4": "use workflow.uuid4()",
    "uuid.uuid1": "use workflow.uuid4()",
    "os.getenv": "read configuration in an activity or pass it as input",
    "open": "do file I/O in an activity",
    "input": "do I/O in an activity",
    "subprocess.run": "run processes in an activity",
    "subprocess.Popen": "run processes in an activity",
    "subprocess.check_output": "run processes in an activity",
    "threading.Thread": "workflows must not start threads",
    "socket.socket": "do network I/O in an activity",
}
NONDETERMINISTIC_PREFIXES = {
    "random.": "use workflow.random()",
    "requests.": "make HTTP calls in an activity",
    "httpx.": "make HTTP calls in an activity",
    "urllib.request.": "make HTTP calls in an activity",
    "os.environ": "read configuration in an activity or pass it as input",
}


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append(_dotted(node.func) + "()")
    return ".".join(reversed(parts))


def _is_workflow_defn(cls: ast.ClassDef) -> bool:
    for dec in cls.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = _dotted(target)
        if name in ("workflow.defn", "defn", "temporalio.workflow.defn"):
            return True
    return False


def _violation(name: str) -> str | None:
    if name in NONDETERMINISTIC:
        return NONDETERMINISTIC[name]
    for prefix, why in NONDETERMINISTIC_PREFIXES.items():
        if name.startswith(prefix) or name == prefix.rstrip("."):
            return why
    return None


def scan_determinism(source: str, filename: str = "<workflow>") -> list[dict]:
    """[{file, line, call, fix}] for nondeterministic calls inside @workflow.defn classes."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return [{"file": filename, "line": e.lineno or 0, "call": "syntax error", "fix": str(e.msg)}]
    found = []
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef) and _is_workflow_defn(cls)):
            continue
        for node in ast.walk(cls):
            name = None
            if isinstance(node, ast.Call):
                name = _dotted(node.func)
            elif isinstance(node, ast.Attribute) and _dotted(node) == "os.environ":
                name = "os.environ"
            if not name or name.startswith("workflow."):
                continue
            why = _violation(name)
            if why:
                found.append({"file": filename, "line": getattr(node, "lineno", 0), "call": name, "fix": why})
    seen, unique = set(), []
    for f in found:
        key = (f["line"], f["fix"])   # os.environ.get(...) is both a call and an attribute
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def _run(cmd: list[str] | str, cwd: Path, env: dict | None = None, timeout: int = 180) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})}, shell=isinstance(cmd, str))
    except subprocess.TimeoutExpired:
        return 124, f"Timed out after {timeout}s."
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_CHARS else "…" + text[-OUTPUT_CHARS:]


def poc_env(ws: Path, extra: dict | None = None) -> dict:
    paths = [str(ws / POC_DIR), str(ws)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    return {"PYTHONPATH": os.pathsep.join(paths), "PYTHONDONTWRITEBYTECODE": "1", **(extra or {})}


def _modules(poc: Path) -> list[str]:
    return sorted(p.stem for p in poc.glob("*.py") if p.stem not in SKIP_MODULES and not p.stem.startswith("test_"))


def check_imports(ws: Path) -> dict:
    poc = ws / POC_DIR
    mods = _modules(poc)
    if not mods:
        return {"id": "imports", "label": "Imports", "ok": False, "output": "temporal-poc/ has no Python modules yet."}
    order = (["stubs"] if "stubs" in mods else []) + [m for m in mods if m != "stubs"]
    code = "import importlib\nfor m in %r:\n    importlib.import_module(m)\nprint('imported', len(%r), 'modules')" % (order, order)
    rc, out = _run([sys.executable, "-c", code], poc, poc_env(ws))
    return {"id": "imports", "label": "Imports", "ok": rc == 0, "output": _tail(out)}


def check_lint(ws: Path) -> dict:
    if shutil.which("ruff"):
        rc, out = _run(["ruff", "check", "--no-cache", "--select", "E9,F63,F7,F82,F401", POC_DIR], ws)
        return {"id": "lint", "label": "Lint", "ok": rc == 0, "output": _tail(out or "No lint errors.")}
    files = [str(p.relative_to(ws)) for p in (ws / POC_DIR).rglob("*.py") if ".tz" not in p.parts]
    rc, out = _run([sys.executable, "-m", "py_compile", *files], ws)
    return {"id": "lint", "label": "Lint", "ok": rc == 0, "output": _tail(out or "ruff not installed, compiled instead.")}


def check_types(ws: Path) -> dict:
    if not shutil.which("mypy"):
        return {"id": "types", "label": "Types", "ok": True, "output": "mypy not installed, skipped."}
    rc, out = _run(["mypy", "--ignore-missing-imports", "--follow-imports=silent", "--no-error-summary",
                    "--cache-dir=/dev/null", "--explicit-package-bases", POC_DIR], ws, poc_env(ws), timeout=300)
    errors = [line for line in out.splitlines() if _counts_as_type_error(line)]
    return {"id": "types", "label": "Types", "ok": not errors,
            "output": _tail("\n".join(errors) if errors else "No type errors in the POC's modules.")}


def check_determinism(ws: Path) -> dict:
    poc = ws / POC_DIR
    found = []
    for path in sorted(poc.rglob("*.py")):
        if ".tz" in path.parts or "legacy" in path.parts or path.name in ("tz_harness.py", "validate.py"):
            continue
        found += scan_determinism(path.read_text(errors="replace"), str(path.relative_to(ws)))
    if not found:
        return {"id": "determinism", "label": "Determinism", "ok": True, "output": "No nondeterministic calls in workflow code."}
    lines = [f"{f['file']}:{f['line']}: {f['call']} in workflow code ({f['fix']})" for f in found]
    return {"id": "determinism", "label": "Determinism", "ok": False, "output": _tail("\n".join(lines))}


def run_checks(run: dict) -> dict:
    ws = workspace_path(run)
    items = [check_imports(ws), check_lint(ws), check_types(ws), check_determinism(ws)]
    return {"ok": all(i["ok"] for i in items), "items": items}
