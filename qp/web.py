"""qp-web: the internet-facing app (behind Caddy). FastAPI + server-rendered HTML.

Holds NEITHER the inference key NOR Docker access. It never parses uploaded files:
it streams the bytes to disk with a size cap, hashes them and checks the first
bytes against the claimed type (clarification 3). Everything else happens in
qp-worker and the sandbox; this process only queues jobs and shows results.

  uvicorn --host 127.0.0.1 --port 8080 qp.web:create_app --factory
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, layer2, pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TYPES = {"csv": "csv", "xlsx": "xlsx", "pdf": "pdf"}
CHUNK = 64 * 1024
DEFAULTS = {"max_upload_bytes": 10 * 1024 * 1024, "max_docs_per_project": 10, "max_queued_jobs": 50,
            "selftest_min_interval_s": 60, "rate_limit_per_minute": 120, "project_ttl_s": 24 * 3600}


def load_secret():
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    path = os.path.join(cred, "web_secret") if cred else os.environ.get("QP_WEB_SECRET_FILE")
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            s = f.read().strip()
        if len(s) >= 32:
            return s
    return secrets.token_bytes(32)  # dev fallback: tokens change on restart


def sniff_ok(declared, head):
    """Check magic bytes only; never parses the file."""
    if declared == "pdf":
        return b"%PDF-" in head[:1024]
    if declared == "xlsx":
        return head[:4] == b"PK\x03\x04"
    if declared == "csv":
        return b"\x00" not in head and not head.startswith((b"%PDF", b"PK\x03\x04", b"\x7fELF", b"MZ"))
    return False


def store_upload(uploads_dir, doc_id, fileobj, max_bytes):
    """Clarification 3: O_CREAT|O_EXCL|O_NOFOLLOW temp file, hash while streaming, size cap,
    fsync, atomic rename to /srv/qp/uploads/<doc_id>, mode 0640. Returns (sha256, size, head)."""
    if not UUID_RE.match(doc_id):
        raise ValueError("bad id")
    dir_fd = os.open(uploads_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    tmp_name = f".{doc_id}.part"
    h, size, head = hashlib.sha256(), 0, b""
    try:
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = fileobj.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"file larger than {max_bytes // (1024 * 1024)} MB")
                    if len(head) < 65536:
                        head += chunk[:65536 - len(head)]
                    h.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if size == 0:
                raise ValueError("empty file")
            os.rename(tmp_name, doc_id, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.chmod(doc_id, 0o640, dir_fd=dir_fd, follow_symlinks=False)
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(dir_fd)
    return h.hexdigest(), size, head


class RateLimiter:
    def __init__(self, per_minute):
        self.per_minute, self.hits, self.lock = per_minute, defaultdict(deque), threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def fmt_money(v):
    if v is None:
        return "Unknown"
    neg = v.startswith("-")
    whole, _, frac = v.lstrip("-").partition(".")
    whole = f"{int(whole):,}"
    return ("-" if neg else "") + whole + "." + (frac or "00").ljust(2, "0")[:2]


def create_app(config_path=None, config=None):
    cfg = dict(DEFAULTS)
    if config is None:
        with open(config_path or os.environ.get("QP_WEB_CONFIG", "/etc/qp-web/web.json"), encoding="utf-8") as f:
            config = json.load(f)
    cfg.update(config)
    secret = config.get("_secret") or load_secret()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # UI redesign: stylesheet, self-hosted font and icons. Same origin, read-only, so the CSP is unchanged.
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    env = Environment(loader=FileSystemLoader(os.path.join(HERE, "templates")),
                      autoescape=select_autoescape(["html"]))
    env.filters["money"] = fmt_money
    env.filters["fromjson"] = json.loads
    env.filters["ts"] = lambda t: time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(t)))
    env.globals["CURRENCY_SYMBOL"] = {"NGN": "₦", "USD": "$"}
    env.globals["SECURITY_CODES"] = SECURITY_CODES
    env.globals["PROCESSING_STATUSES"] = PROCESSING_STATUSES
    env.filters["expires_in"] = expires_in
    limiter = RateLimiter(cfg["rate_limit_per_minute"])
    state = {"last_purge": 0.0, "last_selftest": 0.0}
    local = threading.local()

    def conn():
        if getattr(local, "conn", None) is None:
            local.conn = db.connect(cfg["db"])
        return local.conn

    db.init(db.connect(cfg["db"]))

    def token(pid):
        return hmac.new(secret, pid.encode(), hashlib.sha256).hexdigest()[:32]

    def check_token(pid, t):
        if not t or not hmac.compare_digest(token(pid), t):
            raise HTTPException(403, "invalid form token")

    def render(name, request, **ctx):
        ctx.setdefault("reviewer", request.cookies.get("qp_reviewer", ""))
        ctx.setdefault("selftest_latest", db.latest_selftest(conn()))  # sidebar status line
        return HTMLResponse(env.get_template(name).render(request=request, **ctx),
                            headers={"Cache-Control": "no-store"})

    def project_or_404(pid):
        if not UUID_RE.match(pid):
            raise HTTPException(404)
        p = db.get_project(conn(), pid)
        if not p:
            raise HTTPException(404)
        return p

    def workspace(p):
        """Read-only context shared by the six comparison tabs: header, assurance strip, counts."""
        c = conn()
        docs, comp = pipeline.comparison_for(c, p)
        a = pipeline.assurance(c, p, docs, comp)
        all_docs = db.list_documents(c, p["id"])
        open_f = db.list_findings(c, p["id"], status="open")
        return {"p": p, "a": a, "comp": comp, "cdocs": {d["doc_id"]: d for d in docs}, "all_docs": all_docs,
                "open_findings": len([f for f in open_f if f["severity"] != "info"]),
                "security_open": len([f for f in open_f if f["code"] in SECURITY_CODES]),
                "token": token(p["id"])}

    def purge_expired():
        if time.time() - state["last_purge"] < 600:
            return
        state["last_purge"] = time.time()
        c = conn()
        for pid in db.expired_projects(c):
            for did in db.delete_project(c, pid):
                try:
                    os.unlink(os.path.join(cfg["uploads_dir"], did))
                except FileNotFoundError:
                    pass

    @app.middleware("http")
    async def guard(request: Request, call_next):
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
        if not limiter.allow(ip):
            return HTMLResponse("Too many requests. Please wait a minute.", status_code=429)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > cfg["max_upload_bytes"] + 1024 * 1024:
            return HTMLResponse("Upload too large.", status_code=413)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                   "script-src 'self' 'unsafe-inline'; img-src 'self' data:")
        return resp

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        purge_expired()
        return render("home.html", request, selftest=db.latest_selftest(conn()))

    @app.get("/app", response_class=HTMLResponse)
    def dashboard(request: Request):
        purge_expired()
        return render("app_dashboard.html", request, nav="dashboard", selftest=db.latest_selftest(conn()))

    @app.get("/new", response_class=HTMLResponse)
    def new_comparison(request: Request):
        return render("new.html", request, nav="new", max_mb=cfg["max_upload_bytes"] // (1024 * 1024),
                      max_docs=cfg["max_docs_per_project"])

    @app.get("/comparisons", response_class=HTMLResponse)
    def comparisons(request: Request):
        purge_expired()
        return render("comparisons.html", request, nav="comparisons")

    @app.get("/review-queue", response_class=HTMLResponse)
    def review_queue(request: Request):
        return render("review_queue.html", request, nav="queue")

    @app.post("/projects")
    def create_project(name: str = Form(..., max_length=120), currency: str = Form(...)):
        if currency not in ("NGN", "USD"):
            raise HTTPException(400, "currency must be NGN or USD")
        pid = db.create_project(conn(), name.strip() or "Untitled comparison", currency, cfg["project_ttl_s"])
        return RedirectResponse(f"/p/{pid}", status_code=303)

    @app.get("/p/{pid}", response_class=HTMLResponse)
    def project_page(request: Request, pid: str):
        p = project_or_404(pid)
        docs = db.list_documents(conn(), pid)
        return render("project.html", request, **workspace(p), docs=docs, tab="overview",
                      groups=db.match_groups(conn(), pid), expl=db.get_explanation(conn(), pid),
                      max_mb=cfg["max_upload_bytes"] // (1024 * 1024))

    @app.get("/p/{pid}/documents", response_class=HTMLResponse)
    def documents_page(request: Request, pid: str):
        p = project_or_404(pid)
        c = conn()
        docs = db.list_documents(c, pid)
        findings = db.list_findings(c, pid, status="open")
        per_doc = {d["id"]: len([f for f in findings if f["document_id"] == d["id"] and f["severity"] != "info"])
                   for d in docs}
        return render("documents.html", request, **workspace(p), docs=docs, per_doc=per_doc, tab="documents",
                      max_mb=cfg["max_upload_bytes"] // (1024 * 1024))

    @app.get("/p/{pid}/verification", response_class=HTMLResponse)
    def verification_page(request: Request, pid: str):
        p = project_or_404(pid)
        c = conn()
        docs = db.list_documents(c, pid)
        results = {d["id"]: db.get_results(c, d["id"]) for d in docs if d["status"] == "processed"}
        findings = db.list_findings(c, pid)
        return render("verification.html", request, **workspace(p), docs=docs, results=results,
                      findings=findings, tab="verification")

    @app.get("/p/{pid}/summary")
    def summary(pid: str):
        """Read-only figures for the browser-side comparison list. Same capability as the comparison page:
        only reachable with the comparison's own id; nothing here lists comparisons."""
        p = project_or_404(pid)
        w = workspace(p)
        docs = w["all_docs"]
        return JSONResponse({
            "id": p["id"], "name": p["name"], "currency": p["currency"], "created_at": p["created_at"],
            "expires_at": p["expires_at"], "documents": len(docs),
            "processing": sum(1 for d in docs if d["status"] in PROCESSING_STATUSES),
            "open_findings": w["open_findings"], "security_open": w["security_open"],
            "assurance": w["a"]}, headers={"Cache-Control": "no-store"})

    @app.post("/p/{pid}/upload")
    def upload(pid: str, file: UploadFile, currency: str = Form(...), t: str = Form(...)):
        p = project_or_404(pid)
        check_token(pid, t)
        if currency not in ("NGN", "USD"):
            raise HTTPException(400, "currency must be NGN or USD")
        c = conn()
        if len(db.list_documents(c, pid)) >= cfg["max_docs_per_project"]:
            raise HTTPException(400, f"at most {cfg['max_docs_per_project']} documents per comparison")
        if c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] >= cfg["max_queued_jobs"]:
            raise HTTPException(503, "the server is busy, please try again shortly")
        name = (file.filename or "upload").replace("\\", "/").split("/")[-1][:120]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in TYPES:
            raise HTTPException(400, "only .csv, .xlsx and .pdf files are accepted (macro workbooks are not)")
        doc_id = db.new_id()
        try:
            sha, size, head = store_upload(cfg["uploads_dir"], doc_id, file.file, cfg["max_upload_bytes"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not sniff_ok(TYPES[ext], head):
            os.unlink(os.path.join(cfg["uploads_dir"], doc_id))
            raise HTTPException(400, f"the file's content does not look like a .{ext} file")
        db.add_document(c, doc_id, pid, name, TYPES[ext], sha, size, currency)
        db.add_event(c, pid, None, doc_id, "upload", "received", {"bytes": size, "sha256": sha, "type": ext,
                                                                  "user_currency": currency})
        db.enqueue(c, "process", pid, doc_id)
        return RedirectResponse(f"/p/{pid}", status_code=303)

    @app.get("/p/{pid}/events")
    def events(pid: str, after: int = 0):
        project_or_404(pid)
        c = conn()
        docs = {d["id"]: d for d in db.list_documents(c, pid)}
        evs = db.events_after(c, pid, max(0, after))
        for e in evs:
            d = docs.get(e["document_id"])
            e["document"] = d["original_name"] if d else None
        return JSONResponse({"events": evs, "docs": [{"id": d["id"], "name": d["original_name"], "status": d["status"],
                                                       "supplier": d["supplier"]} for d in docs.values()],
                             "jobs_running": any(j["status"] in ("queued", "running") for j in db.project_jobs(c, pid))})

    @app.get("/p/{pid}/doc/{did}", response_class=HTMLResponse)
    def document_page(request: Request, pid: str, did: str):
        p = project_or_404(pid)
        d = db.get_document(conn(), did) if UUID_RE.match(did) else None
        if not d or d["project_id"] != pid:
            raise HTTPException(404)
        r = db.get_results(conn(), did)
        ex = db.get_extraction(conn(), did)
        findings = [f for f in db.list_findings(conn(), pid) if f["document_id"] == did]
        runs = [dict(x, envelope=json.loads(x["envelope"])) for x in conn().execute(
            "SELECT * FROM runs WHERE document_id=? ORDER BY id", (did,))]
        return render("document.html", request, **workspace(p), d=d, r=r, ex=ex, findings=findings, runs=runs,
                      tab="documents")

    @app.get("/p/{pid}/compare", response_class=HTMLResponse)
    def compare_page(request: Request, pid: str):
        p = project_or_404(pid)
        w = workspace(p)
        return render("compare.html", request, **w, docs=w["cdocs"], tab="results")

    @app.get("/p/{pid}/review", response_class=HTMLResponse)
    def review_page(request: Request, pid: str):
        p = project_or_404(pid)
        c = conn()
        docs = {d["id"]: d for d in db.list_documents(c, pid)}
        findings = db.list_findings(c, pid)
        results = {did: db.get_results(c, did) for did in docs}
        groups = db.match_groups(c, pid)
        items = []
        for did, r in results.items():
            if r and docs[did]["comparable"]:
                for ln in r["l2"]["lines"]:
                    items.append({"doc_id": did, "supplier": docs[did]["supplier"] or docs[did]["original_name"],
                                  "id": ln["id"], "description": ln["description"], "total": ln["line_total"]})
        return render("review.html", request, **workspace(p), docs=docs, findings=findings, results=results,
                      groups=groups, items=items, log=db.review_log(c, pid), tab="review")

    def reviewer_name(request, name):
        n = (name or request.cookies.get("qp_reviewer") or "").strip()[:60]
        if not n:
            raise HTTPException(400, "enter your name (self-declared) before reviewing")
        return n

    def back(pid, request, n, anchor=""):
        resp = RedirectResponse(f"/p/{pid}/review{anchor}", status_code=303)
        resp.set_cookie("qp_reviewer", n, max_age=7 * 24 * 3600, httponly=True, samesite="strict")
        return resp

    @app.post("/p/{pid}/review/finding/{fid}")
    def review_finding(request: Request, pid: str, fid: str, action: str = Form(...), reason: str = Form(""),
                       reviewer: str = Form(""), t: str = Form(...), value_str: str = Form(""), cite: str = Form(""),
                       user_entered: str = Form("")):
        project_or_404(pid)
        check_token(pid, t)
        n = reviewer_name(request, reviewer)
        c = conn()
        f = next((x for x in db.list_findings(c, pid) if x["id"] == fid), None)
        if not f:
            raise HTTPException(404)
        if action not in ("accept", "dismiss", "correct", "reopen", "exclude"):
            raise HTTPException(400, "unknown action")
        if not reason.strip():
            raise HTTPException(400, "a reason is required")
        after = {"status": {"accept": "accepted", "dismiss": "dismissed", "correct": "corrected", "reopen": "open",
                            "exclude": "corrected"}[action]}
        if action == "exclude":
            parts = (f["path"] or "").split(".")
            if not f["document_id"] or len(parts) != 2 or parts[0] not in ("line_items", "charges"):
                raise HTTPException(400, "only a whole line item or charge can be excluded")
            db.set_exclusion(c, f["document_id"], parts[1], reason, n)
            after.update(excluded=f["path"])
            db.enqueue(c, "reverify", pid, f["document_id"], {"reason": f"reviewer excluded {f['path']}"})
        if action == "correct":
            if not f["path"] or not f["document_id"] or (
                    f["path"].split(".")[0] in ("line_items", "charges") and f["path"].count(".") != 2):
                raise HTTPException(400, "this finding has no single value to correct")
            from . import money
            a = money.parse_amount(value_str)
            if not (a.ok or (f["path"].endswith(".description") and value_str.strip())):
                raise HTTPException(400, "enter a plain amount, e.g. 25000.00")
            kind = "user_entered" if user_entered else "corrected"
            if kind == "corrected" and not cite.strip():
                raise HTTPException(400, "a correction must cite a location, or be marked user-entered")
            db.set_override(c, f["document_id"], f["path"], value_str.strip(), kind, cite.strip() or None)
            after.update(value_str=value_str.strip(), kind=kind, cite=cite.strip() or None)
            db.enqueue(c, "reverify", pid, f["document_id"], {"reason": f"review correction of {f['path']}"})
        db.record_review(c, pid, "finding", fid, action, reason, {"status": f["status"], "detail": f["detail"]},
                         after, n)
        db.set_finding_status(c, fid, after["status"])
        return back(pid, request, n, "#findings")

    @app.post("/p/{pid}/review/match/{gid}")
    def review_match(request: Request, pid: str, gid: str, action: str = Form(...), reason: str = Form(""),
                     reviewer: str = Form(""), t: str = Form(...)):
        project_or_404(pid)
        check_token(pid, t)
        n = reviewer_name(request, reviewer)
        c = conn()
        g = next((x for x in db.match_groups(c, pid) if x["id"] == gid), None)
        if not g or action not in ("confirm", "reject", "reopen"):
            raise HTTPException(400)
        if not reason.strip():
            raise HTTPException(400, "a reason is required")
        status = {"confirm": "confirmed", "reject": "rejected", "reopen": "proposed"}[action]
        db.record_review(c, pid, "match", gid, action, reason, {"status": g["status"]}, {"status": status}, n)
        db.set_match_status(c, gid, status, reviewer=f"{n} (self-declared)")
        return back(pid, request, n, "#matches")

    @app.post("/p/{pid}/review/pair")
    async def manual_pair(request: Request, pid: str):
        project_or_404(pid)
        form = await request.form()
        check_token(pid, form.get("t"))
        n = reviewer_name(request, form.get("reviewer"))
        reason, label = (form.get("reason") or "").strip(), (form.get("label") or "").strip()[:120]
        if not reason or not label:
            raise HTTPException(400, "a label and a reason are required")
        c = conn()
        valid = {}
        for d in db.list_documents(c, pid):
            r = db.get_results(c, d["id"])
            if r and d["comparable"]:
                for ln in r["l2"]["lines"]:
                    valid[f"{d['id']}|{ln['id']}"] = (d["id"], ln["id"])
        taken = {(m["doc_id"], m["item_id"]) for g in db.match_groups(c, pid) if g["status"] != "rejected"
                 for m in g["members"]}
        members = [valid[k] for k in form.getlist("item") if k in valid]
        if len({m[0] for m in members}) < 2:
            raise HTTPException(400, "choose items from at least two suppliers")
        if any(m in taken for m in members):
            raise HTTPException(400, "an item is already in another group; reject that group first")
        gid = db.add_match_group(c, pid, label, [{"doc_id": a, "item_id": b} for a, b in members], source="human",
                                 status="confirmed", reason=reason)
        db.set_match_status(c, gid, "confirmed", reviewer=f"{n} (self-declared)")
        db.record_review(c, pid, "match", gid, "pair", reason, None, {"members": members, "label": label}, n)
        return back(pid, request, n, "#matches")

    @app.post("/p/{pid}/match")
    def rerun_matching(pid: str, t: str = Form(...)):
        project_or_404(pid)
        check_token(pid, t)
        db.enqueue(conn(), "match", pid)
        return RedirectResponse(f"/p/{pid}", status_code=303)

    @app.post("/p/{pid}/explain")
    def run_explain(pid: str, t: str = Form(...)):
        project_or_404(pid)
        check_token(pid, t)
        db.enqueue(conn(), "explain", pid)
        return RedirectResponse(f"/p/{pid}", status_code=303)

    @app.get("/p/{pid}/report", response_class=HTMLResponse)
    def report(request: Request, pid: str):
        p = project_or_404(pid)
        c = conn()
        w = workspace(p)
        return render("report.html", request, **w, docs=w["cdocs"], tab="report",
                      findings=db.list_findings(c, pid), log=db.review_log(c, pid), expl=db.get_explanation(c, pid),
                      selftest=db.latest_selftest(c), generated=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))

    @app.get("/containment", response_class=HTMLResponse)
    def containment(request: Request):
        st = db.latest_selftest(conn())
        running = conn().execute("SELECT COUNT(*) FROM jobs WHERE kind='selftest' AND status IN ('queued','running')"
                                 ).fetchone()[0]
        return render("containment.html", request, st=st, running=running, token=token("containment"), nav="containment")

    @app.post("/containment/run")
    def containment_run(t: str = Form(...)):
        check_token("containment", t)
        if time.time() - state["last_selftest"] < cfg["selftest_min_interval_s"]:
            raise HTTPException(429, "the self-test was started less than a minute ago")
        state["last_selftest"] = time.time()
        db.enqueue(conn(), "selftest")
        return RedirectResponse("/containment", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    app.state.cfg = cfg
    app.state.token = token
    return app


PROCESSING_STATUSES = ("uploaded", "indexing", "extracting")
SECURITY_CODES = ("INJECTION_PATTERN", "INJECTION_SUSPECTED", "GENERATED_CODE_SUSPICIOUS")


def expires_in(t):
    left = float(t) - time.time()
    if left <= 0:
        return "expired"
    h, m = int(left // 3600), int(left % 3600 // 60)
    return f"{h} h {m} min" if h else f"{m} min"


LABEL_CLASS = {layer2.QUOTED: "quoted", layer2.CALCULATED: "calc", layer2.ESTIMATED: "est", layer2.UNKNOWN: "unk"}
