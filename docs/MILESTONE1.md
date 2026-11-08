# Milestone 1 runbook: VM, Docker, gVisor, sandbox supervisor, indexer

**Goal:** prove the highest-risk foundation on a real Vultr VM:
- Docker is installed, and only `qp-sandbox` can use it.
- gVisor is proven active (or we fall back to hardened runc and say so).
- `qp-sandboxd` exposes only three commands.
- The deterministic containment self-test passes.
- The trusted indexer works on CSV, XLSX and PDF inside the sandbox.

**Nothing is "done" until `tools/m1_acceptance.py` prints `ALL CHECKS PASSED` on the VM.**

Paste the output of every step back to Claude. Claude reports results only from that pasted output.

---

## 0. Provision the VM (after approval)

1. Vultr → Deploy → **Cloud Compute (Shared CPU) → High Performance**, **2 vCPU / 4 GB**, region **London**.
2. Image: **Ubuntu 24.04 LTS x64**. Add your SSH key. Auto-backups off. Hostname `quoteproof-1`.
3. Copy the code to the VM. Pick one:
   ```bash
   # option A: zip from Claude (on your laptop)
   scp quoteproof-m1.zip root@<VM_IP>:/root/
   ssh root@<VM_IP>
   apt-get update && apt-get install -y unzip && cd /root && unzip quoteproof-m1.zip
   # option B: git
   git clone <your-repo-url> /root/quoteproof
   ```
4. `cd /root/quoteproof`. All commands below are run from this folder, as root (or with `sudo`).

## 1. Host setup
```bash
bash deploy/01_host_setup.sh
```
**Expected:**
- `id` lines for `qp-web`, `qp-worker` and `qp-sandbox` showing their groups.
- `drwxr-s--- qp-web qp-files /srv/qp/uploads`
- `drwx--x--x qp-sandbox qp-sandbox /srv/qp/staging`
- `ufw` reports `Status: active` with only `OpenSSH` allowed.
- The last line is `STEP 1 DONE`.

If the script says a reboot is required, run `reboot`, reconnect, and continue.

## 2. Docker
```bash
bash deploy/02_install_docker.sh
```
**Expected:**
- `Docker server 29.x`
- `Hello from Docker!`
- `docker group members: qp-sandbox`
- The last line is `STEP 2 DONE`.

## 3. gVisor (starts the 90-minute timebox)
```bash
bash deploy/03_install_gvisor.sh
```
**Expected:**
- `runsc version release-2026…`
- `/etc/docker/daemon.json` contains `"runsc": {"path": "/usr/bin/runsc"}`
- The last line is `STEP 3 DONE`.

## 4. gVisor proof: tests (a), (b), (c)
```bash
bash deploy/04_gvisor_proof.sh
```
**Expected:**
```
PASS  (a) runsc registered in Docker
    host kernel:            6.8.0-xx-generic
    runc container kernel:  6.8.0-xx-generic
    runsc container kernel: 4.4.0            (or similar; must differ from host)
PASS  (b) sandbox kernel differs from host
    runsc dmesg:  [   0.000000] Starting gVisor...
    runc dmesg:   dmesg: read kernel buffer failed: Operation not permitted
PASS  (c) dmesg inside runsc shows gVisor
PASS  (c) dmesg under runc is refused
PASS  (smoke) non-root + read-only under runsc
RESULT: gVisor proof (a)(b)(c) PASSED
```
**If any line says FAIL, or the timebox passes 90 minutes:** stop and paste the output to Claude. Then use the fallback: in step 5 run `runc-hardened` instead of `gvisor`. From then on, nothing in the README, UI or video may mention gVisor.

## 5. Install code, build images, write config, run unit tests
```bash
bash deploy/05_install_code_and_build.sh gvisor      # or: runc-hardened (fallback only)
```
**Expected:**
- Two `sha256:…` image IDs.
- `/etc/qp/sandboxd.json` printed with `"isolation_mode": "gvisor"`.
- `55 passed, 2 skipped`. The skipped ones are the indexer unit tests: the indexer libraries are deliberately not installed on the host, and step 7 tests the indexer inside the sandbox instead.
- The last line is `STEP 5 DONE`.

