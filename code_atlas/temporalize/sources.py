"""
Where a run's repository comes from: a git URL cloned into the tz-data volume, or a
local folder under REPOS_ROOT (read where it is, as Code Atlas indexing does).

Clones go to <TZ_DATA_DIR>/clones/<owner>__<repo>__<branch>, the same path in the app,
the index-worker and the tz-worker. A later run of the same URL and branch fetches and
resets the existing clone, so its index is synced incrementally. A token is passed to
git as an HTTP header for that one command; it is never written to the clone's config.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

TZ_DATA_DIR = Path(os.environ.get("TZ_DATA_DIR", "/data/tz"))
CLONE_TIMEOUT_SECONDS = 600


class SourceError(Exception):
    """The repository cannot be used; the message says why and how to fix it."""


def clones_root() -> Path:
    return TZ_DATA_DIR / "clones"


def normalize_url(url: str) -> str:
    """github.com/acme/repo, git@github.com:acme/repo.git and https URLs → an https URL."""
    url = url.strip()
    if not url:
        raise SourceError("Enter the repository URL.")
    m = re.match(r"^git@([^:]+):(.+)$", url)
    if m:
        url = f"https://{m.group(1)}/{m.group(2)}"
    if not re.match(r"^[a-z]+://", url):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.path.strip("/") == "":
        raise SourceError(f"{url} is not a repository URL. Use the form github.com/owner/repo.")
    if parsed.username or parsed.password:
        raise SourceError("Leave credentials out of the URL; add a read-only token instead.")
    return url.rstrip("/")


def repo_name(url: str) -> str:
    return re.sub(r"\.git$", "", urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or "repo"


def clone_dir(url: str, branch: str) -> Path:
    parts = [p for p in urlparse(url).path.strip("/").removesuffix(".git").split("/") if p]
    slug = "__".join(parts[-2:] + [branch or "default"])
    return clones_root() / re.sub(r"[^A-Za-z0-9_.-]", "_", slug)


def _auth_args(token: str | None) -> list[str]:
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]


def _git(args: list[str], cwd: Path | None = None, token: str | None = None,
         timeout: int = CLONE_TIMEOUT_SECONDS) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    try:
        done = subprocess.run(["git", *_auth_args(token), *args], cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise SourceError("git is not installed in this image. Rebuild it: docker compose build.") from e
    except subprocess.TimeoutExpired as e:
        raise SourceError(f"git {args[0]} took longer than {timeout // 60} minutes.") from e
    if done.returncode != 0:
        raise SourceError(_explain((done.stderr or done.stdout).strip(), bool(token)))
    return done.stdout.strip()


def _explain(stderr: str, has_token: bool) -> str:
    low = stderr.lower()
    if "could not read username" in low or "authentication failed" in low or "403" in low or \
            ("repository" in low and "not found" in low):
        return ("Couldn't clone. Check the token has read access." if has_token else
                "Couldn't clone. If the repository is private, add a read-only token.")
    if "remote branch" in low and "not found" in low:
        return "Couldn't clone: that branch does not exist."
    if "could not resolve host" in low:
        return "Couldn't clone: the host could not be reached. Check the URL and your network."
    return f"Couldn't clone: {stderr.splitlines()[-1] if stderr else 'git failed'}"


def clone(url: str, branch: str, token: str | None) -> Path:
    """Clone (or fetch and reset an earlier clone of) the branch; returns the folder."""
    url = normalize_url(url)
    branch = branch.strip() or "main"
    folder = clone_dir(url, branch)
    folder.parent.mkdir(parents=True, exist_ok=True)
    if (folder / ".git").is_dir():
        try:
            _git(["fetch", "--depth", "1", "origin", branch], cwd=folder, token=token)
            _git(["reset", "--hard", "FETCH_HEAD"], cwd=folder)
            _git(["clean", "-fdx"], cwd=folder)
            return folder
        except SourceError:
            shutil.rmtree(folder, ignore_errors=True)  # start over below
    elif folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    try:
        _git(["clone", "--depth", "1", "--branch", branch, "--single-branch", url, str(folder)], token=token)
    except SourceError as e:
        if "branch does not exist" in str(e) and (default := default_branch(url, token)):
            raise SourceError(f"Couldn't clone: there is no branch {branch}. The default branch is {default}.") from e
        raise
    return folder


def default_branch(url: str, token: str | None) -> str | None:
    """The remote's default branch (what HEAD points at), or None."""
    try:
        out = _git(["ls-remote", "--symref", url, "HEAD"], token=token, timeout=30)
    except SourceError:
        return None
    m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", out)
    return m.group(1) if m else None


def head(folder: Path) -> dict:
    """The commit and branch the index will read."""
    try:
        sha = _git(["rev-parse", "HEAD"], cwd=folder, timeout=30)
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=folder, timeout=30)
    except SourceError:
        return {"commit": None, "branch": None}
    return {"commit": sha, "branch": None if branch == "HEAD" else branch}


def local(path: str) -> Path:
    """A git repository under REPOS_ROOT, as the indexing picker accepts it."""
    from indexing import PathError, _repository

    try:
        return _repository(path)
    except PathError as e:
        raise SourceError(str(e)) from e
