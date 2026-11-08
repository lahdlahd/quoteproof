"""qp-sandboxd: Unix-socket server exposing exactly three commands.

Run by systemd as user qp-sandbox (see deploy/qp-sandboxd.service).
One request per connection, handled one at a time.
"""

import argparse
import grp
import json
import os
import pwd
import secrets
import socket
import struct
import sys
import time

from . import limits, protocol, selftest_eval, staging
from .dockerrun import Docker, DockerError, new_name


def log(event, **fields):
    """One JSON line per event to stderr (journald). Never log document contents or code."""
    print(json.dumps({"ts": round(time.time(), 3), "event": event, **fields}), file=sys.stderr, flush=True)


class Config:
    REQUIRED = {"isolation_mode", "images", "socket_path", "uploads_dir", "staging_dir",
                "uploads_owner", "allowed_client_users", "home"}

    def __init__(self, path):
        with open(path) as f:
            c = json.load(f)
        missing = self.REQUIRED - set(c)
        if missing:
            raise SystemExit(f"config missing keys: {sorted(missing)}")
        if c["isolation_mode"] not in limits.ISOLATION_MODES:
            raise SystemExit(f"isolation_mode must be one of {sorted(limits.ISOLATION_MODES)}")
        self.isolation_mode = c["isolation_mode"]
        self.runtime = limits.ISOLATION_MODES[self.isolation_mode]
        self.images = c["images"]
        if set(self.images) != {"runner", "indexer"}:
            raise SystemExit("images must contain exactly 'runner' and 'indexer'")
        self.socket_path = c["socket_path"]
        self.uploads_dir = c["uploads_dir"]
        self.staging_dir = c["staging_dir"]
        self.uploads_owner_uid = pwd.getpwnam(c["uploads_owner"]).pw_uid
        self.allowed_uids = {pwd.getpwnam(u).pw_uid for u in c["allowed_client_users"]} | {0}
        self.home = c["home"]
        self.docker_bin = c.get("docker_bin", "/usr/bin/docker")


class Supervisor:
    def __init__(self, cfg: Config, docker: Docker):
        self.cfg = cfg
        self.docker = docker
        # Canary: a random value that exists only in sandboxd's environment and
        # in a 0600 file in its home. The self-test proves it never reaches a sandbox.
        self.canary = "QPCANARY-" + secrets.token_hex(16)
        os.environ["QP_CANARY"] = self.canary
        canary_path = os.path.join(cfg.home, "canary")
        fd = os.open(canary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.canary)

    # ---------- startup ----------
    def startup_checks(self):
        staging.check_root(self.cfg.uploads_dir, owner_uid=self.cfg.uploads_owner_uid)
        staging.check_root(self.cfg.staging_dir, owner_uid=os.getuid(), exact_mode=0o711)
        leftovers = staging.clear_staging_root(self.cfg.staging_dir)
        runtimes = self.docker.runtimes()
        if self.cfg.runtime not in runtimes:
            raise SystemExit(f"FAIL CLOSED: runtime '{self.cfg.runtime}' not registered in Docker "
                             f"(have {runtimes}). Fix gVisor or set isolation_mode=runc-hardened explicitly.")
        for key, image_id in self.cfg.images.items():
            if not self.docker.image_exists(image_id):
                raise SystemExit(f"FAIL CLOSED: image {key}={image_id} not present")
        reaped = self.docker.reap_leftovers()
        log("startup_ok", isolation_mode=self.cfg.isolation_mode, runtime=self.cfg.runtime,
            docker_runtimes=runtimes, reaped_containers=len(reaped), cleared_staging=len(leftovers))

    # ---------- commands ----------
    def handle(self, req):
        cmd = req["cmd"]
        if cmd == "index_document":
            return self.index_document(req)
        if cmd == "run_extractor":
            return self.run_extractor(req)
        if cmd == "self_test":
            return self.self_test()
        raise protocol.ProtocolError("unknown_command")  # unreachable: validated earlier

    def _run_staged(self, profile, job_id, fill):
        """Create a run folder, let `fill` put inputs in it, run, always clean up."""
        name = new_name()
        run_id = name[3:]
        folder = staging.RunFolder(self.cfg.staging_dir, run_id)
        try:
            extra = fill(folder) or {}
            folder.seal()
            env = self.docker.run(profile, name=name, job_id=job_id, runtime=self.cfg.runtime,
                                  image_id=self.cfg.images[limits.PROFILES[profile]["image_key"]],
                                  input_dir=folder.in_path)
            env.update(extra)
        except staging.StagingError as e:
            env = {"status": "staging_rejected", "error": e.code, "detail": e.detail,
                   "container_started": False}
        finally:
            removed = folder.remove()
        env["staging_removed"] = removed
        log("run_finished", profile=profile, job_id=job_id, status=env.get("status"),
            exit_code=env.get("exit_code"), runtime=env.get("runtime"), duration_ms=env.get("duration_ms"),
            removed=env.get("removed"), staging_removed=removed, stdout_bytes=env.get("stdout_bytes"))
        return env

    def index_document(self, req):
        def fill(folder):
            sha, size = folder.copy_upload(self.cfg.uploads_dir, req["document_id"], req["expected_sha256"],
                                           expected_owner_uid=self.cfg.uploads_owner_uid)
            folder.write_bytes("meta.json", staging.meta_json(req["declared_type"]))
            return {"staged_sha256": sha, "staged_bytes": size}
        return self._run_staged("indexer", req["job_id"], fill)

    def run_extractor(self, req):
        def fill(folder):
            folder.write_bytes("index.json", req["index_json"].encode("utf-8"))
            folder.write_bytes("main.py", req["code"].encode("utf-8"))
            return {"index_sha256": req["index_sha256"]}
        return self._run_staged("extractor", req["job_id"], fill)

    def self_test(self):
        job_id = "00000000-0000-4000-8000-000000000000"
        note = b"QuoteProof self-test input folder. Writes here must fail.\n"
        probe = self._run_staged("selftest_probe", job_id, lambda f: f.write_bytes("README", note) and None)
        sleep = self._run_staged("selftest_sleep", job_id, lambda f: f.write_bytes("README", note) and None)
        report = selftest_eval.evaluate(
            probe_env=probe, sleep_env=sleep, canary=self.canary,
            host_kernel=os.uname().release, configured_runtime=self.cfg.runtime,
            isolation_mode=self.cfg.isolation_mode)
        # Do not return the raw probe stdout to callers beyond the evaluated observations.
        report["probe_envelope"] = {k: v for k, v in probe.items() if k != "stdout"}
        report["sleep_envelope"] = {k: v for k, v in sleep.items() if k != "stdout"}
        log("self_test_finished", passed=report["passed"],
            verdicts={t["test"]: t["verdict"] for t in report["tests"]})
        return report


