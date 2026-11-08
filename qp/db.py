"""SQLite data layer and job queue (shared by qp-web and qp-worker).

* qp-web inserts projects, documents and jobs (status 'queued') and reads results.
* qp-worker atomically claims one job at a time, writes events, results and findings.
* WAL mode; every statement is parameterised; JSON columns hold validated data only.
* The database never stores the inference key, file paths chosen by users, or
  uploaded file contents. Original filenames are stored only for display (escaped).
"""

import hashlib
import json
import sqlite3
import time
import uuid

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL,
  currency TEXT NOT NULL CHECK (currency IN ('NGN','USD')),
  created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  original_name TEXT NOT NULL, declared_type TEXT NOT NULL CHECK (declared_type IN ('csv','xlsx','pdf')),
  sha256 TEXT NOT NULL, size INTEGER NOT NULL,
  user_currency TEXT NOT NULL CHECK (user_currency IN ('NGN','USD')),
  status TEXT NOT NULL DEFAULT 'uploaded',
  supplier TEXT, doc_currency TEXT, comparable INTEGER,
  index_sha256 TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
  document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('process','match','selftest','explain','reverify')),
  status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed')),
  payload TEXT NOT NULL DEFAULT '{}', error TEXT,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, job_id TEXT, document_id TEXT,
  ts REAL NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_project ON events(project_id, id);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, document_id TEXT, attempt INTEGER,
  kind TEXT NOT NULL, model TEXT, envelope TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS extractions (
  document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  attempts INTEGER NOT NULL, model TEXT, code TEXT, code_sha256 TEXT, output TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
  document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  l1 TEXT NOT NULL, l2 TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL, layer TEXT NOT NULL, code TEXT NOT NULL, severity TEXT NOT NULL,
  path TEXT, ref TEXT, detail TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','accepted','corrected','dismissed','resolved')),
  created_at REAL NOT NULL, UNIQUE(project_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS overrides (
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, path TEXT NOT NULL,
  value_str TEXT, kind TEXT NOT NULL CHECK (kind IN ('corrected','user_entered')), cite TEXT,
  PRIMARY KEY (document_id, path)
);
CREATE TABLE IF NOT EXISTS exclusions (
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, item_id TEXT NOT NULL,
  reason TEXT NOT NULL, reviewer TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (document_id, item_id)
);
CREATE TABLE IF NOT EXISTS review_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  target_type TEXT NOT NULL, target_id TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
  before TEXT, after TEXT, reviewer TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS match_groups (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  label TEXT NOT NULL, reason TEXT, confidence TEXT, model TEXT,
  status TEXT NOT NULL CHECK (status IN ('proposed','confirmed','rejected')),
  source TEXT NOT NULL CHECK (source IN ('ai','human')),
  confirmed_by TEXT, confirmed_at REAL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS match_members (
  group_id TEXT NOT NULL REFERENCES match_groups(id) ON DELETE CASCADE,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, item_id TEXT NOT NULL,
  PRIMARY KEY (group_id, document_id, item_id)
);
CREATE TABLE IF NOT EXISTS selftests (
  id TEXT PRIMARY KEY, job_id TEXT, passed INTEGER NOT NULL, report TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS explanations (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  text TEXT, status TEXT NOT NULL, model TEXT, attempts INTEGER, created_at REAL NOT NULL
);
"""

JOB_KINDS = ("process", "match", "selftest", "explain", "reverify")


def new_id():
    return str(uuid.uuid4())


def now():
    return time.time()


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def connect(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init(conn):
    conn.executescript(SCHEMA)


class tx:
    """BEGIN IMMEDIATE ... COMMIT, so a writer holds the lock for the whole block."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, et, ev, tb):
        self.conn.execute("ROLLBACK" if et else "COMMIT")
        return False


def row(r):
    return None if r is None else dict(r)


# ------------------------------------------------------------------ projects & documents
def create_project(conn, name, currency, ttl_s=24 * 3600):
    pid = new_id()
    t = now()
    conn.execute("INSERT INTO projects (id,name,currency,created_at,expires_at) VALUES (?,?,?,?,?)",
                 (pid, name, currency, t, t + ttl_s))
    return pid


def get_project(conn, pid):
    return row(conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())


def add_document(conn, doc_id, project_id, original_name, declared_type, sha256, size, user_currency):
    conn.execute("INSERT INTO documents (id,project_id,original_name,declared_type,sha256,size,user_currency,"
                 "created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (doc_id, project_id, original_name, declared_type, sha256, size, user_currency, now()))


def get_document(conn, doc_id):
    return row(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


def list_documents(conn, project_id):
    return [dict(r) for r in conn.execute("SELECT * FROM documents WHERE project_id=? ORDER BY created_at",
                                          (project_id,))]


_DOC_FIELDS = {"status", "supplier", "doc_currency", "comparable", "index_sha256"}


def update_document(conn, doc_id, **fields):
    bad = set(fields) - _DOC_FIELDS
    if bad:
        raise ValueError(f"cannot update {bad}")
    sets = ", ".join(f"{k}=?" for k in fields)  # keys come from the fixed allow-list above
    conn.execute(f"UPDATE documents SET {sets} WHERE id=?", (*fields.values(), doc_id))


# ------------------------------------------------------------------ job queue
def enqueue(conn, kind, project_id=None, document_id=None, payload=None):
    if kind not in JOB_KINDS:
        raise ValueError(kind)
    jid = new_id()
    conn.execute("INSERT INTO jobs (id,project_id,document_id,kind,status,payload,created_at) VALUES (?,?,?,?,?,?,?)",
                 (jid, project_id, document_id, kind, "queued", dumps(payload or {}), now()))
    return jid


def claim_next(conn):
    """Atomically move the oldest queued job to 'running' and return it (or None)."""
    with tx(conn):
        r = conn.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id = ("
            "  SELECT id FROM jobs WHERE status='queued' ORDER BY created_at, rowid LIMIT 1"
            ") AND status='queued' RETURNING *", (now(),)).fetchone()
    return row(r)


def finish_job(conn, job_id, status="done", error=None):
    if status not in ("done", "failed"):
        raise ValueError(status)
    conn.execute("UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=? AND status='running'",
                 (status, error, now(), job_id))


def recover_running(conn):
    """Called by the worker at startup: nothing is silently lost or shown as succeeded."""
    with tx(conn):
        rows = conn.execute("UPDATE jobs SET status='failed', error='failed (worker restarted)', finished_at=? "
                            "WHERE status='running' RETURNING id, project_id, document_id", (now(),)).fetchall()
        for r in rows:
            add_event(conn, r["project_id"], r["id"], r["document_id"], "job", "failed",
                      {"reason": "worker restarted while this job was running"})
    return [r["id"] for r in rows]


def get_job(conn, job_id):
    return row(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def project_jobs(conn, project_id):
    return [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at", (project_id,))]


# ------------------------------------------------------------------ events (timeline) & runs
def add_event(conn, project_id, job_id, document_id, step, status, detail=None):
    conn.execute("INSERT INTO events (project_id,job_id,document_id,ts,step,status,detail) VALUES (?,?,?,?,?,?,?)",
                 (project_id, job_id, document_id, now(), step, status, dumps(detail or {})))


def events_after(conn, project_id, after_id=0, limit=500):
    return [dict(r, detail=json.loads(r["detail"])) for r in conn.execute(
        "SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id LIMIT ?", (project_id, after_id, limit))]


def add_run(conn, job_id, document_id, attempt, kind, envelope, model=None):
    conn.execute("INSERT INTO runs (job_id,document_id,attempt,kind,model,envelope,created_at) VALUES (?,?,?,?,?,?,?)",
                 (job_id, document_id, attempt, kind, model, dumps(envelope), now()))


# ------------------------------------------------------------------ extraction & results
def save_extraction(conn, document_id, attempts, model, code, output):
    conn.execute("INSERT OR REPLACE INTO extractions (document_id,attempts,model,code,code_sha256,output,created_at) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (document_id, attempts, model, code,
                  hashlib.sha256(code.encode()).hexdigest() if code else None,
                  dumps(output) if output is not None else None, now()))


def get_extraction(conn, document_id):
    r = row(conn.execute("SELECT * FROM extractions WHERE document_id=?", (document_id,)).fetchone())
    if r and r["output"]:
        r["output"] = json.loads(r["output"])
    return r


def save_results(conn, document_id, l1, l2):
    conn.execute("INSERT OR REPLACE INTO results (document_id,l1,l2,updated_at) VALUES (?,?,?,?)",
                 (document_id, dumps(l1), dumps(l2), now()))


def get_results(conn, document_id):
    r = row(conn.execute("SELECT * FROM results WHERE document_id=?", (document_id,)).fetchone())
    if r:
        r["l1"], r["l2"] = json.loads(r["l1"]), json.loads(r["l2"])
    return r


# ------------------------------------------------------------------ findings (review queue)
def fingerprint(document_id, layer, f):
    key = dumps([document_id, layer, f.get("code"), f.get("path"), f.get("ref"), f.get("check"), f.get("id")])
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def sync_findings(conn, project_id, document_id, layer, findings):
    """Replace one layer's findings for a document while KEEPING reviewer decisions:
    a finding with the same fingerprint keeps its status; findings that no longer
    occur become 'resolved' (unless a reviewer already decided them)."""
    with tx(conn):
        existing = {r["fingerprint"]: dict(r) for r in conn.execute(
            "SELECT * FROM findings WHERE project_id=? AND document_id IS ? AND layer=?",
            (project_id, document_id, layer))}
        seen, occurrences = set(), {}
        for f in findings:
            base = fingerprint(document_id, layer, f)
            # identical findings in one batch (e.g. several patterns in one cell) get a stable occurrence number
            n = occurrences.get(base, 0)
            occurrences[base] = n + 1
            fp = base if n == 0 else hashlib.sha256(f"{base}:{n}".encode()).hexdigest()[:32]
            seen.add(fp)
            if fp in existing:
                status = existing[fp]["status"]
                if status == "resolved":
                    status = "open"
                conn.execute("UPDATE findings SET detail=?, severity=?, status=? WHERE id=?",
                             (f.get("detail", ""), f.get("severity", "review"), status, existing[fp]["id"]))
            else:
                conn.execute("INSERT INTO findings (id,project_id,document_id,fingerprint,layer,code,severity,path,ref,"
                             "detail,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (new_id(), project_id, document_id, fp, layer, f["code"], f.get("severity", "review"),
                              f.get("path"), f.get("ref"), f.get("detail", ""), now()))
        for fp, r in existing.items():
            if fp not in seen and r["status"] == "open":
                conn.execute("UPDATE findings SET status='resolved' WHERE id=?", (r["id"],))


def list_findings(conn, project_id, status=None):
    q, args = "SELECT * FROM findings WHERE project_id=?", [project_id]
    if status:
        q += " AND status=?"
        args.append(status)
    return [dict(r) for r in conn.execute(q + " ORDER BY created_at", args)]


def set_finding_status(conn, finding_id, status):
    conn.execute("UPDATE findings SET status=? WHERE id=?", (status, finding_id))


# ------------------------------------------------------------------ review (Layer 3)
def record_review(conn, project_id, target_type, target_id, action, reason, before, after, reviewer):
    if not reason or not reason.strip():
        raise ValueError("a reason is required for every review action")
    if not reviewer or not reviewer.strip():
        raise ValueError("reviewer name is required")
    conn.execute("INSERT INTO review_actions (project_id,target_type,target_id,action,reason,before,after,reviewer,at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (project_id, target_type, target_id, action, reason.strip(), dumps(before), dumps(after),
                  reviewer.strip(), now()))


def review_log(conn, project_id):
    return [dict(r) for r in conn.execute("SELECT * FROM review_actions WHERE project_id=? ORDER BY id", (project_id,))]


def set_override(conn, document_id, path, value_str, kind, cite=None):
    conn.execute("INSERT OR REPLACE INTO overrides (document_id,path,value_str,kind,cite) VALUES (?,?,?,?,?)",
                 (document_id, path, value_str, kind, cite))


def get_overrides(conn, document_id):
    return {r["path"]: dict(r) for r in conn.execute("SELECT * FROM overrides WHERE document_id=?", (document_id,))}


def set_exclusion(conn, document_id, item_id, reason, reviewer):
    """A reviewer decided this extracted 'line item' is not one (e.g. a note row). Never automatic."""
    if not reason or not reason.strip() or not reviewer or not reviewer.strip():
        raise ValueError("reason and reviewer are required")
    conn.execute("INSERT OR REPLACE INTO exclusions (document_id,item_id,reason,reviewer,at) VALUES (?,?,?,?,?)",
                 (document_id, item_id, reason.strip(), reviewer.strip(), now()))


def get_exclusions(conn, document_id):
    return {r["item_id"]: dict(r) for r in conn.execute("SELECT * FROM exclusions WHERE document_id=?", (document_id,))}


# ------------------------------------------------------------------ matching
def add_match_group(conn, project_id, label, members, source, status="proposed", reason=None, confidence=None,
                    model=None):
    gid = new_id()
    conn.execute("INSERT INTO match_groups (id,project_id,label,reason,confidence,model,status,source,created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (gid, project_id, label, reason, confidence, model, status, source, now()))
    for m in members:
        conn.execute("INSERT INTO match_members (group_id,document_id,item_id) VALUES (?,?,?)",
                     (gid, m["doc_id"], m["item_id"]))
    return gid


def set_match_status(conn, group_id, status, reviewer=None):
    if status not in ("proposed", "confirmed", "rejected"):
        raise ValueError(status)
    conn.execute("UPDATE match_groups SET status=?, confirmed_by=?, confirmed_at=? WHERE id=?",
                 (status, reviewer if status == "confirmed" else None, now() if status == "confirmed" else None,
                  group_id))


def match_groups(conn, project_id):
    out = []
    for g in conn.execute("SELECT * FROM match_groups WHERE project_id=? ORDER BY created_at", (project_id,)):
        g = dict(g)
        g["group_id"] = g["id"]
        g["members"] = [{"doc_id": m["document_id"], "item_id": m["item_id"]} for m in conn.execute(
            "SELECT * FROM match_members WHERE group_id=?", (g["id"],))]
        out.append(g)
    return out


def clear_ai_proposals(conn, project_id):
    conn.execute("DELETE FROM match_groups WHERE project_id=? AND source='ai' AND status='proposed'", (project_id,))


# ------------------------------------------------------------------ self-tests & explanations
def save_selftest(conn, job_id, report):
    sid = new_id()
    conn.execute("INSERT INTO selftests (id,job_id,passed,report,created_at) VALUES (?,?,?,?,?)",
                 (sid, job_id, 1 if report.get("passed") else 0, dumps(report), now()))
    return sid


def latest_selftest(conn):
    r = row(conn.execute("SELECT * FROM selftests ORDER BY created_at DESC LIMIT 1").fetchone())
    if r:
        r["report"] = json.loads(r["report"])
    return r


def save_explanation(conn, project_id, text, status, model, attempts):
    conn.execute("INSERT OR REPLACE INTO explanations (project_id,text,status,model,attempts,created_at) "
                 "VALUES (?,?,?,?,?,?)", (project_id, text, status, model, attempts, now()))


def get_explanation(conn, project_id):
    return row(conn.execute("SELECT * FROM explanations WHERE project_id=?", (project_id,)).fetchone())


# ------------------------------------------------------------------ retention
def expired_projects(conn, at=None):
    return [r["id"] for r in conn.execute("SELECT id FROM projects WHERE expires_at < ?", (at or now(),))]


def delete_project(conn, project_id):
    with tx(conn):
        doc_ids = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE project_id=?", (project_id,))]
        conn.execute("DELETE FROM events WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM runs WHERE document_id IN (SELECT id FROM documents WHERE project_id=?)",
                     (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return doc_ids
