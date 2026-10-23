"""The local scan: files, integrations, how it runs today, secrets and exclusions."""
import shutil
import subprocess
from pathlib import Path

import pytest

from temporalize import scan

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

FILES = {
    "audit/morning.py": (
        "import time\n"
        "import json\n"
        "from selenium import webdriver\n"
        "import google.generativeai as genai\n"
        "import requests\n"
        "import subprocess\n\n"
        "def poll():\n"
        "    while True:\n"
        "        check()\n"
        "        time.sleep(60)\n\n"
        "def save(state):\n"
        "    json.dump(state, open('state.json', 'w'))\n\n"
        "def load():\n"
        "    return json.load(open('state.json'))\n\n"
        "def notify(msg):\n"
        "    requests.post('https://ntfy.sh/audit', data=msg)\n\n"
        "def sheets():\n"
        "    subprocess.run(['gws', 'sheets', 'read'])\n\n"
        "if __name__ == '__main__':\n"
        "    poll()\n"
    ),
    "audit/config.py": f"AWS_ACCESS_KEY_ID = '{AWS_KEY}'\n",
    "audit/broken.py": "def oops(:\n",
    "deploy/audit.service": "[Service]\nExecStart=/usr/bin/python audit/morning.py\n",
    "data/shifts.csv": "a,b\n1,2\n",
    "README.md": "# Audit\n",
}


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        f = path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    run = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    return path


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    return _git_repo(tmp_path / "repo", FILES)


@needs_git
def test_list_files_applies_data_and_folder_exclusions(repo):
    files = scan.list_files(repo, scan.DEFAULT_EXTRA_EXCLUDES, [], 1_000_000)
    assert "data/shifts.csv" not in files
    assert "audit/morning.py" in files
    assert "audit/morning.py" not in scan.list_files(repo, scan.DEFAULT_EXTRA_EXCLUDES, ["audit"], 1_000_000)
    assert "data/shifts.csv" in scan.list_files(repo, [], [], 1_000_000)


@needs_git
def test_imports_map_to_integrations(repo):
    out = scan.read_imports(repo, scan.list_files(repo, [], [], 1_000_000))
    names = [t["name"] for t in out["integrations"]]
    assert "Selenium" in names and "Gemini SDK" in names
    assert "Google Workspace CLI" in names      # subprocess.run(["gws", ...])
    assert "ntfy" in names
    # Most telling first: the browser before the AI SDK before plain HTTP.
    assert names.index("Selenium") < names.index("Gemini SDK") < names.index("requests")
    selenium = next(t for t in out["integrations"] if t["id"] == "selenium")
    assert selenium["files"] == ["audit/morning.py"] and selenium["category"] == "browser"


@needs_git
def test_how_it_runs_finds_systemd_polling_and_state(repo):
    files = scan.list_files(repo, [], [], 1_000_000)
    out = scan.how_it_runs(repo, files, scan.all_files(repo))
    ids = {r["id"]: r for r in out["runs_today"]}
    assert ids["systemd"]["evidence"] == ["deploy/audit.service"]
    assert ids["polling"]["evidence"] == ["audit/morning.py"]
    assert "state_files" in ids
    assert "a crontab file" in out["not_found"]
    assert "systemd units" not in out["not_found"]


@needs_git
def test_regex_secrets_find_the_key_without_its_value(repo):
    out = scan.find_secrets(repo, scan.list_files(repo, [], [], 1_000_000), engine="regex")
    assert out["secrets"] == 1
    assert out["secret_findings"] == [{"path": "audit/config.py", "line": 1, "type": "AWS Access Key"}]
    assert AWS_KEY not in repr(out)


def test_placeholder_passwords_are_not_secrets(tmp_path):
    (tmp_path / "settings.py").write_text(
        "password = 'changeme-please'\napi_key = os.environ['KEY']\ntoken = 'Zq8$kL2!vR9#wX4p'\n")
    out = scan.find_secrets(tmp_path, ["settings.py"], engine="regex")
    assert [f["line"] for f in out["secret_findings"]] == [3]


@needs_git
def test_scan_reports_each_step_and_returns_the_contract(repo):
    steps = []
    out = scan.scan(repo, on_step=lambda *a: steps.append(a))
    assert [s[0] for s in steps if s[1] == "done"] == ["count", "imports", "how_it_runs", "secrets"]
    assert ("count", "done", "3 Python files") in steps
    assert list(out) == ["files", "python_files", "lines", "tests", "secrets", "secret_findings", "languages",
                         "integrations", "runs_today", "not_found", "top_folders", "entry_points", "exclusions"]
    assert out["python_files"] == 3 and out["tests"] == 0 and out["secrets"] == 1
    assert {"path": "audit/morning.py", "kind": "__main__"} in out["entry_points"]
    assert out["top_folders"][0] == {"path": "audit", "files": 3}
    assert out["exclusions"]["summary"] == ".git, __pycache__, data files, files over 1 MB"


def test_not_a_git_repository_is_walked(tmp_path):
    (tmp_path / "a.py").write_text("import paramiko\n")
    (tmp_path / "rows.csv").write_text("1\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00\x01")
    files = scan.list_files(tmp_path, scan.DEFAULT_EXTRA_EXCLUDES, [], 1_000_000)
    assert files == ["a.py"]
    assert scan.read_imports(tmp_path, files)["integrations"][0]["name"] == "SSH"


def test_tests_are_counted(tmp_path):
    files = ["tests/test_a.py", "tests/__init__.py", "pkg/b_test.py", "pkg/c.py", "web/x.spec.ts", "conftest.py"]
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x = 1\n")
    assert scan.count(tmp_path, files)["tests"] == 4


def test_exclusions_summary():
    view = scan.exclusions_view(scan.DEFAULT_EXTRA_EXCLUDES + ["*.bak"], ["legacy", "tools/old/"], 2_500_000)
    assert view["summary"] == ".git, __pycache__, data files, *.bak, files over 2.5 MB, legacy/, tools/old/"
    assert scan.exclusions_view([], [], 1_000_000)["summary"] == ".git, __pycache__, files over 1 MB"
    assert scan.index_patterns({"patterns": ["*.csv"], "folders": ["legacy/", " tools/old "]}) == \
        ["*.csv", "legacy/", "tools/old/"]


def test_syntax_errors_and_binary_files_are_skipped(tmp_path):
    (tmp_path / "bad.py").write_text("def (:\n")
    (tmp_path / "bin.py").write_bytes(b"\xff\xfe\x00")
    assert scan.read_imports(tmp_path, ["bad.py", "bin.py", "missing.py"]) == {"integrations": []}
    assert scan.how_it_runs(tmp_path, ["bad.py", "bin.py"])["runs_today"] == []
