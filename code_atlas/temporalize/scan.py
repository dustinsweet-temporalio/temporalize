"""
The local scan behind screen 2 ("What we found"): plain code, no model calls, seconds.

    list_files     the committed files at HEAD the indexer will read (same filter as the indexer)
    count          files, Python files, lines, tests, languages, top folders, entry points
    read_imports   Python imports (ast) mapped to tools and integrations by a lookup table
    how_it_runs    crontab files, systemd units, scheduler libraries, sleep-based polling,
                   state files, CronJobs, scheduled GitHub Actions, Docker, Procfile
    find_secrets   detect-secrets when installed, else a set of regexes; never stores a value
    scan           all of the above in order, reporting each step to on_step

Exclusions are the indexer's (INDEX_EXCLUDE, from .env) plus Temporalize's extra patterns
(`DEFAULT_EXTRA_EXCLUDES`, data files) plus folders the user leaves out; `index_patterns`
turns the run's exclusions into the extra patterns the index sync is started with, so the
scan counts what will be indexed.
"""
from __future__ import annotations

import ast
import configparser
import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Callable

# Data files: left out of the assessment on top of the indexer's own exclusions.
DEFAULT_EXTRA_EXCLUDES: list[str] = [
    "*.csv", "*.tsv", "*.parquet", "*.xlsx", "*.xls", "*.db", "*.sqlite", "*.sqlite3", "*.pkl",
    "*.npy", "*.h5", "*.jsonl", "*.mp4", "*.mp3", "*.wav",
]
DEFAULT_MAX_FILE_BYTES = 1_000_000
MAX_READ_BYTES = 2_000_000

StepCallback = Callable[[str, str, str], None]   # (step id, state, detail)


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------

def excluded_by(path: str, patterns: list[str] | tuple[str, ...]) -> str | None:
    """The first pattern that excludes `path`: the indexer's rules (GitAdapter._matching_exclude_pattern)."""
    parts = path.split("/")
    folders, name = parts[:-1], parts[-1]
    for pattern in patterns:
        if pattern.endswith("/"):
            folder = pattern.rstrip("/")
            if "/" in folder:
                if f"/{folder}/" in f"/{path}":
                    return pattern
            elif any(fnmatch.fnmatch(part, folder) for part in folders):
                return pattern
        elif "/" in pattern:
            if fnmatch.fnmatch(path, pattern.lstrip("/")):
                return pattern
        elif fnmatch.fnmatch(name, pattern):
            return pattern
    return None


def _base_patterns() -> tuple[str, ...]:
    try:
        from semantic_ingress.config import FileFilterConfig

        return FileFilterConfig().exclude_patterns
    except Exception:  # the indexer is not installed (tests outside the image)
        return ("__pycache__/", ".git/", ".venv/", "venv/", "node_modules/", "build/", "dist/", "*.pyc")


def index_patterns(exclude: dict | None) -> list[str]:
    """The run's exclusions as extra index patterns (the sync's `exclude_extra`)."""
    exclude = exclude or {}
    patterns = list(exclude.get("patterns", DEFAULT_EXTRA_EXCLUDES))
    folders = [f.strip().strip("/") + "/" for f in exclude.get("folders", []) if f.strip().strip("/")]
    return patterns + folders


def list_files(repo: Path, patterns: list[str], folders: list[str], max_file_bytes: int) -> list[str]:
    """
    The committed files at HEAD that the indexer would read: regular, text, under the size limit,
    and not excluded by the indexer's patterns, `patterns` or `folders`. A folder that is not a
    git repository is walked on disk with the same rules.
    """
    extra = index_patterns({"patterns": patterns, "folders": folders})
    try:
        import pygit2  # noqa: F401
        from semantic_ingress.config import FileFilterConfig
        from semantic_ingress.core.git_adapter import GitAdapter

        config = FileFilterConfig(max_file_size_bytes=max_file_bytes,
                                  exclude_patterns=tuple(FileFilterConfig().exclude_patterns) + tuple(extra))
        return sorted(GitAdapter(Path(repo), config).get_snapshot().files)
    except ImportError:
        pass
    except ValueError:  # not a git repository, or no commits
        if (Path(repo) / ".git").exists():
            raise
    return _walk(Path(repo), list(_base_patterns()) + extra, max_file_bytes)


