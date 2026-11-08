"""T9: qp-web. Requires fastapi/httpx (installed in the qp-web venv); skipped otherwise."""
import json
import os
import re
import stat

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from qp import db, web, worker  # noqa: E402
from tests.test_pipeline import FakeSandbox, RoleLLM  # noqa: E402
from qp import pipeline  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def env(tmp_path):
    up = tmp_path / "uploads"
    up.mkdir()
    (tmp_path / "indexes").mkdir()
    cfg = {"db": str(tmp_path / "qp.sqlite"), "uploads_dir": str(up), "_secret": b"s" * 32,
           "rate_limit_per_minute": 1000, "selftest_min_interval_s": 0}
    app = web.create_app(config=cfg)
    return TestClient(app), app, cfg, tmp_path


def token(app, pid):
    return app.state.token(pid)


def new_project(client, cur="NGN"):
    r = client.post("/projects", data={"name": "Fit-out <b>x</b>", "currency": cur}, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].split("/")[-1]


def upload(client, app, pid, name, data, cur="NGN"):
    return client.post(f"/p/{pid}/upload", data={"currency": cur, "t": token(app, pid)},
                       files={"file": (name, data)}, follow_redirects=False)


def test_upload_is_stored_by_id_with_hash_and_mode(env):
    client, app, cfg, tmp = env
    pid = new_project(client)
    data = open(os.path.join(ROOT, "fixtures", "ngn_quote.csv"), "rb").read()
    r = upload(client, app, pid, "../../etc/passwd.csv", data)
    assert r.status_code == 303
    conn = db.connect(cfg["db"])
    d = db.list_documents(conn, pid)[0]
    assert d["original_name"] == "passwd.csv"                        # path parts stripped, display only
    stored = os.listdir(cfg["uploads_dir"])
    assert stored == [d["id"]]                                       # filename never used in the path
    st = os.stat(os.path.join(cfg["uploads_dir"], d["id"]))
    assert stat.S_IMODE(st.st_mode) == 0o640
    import hashlib
    assert d["sha256"] == hashlib.sha256(data).hexdigest()
    assert db.claim_next(conn)["kind"] == "process"


@pytest.mark.parametrize("name,data,code", [
    ("q.xlsm", b"PK\x03\x04xx", 400),                 # macro workbook extension
    ("q.exe", b"MZ....", 400),
    ("q.xlsx", b"%PDF-1.7 fake", 400),                 # magic bytes do not match
    ("q.pdf", b"PK\x03\x04", 400),
    ("q.csv", b"a,b\x00,c", 400),                      # binary disguised as CSV
    ("q.csv", b"", 400),
])
def test_upload_rejections(env, name, data, code):
    client, app, cfg, tmp = env
    pid = new_project(client)
    assert upload(client, app, pid, name, data).status_code == code
    assert os.listdir(cfg["uploads_dir"]) == []                      # nothing left behind


def test_upload_size_cap(env):
    client, app, cfg, tmp = env
    app.state.cfg["max_upload_bytes"] = 1000
    pid = new_project(client)
    assert upload(client, app, pid, "big.csv", b"a," * 2000).status_code in (400, 413)
    assert os.listdir(cfg["uploads_dir"]) == []


def test_forms_need_token_and_bad_ids_404(env):
    client, app, cfg, tmp = env
    pid = new_project(client)
    r = client.post(f"/p/{pid}/upload", data={"currency": "NGN", "t": "wrong"}, files={"file": ("a.csv", b"a,b")})
    assert r.status_code == 403
    assert client.get("/p/not-a-uuid").status_code == 404
    assert client.get("/p/00000000-0000-4000-8000-000000000000").status_code == 404


def test_html_is_escaped_and_security_headers(env):
    client, app, cfg, tmp = env
    pid = new_project(client)
    r = client.get(f"/p/{pid}")
    assert "Fit-out &lt;b&gt;x&lt;/b&gt;" in r.text and "<b>x</b>" not in r.text
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]


def _process_all(cfg, tmp):
    conn = db.connect(cfg["db"])
    by_sha = {}
    for name in ("ngn_quote.csv", "ngn_quote.pdf", "usd_quote.xlsx", "poisoned_quote.xlsx"):
        ij = open(os.path.join(ROOT, "fixtures", "indexes", name + ".json"), encoding="utf-8").read()
        by_sha[json.loads(ij)["source_sha256"]] = ij
    ctx = pipeline.Ctx(conn, FakeSandbox(tmp, by_sha), RoleLLM(),
                       {"indexes_dir": str(tmp / "indexes"),
                        "models": {"extractor": "e", "classifier": "c", "matcher": "m", "explainer": "x"}})
    while worker.run_one(ctx):
        pass
    return conn