# ---------- socket server ----------
def peer_uid(conn):
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def read_request(conn):
    buf = bytearray()
    conn.settimeout(30)
    while True:
        chunk = conn.recv(256 * 1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limits.MAX_REQUEST_BYTES + 1:
            raise protocol.ProtocolError("request_too_large")
        if buf.endswith(b"\n"):
            break
    return bytes(buf).rstrip(b"\n")


def send(conn, obj):
    conn.sendall(json.dumps(obj).encode("utf-8") + b"\n")


def serve(sup: Supervisor):
    path = sup.cfg.socket_path
    if os.path.lexists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o117)  # socket file 0660
    try:
        srv.bind(path)
    finally:
        os.umask(old)
    srv.listen(4)
    log("listening", socket=path)
    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                uid = peer_uid(conn)
                if uid not in sup.cfg.allowed_uids:
                    log("client_rejected", uid=uid)
                    send(conn, {"ok": False, "error": "client_not_allowed"})
                    continue
                req = protocol.parse_request(read_request(conn))
                log("request", cmd=req["cmd"], uid=uid, job_id=req.get("job_id"))
                result = sup.handle(req)
                send(conn, {"ok": True, "cmd": req["cmd"], "result": result})
            except protocol.ProtocolError as e:
                log("request_rejected", error=e.code)
                send(conn, {"ok": False, "error": e.code, "detail": e.detail})
            except (DockerError, OSError) as e:
                log("internal_error", error=type(e).__name__)
                try:
                    send(conn, {"ok": False, "error": "internal_error", "detail": type(e).__name__})
                except OSError:
                    pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="QuoteProof sandbox supervisor")
    ap.add_argument("--config", default="/etc/qp/sandboxd.json")
    args = ap.parse_args(argv)
    if os.getuid() == 0:
        raise SystemExit("refusing to run as root; run as qp-sandbox via systemd")
    cfg = Config(args.config)
    sup = Supervisor(cfg, Docker(cfg.docker_bin, cfg.home))
    sup.startup_checks()
    serve(sup)


if __name__ == "__main__":
    main()