def _walk(repo: Path, patterns: list[str], max_file_bytes: int) -> list[str]:
    out = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in names:
            full = Path(root) / name
            rel = full.relative_to(repo).as_posix()
            try:
                if not full.is_file() or full.stat().st_size > max_file_bytes or excluded_by(rel, patterns):
                    continue
                with open(full, "rb") as f:
                    sample = f.read(8192)
                if b"\x00" in sample:
                    continue
                sample.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out.append(rel)
    return sorted(out)


def all_files(repo: Path) -> list[str]:
    """Every committed file (no exclusions): deployment config such as .github/ is excluded from the index."""
    try:
        import pygit2

        r = pygit2.Repository(str(repo))
        out: list[str] = []

        def walk(tree, prefix: str) -> None:
            for entry in tree:
                path = f"{prefix}{entry.name}"
                if entry.type_str == "tree":
                    walk(r.get(entry.id), f"{path}/")
                elif entry.type_str == "blob":
                    out.append(path)

        walk(r[r.head.target].tree, "")
        return sorted(out)
    except Exception:
        return _walk(Path(repo), [".git/"], MAX_READ_BYTES)


def _read(repo: Path, path: str) -> str | None:
    """A file's text, or None when it cannot be read as UTF-8."""
    try:
        full = Path(repo) / path
        if full.stat().st_size > MAX_READ_BYTES:
            return None
        return full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _parse(text: str) -> ast.AST | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


# ----------------------------------------------------------------------
# Counts
# ----------------------------------------------------------------------

LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".java": "Java",
    ".kt": "Kotlin", ".rb": "Ruby", ".rs": "Rust", ".cs": "C#", ".php": "PHP", ".scala": "Scala",
    ".swift": "Swift", ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".sh": "Shell", ".bash": "Shell",
    ".sql": "SQL", ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".yml": "YAML", ".yaml": "YAML",
    ".json": "JSON", ".toml": "TOML", ".md": "Markdown", ".ipynb": "Notebook", ".bat": "Batch", ".ps1": "PowerShell",
}
TEST_FOLDERS = {"test", "tests", "__tests__", "spec", "specs"}
MAIN_GUARD = re.compile(r"^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:", re.MULTILINE)


def is_test_file(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if name.endswith(".py"):
        if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
            return True
        return any(p in TEST_FOLDERS for p in parts[:-1]) and name != "__init__.py"
    if re.search(r"\.(test|spec)\.[jt]sx?$", name) or name.endswith("_test.go"):
        return True
    return False


def _console_scripts(repo: Path, files: list[str]) -> list[dict]:
    out = []
    if "pyproject.toml" in files:
        try:
            import tomllib

            data = tomllib.loads(_read(repo, "pyproject.toml") or "")
            scripts = {**data.get("project", {}).get("scripts", {}),
                       **data.get("tool", {}).get("poetry", {}).get("scripts", {})}
            out += [{"path": "pyproject.toml", "kind": f"console script {name}"} for name in scripts]
        except Exception:
            pass
    if "setup.cfg" in files:
        try:
            cfg = configparser.ConfigParser()
            cfg.read_string(_read(repo, "setup.cfg") or "")
            raw = cfg.get("options.entry_points", "console_scripts", fallback="")
            out += [{"path": "setup.cfg", "kind": f"console script {line.split('=')[0].strip()}"}
                    for line in raw.splitlines() if "=" in line]
        except Exception:
            pass
    return out


def count(repo: Path, files: list[str]) -> dict:
    """Files, Python files, lines, tests, languages, top folders and entry points."""
    lines = 0
    languages: dict[str, int] = {}
    folders: dict[str, int] = {}
    entry_points: list[dict] = []
    for path in files:
        ext = os.path.splitext(path)[1].lower()
        lang = LANGUAGES.get(ext)
        if lang:
            languages[lang] = languages.get(lang, 0) + 1
        if "/" in path:
            top = path.split("/", 1)[0]
            folders[top] = folders.get(top, 0) + 1
        text = _read(repo, path)
        if text is None:
            continue
        lines += text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        if ext == ".py" and MAIN_GUARD.search(text):
            entry_points.append({"path": path, "kind": "__main__"})
        elif path.rsplit("/", 1)[-1] == "Procfile":
            entry_points += [{"path": path, "kind": f"Procfile {line.split(':')[0].strip()}"}
                             for line in text.splitlines() if ":" in line and not line.startswith("#")]
    entry_points += _console_scripts(repo, files)
    return {
        "files": len(files),
        "python_files": sum(1 for f in files if f.endswith(".py")),
        "lines": lines,
        "tests": sum(1 for f in files if is_test_file(f)),
        "languages": [{"name": k, "files": v} for k, v in sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))],
        "top_folders": [{"path": k, "files": v} for k, v in sorted(folders.items(), key=lambda kv: (-kv[1], kv[0]))][:12],
        "entry_points": entry_points,
    }


