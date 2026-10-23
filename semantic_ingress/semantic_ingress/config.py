"""
Configuration management for the semantic ingress system.

Handles environment variables, CLI overrides, and system defaults.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _env_str(name: str, default: str = "") -> str:
    """Read a string setting; blank values fall back to the default."""
    value = os.environ.get(name, "").strip()
    return value if value else default


def _env_opt(name: str, default: Optional[str] = None) -> Optional[str]:
    """
    Read an optional setting. Unset uses the default; set-but-blank means "do not send".
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip() or None


def _env_list(name: str) -> list[str]:
    """Read a comma-separated list; blank items are dropped."""
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _env_int(name: str, default: int) -> int:
    value = _env_str(name)
    return int(value) if value else default


def _env_float_or_none(name: str) -> Optional[float]:
    value = _env_str(name)
    return float(value) if value else None


def _env_bool(name: str, default: bool) -> bool:
    value = _env_str(name).lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


def _knowledge_file(name: str) -> Path:
    """A knowledge file baked into the image, or the repository copy when running from a checkout."""
    image_copy = Path("/app/knowledge") / name
    if image_copy.exists():
        return image_copy
    return Path(__file__).resolve().parents[2] / "knowledge" / name


def _default_pack_path() -> Path:
    return _knowledge_file("temporal-pack.yaml")


@dataclass(frozen=True)
class LLMConfig:
    """
    Configuration for the indexing LLM (Anthropic API).

    Every value comes from the environment (.env). ``effort``, ``thinking`` and
    ``temperature`` are only sent when set, because support differs by model:
    5.x models reject non-default temperatures, while Haiku 4.5 rejects ``effort``.
    """

    model_id: str = field(default_factory=lambda: _env_str("INDEX_MODEL", "claude-sonnet-5-5"))
    max_tokens: int = field(default_factory=lambda: _env_int("INDEX_MAX_TOKENS", 16000))
    effort: Optional[str] = field(default_factory=lambda: _env_opt("INDEX_EFFORT", "low"))
    thinking: Optional[str] = field(default_factory=lambda: _env_opt("INDEX_THINKING"))
    temperature: Optional[float] = field(default_factory=lambda: _env_float_or_none("INDEX_TEMPERATURE"))
    timeout_seconds: int = field(default_factory=lambda: _env_int("INDEX_TIMEOUT_SECONDS", 300))
    max_retries: int = field(default_factory=lambda: _env_int("INDEX_API_MAX_RETRIES", 5))
    # Used for the cost report only. USD per million tokens.
    input_price_per_1m: float = field(
        default_factory=lambda: float(_env_str("INDEX_PRICE_INPUT_PER_1M", "2.00"))
    )
    output_price_per_1m: float = field(
        default_factory=lambda: float(_env_str("INDEX_PRICE_OUTPUT_PER_1M", "10.00"))
    )
    # Prompt-cache reads as a fraction of the input price: 0.05 on the 5.5 models, 0.1 on most others.
    cache_read_multiplier: float = field(
        default_factory=lambda: float(_env_str("INDEX_PRICE_CACHE_READ_MULTIPLIER", "0.05"))
    )
    # Counter key for model calls (semantic_ingress.runtime); None: not counted.
    count_key: Optional[str] = None


# Files and folders that are never indexed. Overridable with INDEX_EXCLUDE (a
# comma-separated list that replaces this one) and extendable with
# INDEX_EXCLUDE_EXTRA. Keep in sync with the documented default in .env.example.
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    # folders (a trailing slash matches a folder at any depth)
    "__pycache__/", ".claude/", ".cursor/", ".git/", ".github/", ".venv/", ".vscode/",
    "build/", "dist/", "node_modules/", "vendor/", "venv/",
    # config noise
    ".dockerignore", ".gitattributes", ".gitignore", ".gitmodules",
    # certificates, keys, binaries, archives, images, generated files
    "*.crt", "*.gif", "*.gz", "*.idx", "*.jpg", "*.key", "*.lock", "*.log", "*.map",
    "*.min.js", "*.pack", "*.pem", "*.png", "*.pyc", "*.pdf", "*.rev", "*.sample",
    "*.sum", "*.svg", "*.tar.gz", "*.zip",
)


def _exclude_patterns() -> tuple[str, ...]:
    base = _env_list("INDEX_EXCLUDE") or list(DEFAULT_EXCLUDE_PATTERNS)
    return tuple(base + _env_list("INDEX_EXCLUDE_EXTRA"))


