"""End-to-end worker pipeline with fakes (the same code runs on the VM with the real
qp-sandboxd and Vultr). The fake sandbox returns our saved Source Indexes for
index_document and runs only our hand-written reference extractor."""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from qp import db, pipeline, worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = open(os.path.join(ROOT, "fixtures", "extractors", "reference_extractor.py"), encoding="utf-8").read()


def index_json(name):
    return open(os.path.join(ROOT, "fixtures", "indexes", name + ".json"), encoding="utf-8").read()


class FakeSandbox:
    def __init__(self, tmp, by_sha):
        self.tmp, self.by_sha, self.calls = tmp, by_sha, []

    def index_document(self, document_id, sha256, declared_type, job_id=None):
        self.calls.append("index")
        out = json.dumps(json.loads(self.by_sha[sha256]))   # same bytes semantics as stdout
        return {"status": "ok", "exit_code": 0, "stdout": out, "runtime": "runsc", "removed": True,
                "container_id": "c" * 12, "duration_ms": 300}

    def run_extractor(self, index_json, code, job_id=None):
        self.calls.append("extract")
        (self.tmp / "i.json").write_text(index_json, encoding="utf-8")
        (self.tmp / "m.py").write_text(code, encoding="utf-8")
        p = subprocess.run([sys.executable, "-I", str(self.tmp / "m.py"), str(self.tmp / "i.json")],
                           capture_output=True, timeout=20)
        return {"status": "ok" if p.returncode == 0 else "nonzero_exit", "exit_code": p.returncode,
                "stdout": p.stdout.decode(), "stderr_tail": p.stderr.decode()[-500:], "runtime": "runsc",
                "removed": True}

    def self_test(self):
        return {"passed": True, "isolation_mode": "gvisor",
                "tests": [{"test": "network_blocked", "verdict": "PASS", "expected": "x"}]}


class RoleLLM:
    """Answers according to which prompt it receives."""

    def __init__(self, explanation="All amounts in NGN. Adeyemi's known total is 985,625.00."):
        self.explanation = explanation
        self.calls = []

    def chat(self, model, messages, temperature=0.1, max_tokens=None):
        sys_prompt = messages[0]["content"]
        self.calls.append(model)
        if "extracts a supplier quotation" in sys_prompt:
            text = f"```python\n{REF}\n```"
        elif "prompt-injection" in sys_prompt:
            text = '{"suspicious": []}'
        elif "Group line items" in sys_prompt:
            text = '{"groups": [{"label": "Main item", "members": [{"supplier": "S1", "item": "L1"},' \
                   ' {"supplier": "S2", "item": "L1"}], "reason": "test pairing", "confidence": "low"}]}'
        else:
            text = self.explanation
        return {"text": text, "model": model, "latency_ms": 1, "usage": {}}


@pytest.fixture
def setup(tmp_path):
    conn = db.connect(str(tmp_path / "qp.sqlite"))
    db.init(conn)
    (tmp_path / "indexes").mkdir()
    files = {"ngn_quote.csv": ("csv", "NGN"), "ngn_quote.pdf": ("pdf", "NGN"), "usd_quote.xlsx": ("xlsx", "USD")}
    by_sha, docs = {}, {}
    pid = db.create_project(conn, "Office fit-out", "NGN")
    for name, (t, cur) in files.items():
        ij = index_json(name)
        sha = json.loads(ij)["source_sha256"]
        by_sha[sha] = ij
        did = db.new_id()
        db.add_document(conn, did, pid, name, t, sha, 100, cur)
        db.enqueue(conn, "process", pid, did)
        docs[name] = did
    cfg = {"indexes_dir": str(tmp_path / "indexes"),
           "models": {"extractor": "m-ext", "classifier": "m-cls", "matcher": "m-match", "explainer": "m-exp"}}
    ctx = pipeline.Ctx(conn, FakeSandbox(tmp_path, by_sha), RoleLLM(), cfg)
    return ctx, pid, docs


def drain(ctx):
    n = 0
    while worker.run_one(ctx):
        n += 1
    return n


def test_full_pipeline(setup):
    ctx, pid, docs = setup
    conn = ctx.conn
    assert drain(ctx) == 4                      # 3 process jobs + auto-queued matching
    d = {n: db.get_document(conn, i) for n, i in docs.items()}
    assert all(x["status"] == "processed" for x in d.values())
    assert d["usd_quote.xlsx"]["comparable"] == 0 and d["ngn_quote.csv"]["comparable"] == 1
    assert d["ngn_quote.csv"]["supplier"] == "Adeyemi Office Supplies Ltd"
    # index stored by the worker, hash recorded
    path = os.path.join(ctx.cfg["indexes_dir"], docs["ngn_quote.csv"] + ".json")
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == d["ngn_quote.csv"]["index_sha256"]

    # timeline has real evidence for each sandbox run
    ev = db.events_after(conn, pid)
    steps = {(e["step"], e["status"]) for e in ev}
    assert {("sandbox_index", "done"), ("sandbox_run_extractor", "done"), ("layer1_source", "done"),
            ("layer2_arithmetic", "done"), ("ai_matching", "done")} <= steps
    assert any(e["detail"].get("runtime") == "runsc" for e in ev if e["step"] == "sandbox_index")

    # matching: proposed only, then confirmed by a human
    groups = db.match_groups(conn, pid)
    assert len(groups) == 1 and groups[0]["status"] == "proposed"
    docs_v, comp = pipeline.comparison_for(conn, db.get_project(conn, pid))
    assert comp["rows"] == [] and len(comp["not_compared_yet"]) == 4
    a = pipeline.assurance(conn, db.get_project(conn, pid), docs_v, comp)
    assert a["headline"].startswith("Draft")
    db.set_match_status(conn, groups[0]["id"], "confirmed", reviewer="Ada (self-declared)")
    docs_v, comp = pipeline.comparison_for(conn, db.get_project(conn, pid))
    assert len(comp["rows"]) == 1 and comp["not_comparable_currency"][0]["currency"] == "USD"
    # PDF quote states no VAT: its tax is Unknown, never zero
    tax = next(r for r in comp["charges"] if r["kind"] == "tax")
    assert tax["cells"][docs["ngn_quote.pdf"]]["label"] == "Unknown"

    # explanation: accepted only with numbers from the facts
    db.enqueue(conn, "explain", pid)
    drain(ctx)
    e = db.get_explanation(conn, pid)
    assert e["status"] == "accepted"