# ----------------------------------------------------------------------
# Tools and integrations
# ----------------------------------------------------------------------

def _tool(id: str, name: str, category: str) -> dict:
    return {"id": id, "name": name, "category": category}


# Imported module (the longest dotted prefix wins) -> the tool it stands for.
INTEGRATIONS: dict[str, dict] = {
    "selenium": _tool("selenium", "Selenium", "browser"),
    "playwright": _tool("playwright", "Playwright", "browser"),
    "pyppeteer": _tool("pyppeteer", "Pyppeteer", "browser"),
    "requests": _tool("requests", "requests", "http"),
    "httpx": _tool("httpx", "httpx", "http"),
    "aiohttp": _tool("aiohttp", "aiohttp", "http"),
    "urllib3": _tool("urllib3", "urllib3", "http"),
    "boto3": _tool("aws", "AWS SDK", "cloud"),
    "botocore": _tool("aws", "AWS SDK", "cloud"),
    "google.cloud": _tool("google_cloud", "Google Cloud SDK", "cloud"),
    "azure": _tool("azure", "Azure SDK", "cloud"),
    "google.generativeai": _tool("gemini", "Gemini SDK", "ai"),
    "google.genai": _tool("gemini", "Gemini SDK", "ai"),
    "vertexai": _tool("gemini", "Gemini SDK", "ai"),
    "openai": _tool("openai", "OpenAI SDK", "ai"),
    "anthropic": _tool("anthropic", "Anthropic SDK", "ai"),
    "langchain": _tool("langchain", "LangChain", "ai"),
    "langchain_core": _tool("langchain", "LangChain", "ai"),
    "litellm": _tool("litellm", "LiteLLM", "ai"),
    "googleapiclient": _tool("google_workspace", "Google Workspace", "workspace"),
    "gspread": _tool("google_workspace", "Google Workspace", "workspace"),
    "sqlalchemy": _tool("sqlalchemy", "SQLAlchemy", "database"),
    "psycopg2": _tool("postgres", "PostgreSQL", "database"),
    "psycopg": _tool("postgres", "PostgreSQL", "database"),
    "asyncpg": _tool("postgres", "PostgreSQL", "database"),
    "pymysql": _tool("mysql", "MySQL", "database"),
    "sqlite3": _tool("sqlite", "SQLite", "database"),
    "pymongo": _tool("mongodb", "MongoDB", "database"),
    "motor": _tool("mongodb", "MongoDB", "database"),
    "redis": _tool("redis", "Redis", "database"),
    "celery": _tool("celery", "Celery", "queue"),
    "kombu": _tool("kombu", "Kombu", "queue"),
    "pika": _tool("rabbitmq", "RabbitMQ", "queue"),
    "kafka": _tool("kafka", "Kafka", "queue"),
    "confluent_kafka": _tool("kafka", "Kafka", "queue"),
    "airflow": _tool("airflow", "Airflow", "orchestrator"),
    "prefect": _tool("prefect", "Prefect", "orchestrator"),
    "dagster": _tool("dagster", "Dagster", "orchestrator"),
    "temporalio": _tool("temporal", "Temporal", "orchestrator"),
    "schedule": _tool("schedule", "schedule", "scheduler"),
    "apscheduler": _tool("apscheduler", "APScheduler", "scheduler"),
    "crontab": _tool("python_crontab", "python-crontab", "scheduler"),
    "paramiko": _tool("ssh", "SSH", "ssh"),
    "fabric": _tool("ssh", "SSH", "ssh"),
    "asyncssh": _tool("ssh", "SSH", "ssh"),
    "smtplib": _tool("email", "Email (SMTP)", "email"),
    "twilio": _tool("twilio", "Twilio", "messaging"),
    "slack_sdk": _tool("slack", "Slack", "messaging"),
    "slack": _tool("slack", "Slack", "messaging"),
    "stripe": _tool("stripe", "Stripe", "payments"),
    "pandas": _tool("pandas", "pandas", "data"),
    "subprocess": _tool("subprocess", "subprocess", "process"),
    "docker": _tool("docker", "Docker SDK", "container"),
    "kubernetes": _tool("kubernetes", "Kubernetes client", "container"),
    "fastapi": _tool("fastapi", "FastAPI", "web"),
    "flask": _tool("flask", "Flask", "web"),
    "django": _tool("django", "Django", "web"),
}

