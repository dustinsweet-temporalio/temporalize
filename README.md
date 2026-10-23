# Temporalize

**Temporalize** (the UI at <http://localhost:8090>) takes an AE or SA from a repository to a running Temporal proof of concept with evidence, in eight screens: setup, a local scan, focus, success criteria, then (from **Start assessment**, the first point where anything leaves your machine) the Code Atlas report and scope, an approvable plan, generation with review and validation, and results with a review packet. See [docs/TEMPORALIZE.md](docs/TEMPORALIZE.md). The Code Atlas UI below is at <http://localhost:8090/atlas>.

Ask questions about your code. Code Atlas indexes a git repository into a hierarchy of LLM-written summaries (files, then folders, up to the repository root), then answers questions by navigating that hierarchy and pulling in source code where needed.

Everything runs locally in Docker. What leaves your machine: the LLM calls, which go to the Anthropic API with your key, and, with the Temporal lens on (the default), documentation searches sent to the Temporal Docs MCP (search questions only, never your code).

## Quick start

```sh
cp .env.example .env          # then set ANTHROPIC_API_KEY in .env
docker compose up --build -d  # MongoDB, the Temporal dev server, the indexing worker and the web app
python3 scripts/mcp_login.py  # one-time sign-in to the Temporal Docs MCP (for the Temporal lens)
```

Skip the sign-in by setting `LENS_ENABLED=false` in `.env` if you do not want the Temporal lens.

Open <http://localhost:8090> for Temporalize, or <http://localhost:8090/atlas> for Code Atlas (change the port with `APP_PORT` in `.env`). Code Atlas opens on your indexes. Click **+ Add index**, choose a git repository, and start indexing (see [Indexing a repository](#indexing-a-repository)). When it is done, click the index (or pick it in the sidebar) and ask your question. Each conversation stays with the index it was started on; the index is shown above the chat and under the conversation's name, and a conversation is saved when you send its first question. Every prompt and answer has a **Copy** button (answers copy as Markdown, ready to paste into Claude Code), and **Open in Compass** above the chat opens the index's database in Compass Web. Indexing runs are visible in the Temporal UI at <http://localhost:8233> (the **Temporal UI** link at the bottom of the sidebar).

## Indexing a repository

### From the UI

- **Add an index:** click **+ Add index**, then type the repository's folder or pick it with **Browse…** (git repositories are marked). The folder must contain `.git`; any other folder is refused. Name the index (it defaults to the folder name). **Cached copy** shows where the mirrored source will live (in the `source-data` volume) and which MongoDB database holds the summaries.
- **Sync an index:** select it and click **Sync…** (or **Sync…** on its card on the home page). The folder it was indexed from is filled in. Choose **Incremental** (only files whose content changed) or **Force full re-index**.
- **Check whether a sync is needed:** **Check whether a sync is needed** compares the blob hash of every eligible file at `HEAD` with what the index holds, and says what a sync would summarize. It makes no model calls.
- **Progress:** when you start, the dialog closes and a progress bar appears above the main view with a one-line description (for example "Summarizing files: 132 of 480") and the overall percentage. **Temporal ↗** opens the run in the Temporal UI in a new tab. When the run ends the bar says what it did and what it cost; **Open chat** starts a chat about the index. Runs started with `index.sh` show there too.
- **Delete an index:** **Delete…** (in the sidebar or on its card) asks for confirmation, then removes the index's summaries, Temporal report and cached source. Your repository is not touched.

The UI can index repositories under `REPOS_ROOT` (default: your home directory), which compose mounts read-only into the app and the indexing worker at the same path. Before a run starts, the same preflight as `index.sh` runs, and a failure is shown in the dialog. Syncs started here are run by the `index-worker` service; if it restarts, it picks them up again. For `--dry-run`, `--refresh-lens`, `--no-report` or `--window-size`, use the command line.

### From the command line

```sh
./scripts/index.sh ~/code/my-service                 # indexed as "my-service"
./scripts/index.sh ~/code/my-service backend         # indexed as "backend"
./scripts/index.sh ~/code/my-service backend --dry-run
```

- The folder must be a git repository. The indexer reads the **committed files at `HEAD`**; uncommitted changes and untracked files are not seen. Getting the git state you want before indexing is up to you.
- The name is how the repository appears in the UI and in questions. Use letters, numbers, `-` and `_`.
- What gets skipped (folders, file types, size limit) is set in `.env`; see `INDEX_EXCLUDE` and the comments above it.
- Re-running on the same name is incremental: only files whose content changed are re-summarized. Add `--force` to rebuild the whole index from scratch (for example `./scripts/index.sh ~/code/my-service my-service --force`). An index built by an older version of the indexer is rebuilt automatically the first time, and the run says why. When a new version changes what summaries say (the summary prompt version), existing summaries are kept: changed files get the new version, the run warns, the UI marks the repository "mixed summaries", and `--force` regenerates everything.
- Before spending money, `docker compose run --rm -v /path/to/repo:/repos/x:ro indexer estimate --repo-path /repos/x` prints a token and cost estimate without calling the API.

The script mounts your folder read-only into the indexer container, so nothing in your repository is modified. It first runs a preflight (knowledge pack, Anthropic API, MongoDB, Temporal server, Temporal Docs MCP) and stops before spending anything if a check fails.

### Indexing runs on Temporal

Each run is a Temporal workflow, `sync-<name>`, on the Temporal dev server in `compose.yaml`. For a run started with `index.sh`, the indexer container runs the worker for as long as `index.sh` runs, and prints a link to the workflow in the Temporal UI (<http://localhost:8233>), where you can follow every file, folder and retry. Runs started from the UI are run by the `index-worker` service, which runs a worker for each of them.

- **Resume:** if a run crashes, the container is killed, or you press Ctrl-C, run the same `index.sh` command again. It attaches to the running workflow (with the options it was started with) and carries on; finished summaries are not generated or paid for again. Only one run per name can be in progress.
- **Throughput:** up to 5 file or folder summaries are generated at a time (`INDEX_WINDOW_SIZE` in `.env`, or `--window-size`). The old default was 8 parallel calls, so a run with default settings takes somewhat longer than before; raise the window if your rate limits allow. `--max-workers` still works as an alias.
- **Retries:** rate limits and transient API errors are retried by Temporal with backoff (honoring the API's retry-after hint), and show up as retries in the UI. A file that still fails is listed at the end of the run, and the next run picks it up again.
- **Nothing is written to the index until generation is done:** summaries are staged in a separate database (`code_atlas_ingest`) and written to the index in one phase at the end; a full re-index replaces the old index only then.

## The Temporal lens

With `LENS_ENABLED=true` (the default), indexing also records how each file, folder and the repository relate to durable execution: side effects, waits, hand-rolled retries and status tables, use of Temporal or of other orchestrators, and which Temporal patterns apply. It is grounded in a knowledge pack, `knowledge/temporal-pack.yaml`, whose entries were checked against the Temporal documentation, and the repository-level pass cites Temporal Docs pages it looked up during the run. In the UI, the repository list shows the pack version an index was built with; ask about durability, orchestration or Temporal and the agent pulls the lens in.

- **Sign-in:** `python3 scripts/mcp_login.py` opens your browser once and stores a refresh token in `~/.config/code-atlas/` (readable only by you). Run it again if indexing reports the login expired.
- **Cost:** roughly $7 per thousand files on top of normal indexing, plus a fixed ~$0.40 per run while parallel calls warm the prompt cache. `estimate` includes it.
- **Pack updates:** a newer pack version does not regenerate anything by itself. Changed files use it, the UI marks the index as mixed, and `--refresh-lens` updates every file's lens (summaries are not regenerated): `./scripts/index.sh ~/code/my-service my-service --refresh-lens`.
- **Off:** set `LENS_ENABLED=false`; indexing then behaves as before and does not contact the Docs MCP.

### Temporal report

With the Temporal lens on, indexing also builds the Temporal report and stores it in the index (`--no-report` skips it). Open it in the UI with **Temporal report** above the chat, or ask about it in the chat. To rebuild it on its own, or to get it as files:

```sh
./scripts/report.sh my-service      # also writes reports/my-service/temporal-report.md and .json
```

The report finds the repository's processes and judges each one on its own: should it use Temporal, and why? If the repository already uses Temporal, the report also reviews that code.

- **Per process:** the report investigates each process in the source and writes up how it runs today: what triggers it, the unit of work and how it scales, the steps and their side effects, where state lives, when results are persisted, and what a crash at each stage loses. Every fact cites source lines and is checked against them.
- **Scores:** a process needs one strong reason to use Temporal: durable pipeline (long or expensive multi-step work, such as LLM pipelines), distributed transaction, long-lived waits, or scheduled work. Its fit is the strongest of these, on a 0–10 scale. Reliability burden, separability and migration ease adjust the score, and verified gates (reasons Temporal is a poor fit) cap it.
- **Verdicts:** suitable from 5.75/10, not suitable up to 4.25, and borderline in between, because a process's score can differ by up to about 0.7 between runs. Suitable and borderline processes get a design (workflows, activities, signals, timers; what code it removes, what stays outside, and why) and count toward the share of durable work that moves to Temporal. Not suitable processes get the reasons, and what would change the verdict.
- **Existing Temporal code:** bars scored from review findings confirmed against the source, each with a fix.

It uses only the index, the mirrored source, the knowledge pack, the report rubric (`knowledge/temporal-rubric.yaml`) and the Temporal Docs MCP. Code checks every fact, path and citation the model writes; anything it cannot ground is dropped and listed in the appendix.

- **In the UI:** the report view leads with the opportunities (each process that would gain from Temporal, its strongest reason, its score, and what you gain), then the picture at a glance, then details per process: how it runs today with line references, what a crash loses, the score breakdown and the design. **Discuss in chat** starts a question about a process. **Detailed report** switches to the complete report (the same document as `temporal-report.md`), and **Download** saves the view you are on: the summary as a standalone HTML page, the detailed report as Markdown.
- **In the chat:** the assistant reads the stored report and can search the Temporal docs (Temporal Docs MCP) for follow-up questions, citing what it uses. The light at the bottom of the sidebar shows whether the docs search works: green when connected, red with the reason and fix in its tooltip, grey when you have not signed in (`python3 scripts/mcp_login.py`, then `docker compose up -d app`).

A first report costs about $0.15–0.30 per process investigated (at most 8). Every model step is cached on its inputs, so re-indexing unchanged code rebuilds the report for about nothing, and a change re-runs only the processes it touches; `./scripts/report.sh my-service --fresh` runs everything again.


### Business impact

For a sales conversation, the report view has a third mode next to **Summary** and **Detailed report**: **Business impact**. On demand, it turns the verified facts of the Temporal report into three documents:

- **Exec brief** (for the economic buyer): the ranked failure stories in plain language, who feels each one, a cost range only where the customer stated the inputs, what is not worth pursuing, and a first step.
- **Champion brief** (for the engineering lead): the same stories with the code facts and line references behind them, what a crash loses today, what changes with Temporal (copied from the report's design), the verdict and a proof-of-concept scope.
- **Discovery sheet** (for the Solutions Architect): the questions to ask, grouped by who to ask, that would turn assumptions into facts, including the missing cost inputs.

Every failure story cites facts from the process card (F ids, checked against the source by the report) and, where used, the customer context (C ids). Anything else is labeled an assumption, and code checks every answer: a story without a valid citation, a category the card does not support, or a figure in prose is dropped and listed under **What was filtered out**. The model never does arithmetic: cost ranges are computed in code from the formulas in `knowledge/business-impact.yaml`, from numbers the context states (copied, not converted) or from labeled placeholder defaults, and the formula and its inputs are shown. With no context the briefs are qualitative only. Processes the report rates not suitable, or whose code shows nothing a crash would lose, cost no model call and are listed as such.

- **Generate:** open **Business impact**, optionally add customer context (call notes, pain points, industry, volumes, currency and an hourly staff cost), and click **Generate business impact**. The check line says beforehand how many processes will use the model and roughly what it costs (about $0.02–0.03 per process). The context is sent to the model provider, like your code summaries, and stored in the index's database; it never enters Temporal workflow history or the logs.
- **Runs on Temporal:** each run is a `BusinessImpactWorkflow` (`impact-<name>` in the Temporal UI) on the `impact-worker` service. One run per index at a time; a run survives a worker restart, and **Cancel** stops it. `IMPACT_RUNNER=inprocess` runs it in the app instead, as a stopgap.
- **Reruns are free when nothing changed:** every model answer is cached on its inputs (the process card, the verdict, the context, the mapping file and the model). **Regenerate** runs everything again; the result is marked stale when the Temporal report is rebuilt.
- **Downloads and chat:** each brief downloads as an HTML page or Markdown, and the chat can read the result (ask "what's the business case for …?").
- **From the command line:** `python scripts/run_impact.py --repo <name>` (inside the compose network) or `--report-json` on an exported report prints the three views. API details are in [code_atlas/README.md](code_atlas/README.md#business-impact-api); limitations in [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md#10-business-impact).

The default cost inputs in `knowledge/business-impact.yaml` (hourly rate, the share of work repeated) are placeholders: review them before showing estimates to a customer.

## Choosing models

Set these in `.env`; any Anthropic model ID works. Defaults are `claude-sonnet-5-5` for both.

| Setting | Purpose |
| --- | --- |
| `QUERY_MODEL` | Answers questions |
| `INDEX_MODEL` | Writes the summaries. Most of the cost is here, so this is the one to drop to a cheaper model (for example `claude-haiku-5-5`) |
| `*_MAX_TOKENS` | Output cap per response (raised well above the old 4096) |
| `*_EFFORT`, `*_THINKING`, `*_TEMPERATURE` | Optional. Only sent when set, because models differ (see below) |
| `INDEX_PRICE_INPUT_PER_1M`, `INDEX_PRICE_OUTPUT_PER_1M` | Used only for the cost report. Update when you change `INDEX_MODEL` |

Model differences that matter:

- **Claude 5.x** (`claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`) reject a non-default `TEMPERATURE`. Leave it blank. They support `EFFORT` (`low` to `max`). Thinking is on by default and its tokens count toward `MAX_TOKENS`.
- **Haiku 4.5 and other older models** do not support `EFFORT`: blank it. `TEMPERATURE` is allowed.
- If you change the model and a request fails with a 400, the optional settings are the first thing to check. The error text names the rejected parameter.

All settings are documented in [`.env.example`](.env.example) and [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## How it works

```
 your git repo ──► indexer (Temporal worker) ──► MongoDB   (file + folder summaries, text-indexed)
 (read-only)          │        ▲
                      │        └── Temporal dev server (the sync workflow; UI on :8233)
                      └──────► source volume (copy of the files, for source retrieval)

 browser ──► app (FastAPI) ──► Temporal: one ChatSessionWorkflow per conversation
 (IndexedDB:     ▲                 │
  transcripts)   │                 ▼
                 └── Redis ◄── chat-worker (agent) ──► MongoDB, source volume, Anthropic API
                   (token stream)
```

Each conversation is a Temporal workflow; each message is an Update on it. The `chat-worker` service runs the agent for every turn and streams its tokens through Redis, which the app relays to the browser. The browser keeps every conversation in IndexedDB (the record); the workflow keeps only the recent context. A turn survives a worker restart, and a page reloaded mid-answer picks the answer up again. Chat sessions appear in the Temporal UI as `chat-<index>-<conversation id>`. Business-impact runs are workflows too (`impact-<index>`), on the `impact-worker` service, which reads only the stored report in MongoDB.

| Path | Purpose |
| --- | --- |
| [semantic_ingress/](semantic_ingress/README.md) | The indexer: git tree analysis, LLM summaries, MongoDB persistence, source mirroring; `sync` as a Temporal workflow |
| [code_atlas/](code_atlas/README.md) | The query agent (tools, prompt) and the web server |
| [web/](web/) | The browser UI (plain HTML/JS, no build step; libraries vendored in `web/vendor/`) |
| [compose.yaml](compose.yaml), [Dockerfile](Dockerfile) | One image for the app, the chat worker and the indexer; plus MongoDB, Temporal, Redis and Compass Web |
| [docs/](docs/INDEX_FLOW.md) | Process diagrams and known limitations |

Questions are answered by an agent that starts at the repository's root summary, follows relevant folders, and reads file summaries and source as needed. See [docs/INDEX_FLOW.md](docs/INDEX_FLOW.md).

## Data and housekeeping

- Summaries live in the `mongo-data` Docker volume; mirrored source in `source-data`; Temporal workflow history in `temporal-data`.
- `docker compose down` stops everything and keeps your data. `docker compose down -v` also **deletes the index and the mirrored source**.
- Delete an index with **Delete…** in the UI. Its Temporal workflow history stays in the Temporal UI.
- MongoDB is not published to your host. To look at what an index stores (summaries, lenses, reports), click **Open in Compass** above a chat, or open Compass Web at <http://localhost:8096> (change the port with `COMPASS_PORT` in `.env`). Compass Web is a community build of Compass's web UI (`haohanyang/compass-web`), not an official MongoDB image, and it can edit data as well as view it. For a shell: `docker compose exec mongo mongosh`.
- Maintenance commands (run through `docker compose run --rm indexer <command> ...`): `sync`, `report`, `preflight`, `estimate`, `dir-list-only`, `source-mirror`. Add `--help` to any of them.
- Temporal reports are written to `reports/<name>/` in this folder (ignored by git).
- Chat history is stored in your browser only (IndexedDB). Clearing the site's data deletes it; conversations from earlier versions (in localStorage) are moved over on first load.

## Troubleshooting

- **Banner says `ANTHROPIC_API_KEY is not set`**: set it in `.env` and run `docker compose up -d` again (containers read `.env` at start).
- **A different page or a 404 at the URL**: another app is probably using the port. Set `APP_PORT` in `.env`, then `docker compose up -d`.
- **`429` / rate limit errors while indexing**: lower `INDEX_WINDOW_SIZE` (or pass `--window-size`). Temporal already retries with backoff; the retries are visible in the Temporal UI.
- **Preflight: `Temporal server not reachable`**: run `docker compose up -d temporal` (the indexer also starts it on demand) and check `TEMPORAL_ADDRESS`.
- **Chat says Temporal or Redis is unavailable**: `docker compose up -d` (the app and the `chat-worker` need `temporal` and `redis`). `docker compose logs chat-worker` shows turn errors.
- **A run was interrupted**: run the same `index.sh` command again; it resumes the workflow. To abandon it instead, terminate `sync-<name>` in the Temporal UI (staged summaries are removed after 7 days).
- **Summaries look truncated or JSON errors during indexing**: raise `INDEX_MAX_TOKENS` or lower `INDEX_EFFORT`.
- **"Not a valid Git repository"**: pass the repository root (the folder that contains `.git`).
- **UI: "Only folders under … can be indexed"**: the UI only sees `REPOS_ROOT` (default: your home directory). Set `REPOS_ROOT` in `.env` to a folder that contains your repositories, then `docker compose up -d`.
- **UI: the progress bar says "Waiting for the indexer worker"**: the `index-worker` service is not running (`docker compose up -d index-worker`). If the bar says the worker cannot run the sync, the reason follows (often the Temporal Docs MCP sign-in; see below); fix it and the worker retries within a minute. `docker compose logs index-worker` has the details.
- **Preflight: `No Temporal Docs MCP credentials` or `token refresh refused`**: run `python3 scripts/mcp_login.py` (or set `LENS_ENABLED=false`).
- **Preflight: canary search returned too few results**: the Temporal Docs MCP is degraded; try again later.
- **UI shows `pack 0.1.0 + 0.1.1` or `(partial)`**: some files carry an older lens or none; index again with `--refresh-lens`.

## Known limitations

Carried over from the original design and unchanged on purpose: see [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). The main ones are that questions are scoped to one repository at a time and search is text search over summaries (not embeddings).
