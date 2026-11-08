"""Request validation for the three sandboxd commands.

Every request is one JSON object. Unknown commands, missing fields and
extra fields are all rejected, so a caller cannot smuggle in options.
"""

import hashlib
import json
import re

from . import limits

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DECLARED_TYPES = {"csv", "xlsx", "pdf"}

# command -> exact set of required fields (besides "cmd")
COMMANDS = {
    "index_document": {"job_id", "document_id", "expected_sha256", "declared_type"},
    "run_extractor": {"job_id", "index_json", "index_sha256", "code"},
    "self_test": set(),
}


class ProtocolError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def parse_request(raw: bytes) -> dict:
    """Parse and strictly validate one request. Returns a clean dict."""
    if len(raw) > limits.MAX_REQUEST_BYTES:
        raise ProtocolError("request_too_large", f"limit {limits.MAX_REQUEST_BYTES} bytes")
    try:
        req = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProtocolError("invalid_json")
    if not isinstance(req, dict):
        raise ProtocolError("invalid_request", "request must be a JSON object")

    cmd = req.get("cmd")
    if cmd not in COMMANDS:
        raise ProtocolError("unknown_command", "allowed: " + ", ".join(sorted(COMMANDS)))

    fields = set(req) - {"cmd"}
    expected = COMMANDS[cmd]
    if fields != expected:
        missing = sorted(expected - fields)
        extra = sorted(fields - expected)
        raise ProtocolError("invalid_fields", f"missing={missing} extra={extra}")

    for key in fields:
        if not isinstance(req[key], str):
            raise ProtocolError("invalid_field_type", f"{key} must be a string")

    if "job_id" in req and not UUID_RE.match(req["job_id"]):
        raise ProtocolError("invalid_job_id", "must be a lowercase UUID")

    if cmd == "index_document":
        if not UUID_RE.match(req["document_id"]):
            raise ProtocolError("invalid_document_id", "must be a lowercase UUID")
        if not SHA256_RE.match(req["expected_sha256"]):
            raise ProtocolError("invalid_sha256")
        if req["declared_type"] not in DECLARED_TYPES:
            raise ProtocolError("invalid_declared_type", "allowed: csv, xlsx, pdf")

    if cmd == "run_extractor":
        if not SHA256_RE.match(req["index_sha256"]):
            raise ProtocolError("invalid_sha256")
        index_bytes = req["index_json"].encode("utf-8")
        code_bytes = req["code"].encode("utf-8")
        if len(index_bytes) > limits.MAX_INDEX_JSON_BYTES:
            raise ProtocolError("index_too_large")
        if len(code_bytes) > limits.MAX_CODE_BYTES:
            raise ProtocolError("code_too_large", f"limit {limits.MAX_CODE_BYTES} bytes")
        if hashlib.sha256(index_bytes).hexdigest() != req["index_sha256"]:
            raise ProtocolError("index_hash_mismatch")

    return req
