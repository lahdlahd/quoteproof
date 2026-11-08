"""qp-worker's client for qp-sandboxd (the only three commands that exist)."""

import hashlib
import uuid

from sandboxd import client as _client

DEFAULT_SOCKET = _client.DEFAULT_SOCKET


class SandboxError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


class SandboxClient:
    def __init__(self, socket_path=DEFAULT_SOCKET, timeout=180):
        self.socket_path, self.timeout = socket_path, timeout

    def _call(self, req):
        try:
            resp = _client.call(req, self.socket_path, timeout=self.timeout)
        except PermissionError:
            raise SandboxError("socket_permission_denied") from None
        except FileNotFoundError:
            raise SandboxError("sandboxd_not_running") from None
        except (OSError, ValueError) as e:
            raise SandboxError("sandboxd_unreachable", type(e).__name__) from None
        if not resp.get("ok"):
            raise SandboxError(resp.get("error", "unknown_error"), resp.get("detail", ""))
        return resp["result"]

    def index_document(self, document_id, sha256, declared_type, job_id=None):
        return self._call({"cmd": "index_document", "job_id": job_id or str(uuid.uuid4()),
                           "document_id": document_id, "expected_sha256": sha256, "declared_type": declared_type})

    def run_extractor(self, index_json, code, job_id=None):
        return self._call({"cmd": "run_extractor", "job_id": job_id or str(uuid.uuid4()), "index_json": index_json,
                           "index_sha256": hashlib.sha256(index_json.encode("utf-8")).hexdigest(), "code": code})

    def self_test(self):
        return self._call({"cmd": "self_test"})


def envelope_summary(env):
    """The evidence fields shown on the timeline for every sandbox run."""
    keys = ("status", "exit_code", "oom_killed", "timed_out", "duration_ms", "runtime", "image_digest",
            "container_id", "removed", "staging_removed", "stdout_sha256")
    return {k: env.get(k) for k in keys if k in env}