# Command-line tools started with subprocess.
CLIS: dict[str, dict] = {
    "gws": _tool("gws", "Google Workspace CLI", "workspace"),
    "gam": _tool("gam", "GAM (Google Workspace)", "workspace"),
    "gcloud": _tool("gcloud", "gcloud CLI", "cloud"),
    "aws": _tool("aws_cli", "AWS CLI", "cloud"),
    "kubectl": _tool("kubectl", "kubectl", "container"),
    "ssh": _tool("ssh", "SSH", "ssh"),
    "scp": _tool("ssh", "SSH", "ssh"),
    "rsync": _tool("rsync", "rsync", "ssh"),
    "ffmpeg": _tool("ffmpeg", "ffmpeg", "process"),
}
NTFY = _tool("ntfy", "ntfy", "notify")

# Dependencies of other ecosystems (package.json, go.mod), kept light.
OTHER_DEPS: dict[str, dict] = {
    "puppeteer": _tool("puppeteer", "Puppeteer", "browser"),
    "playwright": _tool("playwright", "Playwright", "browser"),
    "axios": _tool("axios", "axios", "http"),
    "aws-sdk": _tool("aws", "AWS SDK", "cloud"),
    "@aws-sdk/": _tool("aws", "AWS SDK", "cloud"),
    "openai": _tool("openai", "OpenAI SDK", "ai"),
    "@anthropic-ai/sdk": _tool("anthropic", "Anthropic SDK", "ai"),
    "stripe": _tool("stripe", "Stripe", "payments"),
    "@slack/": _tool("slack", "Slack", "messaging"),
    "pg": _tool("postgres", "PostgreSQL", "database"),
    "mongodb": _tool("mongodb", "MongoDB", "database"),
    "redis": _tool("redis", "Redis", "database"),
    "bullmq": _tool("bullmq", "BullMQ", "queue"),
    "kafkajs": _tool("kafka", "Kafka", "queue"),
    "node-cron": _tool("node_cron", "node-cron", "scheduler"),
    "@temporalio/": _tool("temporal", "Temporal", "orchestrator"),
    "go.temporal.io/sdk": _tool("temporal", "Temporal", "orchestrator"),
    "github.com/aws/aws-sdk-go": _tool("aws", "AWS SDK", "cloud"),
    "github.com/robfig/cron": _tool("robfig_cron", "robfig/cron", "scheduler"),
}

SUBPROCESS_CALLS = {"run", "call", "Popen", "check_output", "check_call", "getoutput", "getstatusoutput"}


def _lookup(module: str) -> dict | None:
    parts = module.split(".")
    for n in range(len(parts), 0, -1):
        hit = INTEGRATIONS.get(".".join(parts[:n]))
        if hit:
            return hit
    return None


def _imported_modules(tree: ast.AST) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            mods.add(node.module)
            mods.update(f"{node.module}.{alias.name}" for alias in node.names)  # from google import genai
    return mods


def _first_word(node: ast.AST) -> str | None:
    """The program a subprocess call starts: ["gws", ...] or "gws ..."."""
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        node = node.elts[0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
        return os.path.basename(node.value.strip().split()[0])
    return None


def _clis(tree: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute)
                and node.func.attr in SUBPROCESS_CALLS | {"system"}
                and isinstance(node.func.value, ast.Name) and node.func.value.id in ("subprocess", "os")):
            word = _first_word(node.args[0])
            if word in CLIS:
                found.add(word)
    return found


def _uses_ntfy(tree: ast.AST) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str) and "ntfy" in n.value.lower()
               for n in ast.walk(tree))


