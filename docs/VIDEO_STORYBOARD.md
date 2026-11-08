# QuoteProof — screen-recording storyboard

Record at **1920×1080**, browser zoom **110–125%**, terminal font **16–18 pt**. Record each scene as a separate
clip; cut them together in the editor. Times match `VIDEO_SCRIPT.md`.

| # | Time | Source | Exact actions | On-screen caption | Transition |
|---|---|---|---|---|---|
| 1 | 0:00–0:12 | Excel / LibreOffice | Open `fixtures/poisoned_quote.xlsx`, click cell **B8**, zoom so the note fills the frame. | A real line from our test quote, cell B8 | Hard cut |
| 2 | 0:12–0:20 | Browser | Show `https://95-179-234-10.sslip.io` home page (padlock visible). | — | Quick zoom out |
| 3 | 0:20–0:45 | Excel + PDF viewer | Three windows side by side: `ngn_quote.csv`, `usd_quote.xlsx` (right-click row 6 → **Unhide** to reveal the $75 delivery row), `ngn_quote.pdf` page 2 ("Prices exclude VAT"). | Different layouts · hidden charges · small print | Cross-fade |
| 4 | 0:45–1:05 | Browser | Home → type **Office fit-out, Lagos HQ** → currency **NGN** → **Create comparison**. | Challenge 01 · Blast Radius Zero · Pattern A | Cut |
| 5 | 1:05–1:35 | Browser | Upload `ngn_quote.csv` (NGN), `ngn_quote.pdf` (NGN), `usd_quote.xlsx` (**USD**), one at a time. Scroll to the **Live processing timeline** and let it fill. Pause on rows `sandbox_index … runtime=runsc` and `sandbox_run_extractor … container_id=… exit_code=0 … removed=True`. | runtime=runsc → gVisor | Zoom-in on the timeline row (editor) |
| 6 | 1:35–2:05 | Browser | Click the CSV in **Documents** → document page. Hover a green **✓ Matches source at R4C3** badge. Scroll to **Totals and reconciliation** (✓ Reconciles, ₦1,585,625.00). Scroll to **AI-written extractor (ran only in the sandbox)** and **Sandbox runs**. | Layer 1 ✓ · Layer 2 ✓ · AI code ran only in the sandbox | Cut |
| 7 | 2:05–2:30 | Browser | Back to the project → upload `poisoned_quote.xlsx` (**USD**). Timeline: `injection_check … flagged`. Open its document page → **Findings**: INJECTION_PATTERN rows at Quote!B8. | Detected · flagged · held for human review | Cut |
| 8 | 2:30–2:50 | Browser | **Review** page → scroll to the poisoned quote's findings (open, awaiting a decision). | Detection is best-effort. Containment is not. | Fade to black (0.3 s) |
| 9 | 2:50–3:25 | Terminal (SSH to VM) | Paste and run the **hostile extractor** command below. When the JSON appears, zoom on `attempts`. | Written by us to act like a compromised model — sent through the AI-code path · Every attempt: blocked | Cut |
| 10 | 3:25–4:00 | Browser | Click **Containment** (top right) → **Run containment self-test now** → wait ~10 s for the page to refresh → scroll the table slowly (Test · Verdict · Expected · Observed). | ALL CONTAINMENT TESTS PASSED · Isolation: gVisor | Cut |
| 11 | 4:00–4:25 | Browser | Project → **Comparison** (Quoted/Calculated/Unknown pills; "Not comparable: different currency") → **Report** (Draft gate + 3 counters). | Unknown is never zero · No currency conversion | Cross-fade |
| 12 | 4:25–4:40 | Slide 13 of the deck, or home page | Hold on the URL. | https://95-179-234-10.sslip.io | Fade out |

## Scene 9 command (run as root on the VM; verified 7 Nov — exact output in README §4.3)
```bash
W=/tmp/qp-demo && rm -rf $W && mkdir -m 755 $W && cp /opt/qp/app/fixtures/indexes/ngn_quote.csv.json $W/index.json \
 && cp /opt/qp/app/fixtures/extractors/hostile.py $W/ && chmod -R a+rX $W && \
runuser -u qp-worker -- python3 -I /opt/qp/app/qpctl.py extract --index-file $W/index.json --code-file $W/hostile.py \
 | jq '{status: .result.status, runtime: .result.runtime, exit_code: .result.exit_code, removed: .result.removed, attempts: (.result.stdout | fromjson)}'
```
Optional, to show what the code tried before you run it: `cat /opt/qp/app/fixtures/extractors/hostile.py`.

What each line of the output means (for the narration and captions):

| Output key | What the code tried | Boundary that stopped it |
|---|---|---|
| `exfiltrate_https` | HTTPS request to `api.vultrinference.com` | `--network none` (no DNS, no route) |
| `raw_tcp` | TCP connection to `1.1.1.1:53` | `--network none` |
| `secret_env_vars_visible` | list environment variables containing KEY/TOKEN/SECRET/CANARY | empty environment set by the entrypoint; key exists only in qp-worker |
| `exists:/srv/qp/uploads`, `/etc/qp`, `/var/run/docker.sock`, `/run/qp-sandboxd` | reach host data, config, Docker, the supervisor | only `/input` (read-only) is mounted |
| `write:/input/...`, `/opt/qp/entrypoint.py`, `/etc/passwd` | tamper with input, the runner, the system | non-root user (uid 65534) on a `--read-only` root and read-only `/input` → `Permission denied` |
| `runtime: "runsc"` · `removed: true` | — | gVisor; container destroyed and removal verified |

## Recording tips
* Pre-load every tab and upload file before recording; clear the browser download bar.
* Use a fresh comparison for the recording so the timeline starts empty.
* If an AI step needs a retry on camera, keep it — the `verification_feedback retry` row is a feature; narrate it.
* Do **not** show `/etc/qp-secrets`, the Vultr API key page, or any terminal with a key in it.
