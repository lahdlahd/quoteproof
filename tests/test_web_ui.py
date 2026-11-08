"""UI redesign regression tests: new read-only pages, the summary route, form wiring, escaping and headers.
Requires fastapi/httpx (installed in the qp-web venv); skipped otherwise."""
import os
import re

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from qp import db  # noqa: E402
from tests.test_web import ROOT, _process_all, env, new_project, token, upload  # noqa: E402,F401


def _full_project(client, app, cfg, tmp):
    pid = new_project(client)
    for name, cur in (("ngn_quote.csv", "NGN"), ("ngn_quote.pdf", "NGN"), ("usd_quote.xlsx", "USD"),
                      ("poisoned_quote.xlsx", "USD")):
        assert upload(client, app, pid, name, open(os.path.join(ROOT, "fixtures", name), "rb").read(), cur).status_code == 303
    conn = _process_all(cfg, tmp)
    return pid, conn


def test_every_page_renders_escaped_with_security_headers(env):
    client, app, cfg, tmp = env
    pid, conn = _full_project(client, app, cfg, tmp)
    did = db.list_documents(conn, pid)[0]["id"]
    paths = ["/", "/app", "/new", "/comparisons", "/review-queue", "/containment", f"/p/{pid}", f"/p/{pid}/documents",
             f"/p/{pid}/verification", f"/p/{pid}/review", f"/p/{pid}/compare", f"/p/{pid}/report", f"/p/{pid}/doc/{did}"]
    for path in paths:
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-security-policy"] == ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                        "script-src 'self' 'unsafe-inline'; img-src 'self' data:"), path
        assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer", path
        assert "<b>x</b>" not in r.text, path            # project name "Fit-out <b>x</b>" is always escaped
        assert not re.search(r'(src|href)="https?://', r.text), path   # no third-party assets
        if path.startswith("/p/"):
            assert "Fit-out &lt;b&gt;x&lt;/b&gt;" in r.text, path


def test_every_post_form_carries_its_token_and_review_forms_carry_reviewer(env):
    client, app, cfg, tmp = env
    pid, conn = _full_project(client, app, cfg, tmp)
    t = token(app, pid)
    for path in (f"/p/{pid}", f"/p/{pid}/documents", f"/p/{pid}/review"):
        html = client.get(path).text
        forms = re.findall(r'<form[^>]*method="post"[^>]*>(.*?)</form>', html, re.S)
        assert forms, path
        for f in forms:
            assert f'name="t" value="{t}"' in f, path
    review = client.get(f"/p/{pid}/review").text
    assert 'id="rv"' in review
    for f in re.findall(r'<form[^>]*class="[^"]*\brv\b[^"]*"[^>]*>(.*?)</form>', review, re.S):
        assert 'name="reviewer"' in f and 'name="reason"' in f
    # the same actions as before the redesign are offered
    for action in ("accept", "dismiss", "confirm", "reject"):
        assert f'value="{action}"' in review
    c = client.get("/containment").text
    assert f'name="t" value="{token(app, "containment")}"' in c


def test_summary_is_read_only_and_scoped_to_one_comparison(env):
    client, app, cfg, tmp = env
    pid, conn = _full_project(client, app, cfg, tmp)
    s = client.get(f"/p/{pid}/summary").json()
    assert s["id"] == pid and s["currency"] == "NGN" and s["documents"] == 4
    assert set(s["assurance"]) >= {"source", "arithmetic", "equivalence", "headline", "open_findings"}
    assert s["security_open"] >= 1                     # the poisoned quote's injection findings
    assert s["assurance"]["headline"].startswith("Draft")
    assert client.get("/p/00000000-0000-4000-8000-000000000000/summary").status_code == 404
    assert client.get("/p/not-a-uuid/summary").status_code == 404
    assert client.post(f"/p/{pid}/summary").status_code == 405


def test_static_assets_served_same_origin(env):
    client, app, cfg, tmp = env
    for path, kind in (("/static/app.css", "text/css"), ("/static/app.js", "javascript"),
                       ("/static/fonts/Inter-var-latin.woff2", "font/woff2")):
        r = client.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"], path
    assert client.get("/static/../web.py").status_code == 404


def test_honest_labels_and_layers_on_workspace_pages(env):
    client, app, cfg, tmp = env
    pid, conn = _full_project(client, app, cfg, tmp)
    comp = client.get(f"/p/{pid}/compare").text
    for text in ("Source verification", "Arithmetic reconciliation", "Human-confirmed equivalence",
                 "Unknown amounts are never counted as zero", "Not comparable: different currency"):
        assert text in comp
    ver = client.get(f"/p/{pid}/verification").text
    assert "INJECTION_PATTERN" in ver and "Prompt-injection detection" in ver
    did = next(d["id"] for d in db.list_documents(conn, pid) if d["original_name"] == "ngn_quote.csv")
    doc = client.get(f"/p/{pid}/doc/{did}").text
    assert "Source text:" in doc                        # cited source text is visible, not hover-only