def test_explanation_with_invented_number_is_rejected(setup):
    ctx, pid, docs = setup
    ctx.llm.explanation = "The cheapest option saves 123,456.78 overall."
    drain(ctx)
    db.enqueue(ctx.conn, "explain", pid)
    drain(ctx)
    e = db.get_explanation(ctx.conn, pid)
    assert e["status"] == "rejected" and e["text"] is None


def test_reviewer_correction_reverifies(setup):
    ctx, pid, docs = setup
    drain(ctx)
    did = docs["ngn_quote.csv"]
    # reviewer enters a delivery amount NOT in the document -> Estimated
    db.set_override(ctx.conn, did, "charges.C1.amount", "30000.00", "user_entered")
    db.enqueue(ctx.conn, "reverify", pid, did, {"reason": "test"})
    drain(ctx)
    l2 = db.get_results(ctx.conn, did)["l2"]
    assert l2["charges"][0]["amount"] == {"value": "30000.00", "label": "Estimated"}
    # and a 'corrected' override must re-verify against the cited source
    db.set_override(ctx.conn, did, "charges.C1.amount", "25000.00", "corrected", cite="R6C4")
    db.enqueue(ctx.conn, "reverify", pid, did, {"reason": "test"})
    drain(ctx)
    r = db.get_results(ctx.conn, did)
    assert r["l1"]["values"]["charges.C1.amount"]["status"] == "VERIFIED"
    assert r["l2"]["charges"][0]["amount"]["label"] == "Quoted"


def test_tampered_index_hash_is_rejected(setup):
    ctx, pid, docs = setup
    did = docs["ngn_quote.csv"]
    sha = db.get_document(ctx.conn, did)["sha256"]
    bad = json.loads(ctx.sandbox.by_sha[sha])
    bad["source_sha256"] = "f" * 64
    ctx.sandbox.by_sha[sha] = json.dumps(bad)
    drain(ctx)
    assert db.get_document(ctx.conn, did)["status"] == "index_failed"
    assert any(f["code"] == "INDEX_INVALID" for f in db.list_findings(ctx.conn, pid))


def test_selftest_job_records_report(setup):
    ctx, pid, docs = setup
    db.enqueue(ctx.conn, "selftest")
    while worker.run_one(ctx):
        pass
    assert db.latest_selftest(ctx.conn)["passed"] == 1


def test_handler_exception_marks_job_failed_not_worker(setup):
    ctx, pid, docs = setup

    class Broken(FakeSandbox):
        def index_document(self, *a, **k):
            raise RuntimeError("socket gone")
    ctx.sandbox = Broken(ctx.sandbox.tmp, ctx.sandbox.by_sha)
    drain(ctx)
    jobs = db.project_jobs(ctx.conn, pid)
    assert all(j["status"] == "failed" for j in jobs if j["kind"] == "process")


def test_info_findings_do_not_hold_the_gate(setup):
    ctx, pid, docs = setup
    drain(ctx)
    p = db.get_project(ctx.conn, pid)
    docs_v, comp = pipeline.comparison_for(ctx.conn, p)
    a = pipeline.assurance(ctx.conn, p, docs_v, comp)
    open_all = db.list_findings(ctx.conn, pid, status="open")
    info = [f for f in open_all if f["severity"] == "info"]
    assert info and a["open_findings"] == len(open_all) - len(info)


def test_reviewer_exclusion_of_a_note_row_restores_reconciliation(setup):
    ctx, pid, docs = setup
    drain(ctx)
    did = docs["ngn_quote.csv"]
    ex = db.get_extraction(ctx.conn, did)["output"]
    ex["line_items"].append({"id": "L9", "description": {"value_str": "Note", "cite": "R9C1"},
                             "qty": None, "unit_price": None, "line_total": None})
    db.save_extraction(ctx.conn, did, 1, "m", "code", ex)
    db.enqueue(ctx.conn, "reverify", pid, did, {"reason": "test"})
    drain(ctx)
    assert db.get_results(ctx.conn, did)["l2"]["status"] == "INCOMPLETE"
    assert any(f["code"] == "LINE_WITHOUT_AMOUNTS" for f in db.list_findings(ctx.conn, pid, status="open"))
    db.set_exclusion(ctx.conn, did, "L9", "note row, not an item", "Ada")
    db.enqueue(ctx.conn, "reverify", pid, did, {"reason": "test"})
    drain(ctx)
    l2 = db.get_results(ctx.conn, did)["l2"]
    assert l2["status"] == "RECONCILES" and l2["totals"]["grand_total_calculated"]["value"] == "1585625.00"
    assert not any(f["code"] == "LINE_WITHOUT_AMOUNTS" for f in db.list_findings(ctx.conn, pid, status="open"))
