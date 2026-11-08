# Milestone 2 runbook (VM)

Milestone 1 stays exactly as accepted: same supervisor, same images, same limits. Milestone 2 adds
qp-worker and qp-web under their own users, plus Caddy for HTTPS.

| Step | Command (on the VM, as root, in `/root/quoteproof`) | Success looks like |
|---|---|---|
| 7 | `bash deploy/07_install_m2.sh` | `schema ok`, `{"ok":true}` from `/healthz`, unit tests pass, `STEP 7 DONE` |
| 8 | `bash deploy/08_set_inference_key.sh` | hidden prompt; `Saved: root:root 600`; a model list; `INFERENCE CHECK OK`; qp-worker `active (running)`; `STEP 8 DONE` |
| bake-off | `QP_INFERENCE_KEY_FILE=/etc/qp-secrets/inference_key python3 -I /opt/qp/app/tools/model_bakeoff.py --models <ids> --runs 3` | a ranking table; evidence in `/root/qp-evidence/bakeoff-*.json` |
| models | edit `/etc/qp-worker/models.env`, then `bash deploy/07_install_m2.sh` again | worker restarts with the chosen IDs |
| 9 | `bash deploy/09_public_https.sh` (or `... yourdomain.com`) | `Public URL: https://<ip>.sslip.io/`, `STEP 9 DONE` |
| accept | `python3 -I /opt/qp/app/tools/m2_acceptance.py` | `MILESTONE 2 ACCEPTANCE: ALL CHECKS PASSED` |

## Who can do what (Milestone 2)

| Process | User | Has | Does not have |
|---|---|---|---|
| qp-web | qp-web | uploads (write), database | inference key, Docker, supervisor socket, indexes, **any outbound network** |
| qp-worker | qp-worker | inference key (systemd credential), supervisor socket, database, indexes | Docker, raw uploads |
| qp-sandboxd | qp-sandbox | Docker, uploads (read), staging | inference key, network listener |
| Caddy | caddy | ports 80/443 → 127.0.0.1:8080 | everything else |

## Residual risks (for the README)
* qp-web and qp-worker share one SQLite database. A compromised qp-web could alter stored results, but it
  still cannot reach the key, Docker or the sandbox. Post-hackathon: split read and write paths.
* The inference key can be read by root and by qp-worker at runtime. qp-worker is not internet-facing.
* Prompt-injection detection is best-effort; containment does not depend on it.
* AI output is nondeterministic. Every number is verified by trusted code, so a bad extraction shows up
  as findings, never as a silently wrong total.
