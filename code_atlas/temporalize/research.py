"""
What the design step (screen 6) and the coding agent (screen 7) consult about Temporal:
Temporal's temporal-developer skill (vendored in knowledge/temporal-developer-skill, core
and Python references) and the Temporal Docs MCP search.

`Research` serves both as model tools and records what was used (skill files read,
searches made, docs URLs returned), so the plan and the review packet can show that the
design rests on them, and say so when the docs search was not available.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("code_atlas.temporalize.research")

SKILL_NAME = "temporal-developer"
MAX_SEARCHES = 8
MAX_READ_CHARS = 40_000
CHUNKS_PER_SEARCH = 5
CHUNK_CHARS = 1800
_FEEDBACK = re.compile(r"\n## Feedback\b.*?(?=\n## |\Z)", re.S)   # a note for interactive users, not for a model


def skill_dir() -> Path:
    from semantic_ingress.config import _knowledge_file

    return _knowledge_file("temporal-developer-skill")


def skill_files() -> list[str]:
    root = skill_dir()
    if not root.is_dir():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.md") if p.name != "README.md")


def read_skill(path: str) -> str:
    """One file of the skill (SKILL.md, references/core/*.md, references/python/*.md)."""
    root = skill_dir().resolve()
    target = (root / path.strip().lstrip("/")).resolve()
    if root not in target.parents or target.suffix != ".md" or not target.is_file():
        files = ", ".join(skill_files())
        return f"No such skill file: {path!r}. Files: {files}"
    text = target.read_text(encoding="utf-8", errors="replace")
    if target.name == "SKILL.md":
        text = _FEEDBACK.sub("\n", text)
    return text[:MAX_READ_CHARS]


def skill_overview() -> str:
    """SKILL.md (without its feedback note), for a system prompt."""
    return read_skill("SKILL.md") if (skill_dir() / "SKILL.md").is_file() else ""


READ_SKILL_TOOL = {
    "name": "read_temporal_skill",
    "description": (
        "Read a file of Temporal's temporal-developer skill: SKILL.md, references/core/<topic>.md (determinism, "
        "patterns, gotchas, versioning, troubleshooting, error-reference, ai-patterns, ...) or "
        "references/python/<topic>.md (python, patterns, determinism, determinism-protection, gotchas, "
        "error-handling, testing, sync-vs-async, data-handling, versioning, ...). Read the Python getting started "
        "guide and the references for the patterns you use before you design or write Temporal code."),
    "input_schema": {"type": "object", "properties": {
        "path": {"type": "string", "description": "e.g. references/python/patterns.md"}}, "required": ["path"]},
}

SEARCH_DOCS_TOOL = {
    "name": "search_temporal_docs",
    "description": (
        "Search the official Temporal documentation (Temporal Docs MCP). Returns the most relevant passages with their "
        "source URLs. Use a complete natural-language question. Use it for requirements the skill does not settle "
        "(limits, timeouts, Schedules, retry semantics, Cloud specifics) and cite the URLs you rely on."),
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string", "description": "A complete question"}}, "required": ["query"]},
}


def format_chunks(chunks: list[dict]) -> str:
    """Passages for the model, docs.temporal.io first (as the indexer's repository pass formats them)."""
    official = [c for c in chunks if "docs.temporal.io" in c.get("source_url", "")]
    picked = (official + [c for c in chunks if c not in official])[:CHUNKS_PER_SEARCH]
    if not picked:
        return "No results."
    return "\n\n".join(f"SOURCE: {c.get('source_url', '')}\n{(c.get('content') or '')[:CHUNK_CHARS]}" for c in picked)


@dataclass
class Research:
    """The two tools, with a record of what was used. `search` is injectable for tests."""

    search: object = None                       # callable(query) -> list[{"source_url", "content"}]; None: the MCP
    max_searches: int = MAX_SEARCHES
    skill_read: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    docs_error: str = ""

    @property
    def tools(self) -> list[dict]:
        return [READ_SKILL_TOOL, SEARCH_DOCS_TOOL]

    def handles(self, name: str) -> bool:
        return name in ("read_temporal_skill", "search_temporal_docs")

    def handle(self, name: str, args: dict) -> str:
        if name == "read_temporal_skill":
            path = str(args.get("path") or "SKILL.md")
            text = read_skill(path)
            if not text.startswith("No such skill file"):
                self.skill_read.append(path)
            return text
        if name == "search_temporal_docs":
            return self._search(str(args.get("query") or "").strip())
        return f"Unknown tool {name}."

    def _search(self, query: str) -> str:
        if not query:
            return "Give a question to search for."
        if len(self.queries) >= self.max_searches:
            return "Search limit reached. Go on with what you have."
        self.queries.append(query)
        try:
            if self.search is not None:
                chunks = self.search(query)
            else:
                from utils.temporal_docs import search

                chunks = search(query)
        except Exception as e:
            self.docs_error = str(e)[:300] or type(e).__name__
            logger.warning(f"Temporal docs search failed: {self.docs_error}")
            return (f"The Temporal Docs search is unavailable ({self.docs_error}). Rely on the skill, and say in "
                    "your answer that the docs could not be checked.")
        for c in chunks:
            url = c.get("source_url")
            if url and url not in self.urls:
                self.urls.append(url)
        return format_chunks(chunks)

    def record(self) -> dict:
        """What the plan stores: shown on screen 6 and in the review packet."""
        return {"skill": SKILL_NAME, "skill_read": list(dict.fromkeys(self.skill_read)),
                "docs_queries": list(self.queries), "docs_urls": list(self.urls),
                "docs_available": not self.docs_error, "docs_error": self.docs_error}
