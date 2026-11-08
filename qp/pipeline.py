"""qp-worker job handlers. Dependencies (db connection, sandbox, llm, config) are injected,
so the whole pipeline is tested here with fakes and runs unchanged on the VM.

process  : index (sandbox) -> validate -> injection checks -> AI extractor (sandbox, <=2 retries)
           -> currency checks -> Layer 1 -> Layer 2 -> findings -> maybe queue matching
reverify : re-run Layers 1-2 after a reviewer correction (no AI involved)
match    : AI proposes equivalence groups (human confirms in the UI)
explain  : AI summary from verified facts, number-checked
selftest : the containment self-test via qp-sandboxd
"""

import copy
import hashlib
import json
import os

from . import compare, db, explain, extractor_agent, feedback, injection, layer1, layer2, matching, schemas
from .sandbox import envelope_summary


class Ctx:
    def __init__(self, conn, sandbox, llm, config):
        self.conn, self.sandbox, self.llm, self.cfg = conn, sandbox, llm, config

    @property
    def models(self):
        return self.cfg["models"]


def _emitter(ctx, job):
    def emit(step, status, detail=None):
        db.add_event(ctx.conn, job.get("project_id"), job["id"], job.get("document_id"), step, status, detail)
    return emit


# ------------------------------------------------------------------ index storage (worker-owned)
def _index_path(ctx, doc_id):
    if not schemas_uuid(doc_id):
        raise ValueError("bad document id")
    return os.path.join(ctx.cfg["indexes_dir"], doc_id + ".json")


def schemas_uuid(s):
    import re
    return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s or ""))


def save_index(ctx, doc_id, index_json):
    path = _index_path(ctx, doc_id)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(index_json)
    os.replace(tmp, path)
    return hashlib.sha256(index_json.encode("utf-8")).hexdigest()


def load_index(ctx, doc):
    with open(_index_path(ctx, doc["id"]), encoding="utf-8") as f:
        index_json = f.read()
    if hashlib.sha256(index_json.encode("utf-8")).hexdigest() != doc["index_sha256"]:
        raise ValueError("stored index does not match its recorded hash")
    return schemas.validate_index(json.loads(index_json)), index_json


