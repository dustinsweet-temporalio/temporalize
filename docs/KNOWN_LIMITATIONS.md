# Known limitations

These come from the original indexing and query design and were deliberately left unchanged when the system moved to a fully local setup.

## 1. Input is a local git repository at HEAD

The indexer reads the committed tree at `HEAD`. Uncommitted edits and untracked files are not summarized. The source copy is made from the working tree, so uncommitted edits to tracked files can appear in retrieved source while the summaries describe `HEAD`. Keep the working tree clean when indexing.

Eligibility checks exclude non-regular files, detected binaries, files over `INDEX_MAX_FILE_BYTES` (1 MB by default), and files matching `INDEX_EXCLUDE` / `INDEX_EXCLUDE_EXTRA` in `.env`. `.gitignore` is not consulted: anything committed is a candidate unless excluded. File content beyond 525,000 characters is truncated before analysis.

## 2. Questions are repository-scoped

The agent is directed to identify one repository first. `text_search_summaries` searches one repository's file and folder summaries. There is no automatic search or aggregation across all repositories and no corpus-level root summary.

Search is MongoDB `$text` over the LLM-generated `prose_summary` field. There is no embedding or vector search.

## 3. Repository names

Names may contain only letters, numbers, hyphens and underscores. Each name becomes a database `<DB_PREFIX>_<name>`; MongoDB limits database names to 63 bytes, so keep names reasonably short. The indexer and web app must use the same `DB_PREFIX`.

## 4. Very large folders

Folders with more than 500 direct file summaries get a mechanical stub instead of an ordinary LLM summary, and large-folder processing may condense child details.

The repository summary (`repo_summary`, read by `get_repo_summary`) is generated from the root-folder summary and the top-level folder summaries only, capped at 400,000 characters of input. In a repository with a very large number of top-level folders, the extras are left out of that summary. It is regenerated whenever a folder summary changes, and added on the next run if an older index lacks one.

## 5. Writes are not one transaction

`sync` stages everything it generates and writes to the index in one phase at the end (deletions of removed files, then batched upserts, then index metadata and text indexes). That phase is not a transaction: a run that fails inside it can leave the index partly updated. Re-running `sync` resumes the workflow, and the writes are idempotent.

## 6. Indexing on Temporal

- **An `index.sh` run's worker lives in the `sync` process.** The workflow only makes progress while `index.sh` (the indexer container) runs, because the activities need the bind-mounted repository. An interrupted run waits on the Temporal server until you run the same command again. Runs started from the UI are run by the `index-worker` service instead, and resume when it restarts.
- **The UI indexes only under `REPOS_ROOT`, and can read all of it.** The app and the worker mount `REPOS_ROOT` (default: your home directory) read-only; the folder picker lists the folder names in it. Narrow `REPOS_ROOT` if that is more than you want the containers to see.
- **The UI's percentage is an estimate.** Generation is weighted by item counts (a folder counts double), and the folder count is estimated until the folder phase starts, so the bar can step when the real count arrives. The Temporal report reports no progress of its own, so the bar holds at 85% while it runs.
- **The UI offers incremental and full syncs only.** `--dry-run`, `--refresh-lens`, `--no-report` and `--window-size` are on the command line.
- **One run per repository name.** Re-running while a run is in progress (or after it was interrupted) attaches to it with the options it was started with; new options apply to the next run.
- **Resume picks up in-flight items after a short wait.** Summaries that were being generated when the worker died are retried once their heartbeat times out (30 seconds); finished ones are not repeated.
- **Default throughput is lower than before.** At most `INDEX_WINDOW_SIZE` (default 5) summaries are generated at a time, against 8 parallel calls before. Folders are generated one depth level at a time, deepest first.
- **Dry runs no longer preview the source mirror.** `--dry-run` generates summaries (and spends tokens), writes nothing and mirrors nothing.
- **Staging is kept for 7 days.** An abandoned run's staged summaries are removed by a TTL index after 7 days; terminating the workflow does not remove them sooner. The changes of an incremental run (paths and blob ids) are stored in one MongoDB document, which limits a single incremental run to roughly 100,000 changed files.
- **Single-node dev server.** The `temporal` service is the Temporal dev server with a SQLite file on a volume: fine on a workstation, not a production deployment.

## 7. Conversations

Each conversation is a Temporal session workflow that keeps the recent context (up to `CHAT_CONTEXT_CHARS`); the browser keeps the whole transcript.