@dataclass(frozen=True)
class FileFilterConfig:
    """
    Configuration for file filtering during repository scan.

    Values come from the environment (.env): INDEX_EXCLUDE, INDEX_EXCLUDE_EXTRA and
    INDEX_MAX_FILE_BYTES. Files are also skipped when they are not regular files or
    are binary (not valid UTF-8). ``respect_gitignore`` is not implemented.
    """

    respect_gitignore: bool = True
    max_file_size_bytes: int = field(
        default_factory=lambda: _env_int("INDEX_MAX_FILE_BYTES", 1_000_000)
    )
    exclude_patterns: tuple[str, ...] = field(default_factory=_exclude_patterns)


@dataclass(frozen=True)
class LensConfig:
    """
    Temporal lens settings (.env). The lens adds a Temporal-oriented record to every
    file, folder and repository summary, grounded in the knowledge pack.
    """

    enabled: bool = field(default_factory=lambda: _env_bool("LENS_ENABLED", True))
    pack_path: Path = field(
        default_factory=lambda: Path(_env_str("LENS_PACK_PATH")) if _env_str("LENS_PACK_PATH") else _default_pack_path()
    )
    mcp_url: str = field(default_factory=lambda: _env_str("TEMPORAL_DOCS_MCP_URL", "https://temporal.mcp.kapa.ai"))
    mcp_credentials: Path = field(
        default_factory=lambda: Path(
            _env_str("TEMPORAL_DOCS_MCP_CREDENTIALS", "/run/code-atlas/temporal-docs-mcp.json")
        )
    )


@dataclass(frozen=True)
class ReportConfig:
    """Temporal report settings (.env). Investigation runs at its own effort level."""

    rubric_path: Path = field(
        default_factory=lambda: Path(_env_str("REPORT_RUBRIC_PATH"))
        if _env_str("REPORT_RUBRIC_PATH") else _knowledge_file("temporal-rubric.yaml")
    )
    investigate_effort: Optional[str] = field(default_factory=lambda: _env_opt("REPORT_EFFORT", "medium"))


@dataclass(frozen=True)
class SourceStoreConfig:
    """Configuration for the local source-file mirror."""

    root: Path = field(default_factory=lambda: Path(_env_str("SOURCE_DIR", "/data/source")))
    max_workers: int = 8


@dataclass(frozen=True)
class MongoConfig:
    """Configuration for the MongoDB semantic index."""

    url: str = field(default_factory=lambda: _env_str("MONGO_URL", "mongodb://localhost:27017"))
    server_selection_timeout_ms: int = 10000


def _window_size() -> int:
    size = _env_int("INDEX_WINDOW_SIZE", 5)
    if size < 1:
        raise ValueError(f"INDEX_WINDOW_SIZE must be at least 1, got {size}")
    return size


@dataclass(frozen=True)
class TemporalConfig:
    """
    Temporal settings (.env). `sync` runs as a Temporal workflow on this server, with
    up to `window_size` file or folder summaries generated at a time.
    """

    address: str = field(default_factory=lambda: _env_str("TEMPORAL_ADDRESS", "localhost:7233"))
    namespace: str = field(default_factory=lambda: _env_str("TEMPORAL_NAMESPACE", "default"))
    # Browser-facing URL of the Temporal UI (printed as a link; not used to connect).
    ui_url: str = field(default_factory=lambda: _env_str("TEMPORAL_UI_URL", "http://localhost:8233"))
    window_size: int = field(default_factory=_window_size)

    def workflow_url(self, workflow_id: str) -> str:
        return f"{self.ui_url.rstrip('/')}/namespaces/{self.namespace}/workflows/{workflow_id}"


@dataclass
class IngestionConfig:
    """Root configuration for the ingestion system."""

    repo_path: Path
    output_dir: Path
    prefix: str
    repo_id: Optional[str] = None
    llm: LLMConfig = field(default_factory=LLMConfig)
    file_filter: FileFilterConfig = field(default_factory=FileFilterConfig)
    lens: LensConfig = field(default_factory=LensConfig)
    source: SourceStoreConfig = field(default_factory=SourceStoreConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    mongo: MongoConfig = field(default_factory=MongoConfig)
    temporal: TemporalConfig = field(default_factory=TemporalConfig)

    def __post_init__(self) -> None:
        """Validate and normalize configuration."""
        if not self.repo_path.exists():
            raise ValueError(f"Repository path does not exist: {self.repo_path}")
        if not self.repo_path.is_dir():
            raise ValueError(f"Repository path is not a directory: {self.repo_path}")

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Derive repo_id from path if not provided
        if self.repo_id is None:
            object.__setattr__(self, "repo_id", self.repo_path.name)

        # Validate prefix format
        if not self.prefix.startswith("sem"):
            raise ValueError(
                f"Invalid prefix '{self.prefix}'. "
                "Prefix must start with 'sem'."
            )
