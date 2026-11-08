"""End-to-end through the real socket server, staging and lifecycle code, with a FAKE docker.

What this proves: request handling, peer checks, staging, output caps, timeouts,
inspect/remove/verify and cleanup logic all work.
What this does NOT prove: any containment. The fake docker runs code unsandboxed,
and the self-test is expected to FAIL here (see test_self_test_fails_without_sandbox).
"""
import hashlib
import json
import os
import pwd
import shutil
import socket
import threading
import time
import uuid

import pytest

from sandboxd import client, limits
from sandboxd import server as srv
from sandboxd.dockerrun import Docker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fake_docker.py")
FIX = os.path.join(ROOT, "fixtures")


@pytest.fixture
def env(tmp_path, monkeypatch):
    os.chmod(FAKE, 0o755)
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "fakestate"))
    uploads = tmp_path / "uploads"; uploads.mkdir(mode=0o750)
    stage = tmp_path / "staging"; stage.mkdir(); os.chmod(stage, 0o711)
    home = tmp_path / "home"; home.mkdir()
    me = pwd.getpwuid(os.getuid()).pw_name
    cfg_path = tmp_path / "sandboxd.json"
    cfg_path.write_text(json.dumps({
        "isolation_mode": "gvisor",
        "images": {"runner": "sha256:" + "1" * 64, "indexer": "sha256:" + "2" * 64},
        "socket_path": str(tmp_path / "s.sock"), "uploads_dir": str(uploads), "staging_dir": str(stage),
        "uploads_owner": me, "allowed_client_users": [me], "home": str(home), "docker_bin": FAKE}))
    cfg = srv.Config(str(cfg_path))
    docker = Docker(FAKE, str(home))
    docker.env = {**docker.env, "FAKE_DOCKER_STATE": str(tmp_path / "fakestate"),
                  "PATH": os.environ["PATH"]}
    sup = srv.Supervisor(cfg, docker)
    sup.startup_checks()
    t = threading.Thread(target=srv.serve, args=(sup,), daemon=True)
    t.start()
    for _ in range(100):
        if os.path.exists(cfg.socket_path):
            break
        time.sleep(0.02)
    yield {"sup": sup, "sock": cfg.socket_path, "uploads": str(uploads), "stage": str(stage), "docker": docker}
    os.environ.pop("QP_CANARY", None)


def upload(env, fixture):
    doc = str(uuid.uuid4())
    data = open(os.path.join(FIX, fixture), "rb").read()
    with open(os.path.join(env["uploads"], doc), "wb") as f:
        f.write(data)
    return doc, hashlib.sha256(data).hexdigest()


def call(env, req):
    return client.call(req, env["sock"])


def test_socket_permissions_0660(env):
    assert oct(os.stat(env["sock"]).st_mode & 0o777) == "0o660"


def test_index_xlsx_end_to_end(env):
    pytest.importorskip("openpyxl")
    doc, sha = upload(env, "usd_quote.xlsx")
    r = call(env, {"cmd": "index_document", "job_id": str(uuid.uuid4()), "document_id": doc,
                   "expected_sha256": sha, "declared_type": "xlsx"})
    assert r["ok"], r
    res = r["result"]
    assert res["status"] == "ok" and res["removed"] is True and res["staging_removed"] is True
    assert res["staged_sha256"] == sha
    idx = json.loads(res["stdout"])
    assert idx["ok"] and idx["source_sha256"] == sha
    assert os.listdir(env["stage"]) == []


def test_hash_mismatch_never_starts_container(env):
    doc, _ = upload(env, "ngn_quote.csv")
    r = call(env, {"cmd": "index_document", "job_id": str(uuid.uuid4()), "document_id": doc,
                   "expected_sha256": "0" * 64, "declared_type": "csv"})
    res = r["result"]
    assert res["status"] == "staging_rejected" and res["error"] == "hash_mismatch"
    assert res["container_started"] is False and res["staging_removed"] is True