# ------------------------------------------------------------------ process
def process(ctx, job):
    conn, emit = ctx.conn, _emitter(ctx, job)
    doc = db.get_document(conn, job["document_id"])
    project = db.get_project(conn, doc["project_id"])
    pid, did = project["id"], doc["id"]
    db.update_document(conn, did, status="indexing")

    # 1. index the raw file -- only ever inside the sandbox
    emit("sandbox_index", "started", {"declared_type": doc["declared_type"]})
    env = ctx.sandbox.index_document(did, doc["sha256"], doc["declared_type"], job_id=job["id"])
    db.add_run(conn, job["id"], did, 1, "indexer", envelope_summary(env))
    emit("sandbox_index", "done" if env.get("status") == "ok" else "failed", envelope_summary(env))
    if env.get("status") != "ok":
        db.update_document(conn, did, status="index_failed")
        db.sync_findings(conn, pid, did, "INDEX", [{"code": "INDEX_FAILED", "severity": "block",
                                                     "detail": f"indexer status {env.get('status')}"}])
        return "failed"

    # 2. the worker is the trust point for the index
    index_json = env["stdout"]
    try:
        index = schemas.validate_index(json.loads(index_json))
        if index["source_sha256"] != doc["sha256"]:
            raise schemas.SchemaError("$.source_sha256", "does not match the uploaded file's hash")
    except (ValueError, schemas.SchemaError) as e:
        emit("index_validation", "failed", {"error": str(e)[:200]})
        db.update_document(conn, did, status="index_failed")
        db.sync_findings(conn, pid, did, "INDEX", [{"code": "INDEX_INVALID", "severity": "block", "detail": str(e)[:200]}])
        return "failed"
    db.update_document(conn, did, index_sha256=save_index(ctx, did, index_json))
    emit("index_validation", "passed", {"cells": len(index.get("cells") or []), "lines": len(index.get("lines") or []),
                                        "warnings": [w["code"] for w in index["warnings"]][:20],
                                        "truncated": index["truncated"]})
    if not index["ok"]:
        db.update_document(conn, did, status="rejected")
        db.sync_findings(conn, pid, did, "INDEX", [{"code": "DOCUMENT_REJECTED", "severity": "block",
                                                     "detail": index["error"]["code"]}])
        emit("document", "rejected", {"reason": index["error"]["code"]})
        return "done"

    # 3. prompt-injection checks on the document (best-effort; containment applies regardless)
    inj = injection.scan(index)
    cls_findings, cls_info = injection.classify(index, ctx.llm, ctx.models["classifier"])
    emit("injection_check", "flagged" if inj or cls_findings else "clean",
         {"pattern_hits": len(inj), "classifier": cls_info, "classifier_hits": len(cls_findings)})

    # 4. the AI writes the extractor; it runs in the sandbox
    db.update_document(conn, did, status="extracting")
    injected = frozenset(f["ref"] for f in inj + cls_findings if f.get("ref"))
    cur = doc["user_currency"]

    def verify(ex):
        return feedback.evaluate(index, ex, cur, ignore_refs=injected)

    res = extractor_agent.run(index, index_json, ctx.llm, ctx.sandbox, ctx.models["extractor"], emit,
                              job_id=job["id"], verify=verify)
    if not res["ok"] and not res.get("infrastructure_error") and ctx.models.get("fallback") and all(
            a["stage"] == "llm" for a in res["attempts_log"]):
        emit("llm_fallback", "started", {"model": ctx.models["fallback"], "reason": "primary model unavailable"})
        res = extractor_agent.run(index, index_json, ctx.llm, ctx.sandbox, ctx.models["fallback"], emit,
                                  job_id=job["id"], verify=verify)
    db.save_extraction(conn, did, res["attempts"], res["model"], res["code"], res["extraction"])
    if res.get("envelope"):
        db.add_run(conn, job["id"], did, res["attempts"], "extractor", envelope_summary(res["envelope"]), res["model"])
    code_f = injection.code_findings(res["flags"])
    db.sync_findings(conn, pid, did, "INJ", inj + cls_findings + code_f)
    if not res["ok"]:
        db.update_document(conn, did, status="extraction_failed")
        db.sync_findings(conn, pid, did, "EXTRACT", [{"code": "EXTRACTION_FAILED", "severity": "block",
                                                       "detail": f"no valid extraction after {res['attempts']} attempts"}])
        return "failed"
    db.sync_findings(conn, pid, did, "EXTRACT", [])

    # 5-6. trusted verification
    _verify_and_store(ctx, project, doc, index, res["extraction"], emit)
    _maybe_queue_matching(ctx, pid)
    return "done"


def _apply_corrections(extraction, overrides):
    """'corrected' overrides replace the value AND citation, so Layer 1 re-verifies them
    against the source. 'user_entered' overrides are passed to Layer 2 (Estimated)."""
    ex = copy.deepcopy(extraction)
    l2_overrides = {}
    for path, o in overrides.items():
        if o["kind"] == "user_entered":
            l2_overrides[path] = o
            continue
        parts = path.split(".")
        target = None
        if parts[0] == "stated" and len(parts) == 2:
            target = ex["stated"].get(parts[1])
            if target is None:
                ex["stated"][parts[1]] = target = {"value_str": "", "cite": None}
        elif parts[0] in ("line_items", "charges") and len(parts) == 3:
            item = next((x for x in ex[parts[0]] if x["id"] == parts[1]), None)
            if item is not None:
                if item.get(parts[2]) is None:
                    item[parts[2]] = {"value_str": "", "cite": None}
                target = item[parts[2]]
        if target is not None:
            target["value_str"], target["cite"] = o["value_str"], o.get("cite")
    return ex, l2_overrides


