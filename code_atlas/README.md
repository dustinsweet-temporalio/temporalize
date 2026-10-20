# Code Atlas query agent and web server

Answers questions about indexed repositories. An LLM agent (Strands with the Anthropic provider) navigates the summaries that `semantic_ingress` wrote to MongoDB and reads mirrored source files when it needs implementation detail. A small FastAPI server streams the answers to the browser UI in [../web/](../web/).

The `tools/` and `utils/` packages are imported as top-level modules, so run everything from this directory.

## Layout

| Path | Purpose |
| --- | --- |
| `app.py` | FastAPI server: health, repositories, reports, the chat API (below), static UI |
| `indexing.py` | Indexing from the UI: folder picker (`GET /api/fs`), the sync check (`POST /api/sync/check`), starting a sync (`POST /api/sync`), progress (`GET /api/syncs`), deleting an index (`DELETE /api/repos/{name}`) |
| `agent.py` | Builds the model from `QUERY_*` settings and streams the answer; conversation history goes in the system prompt |
| `impact_api.py` | Business impact from the UI: `/api/impact/{name}` (below) |
| `impact/` | Business impact on Temporal: `BusinessImpactWorkflow` (one run per index at a time), its activities, the worker (`python -m impact.worker`, the compose `impact-worker` service), and the runner the API uses |
| `chat/` | Chat sessions on Temporal: `ChatSessionWorkflow` (one per conversation), the `run_agent_turn` activity, Redis token streaming, and the worker (`python -m chat.worker`, the compose `chat-worker` service) |
| `config.py` | Environment-driven settings and the agent system prompt |
| `tools/navigation/` | List repositories; repository, folder and file summaries; directory listing |
| `tools/search/` | Text search over summaries |
| `tools/source/` | Source retrieval from the local source store (`get_file_source`, `get_file_source_lines`), plus manual fallbacks that ask the user to paste code |
| `utils/mongo/` | MongoDB client, validators, exceptions |

## Running outside Docker

```bash
python -m pip install -r requirements.txt
export ANTHROPIC_API_KEY=... MONGO_URL=mongodb://localhost:27017 SOURCE_DIR=/path/to/source-store \
       TEMPORAL_ADDRESS=localhost:7233 REDIS_URL=redis://localhost:6379/0
python -m chat.worker &          # runs chat turns
python -m impact.worker &        # runs business-impact jobs (or IMPACT_RUNNER=inprocess)
uvicorn app:app --port 8080
```

Tests: `python -m pytest` from this directory. The durability test needs a running Temporal server and Redis: `CHAT_INTEGRATION=1 TEMPORAL_ADDRESS=... REDIS_URL=... python -m pytest tests/chat/test_integration.py`.

All settings are in [docs/CONFIGURATION.md](../docs/CONFIGURATION.md).

## Chat API

Each conversation (browser id `session_id`) is the workflow `chat-<repo_id>-<session_id>`; each message is an Update on it, started with the workflow when none is running (Update-with-Start).

`POST /api/chat` with

```json
{"repo_id": "...", "session_id": "<uuid>", "turn_id": "<uuid>", "prior_count": 4, "resync": false,
 "messages": [{"role": "user" | "assistant", "content": "..."}, ..., {"role": "user", "content": "new question"}]}
```

`messages` is the newest part of the conversation within `chat_context_chars` (from `GET /api/config`), ending with the new user message; `prior_count` is how many messages the browser holds before it. `resync: true` replaces the session's context with the messages sent (after a stop or a failure). A `turn_id` already answered returns the recorded answer.

The response is `text/event-stream`:

| Event | Meaning |
| --- | --- |
| `data: "<text>"` | The next chunk of the answer (JSON-encoded string) |
| `data: {"reset": true}` | Discard the text so far (a retry restarted the answer, or the final answer differs from what streamed) |
| `data: {"stopped": true}` | The turn was stopped |
| `data: {"error": "..."}` | The turn failed |
| `: keepalive` | Comment, sent while nothing else is |
| `data: [DONE]` | End of the stream |

Tokens come from the turn's Redis Stream; the final answer of the Update is authoritative, so the stream is reconciled with it before `[DONE]`.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/chat/{repo_id}/{session_id}/stop` `{"turn_id": ...}` | Stop the turn (202); its stream ends with `{"stopped": true}` |
| `DELETE /api/chat/{repo_id}/{session_id}` | End the session workflow (202) |
| `GET /api/chat/{repo_id}/{session_id}/turns/{turn_id}/stream` | Reattach to a turn after a reload (same events; 404 when nothing can be recovered) |

## Tools the agent can call

| Group | Tools |
| --- | --- |
| Navigation | `list_indexed_repositories`, `get_repo_summary`, `get_folder_summary`, `get_file_summary`, `get_dir_listing` |
| Search | `text_search_summaries` |
| Source | `get_file_source`, `get_file_source_lines`, `request_full_file_source`, `request_file_source_lines` |
| System | `get_agent_version` |

Text search uses MongoDB `$text` over `prose_summary`. The indexer creates the text indexes; the query side verifies them the first time a repository is used.

## Business impact API

Generated on demand from the stored Temporal report (the domain logic is `semantic_ingress/report/impact/`). The request, with the customer context, is written to MongoDB first; the run (`BusinessImpactWorkflow`, id `impact-<name>`, on the `impact-worker`) gets only the repository and request ids.

| Endpoint | Does |
| --- | --- |
| `POST /api/impact/{name}/check` | Body `{context, fresh}` (optional). No model calls: the processes that would call the model, those reused from cache, those skipped (not a fit, nothing to translate, or failed in the report), and an estimated cost |
| `POST /api/impact/{name}` | Body `{context, fresh}`. Starts a run; `{workflow_id, request_id}`. 409 while a run is open (conflict policy FAIL), 422 when there is no report or it has status `full`, 503 without `ANTHROPIC_API_KEY` |
| `GET /api/impact/{name}/status` | While a run is open: its phase and per-process progress (the workflow's `progress` query; `waiting` when no worker answers). After: `ok`, `failed`, `cancelled` or `interrupted`, from the request in MongoDB |
| `GET /api/impact/{name}` | The latest result: `status`, `data`, the `context` it was built with, and `stale` when the Temporal report was rebuilt since. 404 if none |
| `GET /api/impact/{name}/markdown?view=exec\|champion\|discovery` | One view as Markdown |
| `DELETE /api/impact/{name}` | Cancels the open run; the last result is kept |

`context` is `{text, industry, volumes, pain_points, currency, hourly_rate, audience}`, all optional (`text` up to 4,000 characters). Without context the views are qualitative and the missing cost inputs become discovery questions. The chat agent reads the result with the `get_business_impact` tool.

Run it outside the UI, in-process, on a stored report: `python scripts/run_impact.py --report-json report.json --view exec` (see the script's help).

