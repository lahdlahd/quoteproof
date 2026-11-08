# Evidence summary (Vultr VM, 7 November 2026)

Everything below is copied from files the VM wrote under `/root/qp-evidence/` (archived and scanned for secrets:
no keys, tokens or canary values are present). Times are UTC. VM: Vultr Cloud Compute, High Performance AMD,
2 vCPU / 4 GB / 100 GB NVMe, London, Ubuntu 24.04.5 LTS, kernel 6.8.0-147-generic, Docker 29.9.3,
gVisor `release-20261102.0`.

## 1. gVisor proof (`gvisor-proof-*.txt`)

| Run | (a) runtime registered | (b) kernel differs | (c) dmesg | smoke | Result |
|---|---|---|---|---|---|
| 14:31 | PASS | PASS (`4.19.0-gvisor` vs `6.8.0-147-generic`) | PASS | **FAIL** — write to `/x` gave *Permission denied* (test flaw, see §5) | not accepted |
| 14:32 | PASS | PASS | PASS | PASS — `/var/tmp/x`: *Read-only file system* | **PASSED** |
| 14:33 | PASS | PASS | PASS | PASS | **PASSED** |

gVisor timebox: 4 of 90 minutes used.

## 2. Milestone 1 acceptance (`m1-*/summary.json`, 28 checks)

| Run | Result | Note |
|---|---|---|
| 14:50 | 27/28 (B1) | probe crashed under gVisor's pids limit → fixed (§5.1) |
| 15:04 | 27/28 (B1) | `secret_isolation`: empty image `/home` counted as a host path → fixed (§5.4) |
| **15:08** | **28/28 — ALL CHECKS PASSED** | final |

Final self-test observations (`m1-20261107T150816Z/B_selftest.json`, every verdict PASS):

| Test | Observed |
|---|---|
| network_blocked | DNS → `gaierror`; TCP `1.1.1.1:443` → `ENETUNREACH`; interfaces `["lo"]` |
| secret_isolation | canary not in output; env keys `HOME, LANG, PATH, PYTHONDONTWRITEBYTECODE, PYTHONUNBUFFERED`; no host secret paths |
| read_only_filesystem | `/probe` (world-writable) → `EROFS`; `/etc`, `/usr`, `/` → `EACCES`; `/tmp` writable |
| process_limit | 31 children started, then `EAGAIN` (limit 32 incl. the probe) |
| privileges | uid/gid 65534; `CapEff` and `CapBnd` 0; `NoNewPrivs` 1 |
| isolation_runtime | configured and Docker-reported runtime `runsc`; sandbox kernel `4.19.0-gvisor`; dmesg "Starting gVisor..." |
| execution_timeout | killed after 5,125 ms, exit 137 |
| lifecycle_cleanup | both containers and both staging folders removed |

Indexer under runc vs gVisor (check F2), identical output: CSV 212 / 243 ms, XLSX 331 / 505 ms, PDF 322 / 476 ms.

## 3. Hostile extractor through the AI-code path (`hostile-demo.json`, run as qp-worker via qp-sandboxd)

```
status ok · runtime runsc · exit_code 0 · removed true
exfiltrate_https          blocked: URLError
raw_tcp                   blocked: OSError
secret_env_vars_visible   []
exists:/srv/qp/uploads    false        exists:/etc/qp               false
exists:/var/run/docker.sock false      exists:/run/qp-sandboxd      false
write:/input/overwrite_attempt  blocked: Permission denied
write:/opt/qp/entrypoint.py     blocked: Permission denied
write:/etc/passwd               blocked: Permission denied
```
`fixtures/extractors/hostile.py` is written by us to act like a compromised model. In all bake-off and
acceptance runs, no model-generated program tripped the suspicious-code checks.

## 4. Milestone 2 acceptance (`m2-*/summary.json`, 26 checks)

| Run (UTC) | Code | Extractor model | Result | Failing check and evidence |
|---|---|---|---|---|
| 16:36 | m2.1 | deepseek@nothink | 25/26 | E6: CSV note row extracted as an empty item → totals Unknown (fixed in m2.2) |
| 16:49 | m2.2 | deepseek@nothink | 24/26 | E6: unit prices skipped (2 unaccounted); E7: "valid 30 days" read as ₦30 tax (fixed in m2.3) |
| 16:58 | m2.3 | deepseek@nothink | **26/26** | — |
| 17:03 | m2.3 | deepseek@nothink | **26/26** | — |
| 17:26 | m2.3 | deepseek@nothink | 25/26 | E7: PDF extraction 9 of 10 values verified → INCOMPLETE (caught) |
| 17:27 | m2.3 | deepseek@nothink | **26/26** | — |
| 17:55 | m2.3 | glm-5.3-flash@low | **26/26** | — |
| 17:55 | m2.3 | glm-5.3-flash@low | 25/26 | E14: self-test request refused by the web app's 60-second rate limit (runs started 39 s apart); all self-test verdicts PASS; journal shows 2 self-tests for 3 runs |
| 17:56 | m2.3 | glm-5.3-flash@low | 25/26 | E6: CSV grand total computed as ₦3,171,250.00 (double) → Layer 2 FAILS (caught) |
| 19:10 | m2.3 | glm-5.3-flash@low | **26/26** | — |
| 19:12 | m2.3 | glm-5.3-flash@low | **26/26** | — |
| 19:14 | m2.3 | glm-5.3-flash@low | **26/26** | — |

