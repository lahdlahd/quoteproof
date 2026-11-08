"""Milestone 2 acceptance, run on the VM as root after steps 7-8 (and optionally 9):

  sudo python3 -I /opt/qp/app/tools/m2_acceptance.py

S. Privilege separation across the four processes (on top of Milestone 1's checks).
E. A real end-to-end run through qp-web on 127.0.0.1:8080 with the real Vultr model and the real
   sandbox, checked against hand-worked answers. Ends with ALL CHECKS PASSED or the failing checks.
Evidence is saved to /root/qp-evidence/m2-<time>/.
"""
import http.cookiejar
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

APP = "/opt/qp/app"
BASE = "http://127.0.0.1:8080"
DB = "/srv/qp/db/qp.sqlite"
TS = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
EV = f"/root/qp-evidence/m2-{TS}"
RESULTS = []
SAMPLES = [("ngn_quote.csv", "NGN"), ("ngn_quote.pdf", "NGN"), ("usd_quote.xlsx", "USD"), ("poisoned_quote.xlsx", "USD")]


def check(cid, name, ok, evidence=None):
    RESULTS.append({"id": cid, "name": name, "result": "PASS" if ok else "FAIL", "evidence": evidence})
    print(f"{'PASS' if ok else 'FAIL'}  {cid:4} {name}")
    if not ok and evidence is not None:
        print("         evidence:", json.dumps(evidence, default=str)[:400])


def sh(cmd, user=None):
    if user:
        cmd = ["runuser", "-u", user, "--", *cmd]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def main_pid(unit):
    return sh(["systemctl", "show", "-p", "MainPID", "--value", unit])[1].strip()


def proc_user(pid):
    return sh(["ps", "-o", "user=", "-p", pid])[1].strip()


# ------------------------------------------------------------------ S: privileges
def section_s():
    print("\n== S. Four processes, four users, separated secrets")
    users = {u: proc_user(main_pid(u)) for u in ("qp-web", "qp-worker", "qp-sandboxd")}
    check("S1", "services run as qp-web / qp-worker / qp-sandbox",
          users == {"qp-web": "qp-web", "qp-worker": "qp-worker", "qp-sandboxd": "qp-sandbox"}, users)
    members = sh(["getent", "group", "docker"])[1].strip().split(":")[-1]
    check("S2", "docker group is still only qp-sandbox", members == "qp-sandbox", {"members": members})
    for cid, user in (("S3a", "qp-web"), ("S3b", "qp-worker")):
        rc, _, _ = sh(["cat", "/etc/qp-secrets/inference_key"], user=user)
        check(cid, f"{user} cannot read the inference key file", rc != 0, {"rc": rc})
    rc, _, _ = sh(["cat", "/run/credentials/qp-worker.service/inference_key"], user="qp-web")
    check("S4", "qp-web cannot read qp-worker's credential", rc != 0, {"rc": rc})
    key = open("/etc/qp-secrets/inference_key").read().strip()
    leaks = {}
    for unit in ("qp-web", "qp-worker", "qp-sandboxd"):
        try:
            leaks[unit] = key in open(f"/proc/{main_pid(unit)}/environ", "rb").read().decode(errors="replace")
        except OSError as e:
            leaks[unit] = f"unreadable: {e}"
    check("S5", "the key is in no process environment", leaks == {u: False for u in leaks}, leaks)
    rc, out, err = sh(["python3", "-I", f"{APP}/qpctl.py", "selftest"], user="qp-web")
    check("S6", "qp-web cannot reach the sandbox supervisor", "socket_permission_denied" in out, {"out": out[-200:]})
    rc, out, err = sh(["ls", "/srv/qp/uploads"], user="qp-worker")
    check("S7", "qp-worker cannot read raw uploads", rc != 0, {"rc": rc})
    rc, out, err = sh(["docker", "ps"], user="qp-worker")
    rc2, out2, err2 = sh(["docker", "ps"], user="qp-web")
    check("S8", "neither qp-web nor qp-worker can use Docker", rc != 0 and rc2 != 0, {"worker": rc, "web": rc2})
    listen = sh(["ss", "-ltnpH"])[1]
    web_pid = main_pid("qp-web")
    mine = [l.split()[3] for l in listen.splitlines() if f"pid={web_pid}," in l]
    check("S9", "qp-web listens on 127.0.0.1:8080 only", mine == ["127.0.0.1:8080"], {"listeners": mine})
    deny = sh(["systemctl", "show", "-p", "IPAddressDeny", "--value", "qp-web"])[1].strip()
    check("S10", "qp-web has no outbound network (IPAddressDeny)", deny not in ("", "[]"), {"IPAddressDeny": deny})


