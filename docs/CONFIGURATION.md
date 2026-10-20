# Configuration

All settings live in `.env` (copy [`.env.example`](../.env.example)). Compose passes the file to the app, the indexing worker and the indexer containers. Optional settings are only sent to the API when they are not blank.

| Setting | Default | Used by | Purpose |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | (required) | both | Anthropic API key |
| `QUERY_MODEL` | `claude-sonnet-5-5` | app | Model that answers questions |
| `QUERY_MAX_TOKENS` | `32000` | app | Output cap per model response (thinking counts toward it on 5.x models) |
| `QUERY_EFFORT` | `medium` | app | `low`, `medium`, `high`, `xhigh`, `max`; blank for models without effort support |
| `QUERY_THINKING` | blank | app | Blank (model default), `adaptive`, or `between_tools` |
| `QUERY_TEMPERATURE` | blank | app | Leave blank on 5.x models |
| `INDEX_MODEL` | `claude-sonnet-5-5` | indexer | Model that writes summaries |
| `INDEX_MAX_TOKENS` | `16000` | indexer | Output cap per summary call |
| `INDEX_EFFORT` | `low` | indexer | As `QUERY_EFFORT` |
| `INDEX_THINKING` | blank | indexer | As `QUERY_THINKING` |
| `INDEX_TEMPERATURE` | blank | indexer | As `QUERY_TEMPERATURE` |
| `INDEX_TIMEOUT_SECONDS` | `300` | indexer | Per-request timeout |
| `INDEX_API_MAX_RETRIES` | `5` | indexer | SDK retries on rate limits and transient errors (`report` and the other commands; `sync` leaves retries to Temporal) |
| `INDEX_WINDOW_SIZE` | `5` | indexer | File or folder summaries generated at a time by `sync` (`--window-size` overrides; `--max-workers` is a deprecated alias). Replaces `INDEX_MAX_WORKERS` (default 8) |
| `TEMPORAL_ADDRESS` | `localhost:7233` (compose: `temporal:7233`) | indexer | Temporal server that runs the `sync` workflow |
| `TEMPORAL_NAMESPACE` | `default` | both | Temporal namespace |
| `TEMPORAL_UI_URL` | `http://localhost:8233` | both | Temporal UI as your browser reaches it; printed by `sync` and linked from the chat sidebar |
| `INDEX_PRICE_INPUT_PER_1M` | `2.00` | indexer | USD per million input tokens, for the cost report only |
| `INDEX_PRICE_OUTPUT_PER_1M` | `10.00` | indexer | USD per million output tokens, for the cost report only |
| `INDEX_PRICE_CACHE_READ_MULTIPLIER` | `0.05` | indexer | Prompt-cache reads as a fraction of the input price (0.05 on the 5.5 models, 0.1 on most others), for the cost report only |
| `INDEX_EXCLUDE` | built-in list | indexer | Comma-separated exclude patterns; replaces the built-in list (see the comments in `.env.example` for pattern rules) |
| `INDEX_EXCLUDE_EXTRA` | blank | indexer | Extra exclude patterns added to the list |
| `INDEX_MAX_FILE_BYTES` | `1000000` | indexer | Files larger than this are skipped |
| `LENS_ENABLED` | `true` | indexer | Add a Temporal lens record to every summary; when on, each run first checks the Temporal Docs MCP and stops if it fails |
| `LENS_PACK_PATH` | built into the image | indexer | Path to `temporal-pack.yaml` (the knowledge pack the lens is grounded in) |
| `TEMPORAL_DOCS_MCP_URL` | `https://temporal.mcp.kapa.ai` | indexer | Temporal Docs MCP endpoint |
| `TEMPORAL_DOCS_MCP_CREDENTIALS` | `/run/code-atlas/temporal-docs-mcp.json` | indexer | Credentials file from `scripts/mcp_login.py`, as seen inside the container |
| `REPORT_EFFORT` | `medium` | indexer (`report`) | Effort for the Temporal report's process investigation; the report's other calls use the `INDEX_*` settings |
| `REPORT_RUBRIC_PATH` | built into the image | indexer (`report`) | Path to `temporal-rubric.yaml` (how the report scores processes and Temporal code) |
| `DB_PREFIX` | `semv2` | both | Database name prefix; must start with `sem` and match on both sides |
| `APP_PORT` | `8090` | compose | Host port for the UI (bound to localhost) |
| `TEMPORAL_PORT` | `7233` | compose | Host port for the Temporal server (gRPC, bound to localhost) |
| `TEMPORAL_UI_PORT` | `8233` | compose | Host port for the Temporal UI (bound to localhost) |
| `COMPASS_PORT` | `8096` | compose | Host port for Compass Web (bound to localhost), opened by **Open in Compass** |
| `REPOS_ROOT` | your home directory | compose (app, index-worker) | Folder under which the UI can index repositories. Mounted read-only at the same path into the app (folder picker, sync check) and the `index-worker` service (indexing) |
| `LOG_LEVEL` | `INFO` | app | Query-side log level |
| `CHAT_IDLE_HOURS` | `24` | app | A chat session workflow ends after this long without a message; the next message starts a new one from the browser's copy |
| `CHAT_CONTEXT_CHARS` | `240000` | app, browser | Characters of conversation sent to the model and kept by the session (oldest messages dropped first; the browser keeps everything) |
| `CHAT_TURN_TIMEOUT_MINUTES` | `20` | app | Longest one answer may take (the turn activity's start-to-close timeout) |
| `CHAT_STREAM_TTL_SECONDS` | `3600` | chat-worker | How long a turn's streamed tokens stay in Redis (for a reloaded page) |
| `CHAT_MAX_CONCURRENT_TURNS` | `8` | chat-worker | Turns the chat worker runs at once |
| `CHAT_TASK_QUEUE` | `code-atlas-chat` | app, chat-worker | Temporal task queue for chat sessions |
| `REDIS_URL` | `redis://redis:6379/0` | app, chat-worker | Redis for chat token streams (set by compose) |
| `IMPACT_RUNNER` | `temporal` | app | Where business-impact runs execute: `temporal` (a `BusinessImpactWorkflow` on the `impact-worker` service) or `inprocess` (a thread of the app; a stopgap, lost on restart) |
| `IMPACT_TASK_QUEUE` | `code-atlas-impact` | app, impact-worker | Temporal task queue for business-impact runs |
| `IMPACT_CONCURRENCY` | `4` | impact-worker | Activities (mostly model calls) the impact worker runs at once |
| `IMPACT_MAPPING_PATH` | built into the image | app, impact-worker | Path to `business-impact.yaml` (consequence categories, severity rules and cost formulas); its hash is part of every impact cache key |

Set by `compose.yaml` (no need to change): `MONGO_URL=mongodb://mongo:27017`, `SOURCE_DIR=/data/source`, `TEMPORAL_ADDRESS=temporal:7233`, `TEMPORAL_NAMESPACE=default`, `TEMPORAL_UI_URL=http://localhost:${TEMPORAL_UI_PORT:-8233}`, `COMPASS_URL=http://compass:8080`, `COMPASS_PUBLIC_URL=http://localhost:<COMPASS_PORT>`, `REDIS_URL=redis://redis:6379/0`, `REPOS_ROOT` (blank means `$HOME`), `HOST_HOME=$HOME` (so `~` typed in the UI means your home directory).

## Changing models

Any Anthropic model ID can go in `QUERY_MODEL` and `INDEX_MODEL`. When switching, check the optional settings against the model:

| Model family | `*_EFFORT` | `*_TEMPERATURE` | `*_THINKING` |
| --- | --- | --- | --- |
| `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5` | supported | leave blank (400 error otherwise) | blank, `adaptive`, or `between_tools` |
| `claude-haiku-4-5`, `claude-sonnet-4-5` | leave blank | allowed | leave blank |

If a request fails with a 400, the error names the rejected parameter. Also update the two `INDEX_PRICE_*` values so the cost report matches the model.

## Data locations

| Data | Where |
| --- | --- |
| Summaries | `mongo-data` volume (MongoDB), one database per repository |
| Mirrored source | `source-data` volume, `<SOURCE_DIR>/<name>/<path>`; blob IDs in `<SOURCE_DIR>/.oids/<name>/<path>` |
| Chat history | Browser IndexedDB (database `code-atlas`); the session's recent context also passes through Temporal workflow history |
| Chat token streams | Redis, in memory only, per turn for `CHAT_STREAM_TTL_SECONDS` |
| Temporal Docs MCP sign-in | `~/.config/code-atlas/temporal-docs-mcp.json` on the host (0600), mounted at `/run/code-atlas` into the indexer and, for docs search in chat, into the app (read-write, so a rotated token can be saved) |
| Knowledge pack | `knowledge/temporal-pack.yaml`, built into the image at `/app/knowledge` |
| Temporal reports | `reports/<name>/temporal-report.md` and `.json` in this folder, mounted into the indexer at `/reports` |
| Process cards | `process_cards` collection in the repository's database; reused by later reports while the files they cite are unchanged |
| Stored Temporal report | `reports` collection (the latest report, with its commit and versions); cached report steps in `report_cache` |
| Report rubric | `knowledge/temporal-rubric.yaml`, built into the image at `/app/knowledge` |
| Sync workflow history | `temporal-data` volume (the dev server's SQLite file) |
| Business impact | `reports` collection (`<name>::business_impact`: the latest result, its views as Markdown, and the context it was built with); each run's request (with the customer context) and per-process results in `impact_requests`, removed 7 days after its last write; cached answers in `report_cache`. Workflow history holds only ids, keys, process names and counts |
| Business-impact mapping | `knowledge/business-impact.yaml`, built into the image at `/app/knowledge` |
| Staged summaries of a sync in progress | `code_atlas_ingest` database (MongoDB); removed when the run finishes, or after 7 days for an abandoned run |
| Why the worker service cannot run a UI sync | `worker_status` collection in `code_atlas_ingest`, one document per index; cleared when the run starts, or when the index is deleted |