def read_imports(repo: Path, files: list[str]) -> dict:
    """Tools and integrations: Python imports (and a few other manifests) through the lookup tables."""
    found: dict[str, dict] = {}

    def add(tool: dict, path: str) -> None:
        entry = found.setdefault(tool["id"], {**tool, "files": []})
        if path not in entry["files"]:
            entry["files"].append(path)

    for path in files:
        name = path.rsplit("/", 1)[-1]
        if path.endswith(".py"):
            text = _read(repo, path)
            tree = _parse(text) if text else None
            if tree is None:
                continue
            for module in _imported_modules(tree):
                tool = _lookup(module)
                if tool:
                    add(tool, path)
            for cli in _clis(tree):
                add(CLIS[cli], path)
            if _uses_ntfy(tree):
                add(NTFY, path)
        elif name == "package.json":
            try:
                data = json.loads(_read(repo, path) or "{}")
            except ValueError:
                continue
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            for dep in deps:
                for key, tool in OTHER_DEPS.items():
                    if dep == key or (key.endswith("/") and dep.startswith(key)):
                        add(tool, path)
        elif name == "go.mod":
            text = _read(repo, path) or ""
            for key, tool in OTHER_DEPS.items():
                if "." in key.split("/")[0] and key in text:
                    add(tool, path)
    order = ["browser", "ai", "workspace", "payments", "cloud", "ssh", "messaging", "notify", "email", "http",
             "queue", "database", "orchestrator", "scheduler", "container", "web", "process", "data"]
    integrations = sorted(found.values(), key=lambda t: (order.index(t["category"]) if t["category"] in order
                                                         else len(order), -len(t["files"]), t["name"]))
    return {"integrations": integrations}


# ----------------------------------------------------------------------
# How it runs today
# ----------------------------------------------------------------------

def _sleep_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return ((isinstance(f, ast.Attribute) and f.attr == "sleep")
            or (isinstance(f, ast.Name) and f.id == "sleep"))


