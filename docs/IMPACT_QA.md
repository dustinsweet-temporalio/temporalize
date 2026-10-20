# Business impact: manual QA checklist

The XSS check in `code_atlas/tests/test_impact_web.py` drives the view renderers in node;
everything else in `web/impact.js` is checked by hand. Run these after changing
`web/impact.js`, the report toolbar in `web/app.js`, or `code_atlas/impact_api.py`. Use the
compose stack (`docker compose up --build -d`) and an index whose Temporal report has
processes to assess (lens status `none`, `adjacent` or `partial`; devika-style repositories
work well). Open the index, click **Temporal report**, then **Business impact**.

| # | Check | Expected |
| --- | --- | --- |
| 1 | Open **Business impact** on an index that has never had one. | The empty state: **Generate business impact**, a collapsed **Customer context (optional)** form, and a check line such as "3 processes will use the model (est. ~$0.10); 1 skipped (not a fit, or nothing to translate)." The privacy note under the form says context goes to the model provider and is stored locally. |
| 2 | Type in **Call notes** and wait a second. | The check line updates (a new context is not cached). Typing more than 4,000 characters is stopped by the field. |
| 3 | Click **Generate** with the context empty. | The running state: a phase line, a progress bar, a dot per process (pulsing while pending, green when done, grey for not-a-fit), and **Cancel**. When it finishes, the **Exec brief** tab shows; no currency figure appears anywhere; each process says "No cost estimate yet". |
| 4 | Open **Discovery sheet**. | Questions grouped by who to ask; template questions for missing inputs are tagged **cost input** with their unit; "Missing inputs for cost estimates" lists them; "Customer context used" says none was given. |
| 5 | **New context…**, enter "About 4 failed batches a month. Each one costs around 250 in refunds." and an hourly rate, then generate. | Cost ranges appear only for processes whose inputs the context states, with the formula and an input table. Context inputs are tagged **context C#** (green); defaulted ones **default** (amber) with "Defaults are placeholders". |
| 6 | **Why we say this** under a story (Exec), or the **Champion brief**. | The cited facts (F#) with their text and `path@Lstart-end` references, and any cited context items (C#). Champion also shows what a crash loses, what changes with Temporal, the verdict and the proof-of-concept scope. |
| 7 | Click **Generate** again (same context) from a second tab while a run is going. | The second start is refused with "already in progress" (409); the first run is unaffected. |
| 8 | Reload the page while a run is in progress, then open **Business impact** again. | The running state comes back and polling resumes. |
| 9 | **Cancel** a run. | The button says "Cancelling…", the run ends within a process or two, and the previous result (if any) is shown unchanged. |
| 10 | Rebuild the Temporal report (`./scripts/report.sh <index>`), then open **Business impact**. | A stale banner: "The Temporal report was rebuilt since this was generated" with **Regenerate**. Regenerating reuses cached answers for unchanged processes. |
| 11 | Stop the worker that runs impact jobs (`docker compose stop impact-worker`) mid-run, wait, start it again. | The run waits, then finishes (visible in the Temporal UI as `impact-<index>`). With `IMPACT_RUNNER=inprocess`, restarting the app instead shows "The run stopped before it finished". |
| 12 | Make a run fail (for example a wrong `ANTHROPIC_API_KEY` in the worker's environment). | The failure notice with the error, and the last good result still shown below it. |
| 13 | **Download HTML** and **Download .md** on each tab. | The HTML page opens standalone with the app's styles and every "Why we say this" expanded; the Markdown matches `GET /api/impact/<index>/markdown?view=<tab>`. |
| 14 | In the **Summary** report view after an impact exists, each opportunity card. | A **See business impact** link that opens the impact view scrolled to that process. **Discuss in chat** in the impact view prefills a business-case question. |
| 15 | Expand **What was filtered out**. | Every statement the checks removed or corrected, or "Nothing" if all passed. |
| 16 | Switch the OS between light and dark. | Both themes are readable: chips, tags, the cost box, progress dots and the form. |
| 17 | XSS by hand: put `<script>alert(1)</script> "><img src=x onerror=alert(1)>` in **Call notes** and generate. | No alert and no broken layout; the text appears literally under "Customer context used". |
| 18 | An index whose report has status `full` (it already uses Temporal). | **Generate** is disabled and the check line says business-impact translation of evaluation findings isn't supported yet. |