def test_unknown_command_rejected(env):
    r = call(env, b'{"cmd":"docker","args":["run","--privileged","alpine"]}')
    assert r == {"ok": False, "error": "unknown_command", "detail": "allowed: index_document, run_extractor, self_test"}


def test_extractor_runs_and_output_returned(env):
    idx = json.dumps({"schema": "test"})
    # Reads index.json next to main.py: /input/index.json in a real container,
    # the staging folder under the fake docker.
    code = ("import json, os\n"
            "p = os.path.join(os.path.dirname(__file__), 'index.json')\n"
            "print(json.dumps({'items': [], 'read': json.load(open(p))['schema']}))\n")
    r = call(env, {"cmd": "run_extractor", "job_id": str(uuid.uuid4()), "index_json": idx,
                   "index_sha256": hashlib.sha256(idx.encode()).hexdigest(), "code": code})
    res = r["result"]
    assert res["status"] == "ok", res
    assert json.loads(res["stdout"]) == {"items": [], "read": "test"}


def test_output_flood_is_capped_and_killed(env, monkeypatch):
    env["docker"].env["FAKE_BEHAVIOUR"] = "flood"
    idx = "{}"
    r = call(env, {"cmd": "run_extractor", "job_id": str(uuid.uuid4()), "index_json": idx,
                   "index_sha256": hashlib.sha256(idx.encode()).hexdigest(), "code": "pass"})
    res = r["result"]
    assert res["status"] == "output_limit_exceeded" and res["stdout"] is None
    assert res["stdout_bytes"] > limits.PROFILES["extractor"]["stdout_cap"]
    assert res["removed"] is True


def test_hang_is_killed_at_timeout(env, monkeypatch):
    env["docker"].env["FAKE_BEHAVIOUR"] = "hang"
    monkeypatch.setitem(limits.PROFILES["extractor"], "timeout_s", 2)
    idx = "{}"
    t0 = time.time()
    r = call(env, {"cmd": "run_extractor", "job_id": str(uuid.uuid4()), "index_json": idx,
                   "index_sha256": hashlib.sha256(idx.encode()).hexdigest(), "code": "pass"})
    res = r["result"]
    assert res["status"] == "timeout" and res["timed_out"] is True
    assert time.time() - t0 < 10 and res["removed"] is True


def test_crash_stderr_is_sanitized(env):
    env["docker"].env["FAKE_BEHAVIOUR"] = "crash"
    idx = "{}"
    r = call(env, {"cmd": "run_extractor", "job_id": str(uuid.uuid4()), "index_json": idx,
                   "index_sha256": hashlib.sha256(idx.encode()).hexdigest(), "code": "pass"})
    res = r["result"]
    assert res["status"] == "nonzero_exit" and res["exit_code"] == 3
    assert "SHOULDNOTLEAK" not in res["stderr_tail"]


def test_self_test_fails_without_sandbox(env):
    """Honesty check: run unsandboxed (fake docker), the self-test must NOT pass."""
    r = call(env, {"cmd": "self_test"})
    rep = r["result"]
    verdicts = {t["test"]: t["verdict"] for t in rep["tests"]}
    assert rep["passed"] is False
    assert verdicts["read_only_filesystem"] == "FAIL"
    assert verdicts["privileges"] in ("FAIL", "INCONCLUSIVE")
    assert verdicts["secret_isolation"] == "FAIL"   # the canary is in this process's environment
    assert verdicts["lifecycle_cleanup"] == "PASS"


def test_rejects_disallowed_peer(env, monkeypatch):
    monkeypatch.setattr(env["sup"].cfg, "allowed_uids", {987654})
    r = call(env, {"cmd": "self_test"})
    assert r == {"ok": False, "error": "client_not_allowed"}


def test_fail_closed_when_runtime_missing(env, monkeypatch):
    monkeypatch.setattr(env["docker"], "runtimes", lambda: ["runc"])
    with pytest.raises(SystemExit) as e:
        env["sup"].startup_checks()
    assert "FAIL CLOSED" in str(e.value)