def test_full_flow_pages_review_and_report(env):
    client, app, cfg, tmp = env
    pid = new_project(client)
    for name, cur in (("ngn_quote.csv", "NGN"), ("ngn_quote.pdf", "NGN"), ("usd_quote.xlsx", "USD"),
                      ("poisoned_quote.xlsx", "USD")):
        assert upload(client, app, pid, name, open(os.path.join(ROOT, "fixtures", name), "rb").read(), cur).status_code == 303
    conn = _process_all(cfg, tmp)
    ev = client.get(f"/p/{pid}/events?after=0").json()
    assert any(e["step"] == "sandbox_run_extractor" for e in ev["events"]) and ev["jobs_running"] is False
    assert any(e["step"] == "injection_check" and e["status"] == "flagged" for e in ev["events"])

    for path in ("", "/compare", "/review", "/report"):
        r = client.get(f"/p/{pid}{path}")
        assert r.status_code == 200, path
    docs = db.list_documents(conn, pid)
    for d in docs:
        assert client.get(f"/p/{pid}/doc/{d['id']}").status_code == 200
    rep = client.get(f"/p/{pid}/report").text
    assert "Draft" in rep and "No currency conversion performed" in rep and "Not comparable: different currency" in rep

    # confirm the AI proposal: reason required
    g = db.match_groups(conn, pid)[0]
    t = token(app, pid)
    bad = client.post(f"/p/{pid}/review/match/{g['id']}", data={"action": "confirm", "reason": "", "reviewer": "Ada", "t": t})
    assert bad.status_code == 400
    ok = client.post(f"/p/{pid}/review/match/{g['id']}", data={"action": "confirm", "reason": "same spec", "reviewer": "Ada",
                                                              "t": t}, follow_redirects=False)
    assert ok.status_code == 303
    assert db.match_groups(conn, pid)[0]["confirmed_by"] == "Ada (self-declared)"
    comp = client.get(f"/p/{pid}/compare").text
    assert "Confirmed by Ada (self-declared)" in comp and "Equivalence: 2/4 items confirmed" in comp

    # correct a value as user-entered -> Estimated after reverify
    f = next(x for x in db.list_findings(conn, pid, status="open") if x["code"] == "INJECTION_PATTERN")
    r = client.post(f"/p/{pid}/review/finding/{f['id']}", data={"action": "accept", "reason": "known poisoned sample",
                                                               "reviewer": "Ada", "t": t}, follow_redirects=False)
    assert r.status_code == 303
    log = db.review_log(conn, pid)
    assert [x["action"] for x in log] == ["confirm", "accept"] and all(x["reason"] for x in log)

    # manual pair across suppliers
    csv_id = next(d["id"] for d in docs if d["original_name"] == "ngn_quote.csv")
    pdf_id = next(d["id"] for d in docs if d["original_name"] == "ngn_quote.pdf")
    r = client.post(f"/p/{pid}/review/pair", data={"t": t, "reviewer": "Ada", "label": "Item 2", "reason": "test",
                                                   "item": [f"{csv_id}|L2", f"{pdf_id}|L2"]}, follow_redirects=False)
    assert r.status_code == 303
    assert any(g["source"] == "human" and g["status"] == "confirmed" for g in db.match_groups(conn, pid))


def test_correction_requires_cite_or_user_entered(env):
    client, app, cfg, tmp = env
    pid = new_project(client)
    upload(client, app, pid, "ngn_quote.csv", open(os.path.join(ROOT, "fixtures", "ngn_quote.csv"), "rb").read())
    conn = _process_all(cfg, tmp)
    did = db.list_documents(conn, pid)[0]["id"]
    db.sync_findings(conn, pid, did, "L1", [{"code": "UNCITED", "path": "charges.C1.amount", "detail": "x"}])
    fid = next(f["id"] for f in db.list_findings(conn, pid) if f["code"] == "UNCITED")
    t = token(app, pid)
    base = {"action": "correct", "reason": "phoned supplier", "reviewer": "Ada", "t": t, "value_str": "25000.00"}
    assert client.post(f"/p/{pid}/review/finding/{fid}", data=base).status_code == 400
    r = client.post(f"/p/{pid}/review/finding/{fid}", data={**base, "user_entered": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.get_overrides(conn, did)["charges.C1.amount"]["kind"] == "user_entered"
    assert any(j["kind"] == "reverify" for j in db.project_jobs(conn, pid))


def test_containment_panel_and_rate_limit(env):
    client, app, cfg, tmp = env
    assert client.get("/containment").status_code == 200
    t = token(app, "containment")
    assert client.post("/containment/run", data={"t": t}, follow_redirects=False).status_code == 303
    app.state.cfg["selftest_min_interval_s"] = 60
    assert client.post("/containment/run", data={"t": t}).status_code == 429


def test_web_module_never_imports_inference_or_docker():
    src = open(os.path.join(ROOT, "qp", "web.py"), encoding="utf-8").read()
    assert not re.search(r"^\s*(from|import)\s+.*\b(inference|sandbox|docker|subprocess)\b", src, re.M)


def test_exclude_action_records_reason_and_reverifies(env):
    client, app, cfg, tmp = env
    pid = new_project(client)
    upload(client, app, pid, "ngn_quote.csv", open(os.path.join(ROOT, "fixtures", "ngn_quote.csv"), "rb").read())
    conn = _process_all(cfg, tmp)
    did = db.list_documents(conn, pid)[0]["id"]
    db.sync_findings(conn, pid, did, "L2", [{"code": "LINE_WITHOUT_AMOUNTS", "path": "line_items.L9", "detail": "Note"}])
    fid = next(f["id"] for f in db.list_findings(conn, pid) if f["code"] == "LINE_WITHOUT_AMOUNTS")
    assert "Not a line item" in client.get(f"/p/{pid}/review").text
    t = token(app, pid)
    assert client.post(f"/p/{pid}/review/finding/{fid}", data={"action": "exclude", "reason": "", "reviewer": "Ada",
                                                              "t": t}).status_code == 400
    r = client.post(f"/p/{pid}/review/finding/{fid}", data={"action": "exclude", "reason": "note row",
                                                           "reviewer": "Ada", "t": t}, follow_redirects=False)
    assert r.status_code == 303 and "L9" in db.get_exclusions(conn, did)
    assert db.review_log(conn, pid)[-1]["action"] == "exclude"
    assert any(j["kind"] == "reverify" for j in db.project_jobs(conn, pid))