def _verify_and_store(ctx, project, doc, index, extraction, emit):
    conn, pid, did = ctx.conn, project["id"], doc["id"]
    ex, l2_over = _apply_corrections(extraction, db.get_overrides(conn, did))
    excluded = db.get_exclusions(conn, did)
    if excluded:
        ex["line_items"] = [li for li in ex["line_items"] if li["id"] not in excluded]
        ex["charges"] = [c for c in ex["charges"] if c["id"] not in excluded]
    l1 = layer1.verify(index, ex, project["currency"])
    doc_cur, comparable, cur_f = layer1.resolve_currency(l1["currency"], ex["currency"], doc["user_currency"],
                                                         project["currency"])
    if not comparable:
        # amounts in another currency are not errors of THIS comparison; keep them out of it
        l1["findings"] = [f for f in l1["findings"] if f["code"] != "CURRENCY_CONFLICT"]
    l2 = layer2.reconcile(ex, l1, l2_over)
    db.save_results(conn, did, l1, l2)
    db.sync_findings(conn, pid, did, "L1", l1["findings"] + cur_f)
    db.sync_findings(conn, pid, did, "L2", l2["findings"])
    supplier = ex["supplier"]["value_str"] if ex.get("supplier") else None
    db.update_document(conn, did, status="processed", supplier=supplier, doc_currency=doc_cur,
                       comparable=1 if comparable else 0)
    emit("layer1_source", "done", {**l1["summary"], "currency_detected": l1["currency"]["detected"],
                                   "document_currency": doc_cur, "comparable": comparable})
    emit("layer2_arithmetic", "done", {"status": l2["status"],
                                       "checks": [(c["check"], c["status"]) for c in l2["checks"]]})


def _maybe_queue_matching(ctx, project_id):
    """Queue AI matching once >= 2 comparable documents are processed, no other document
    of the project is still waiting, and no matching job is already queued or running."""
    docs = [d for d in db.list_documents(ctx.conn, project_id) if d["status"] == "processed" and d["comparable"]]
    jobs = db.project_jobs(ctx.conn, project_id)
    match_pending = any(j["kind"] == "match" and j["status"] in ("queued", "running") for j in jobs)
    docs_waiting = any(j["kind"] == "process" and j["status"] == "queued" for j in jobs)
    if len(docs) >= 2 and not match_pending and not docs_waiting:
        db.enqueue(ctx.conn, "match", project_id)


# ------------------------------------------------------------------ reverify (after review)
def reverify(ctx, job):
    emit = _emitter(ctx, job)
    doc = db.get_document(ctx.conn, job["document_id"])
    project = db.get_project(ctx.conn, doc["project_id"])
    ex = db.get_extraction(ctx.conn, doc["id"])
    if not ex or not ex["output"]:
        emit("reverify", "skipped", {"reason": "no extraction"})
        return "done"
    index, _ = load_index(ctx, doc)
    emit("reverify", "started", {"reason": json.loads(job["payload"]).get("reason", "review")})
    _verify_and_store(ctx, project, doc, index, ex["output"], emit)
    return "done"


# ------------------------------------------------------------------ comparison view (shared with qp-web)
def comparison_for(conn, project):
    docs = []
    for d in db.list_documents(conn, project["id"]):
        r = db.get_results(conn, d["id"]) if d["status"] == "processed" else None
        if r:
            docs.append({"doc_id": d["id"], "supplier": d["supplier"] or d["original_name"],
                         "currency": d["doc_currency"], "comparable": bool(d["comparable"]), "l1": r["l1"], "l2": r["l2"]})
    groups = [g for g in db.match_groups(conn, project["id"]) if g["status"] != "rejected"]
    comp = compare.compare(docs, groups, project["currency"])
    return docs, comp