def _polls(tree: ast.AST) -> bool:
    """A `while True` (or `while not stop...`) loop that sleeps: hand-rolled polling."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.While):
            continue
        test = node.test
        forever = (isinstance(test, ast.Constant) and bool(test.value)) or isinstance(test, (ast.UnaryOp, ast.Name,
                                                                                               ast.Attribute, ast.Call))
        if forever and any(_sleep_call(n) for stmt in node.body for n in ast.walk(stmt)):
            return True
    return False


def _json_state(tree: ast.AST) -> bool:
    """The file both json.dump()s and json.load()s: state kept in a JSON file between runs."""
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id in ("json", "yaml")}
    return bool(calls & {"dump", "safe_dump"}) and bool(calls & {"load", "safe_load"})


# id -> (name in "How it runs today", phrase in "Not found"); only the ones with a phrase are listed as missing.
RUNS = {
    "systemd": ("systemd units", "systemd units"),
    "crontab": ("crontab file", "a crontab file"),
    "k8s_cronjob": ("Kubernetes CronJob", None),
    "gha_schedule": ("Scheduled GitHub Actions", None),
    "scheduler": ("Scheduler library", "a scheduler library"),
    "celery_beat": ("Celery beat", None),
    "airflow": ("Airflow DAGs", None),
    "polling": ("Sleep-based polling", None),
    "state_files": ("JSON state files", None),
    "docker": ("Docker / Compose", None),
    "procfile": ("Procfile", None),
}


def how_it_runs(repo: Path, files: list[str], config_files: list[str] | None = None) -> dict:
    """
    How the code runs today, from deployment files (`config_files`: every committed file, since
    the index leaves out folders such as .github/) and the Python source (`files`).
    """
    evidence: dict[str, list[str]] = {}
    schedulers: set[str] = set()

    def add(kind: str, path: str) -> None:
        paths = evidence.setdefault(kind, [])
        if path not in paths and len(paths) < 10:
            paths.append(path)

    for path in config_files if config_files is not None else files:
        name = path.rsplit("/", 1)[-1]
        lower = name.lower()
        if lower.endswith((".service", ".timer")):
            add("systemd", path)
        elif lower in ("crontab", "cronfile") or lower.endswith((".cron", ".crontab")) or "/cron.d/" in f"/{path}":
            add("crontab", path)
        elif lower in ("dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml") \
                or lower.startswith("dockerfile."):
            add("docker", path)
        elif name == "Procfile":
            add("procfile", path)
        elif lower.endswith((".yml", ".yaml")):
            text = _read(repo, path) or ""
            if re.search(r"^\s*kind:\s*CronJob\b", text, re.MULTILINE):
                add("k8s_cronjob", path)
            if path.startswith(".github/workflows/") and re.search(r"^\s*-?\s*cron:", text, re.MULTILINE):
                add("gha_schedule", path)
        if re.search(r"state[^/]*\.json$", lower) or lower.endswith(".state"):
            add("state_files", path)

    for path in files:
        if not path.endswith(".py"):
            continue
        text = _read(repo, path)
        tree = _parse(text) if text else None
        if tree is None:
            continue
        mods = {m.split(".")[0] for m in _imported_modules(tree)}
        for lib, label in (("schedule", "schedule"), ("apscheduler", "APScheduler"), ("crontab", "python-crontab")):
            if lib in mods:
                add("scheduler", path)
                schedulers.add(label)
        if "celery" in mods and "beat_schedule" in text:
            add("celery_beat", path)
        if "airflow" in mods and "DAG" in text:
            add("airflow", path)
        if _polls(tree):
            add("polling", path)
        if _json_state(tree):
            add("state_files", path)

    runs = []
    for kind, (name, _) in RUNS.items():
        if kind in evidence:
            if kind == "scheduler" and schedulers:
                name = f"Scheduler library ({', '.join(sorted(schedulers))})"
            runs.append({"id": kind, "name": name, "evidence": evidence[kind]})
    not_found = [phrase for kind, (_, phrase) in RUNS.items() if phrase and kind not in evidence]
    return {"runs_today": runs, "not_found": not_found}


# ----------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS Access Key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Private Key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY")),
    ("Anthropic API Key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API Key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("GitHub Token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("Slack Token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Slack Webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Stripe Key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
]
PASSWORD = re.compile(
    r"""(?i)\b(?:password|passwd|pwd|secret|api_?key|token|access_?key)\b\s*[:=]\s*['"]([^'"\s]{8,})['"]""")
PLACEHOLDER = re.compile(r"(?i)(example|changeme|change_me|your|xxx|dummy|placeholder|<|\$\{|\{\{|os\.environ|getenv|://)")
# detect-secrets' entropy plugins flag hashes, ids and test vectors; the specific detectors are kept.
NOISY_TYPES = {"Hex High Entropy String", "Base64 High Entropy String"}


def _entropy(value: str) -> float:
    import math

    counts = {c: value.count(c) for c in set(value)}
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def _noisy_path(path: str) -> bool:
    parts = path.lower().split("/")
    return any(p in ("fixtures", "testdata", "test_data", "__snapshots__") for p in parts[:-1]) \
        or parts[-1].endswith((".lock", ".min.js", ".map", ".svg"))


def _keyword_secret(line: str, path: str) -> bool:
    """A quoted, high-entropy value assigned to a secret-sounding name, outside tests (not a URL or placeholder)."""
    m = PASSWORD.search(line)
    return bool(m) and not PLACEHOLDER.search(m.group(1)) and _entropy(m.group(1)) >= 3.0 and not is_test_file(path)


def _regex_secrets(repo: Path, files: list[str]) -> list[dict]:
    findings = []
    for path in files:
        if _noisy_path(path):
            continue
        text = _read(repo, path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for kind, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append({"path": path, "line": lineno, "type": kind})
                    break
            else:
                if _keyword_secret(line, path):
                    findings.append({"path": path, "line": lineno, "type": "Secret Keyword"})
    return findings


def _detect_secrets(repo: Path, files: list[str]) -> list[dict]:
    from detect_secrets import SecretsCollection
    from detect_secrets.settings import default_settings

    collection = SecretsCollection(root=str(repo))
    with default_settings():
        for path in files:
            if not _noisy_path(path):
                collection.scan_file(path)
    findings = []
    lines: dict[str, list[str]] = {}
    for filename, secret in collection:
        path = os.path.relpath(filename, repo) if os.path.isabs(filename) else filename
        if secret.type in NOISY_TYPES or (secret.type == "Basic Auth Credentials" and is_test_file(path)):
            continue
        if secret.type == "Secret Keyword":
            # The keyword plugin flags every password-ish name (labels, URLs, translations): keep the strong ones.
            text = lines.setdefault(path, (_read(repo, path) or "").splitlines())
            line = text[secret.line_number - 1] if 0 < secret.line_number <= len(text) else ""
            if not _keyword_secret(line, path):
                continue
        findings.append({"path": path, "line": secret.line_number, "type": secret.type})
    return findings


def find_secrets(repo: Path, files: list[str], engine: str = "auto") -> dict:
    """
    Possible secrets: path, line and type only (the value is never kept). `engine` is auto
    (detect-secrets when installed, else regexes), detect-secrets or regex.
    """
    findings = None
    if engine in ("auto", "detect-secrets"):
        try:
            findings = _detect_secrets(Path(repo), files)
        except ImportError:
            if engine == "detect-secrets":
                raise
    if findings is None:
        findings = _regex_secrets(Path(repo), files)
    findings.sort(key=lambda f: (f["path"], f["line"]))
    return {"secrets": len(findings), "secret_findings": findings}


# ----------------------------------------------------------------------
# Exclusions and the whole scan
# ----------------------------------------------------------------------

def _size(n: int) -> str:
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000} MB"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    return f"{n // 1000} KB" if n >= 1000 else f"{n} bytes"


def exclusions_view(patterns: list[str], folders: list[str], max_file_bytes: int) -> dict:
    """What is left out of the assessment, with a one-line summary for screen 2."""
    parts = [".git", "__pycache__"]
    others = list(patterns)
    if set(DEFAULT_EXTRA_EXCLUDES) <= set(patterns):
        parts.append("data files")
        others = [p for p in patterns if p not in DEFAULT_EXTRA_EXCLUDES]
    parts += others[:6] + ([f"{len(others) - 6} more patterns"] if len(others) > 6 else [])
    parts.append(f"files over {_size(max_file_bytes)}")
    parts += [f"{f.strip('/')}/" for f in folders if f.strip("/")]
    return {"patterns": list(patterns), "folders": list(folders), "max_file_bytes": max_file_bytes,
            "summary": ", ".join(parts)}


def scan(repo: Path, exclude: dict | None = None, on_step: StepCallback | None = None) -> dict:
    """
    The whole local scan, in checklist order. `on_step(step, state, detail)` is called with
    state "running" before and "done" (or "failed", then the error is raised) after each step:
    count, imports, how_it_runs, secrets.
    """
    repo = Path(repo)
    exclude = exclude or {}
    patterns = list(exclude.get("patterns", DEFAULT_EXTRA_EXCLUDES))
    folders = list(exclude.get("folders", []))
    max_bytes = int(exclude.get("max_file_bytes") or _default_max_bytes())
    report = on_step or (lambda *_: None)

    def step(step_id: str, running: str, fn: Callable[[], dict], done: Callable[[dict], str]) -> dict:
        report(step_id, "running", running)
        try:
            out = fn()
        except Exception as e:
            report(step_id, "failed", str(e))
            raise
        report(step_id, "done", done(out))
        return out

    files: list[str] = []

    def counting() -> dict:
        files.extend(list_files(repo, patterns, folders, max_bytes))
        return count(repo, files)

    result: dict = {}
    result.update(step("count", "Counting files", counting,
                       lambda o: _plural(o["python_files"], "Python file") if o["python_files"]
                       else _plural(o["files"], "file")))
    result.update(step("imports", "", lambda: read_imports(repo, files),
                       lambda o: _plural(len(o["integrations"]), "integration")))
    result.update(step("how_it_runs", "cron, systemd, schedulers",
                       lambda: how_it_runs(repo, files, all_files(repo)),
                       lambda o: ", ".join(r["name"] for r in o["runs_today"][:3]) or "nothing found"))
    result.update(step("secrets", "", lambda: find_secrets(repo, files),
                       lambda o: f"{o['secrets']} found"))
    result["exclusions"] = exclusions_view(patterns, folders, max_bytes)
    order = ["files", "python_files", "lines", "tests", "secrets", "secret_findings", "languages", "integrations",
             "runs_today", "not_found", "top_folders", "entry_points", "exclusions"]
    return {k: result[k] for k in order}


def _default_max_bytes() -> int:
    try:
        from semantic_ingress.config import FileFilterConfig

        return FileFilterConfig().max_file_size_bytes
    except Exception:
        return DEFAULT_MAX_FILE_BYTES
