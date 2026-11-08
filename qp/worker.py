"""qp-worker: one job at a time from the SQLite queue.

Holds the inference key (systemd credential). Has NO Docker access and NO access to
raw uploads: raw files are only read by the indexer inside the sandbox, via qp-sandboxd.

  python3 -I -m qp.worker --config /etc/qp-worker/worker.json
"""

import argparse
import json
import signal
import sys
import time
import traceback

from . import db, inference, pipeline
from .sandbox import SandboxClient

REQUIRED = {"db", "indexes_dir", "socket_path", "base_url", "models"}
MODEL_ROLES = {"extractor", "classifier", "matcher", "explainer"}


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = REQUIRED - set(cfg)
    if missing:
        raise SystemExit(f"config missing {sorted(missing)}")
    if not MODEL_ROLES <= set(cfg["models"]):
        raise SystemExit(f"config.models needs {sorted(MODEL_ROLES)} (+ optional fallback)")
    return cfg


def log(event, **fields):
    print(json.dumps({"ts": round(time.time(), 3), "event": event, **fields}), file=sys.stderr, flush=True)


def run_one(ctx):
    job = db.claim_next(ctx.conn)
    if job is None:
        return False
    handler = pipeline.HANDLERS.get(job["kind"])
    log("job_started", job_id=job["id"], kind=job["kind"])
    try:
        status = handler(ctx, job) if handler else "failed"
        db.finish_job(ctx.conn, job["id"], "done" if status == "done" else "failed")
        log("job_finished", job_id=job["id"], status=status)
    except Exception as e:  # noqa: BLE001 - a failed job must never kill the worker
        db.finish_job(ctx.conn, job["id"], "failed", error=type(e).__name__)
        db.add_event(ctx.conn, job.get("project_id"), job["id"], job.get("document_id"), "job", "failed",
                     {"error": type(e).__name__, "detail": str(e)[:200]})
        if job.get("document_id"):
            db.update_document(ctx.conn, job["document_id"], status="error")
        log("job_error", job_id=job["id"], error=type(e).__name__, trace=traceback.format_exc()[-800:])
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="QuoteProof worker")
    ap.add_argument("--config", default="/etc/qp-worker/worker.json")
    ap.add_argument("--once", action="store_true", help="process the queue until empty, then exit")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg["db"])
    db.init(conn)
    llm = inference.InferenceClient(inference.load_key(), base_url=cfg["base_url"])
    ctx = pipeline.Ctx(conn, SandboxClient(cfg["socket_path"]), llm, cfg)
    recovered = db.recover_running(conn)
    log("worker_started", recovered_jobs=len(recovered), models=cfg["models"])

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(flag=True))
    while not stop["flag"]:
        busy = run_one(ctx)
        if not busy:
            if args.once:
                break
            time.sleep(0.5)
        # retention: expired projects are removed by qp-web (it owns the uploads)
    log("worker_stopped")


if __name__ == "__main__":
    main()