def assurance(conn, project, docs, comp):
    """Clarification 2: three separate counters and the Complete/Draft gate."""
    cmp_docs = [d for d in docs if d["comparable"]]
    amounts = [v for d in cmp_docs for v in d["l1"]["values"].values() if v["kind"] != "text"]
    src_ok = sum(1 for v in amounts if v["status"] == "VERIFIED")
    arith_ok = sum(1 for d in cmp_docs if d["l2"]["status"] == "RECONCILES")
    items = sum(len(d["l2"]["lines"]) for d in cmp_docs)
    confirmed_items = sum(len([m for m in g["members"]]) for g in db.match_groups(conn, project["id"])
                          if g["status"] == "confirmed")
    # informational findings (e.g. "other currency, shown separately") need no decision
    open_findings = sum(1 for f in db.list_findings(conn, project["id"], status="open") if f["severity"] != "info")
    proposed = [g for g in db.match_groups(conn, project["id"]) if g["status"] == "proposed"]
    source_done = bool(amounts) and src_ok == len(amounts)
    arith_done = bool(cmp_docs) and arith_ok == len(cmp_docs)
    equiv_done = confirmed_items > 0 and not proposed
    complete = source_done and arith_done and equiv_done and open_findings == 0
    return {"source": {"verified": src_ok, "total": len(amounts)},
            "arithmetic": {"reconciled": arith_ok, "total": len(cmp_docs)},
            "equivalence": {"confirmed_items": confirmed_items, "total_items": items, "proposed_groups": len(proposed)},
            "open_findings": open_findings,
            "headline": "Complete" if complete else f"Draft — {open_findings} items need review"}


# ------------------------------------------------------------------ match
def match(ctx, job):
    emit = _emitter(ctx, job)
    project = db.get_project(ctx.conn, job["project_id"])
    docs, _ = comparison_for(ctx.conn, project)
    cmp_docs = [{"doc_id": d["doc_id"], "supplier": d["supplier"], "lines": d["l2"]["lines"]}
                for d in docs if d["comparable"]]
    if len(cmp_docs) < 2:
        emit("ai_matching", "skipped", {"reason": "fewer than two comparable documents"})
        return "done"
    # items already in confirmed groups are not re-proposed
    taken = {(m["doc_id"], m["item_id"]) for g in db.match_groups(ctx.conn, project["id"]) if g["status"] == "confirmed"
             for m in g["members"]}
    for d in cmp_docs:
        d["lines"] = [ln for ln in d["lines"] if (d["doc_id"], ln["id"]) not in taken]
    emit("ai_matching", "started", {"model": ctx.models["matcher"], "suppliers": len(cmp_docs)})
    try:
        groups, rejected, model = matching.propose(cmp_docs, ctx.llm, ctx.models["matcher"])
    except Exception as e:  # noqa: BLE001
        emit("ai_matching", "failed", {"error": str(e)[:200], "note": "items can still be paired manually"})
        return "failed"
    db.clear_ai_proposals(ctx.conn, project["id"])
    for g in groups:
        db.add_match_group(ctx.conn, project["id"], g["label"], g["members"], source="ai", reason=g["reason"],
                           confidence=g["confidence"], model=model)
    emit("ai_matching", "done", {"model": model, "proposed_groups": len(groups), "discarded": rejected[:5],
                                 "note": "Proposed by AI, not confirmed"})
    return "done"


# ------------------------------------------------------------------ explain
def explain_job(ctx, job):
    emit = _emitter(ctx, job)
    project = db.get_project(ctx.conn, job["project_id"])
    docs, comp = comparison_for(ctx.conn, project)
    facts = explain.facts_for(comp, assurance(ctx.conn, project, docs, comp))
    emit("ai_explanation", "started", {"model": ctx.models["explainer"]})
    out = explain.explain(facts, ctx.llm, ctx.models["explainer"])
    db.save_explanation(ctx.conn, project["id"], out["text"], out["status"], out["model"], out["attempts"])
    emit("ai_explanation", out["status"], {"model": out["model"], "attempts": out["attempts"],
                                           "rejected_numbers": out.get("rejected_numbers")})
    return "done"


# ------------------------------------------------------------------ selftest
def selftest(ctx, job):
    emit = _emitter(ctx, job)
    emit("containment_selftest", "started", {})
    report = ctx.sandbox.self_test()
    db.save_selftest(ctx.conn, job["id"], report)
    for t in report.get("tests", []):
        emit("containment_test", t["verdict"], {"test": t["test"], "expected": t.get("expected")})
    emit("containment_selftest", "passed" if report.get("passed") else "failed",
         {"isolation_mode": report.get("isolation_mode")})
    return "done"


HANDLERS = {"process": process, "reverify": reverify, "match": match, "explain": explain_job, "selftest": selftest}