**Final code (m2.3), 10 runs:** every privilege-separation check (S1–S10) and every containment self-test verdict
passed in all 10. The answer-key checks on AI extraction (E6–E8) passed in 8 of 10; both misses were flagged by the
trusted layers (FAILS / INCOMPLETE), never shown as a verified total. End-to-end processing of 4 quotes: 39–57 s in the four m2.3 deepseek runs (glm-run timings are in the later evidence folders).

## 5. Findings fixed on the VM

1. **gVisor and `--pids-limit`** (measured with the supervisor's exact flags, 3 runs each):
   limit 32 → children refused after 1–2, exit 2 in 1 of 3; limit 64 → after 16–17, exit 2 in 1 of 3;
   limit 128 → never refused. With `--ulimit nproc=32:32` (+ host ceiling 128 or 256): refused at 31, 6 of 6, no crashes.
2. **Smoke test flaw:** gVisor returns `EACCES` before `EROFS` for a root-owned path; test moved to world-writable `/var/tmp`.
3. **Service groups:** supervisor could not read `/etc/qp/sandboxd.json` (systemd does not add the user's primary
   group when `Group=` differs); it failed closed. Fixed with `SupplementaryGroups=qp-sandbox …`.
4. **Empty `/home` in the base image** counted as a host path; probe now reports folder contents.
5. **Key check:** listing models succeeded with the subscription ID (not the key); chat returned 401. Step 8 now sends a real chat request.
6. **Reasoning budget:** `qwen3.8-27b` (full reasoning) used 8,192, then 32,768, reasoning tokens and returned no content (206 s for 3 attempts).
7. **Verification feedback** (m2.3): retries now carry Layer 1–2 findings; end-to-end went from 24/26 to 26/26.

## 6. Model bake-off (`bakeoff-*.json`; real model → code → gVisor → Layers 1–2; 4 samples)

| Round | Configuration | First try | Valid (≤2 retries) | Values match source | Reconciles (clean) | Avg s |
|---|---|---|---|---|---|---|
| R1 ×1 | deepseek-v4.1-flash-1017@nothink | 4/4 | 4/4 | 100% | 3/3 | 6.0 |
| R1 ×1 | glm-5.3-flash@low | 2/4 | 4/4 | 100% | 3/3 | ~6 |
| R1 ×1 | minimax-m3.1-flash@low | 2/4 | 4/4 | 100% | 2/3 | ~8.5 |
| R1 ×1 | glm-5.3@low | 3/4 | 4/4 | 3 of 4 samples | 2/3 | ~8 |
| R1 ×1 | qwen3.8-27b@nothink | 2/4 | 2/4 | 100% of valid | 1/3 | 9.8 |
| R1 ×1 | qwen3.8-27b (full reasoning) | 0/1 | 0/1 | — | — | 206.6 |
| R2 ×3 | deepseek@nothink | 8/12 | 12/12 | 100% | 8/9 | 7.0 |
| R2 ×3 | glm-5.3-flash@low | 8/12 | 12/12 | 100% | 6/9 | 6.1 |
| m2.2 prompt ×3 | deepseek@nothink | 12/12 | 12/12 | 100% | 7/9 | 6.5 |
| m2.2 prompt ×3 | glm-5.3-flash@low | 11/12 | 12/12 | 100% | 7/9 | 5.0 |
| **m2.3 feedback ×3** | deepseek@nothink | 9/12 | 12/12 | 100% | 6/9 | 10.9 |
| **m2.3 feedback ×3** | **glm-5.3-flash@low** | 6/12 | 12/12 | 100% | **9/9** | 7.9 |

**Final configuration** (`/etc/qp-worker/models.env`): extractor `glm-5.3-flash@low`; fallback
`deepseek-v4.1-flash-1017@nothink`; injection classifier `glm-5.3-flash@low`; matcher and summary
`deepseek-v4.1-flash-1017@nothink`. Chosen on the m2.3 bake-off and confirmed by acceptance (5 of 6 runs with all
extraction checks passing vs 3 of 4 for deepseek).