- **Transcripts live only in the browser.** They are in IndexedDB; clearing the site's data, or using another browser, loses them. The server keeps no copy beyond the session's recent context.
- **Transcripts pass through Temporal history.** Messages and answers are workflow payloads, visible in the Temporal UI and in the dev server's SQLite volume while the session runs and for the namespace's retention period after it ends. There is no payload encryption.
- **Last write wins across tabs.** Two tabs on the same conversation overwrite each other's saved copy.
- **A stopped answer is `_(stopped)_` to the model.** The session records a stopped turn as `_(stopped)_` until the next message resyncs it with the partial answer the browser kept.
- **Long conversations are trimmed.** Only the newest messages within `CHAT_CONTEXT_CHARS` reach the model; the agent does not see older ones.
- **Context costs tokens on every turn.** The recent conversation goes into the system prompt each time, so long conversations cost more input tokens per turn.

## 8. Temporal lens

- **Review findings are leads.** For code that already uses a Temporal SDK, each file's lens lists review checks it may violate (`f`). In calibration about half were accurate; they point at where to look and must be confirmed against the source before they are reported.
- **The Docs MCP sign-in is personal.** The stored refresh token belongs to whoever ran `scripts/mcp_login.py`. That is fine on a workstation; a shared deployment needs a service credential.
- **Fixed cost per run.** Parallel workers each write the cached system prompt before any can read it, which adds roughly $0.40 per run; noticeable on small repositories only.
- **Tier 0 detection is pattern-based.** Temporal SDK use is detected from imports per language, and other orchestrators from dependency manifests and import lines, using the markers in the pack. Unusual layouts (vendored SDKs, dynamic imports) can be missed.
- **Repository pass reads a budget.** It sees relevance-3 file records first, then relevance-2, then relevance-1, up to about 60k tokens; in very large repositories lower-relevance records are left out (the run says how many).

## 9. Temporal report

- **Leads are checked one file at a time.** A check that depends on another file (for example, whether activities registered in the worker are synchronous) comes back "unclear" and is listed in the appendix, not in the findings.
- **At most 60 leads per report.** Gates come first, then findings by relevance; the appendix says how many were not checked.
- **At most 8 processes per report.** Further processes, and relevance-1 entry points no process covers, are listed but not investigated.
- **Investigation is bounded.** Each process gets at most 25 tool calls. A card can miss a fact that needs more reading (for example, a launch script that decides where a cache file lives); facts it cannot establish say so, and a card without a persistence or crash-loss fact is marked low confidence.
- **The scale is assumed.** For a tool, how many units a run has depends on its input, which the code does not show. Each card states its assumption, and the verdict depends on it.
- **Verdicts near the threshold can change between runs.** Each run investigates and scores again, and a process's overall score moved by up to 0.8 between runs in calibration. Scores within 0.75 of the threshold are labeled borderline. A process near the edge of that band can move between borderline and a neighboring verdict; calibration saw no move between suitable and not suitable. Cards are reused while their files are unchanged, so repeated reports on an unchanged index give the same verdicts.
- **Uncited prose.** Bar reasons, design elements and reasons for unsuitability must cite a pack entry or a docs page, or they are dropped. The verdict paragraphs and the removes/stays lists are not cited item by item.
- **Only the latest report is kept.** Rebuilding replaces it; earlier reports are not stored.
- **Docs search in chat uses your personal sign-in.** The app container reads the same refresh token as the indexer. If the sign-in expires, the sidebar light turns red and chat says it cannot search the docs; run `python3 scripts/mcp_login.py`, then restart the app.
- **Chat is guided, not checked.** The report's claims are checked in code. The chat assistant is told to rely on the report and the docs and to cite them, but its answers are not checked the same way.

## 10. Business impact

- **Only translates conversion findings.** An index whose report has status `full` (it already uses Temporal) gets no business impact: translating the evaluation findings into business risk is not built yet.
- **Default cost inputs are placeholders.** The hourly rate and the shares in `knowledge/business-impact.yaml` (`default_range`) were not set by anyone who knows the customer. They are tagged "default" wherever they appear; have someone review them before showing estimates to a customer.
- **Costs need numbers the customer stated.** A cost input is used only if a context item states that number. The model may not convert units or multiply, so "~40k payouts/day" does not become "failed runs per month"; it becomes a discovery question instead.
- **The figure check is a heuristic.** Prose with currency, percentages, rates or large numbers is rejected, but numbers written as words get through. What it rejects is listed under "What was filtered out".
- **Estimates are per process.** Two processes can rest on the same stated numbers (for example the same monthly failure count); the views never add estimates up, and neither should a reader.
- **It inherits the report's drift.** Verdicts near the threshold move between report runs, and the business case follows them. Borderline and low-confidence processes are capped at a moderate case.
- **One result per index.** Generating again replaces the result; there is no history of earlier ones and no per-prospect variants.
- **Context goes to the model provider.** Like your code summaries, the customer context is sent to the Anthropic API. It is stored in the index's database (with the result, and with the request for 7 days) and never in Temporal workflow history or logs.
- **`/check` is an estimate.** It counts the processes that would call the model and sizes their prompts; it leaves out the occasional retry.
