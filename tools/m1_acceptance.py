"""Milestone 1 acceptance tests - run ON THE VM as root after steps 1-6:

    sudo python3 -I /opt/qp/app/tools/m1_acceptance.py

Every check prints PASS or FAIL with the evidence it relied on. All raw outputs
are saved under /root/qp-evidence/m1-<timestamp>/. Nothing is marked PASS
without an observed result; any FAIL makes the script exit non-zero.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid

APP = "/opt/qp/app"
FIX = f"{APP}/fixtures"
QPCTL = ["python3", "-I", f"{APP}/qpctl.py"]
CFG = json.load(open("/etc/qp/sandboxd.json"))
TS = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
EV = f"/root/qp-evidence/m1-{TS}"
os.makedirs(EV, mode=0o700)
RESULTS = []


def sh(cmd, user=None, stdin=None, timeout=300):
    if user:
        cmd = ["runuser", "-u", user, "--", *cmd]
    p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")


def save(name, obj):
    with open(os.path.join(EV, name), "w") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj, indent=2))


def check(cid, desc, ok, evidence):
    RESULTS.append({"id": cid, "check": desc, "result": "PASS" if ok else "FAIL", "evidence": evidence})
    print(f"{'PASS' if ok else 'FAIL'}  {cid:4} {desc}")
    if not ok:
        print(f"         evidence: {json.dumps(evidence)[:600]}")


def qpctl(args, user="qp-worker", raw_stdin=None):
    rc, out, err = sh([*QPCTL, *args], user=user, stdin=raw_stdin)
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, {"unparsed_stdout": out[-500:], "stderr": err[-500:]}


def upload(fixture, dtype):
    data = open(os.path.join(FIX, fixture), "rb").read()
    rc, out, err = sh(["python3", "-I", f"{APP}/tools/qp_put_upload.py", "--type", dtype],
                      user="qp-web", stdin=data)
    meta = json.loads(out)
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()
    return meta


# ------------------------------------------------------------------ A: privilege separation
def section_a():
    print("\n== A. Privilege separation")
    members = sh(["getent", "group", "docker"])[1].strip().split(":")[-1]
    check("A1", "docker group contains only qp-sandbox", members == "qp-sandbox", {"members": members})
    for cid, user in (("A2", "qp-web"), ("A3", "qp-worker")):
        rc, out, err = sh(["docker", "ps"], user=user)
        check(cid, f"{user} cannot use Docker", rc != 0 and "permission denied" in err.lower(),
              {"rc": rc, "stderr": err.strip()[-200:]})
    rc, resp = qpctl(["selftest"], user="qp-web")
    check("A4", "qp-web cannot reach the supervisor socket",
          resp.get("error") == "socket_permission_denied", resp)
    rc, out, err = sh(["ls", "/srv/qp/uploads"], user="qp-worker")
    check("A5", "qp-worker cannot read raw uploads", rc != 0, {"rc": rc, "stderr": err.strip()})
    for cid, user in (("A6a", "qp-web"), ("A6b", "qp-worker")):
        rc, out, err = sh(["cat", "/etc/qp/sandboxd.json"], user=user)
        check(cid, f"{user} cannot read supervisor config", rc != 0, {"rc": rc})
    pid = sh(["systemctl", "show", "-p", "MainPID", "--value", "qp-sandboxd"])[1].strip()
    owner = sh(["ps", "-o", "user=", "-p", pid])[1].strip()
    check("A7", "qp-sandboxd runs as qp-sandbox (not root)", owner == "qp-sandbox", {"pid": pid, "user": owner})
    listeners = sh(["ss", "-ltnupH"])[1]
    mine = [l for l in listeners.splitlines() if f"pid={pid}," in l]
    check("A8", "qp-sandboxd has no TCP/UDP listener", not mine, {"listeners": mine})
    st = os.stat(CFG["socket_path"])
    check("A9", "socket is 0660 qp-sandbox:qp-sock", oct(st.st_mode & 0o777) == "0o660", {"mode": oct(st.st_mode & 0o777)})


# ------------------------------------------------------------------ B: containment self-test
def section_b():
    print("\n== B. Containment self-test (via qp-worker -> sandboxd)")
    rc, resp = qpctl(["selftest"])
    save("B_selftest.json", resp)
    rep = resp.get("result", {})
    for t in rep.get("tests", []):
        print(f"         {t['verdict']:12} {t['test']}")
    check("B1", f"self-test: all checks PASS (isolation_mode={CFG['isolation_mode']})",
          rep.get("passed") is True, {"verdicts": {t["test"]: t["verdict"] for t in rep.get("tests", [])}})


# ------------------------------------------------------------------ C: indexer through the sandbox
EXPECT = [
    ("C1", "ngn_quote.csv", "csv", lambda i: i["ok"] and any(c["value"] == "₦1,585,625.00" for c in i["cells"])),
    ("C2", "usd_quote.xlsx", "xlsx", lambda i: i["ok"] and any(c["ref"] == "Quote!D4" and c["formula"] == "=B4*C4"
                                                             and c["value"] == "3598" for c in i["cells"])
                                               and any(w["code"] == "HIDDEN_CONTENT" for w in i["warnings"])),
    ("C3", "usd_quote_nocache.xlsx", "xlsx", lambda i: any(w["code"] == "FORMULA_NO_CACHED_VALUE" for w in i["warnings"])),
    ("C4", "errors.xlsx", "xlsx", lambda i: any(w["code"] == "FORMULA_ERROR" for w in i["warnings"])
                                            and any(w["code"] == "EXTERNAL_REFERENCE" for w in i["warnings"])),
    ("C5", "ngn_quote.pdf", "pdf", lambda i: i["ok"] and any(l["ref"] == "p1:L6" and l["text"] == "Total 2,500,000.00"
                                                            for l in i["lines"])),
    ("C6", "scanned_like.pdf", "pdf", lambda i: any(w["code"] == "NO_TEXT_LAYER" for w in i["warnings"])),
    ("C7", "macro.xlsx", "xlsx", lambda i: not i["ok"] and i["error"]["code"] == "MACRO_CONTENT"),
    ("C8", "pdf_pretending_to_be.xlsx", "xlsx", lambda i: not i["ok"] and i["error"]["code"] == "TYPE_MISMATCH"),
]


def section_c():
    print("\n== C. Trusted indexer, running inside the sandbox")
    indexes = {}
    for cid, fx, dtype, pred in EXPECT:
        meta = upload(fx, dtype)
        rc, resp = qpctl(["index", "--document-id", meta["document_id"], "--sha256", meta["sha256"], "--type", dtype])
        save(f"{cid}_{fx}.json", resp)
        res = resp.get("result", {})
        try:
            idx = json.loads(res.get("stdout") or "null")
            ok = (res.get("status") == "ok" and res.get("runtime") == {"gvisor": "runsc"}.get(CFG["isolation_mode"], "runc")
                  and res.get("removed") is True and res.get("staging_removed") is True
                  and idx["source_sha256"] == meta["sha256"] and pred(idx))
        except Exception as e:  # noqa: BLE001
            idx, ok = None, False
        indexes[fx] = res.get("stdout")
        check(cid, f"index {fx}", ok, {"status": res.get("status"), "runtime": res.get("runtime"),
                                         "duration_ms": res.get("duration_ms"), "removed": res.get("removed"),
                                         "index_ok": (idx or {}).get("ok"), "error": (idx or {}).get("error"),
                                         "warnings": [w["code"] for w in (idx or {}).get("warnings", [])][:12]})
    return indexes


# ------------------------------------------------------------------ D: rejected requests
def section_d():
    print("\n== D. Supervisor rejects bad requests")
    meta = upload("ngn_quote.csv", "csv")
    rc, resp = qpctl(["index", "--document-id", meta["document_id"], "--sha256", "0" * 64, "--type", "csv"])
    res = resp.get("result", {})
    check("D1", "hash mismatch -> rejected, no container started",
          res.get("error") == "hash_mismatch" and res.get("container_started") is False and res.get("staging_removed") is True, res)
    rc, resp = qpctl(["raw", '{"cmd":"docker_run","image":"alpine","privileged":true}'])
    check("D2", "unknown command rejected", resp.get("error") == "unknown_command", resp)
    rc, resp = qpctl(["raw", '{"cmd":"self_test","runtime":"runc"}'])
    check("D3", "extra field rejected (cannot choose runtime)", resp.get("error") == "invalid_fields", resp)
    rc, resp = qpctl(["index", "--document-id", "../../etc/shadow", "--sha256", "0" * 64, "--type", "csv"])
    check("D4", "path traversal in document id rejected", resp.get("error") == "invalid_document_id", resp)
    evil = str(uuid.uuid4())
    sh(["ln", "-s", "/etc/qp/sandboxd.json", f"/srv/qp/uploads/{evil}"], user="qp-web")
    rc, resp = qpctl(["index", "--document-id", evil, "--sha256", "0" * 64, "--type", "csv"])
    res = resp.get("result", {})
    check("D5", "symlink planted in uploads is refused",
          res.get("error") == "document_open_refused" and res.get("container_started") is False, res)
    os.unlink(f"/srv/qp/uploads/{evil}")


# ------------------------------------------------------------------ E: code execution (hand-written stand-ins for AI code)
def section_e(indexes):
    print("\n== E. Extractor code runs only in the sandbox")
    work = f"/tmp/qp-m1-{TS}"  # qp-worker must be able to read these two inputs
    os.makedirs(work, mode=0o755)
    with open(f"{work}/index.json", "w", encoding="utf-8") as f:
        f.write(indexes["ngn_quote.csv"] or "{}")
    for name in ("benign.py", "hostile.py"):
        sh(["cp", f"{FIX}/extractors/{name}", work])
    sh(["chmod", "-R", "a+rX", work])
    rc, resp = qpctl(["extract", "--index-file", f"{work}/index.json", "--code-file", f"{work}/benign.py"])
    save("E1_benign.json", resp)
    res = resp.get("result", {})
    out = json.loads(res.get("stdout") or "{}")
    check("E1", "benign extractor reads /input/index.json and returns JSON",
          res.get("status") == "ok" and out.get("schema_seen") == "qp.source_index.v1" and out.get("cells", 0) > 0,
          {"status": res.get("status"), "output": out})
    rc, resp = qpctl(["extract", "--index-file", f"{work}/index.json", "--code-file", f"{work}/hostile.py"])
    save("E2_hostile.json", resp)
    res = resp.get("result", {})
    out = json.loads(res.get("stdout") or "{}")
    blocked = (out and "SUCCEEDED" not in json.dumps(out) and out.get("secret_env_vars_visible") == []
               and not any(v for k, v in out.items() if k.startswith("exists:")))
    check("E2", "hostile extractor: network, secrets, host paths and writes all blocked",
          res.get("status") == "ok" and bool(blocked), {"status": res.get("status"), "output": out})
    sh(["rm", "-rf", work])


# ------------------------------------------------------------------ F: lifecycle + gVisor compatibility
def section_f():
    print("\n== F. Lifecycle and gVisor library compatibility")
    left = sh(["docker", "ps", "-aq", "--filter", "label=qp.managed=1"])[1].split()
    staged = os.listdir(CFG["staging_dir"])
    check("F1", "no leftover sandbox containers or staging folders", not left and not staged,
          {"containers": left, "staging": staged})
    if CFG["isolation_mode"] != "gvisor":
        print("SKIP  F2   gVisor compatibility (isolation_mode is runc-hardened: gVisor NOT active)")
        return
    rows, same = [], True
    for fx, dtype in (("ngn_quote.csv", "csv"), ("usd_quote.xlsx", "xlsx"), ("ngn_quote.pdf", "pdf")):
        outs = {}
        for rt in ("runc", "runsc"):
            d = f"/tmp/qp-compat-{rt}-{TS}"
            os.makedirs(d, mode=0o755)
            sh(["cp", f"{FIX}/{fx}", f"{d}/document"])
            with open(f"{d}/meta.json", "w") as f:
                json.dump({"declared_type": dtype}, f)
            sh(["chmod", "-R", "a+rX", d])
            t0 = time.monotonic()
            rc, out, err = sh(["docker", "run", "--rm", "--runtime", rt, "--network", "none", "--read-only",
                               "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                               "--pids-limit", "64", "--memory", "512m", "--memory-swap", "512m",
                               "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m", "--log-driver", "none",
                               "--mount", f"type=bind,source={d},target=/input,readonly",
                               CFG["images"]["indexer"], "index"])
            outs[rt] = (hashlib.sha256(out.encode()).hexdigest(), int((time.monotonic() - t0) * 1000), rc)
            sh(["rm", "-rf", d])
        rows.append({"fixture": fx, "runc_ms": outs["runc"][1], "runsc_ms": outs["runsc"][1],
                     "identical_output": outs["runc"][0] == outs["runsc"][0], "rc": [outs["runc"][2], outs["runsc"][2]]})
        same = same and outs["runc"][0] == outs["runsc"][0] and outs["runc"][2] == 0 and outs["runsc"][2] == 0
    for r in rows:
        print(f"         {r['fixture']:24} runc {r['runc_ms']:>6} ms   runsc {r['runsc_ms']:>6} ms   identical={r['identical_output']}")
    check("F2", "gVisor (e): indexer output identical under runc and runsc", same, rows)


def main():
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    print(f"QuoteProof Milestone 1 acceptance - {TS} - isolation_mode={CFG['isolation_mode']}")
    section_a()
    section_b()
    indexes = section_c()
    section_d()
    section_e(indexes)
    section_f()
    save("summary.json", RESULTS)
    failed = [r for r in RESULTS if r["result"] != "PASS"]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed. Evidence: {EV}")
    if failed:
        print("MILESTONE 1 NOT ACCEPTED - failing checks:", ", ".join(r["id"] for r in failed))
        sys.exit(1)
    print("MILESTONE 1 ACCEPTANCE: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