## 6. Start the supervisor
```bash
bash deploy/06_install_sandboxd.sh
```
**Expected:**
- `Active: active (running)`
- A log line containing `"event": "startup_ok"`, `"runtime": "runsc"` and `"docker_runtimes": [..., "runsc"]`
- `srw-rw---- qp-sandbox qp-sock /run/qp-sandboxd/sandboxd.sock`
- `network listeners owned by qp-sandbox: none`
- The last line is `STEP 6 DONE`.

If gVisor is configured but missing, the service **refuses to start** and logs `FAIL CLOSED`. That is intended.

## 7. Acceptance tests
```bash
python3 -I /opt/qp/app/tools/m1_acceptance.py
```
Evidence is saved to `/root/qp-evidence/m1-<timestamp>/`. Expected end: `MILESTONE 1 ACCEPTANCE: ALL CHECKS PASSED`.

| ID | What it proves |
|---|---|
| A1 | The docker group contains only `qp-sandbox` |
| A2/A3 | `qp-web` and `qp-worker` get "permission denied" from Docker |
| A4 | `qp-web` cannot even reach the supervisor socket |
| A5 | `qp-worker` cannot read raw uploads |
| A6a/b | Neither can read the supervisor config |
| A7 | sandboxd runs as `qp-sandbox`, not root |
| A8 | sandboxd has no TCP/UDP listener |
| A9 | Socket permissions are 0660 |
| B1 | **Containment self-test, all PASS:** network blocked, secret isolation (canary), read-only filesystem (EROFS on a world-writable folder), process limit, 5-second timeout kill, privileges (uid 65534, no capabilities, NoNewPrivs), isolation runtime (gVisor identity), lifecycle cleanup |
| C1–C8 | The indexer, running in the sandbox, produces correct Source Indexes for CSV, XLSX (formulas, cached values, hidden rows, errors, external refs, missing cache) and PDF, and rejects macro and type-mismatch files |
| D1–D5 | Rejected: hash mismatch (no container started), unknown command, extra field, path traversal, planted symlink |
| E1 | A benign extractor runs and reads `/input/index.json` |
| E2 | A hostile extractor's network, secret, host-path and write attempts are all blocked |
| F1 | No leftover containers or staging folders |
| F2 | gVisor test (e): indexer output is byte-identical under runc and runsc, with timings |

## Fallback: hardened runc

```bash
bash deploy/05_install_code_and_build.sh runc-hardened && systemctl restart qp-sandboxd
python3 -I /opt/qp/app/tools/m1_acceptance.py
```
In this mode the self-test's `isolation_runtime` check reports `gvisor_active: false`, and F2 is skipped. Every surface must then say **"Isolation: Docker (runc) + hardening"**.

## Known risks to watch on the VM

These can only be confirmed there:

1. **Process limit under gVisor.** Docker's `--pids-limit` may not reach processes *inside* gVisor. The entrypoint also sets `RLIMIT_NPROC=64` as a backup. If B1's `process_limit` check fails under runsc, paste the output. It's a real finding, not something to hide.
2. **`--log-driver none` with attached output.** If containers return empty stdout, this flag is the first suspect.
3. **systemd hardening.** `RestrictAddressFamilies=AF_UNIX` is strict. If the Docker CLI fails inside the service, the journal will show it.
4. **Traverse-only staging folders (0711/0511).** If gVisor can't open `/input/document`, the indexer checks (C) will fail and we'll relax the folders to 0555.

## Residual risks (for the README)

- **sandboxd is root-equivalent.** It holds Docker access. This is mitigated by its tiny surface, no network, three fixed commands, peer-uid check and systemd hardening. Upgrade path: a separate sandbox VM.
- **Brief world-readable staged copies.** While a run is in progress, the staged copy is world-readable inside a non-listable folder with a random 128-bit name, and it's deleted when the run ends. This is needed so the container user (65534) can read it.
- **A parser exploit inside the indexer could forge its own index.** Damage is contained: no network, no secrets, container destroyed. Wrong data is caught by verification layers 2–3 (Milestone 2).
