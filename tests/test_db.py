"""T2: SQLite data layer and job queue."""
import multiprocessing
import os
import sqlite3

import pytest

from qp import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "qp.sqlite"))
    db.init(c)
    return c


def test_currency_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.create_project(conn, "x", "EUR")


def test_queue_fifo_and_claim(conn):
    p = db.create_project(conn, "p", "NGN")
    j1 = db.enqueue(conn, "process", p)
    j2 = db.enqueue(conn, "selftest")
    assert db.claim_next(conn)["id"] == j1
    assert db.claim_next(conn)["id"] == j2
    assert db.claim_next(conn) is None
    db.finish_job(conn, j1)
    assert db.get_job(conn, j1)["status"] == "done"


def _claimer(path, n, q):
    c = db.connect(path)
    got = []
    while True:
        j = db.claim_next(c)
        if j is None:
            break
        got.append(j["id"])
    q.put(got)


def test_two_workers_never_claim_the_same_job(tmp_path):
    path = str(tmp_path / "qp.sqlite")
    c = db.connect(path)
    db.init(c)
    ids = {db.enqueue(c, "selftest") for _ in range(60)}
    q = multiprocessing.Queue()
    procs = [multiprocessing.Process(target=_claimer, args=(path, i, q)) for i in range(3)]
    for p in procs:
        p.start()
    results = [q.get(timeout=30) for _ in procs]
    for p in procs:
        p.join(10)
    claimed = [j for r in results for j in r]
    assert len(claimed) == len(set(claimed)) == 60 and set(claimed) == ids


def test_restart_recovery_marks_running_failed(conn):
    p = db.create_project(conn, "p", "USD")
    j = db.enqueue(conn, "process", p)
    db.claim_next(conn)
    assert db.recover_running(conn) == [j]
    job = db.get_job(conn, j)
    assert job["status"] == "failed" and "worker restarted" in job["error"]
    assert db.events_after(conn, p)[-1]["status"] == "failed"


def test_unknown_job_kind_and_document_field_rejected(conn):
    with pytest.raises(ValueError):
        db.enqueue(conn, "shell")
    p = db.create_project(conn, "p", "NGN")
    db.add_document(conn, "d1", p, "a.csv", "csv", "0" * 64, 10, "NGN")
    with pytest.raises(ValueError):
        db.update_document(conn, "d1", sha256="x")


def test_findings_keep_reviewer_decisions_across_reruns(conn):
    p = db.create_project(conn, "p", "NGN")
    db.add_document(conn, "d1", p, "a.csv", "csv", "0" * 64, 10, "NGN")
    f1 = {"code": "MISMATCH", "path": "line_items.L1.unit_price", "ref": "R4C3", "detail": "a"}
    f2 = {"code": "UNACCOUNTED_AMOUNT", "ref": "R6C4", "detail": "b"}
    db.sync_findings(conn, p, "d1", "L1", [f1, f2])
    fid = next(f["id"] for f in db.list_findings(conn, p) if f["code"] == "MISMATCH")
    db.set_finding_status(conn, fid, "dismissed")
    db.sync_findings(conn, p, "d1", "L1", [f1])            # rerun: f2 no longer occurs
    by_code = {f["code"]: f["status"] for f in db.list_findings(conn, p)}
    assert by_code == {"MISMATCH": "dismissed", "UNACCOUNTED_AMOUNT": "resolved"}


def test_review_requires_reason_and_reviewer(conn):
    p = db.create_project(conn, "p", "NGN")
    with pytest.raises(ValueError):
        db.record_review(conn, p, "finding", "f", "dismiss", "  ", None, None, "Ada")
    with pytest.raises(ValueError):
        db.record_review(conn, p, "finding", "f", "dismiss", "dup", None, None, "")
    db.record_review(conn, p, "finding", "f", "dismiss", "duplicate row", {"a": 1}, {"a": 2}, "Ada")
    log = db.review_log(conn, p)
    assert log[0]["reviewer"] == "Ada" and log[0]["reason"] == "duplicate row"


def test_match_groups_and_confirmation(conn):
    p = db.create_project(conn, "p", "NGN")
    for d in ("A", "B"):
        db.add_document(conn, d, p, d, "csv", "0" * 64, 1, "NGN")
    g = db.add_match_group(conn, p, "chairs", [{"doc_id": "A", "item_id": "L1"}, {"doc_id": "B", "item_id": "L3"}],
                           source="ai", reason="both mesh office chairs", confidence="high", model="m")
    assert db.match_groups(conn, p)[0]["status"] == "proposed"
    db.set_match_status(conn, g, "confirmed", reviewer="Ada (self-declared)")
    grp = db.match_groups(conn, p)[0]
    assert grp["status"] == "confirmed" and grp["confirmed_by"] == "Ada (self-declared)"
    db.clear_ai_proposals(conn, p)                        # confirmed groups survive a re-match
    assert len(db.match_groups(conn, p)) == 1


def test_expiry_and_cascade(conn):
    p = db.create_project(conn, "p", "NGN", ttl_s=-1)
    db.add_document(conn, "d1", p, "a.csv", "csv", "0" * 64, 10, "NGN")
    db.enqueue(conn, "process", p, "d1")
    assert db.expired_projects(conn) == [p]
    assert db.delete_project(conn, p) == ["d1"]
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_no_string_formatting_of_values_in_sql():
    src = open(os.path.join(os.path.dirname(db.__file__), "db.py"), encoding="utf-8").read()
    # the only f-string SQL is update_document's column list, built from a fixed allow-list
    assert src.count('f"UPDATE') == 1 and "_DOC_FIELDS" in src


def test_duplicate_findings_in_one_batch_are_kept_and_stable(conn):
    p = db.create_project(conn, "p", "NGN")
    db.add_document(conn, "d1", p, "a.xlsx", "xlsx", "0" * 64, 10, "NGN")
    same = [{"code": "INJECTION_PATTERN", "ref": "Quote!B8", "detail": f"pattern {i}"} for i in range(5)]
    db.sync_findings(conn, p, "d1", "INJ", same)
    ids = sorted(f["id"] for f in db.list_findings(conn, p))
    assert len(ids) == 5
    db.sync_findings(conn, p, "d1", "INJ", same)                # rerun: same five, no duplicates
    assert sorted(f["id"] for f in db.list_findings(conn, p)) == ids