# ------------------------------------------------------------------ HTTP helpers
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(method, path, data=None, headers=None, follow=True):
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    opener = OPENER if follow else urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR), NoRedirect)
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), dict(e.headers)


def form(path, fields):
    return http("POST", path, urllib.parse.urlencode(fields, doseq=True).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"}, follow=False)


def multipart(path, fields, filename, content):
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return http("POST", path, b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={b}"}, follow=False)


def token_from(html):
    m = re.search(r'name="t" value="([0-9a-f]{32})"', html)
    return m.group(1) if m else None


def q(sql, *args):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def wait_jobs(pid, minutes):
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        pending = q("SELECT COUNT(*) n FROM jobs WHERE project_id=? AND status IN ('queued','running')", pid)[0]["n"]
        if pending == 0:
            return True
        time.sleep(3)
    return False


# ------------------------------------------------------------------ E: end to end
def section_e():
    print("\n== E. End-to-end through qp-web with the real model and the real sandbox")
    st, _, h = form("/projects", {"name": f"M2 acceptance {TS}", "currency": "NGN"})
    pid = (h.get("location") or h.get("Location") or "").rsplit("/", 1)[-1]
    check("E1", "create a comparison (NGN)", st == 303 and len(pid) == 36, {"status": st})
    st, html, _ = http("GET", f"/p/{pid}")
    tok = token_from(html)
    ids = {}
    for name, cur in SAMPLES:
        content = open(f"{APP}/fixtures/{name}", "rb").read()
        st, _, _ = multipart(f"/p/{pid}/upload", {"currency": cur, "t": tok}, name, content)
        ids[name] = st
    check("E2", "upload 4 sample quotes (CSV, PDF, XLSX, poisoned XLSX)", all(v == 303 for v in ids.values()), ids)
    print("         processing with the real model... (can take a few minutes)")
    t0 = time.time()
    done = wait_jobs(pid, 15)
    check("E3", f"all jobs finished ({int(time.time() - t0)} s)", done)
    docs = {d["original_name"]: d for d in q("SELECT * FROM documents WHERE project_id=?", pid)}
    check("E4", "all 4 documents processed", all(d["status"] == "processed" for d in docs.values()),
          {n: d["status"] for n, d in docs.items()})
    runs = q("SELECT r.kind, r.envelope FROM runs r JOIN documents d ON d.id=r.document_id WHERE d.project_id=?", pid)
    rts = {(r["kind"], json.loads(r["envelope"]).get("runtime")) for r in runs}
    mode = json.load(open("/etc/qp/sandboxd.json"))["isolation_mode"]
    want = "runsc" if mode == "gvisor" else "runc"
    check("E5", f"every indexer and extractor run used {want}", rts and all(rt == want for _, rt in rts), sorted(rts))

    def res(name):
        d = docs.get(name)
        r = q("SELECT * FROM results WHERE document_id=?", d["id"]) if d else []
        return (json.loads(r[0]["l1"]), json.loads(r[0]["l2"])) if r else (None, None)

    l1, l2 = res("ngn_quote.csv")
    check("E6", "CSV: all amounts source-verified; reconciles; grand total 1,585,625.00",
          bool(l1) and l1["summary"]["amounts_verified"] == l1["summary"]["amounts_total"] and l2["status"] == "RECONCILES"
          and l2["totals"]["grand_total_calculated"]["value"] == "1585625.00",
          {"l1": l1 and l1["summary"], "l2": l2 and l2["status"], "gt": l2 and l2["totals"]["grand_total_calculated"]})
    l1, l2 = res("ngn_quote.pdf")
    check("E7", "PDF: reconciles; grand total 2,500,000.00",
          bool(l2) and l2["status"] == "RECONCILES" and l2["totals"]["grand_total_calculated"]["value"] == "2500000.00",
          {"l1": l1 and l1["summary"], "l2": l2 and l2["status"]})
    l1, l2 = res("usd_quote.xlsx")
    hidden = bool(l1) and any(f["code"] == "HIDDEN_CONTENT" for f in l1["findings"])
    check("E8", "USD XLSX: kept out of the NGN comparison; stated subtotal 4,153.00; hidden row flagged",
          docs["usd_quote.xlsx"]["comparable"] == 0 and bool(l2) and l2["totals"]["stated_subtotal"]["value"] == "4153.00"
          and hidden, {"comparable": docs["usd_quote.xlsx"]["comparable"], "l2": l2 and l2["totals"]})
    pois = docs["poisoned_quote.xlsx"]
    f = q("SELECT code FROM findings WHERE document_id=?", pois["id"])
    check("E9", "poisoned quote: injection flagged and listed for review",
          any(x["code"] in ("INJECTION_PATTERN", "INJECTION_SUSPECTED") for x in f), sorted({x["code"] for x in f}))
    ev = q("SELECT step, status, detail FROM events WHERE project_id=?", pid)
    steps = {(e["step"], e["status"]) for e in ev}
    check("E10", "AI matching ran (proposals only, none auto-confirmed)",
          ("ai_matching", "done") in steps or ("ai_matching", "skipped") in steps,
          sorted(s for s in steps if s[0] == "ai_matching"))
    confirmed = q("SELECT COUNT(*) n FROM match_groups WHERE project_id=? AND status='confirmed'", pid)[0]["n"]
    check("E11", "no equivalence is confirmed without a human", confirmed == 0, {"confirmed": confirmed})
    pages = {p: http("GET", f"/p/{pid}{p}")[0] for p in ("", "/compare", "/review", "/report")}
    pages.update({f"/doc/{d['original_name']}": http("GET", f"/p/{pid}/doc/{d['id']}")[0] for d in docs.values()})
    check("E12", "all pages render", all(v == 200 for v in pages.values()), pages)
    rep = http("GET", f"/p/{pid}/report")[1]
    check("E13", "report: Draft gate, three counters, no-conversion statement",
          "Draft" in rep and "Source:" in rep and "Arithmetic:" in rep and "Equivalence:" in rep
          and "No currency conversion performed" in rep)

    # containment self-test through the web panel
    html = http("GET", "/containment")[1]
    st, _, _ = form("/containment/run", {"t": token_from(html)})
    deadline = time.time() + 120
    while time.time() < deadline and q("SELECT COUNT(*) n FROM jobs WHERE kind='selftest' AND status IN ('queued','running')")[0]["n"]:
        time.sleep(2)
    stt = q("SELECT * FROM selftests ORDER BY created_at DESC LIMIT 1")
    rep = json.loads(stt[0]["report"]) if stt else {}
    check("E14", "containment self-test from the web panel passes", st == 303 and rep.get("passed") is True,
          {t["test"]: t["verdict"] for t in rep.get("tests", [])})

    # AI summary: accepted only if every number is verified
    form(f"/p/{pid}/explain", {"t": tok})
    wait_jobs(pid, 3)
    ex = q("SELECT * FROM explanations WHERE project_id=?", pid)
    check("E15", "AI summary produced and number-checked (accepted or safely rejected)",
          bool(ex) and ex[0]["status"] in ("accepted", "rejected"), ex and {k: ex[0][k] for k in ("status", "model", "attempts")})
    os.makedirs(EV, exist_ok=True)
    json.dump({"project_id": pid, "events": ev, "docs": docs}, open(f"{EV}/run.json", "w"), default=str, indent=1)
    print(f"         project page: {BASE}/p/{pid}  (via your public URL: /p/{pid})")


def main():
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    os.makedirs(EV, exist_ok=True)
    print(f"QuoteProof Milestone 2 acceptance - {TS}")
    section_s()
    section_e()
    json.dump(RESULTS, open(f"{EV}/summary.json", "w"), indent=1, default=str)
    failed = [r for r in RESULTS if r["result"] != "PASS"]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed. Evidence: {EV}")
    if failed:
        print("MILESTONE 2 NOT ACCEPTED - failing checks:", ", ".join(r["id"] for r in failed))
        sys.exit(1)
    print("MILESTONE 2 ACCEPTANCE: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
