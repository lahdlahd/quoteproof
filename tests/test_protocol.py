"""The supervisor accepts exactly three commands with exact fields."""
import hashlib
import json
import uuid

import pytest

from sandboxd import limits
from sandboxd.protocol import ProtocolError, parse_request

JOB = str(uuid.uuid4())
DOC = str(uuid.uuid4())
SHA = "a" * 64


def raw(obj):
    return json.dumps(obj).encode()


def test_valid_index_request():
    r = parse_request(raw({"cmd": "index_document", "job_id": JOB, "document_id": DOC,
                           "expected_sha256": SHA, "declared_type": "xlsx"}))
    assert r["cmd"] == "index_document"


def test_valid_self_test():
    assert parse_request(raw({"cmd": "self_test"}))["cmd"] == "self_test"


def test_valid_extractor_request():
    idx = '{"schema":"x"}'
    r = parse_request(raw({"cmd": "run_extractor", "job_id": JOB, "index_json": idx,
                           "index_sha256": hashlib.sha256(idx.encode()).hexdigest(), "code": "print(1)"}))
    assert r["cmd"] == "run_extractor"


@pytest.mark.parametrize("payload,code", [
    ({"cmd": "docker_run", "image": "alpine"}, "unknown_command"),
    ({"cmd": "self_test", "runtime": "runc"}, "invalid_fields"),          # extra field
    ({"cmd": "index_document", "job_id": JOB, "document_id": DOC,
      "expected_sha256": SHA, "declared_type": "xlsx", "mounts": ["/"]}, "invalid_fields"),
    ({"cmd": "index_document", "job_id": JOB, "document_id": "../../etc/passwd",
      "expected_sha256": SHA, "declared_type": "xlsx"}, "invalid_document_id"),
    ({"cmd": "index_document", "job_id": JOB, "document_id": DOC.upper(),
      "expected_sha256": SHA, "declared_type": "xlsx"}, "invalid_document_id"),
    ({"cmd": "index_document", "job_id": JOB, "document_id": DOC,
      "expected_sha256": "xyz", "declared_type": "xlsx"}, "invalid_sha256"),
    ({"cmd": "index_document", "job_id": JOB, "document_id": DOC,
      "expected_sha256": SHA, "declared_type": "exe"}, "invalid_declared_type"),
    ({"cmd": "index_document", "job_id": "1; rm -rf /", "document_id": DOC,
      "expected_sha256": SHA, "declared_type": "csv"}, "invalid_job_id"),
    ({"cmd": "index_document", "job_id": JOB, "document_id": DOC,
      "expected_sha256": SHA, "declared_type": 5}, "invalid_field_type"),
    ({"cmd": "run_extractor", "job_id": JOB, "index_json": "{}",
      "index_sha256": SHA, "code": "x"}, "index_hash_mismatch"),
])
def test_rejections(payload, code):
    with pytest.raises(ProtocolError) as e:
        parse_request(raw(payload))
    assert e.value.code == code


def test_code_too_large():
    idx = "{}"
    big = "#" * (limits.MAX_CODE_BYTES + 1)
    with pytest.raises(ProtocolError) as e:
        parse_request(raw({"cmd": "run_extractor", "job_id": JOB, "index_json": idx,
                           "index_sha256": hashlib.sha256(idx.encode()).hexdigest(), "code": big}))
    assert e.value.code == "code_too_large"


def test_request_too_large():
    with pytest.raises(ProtocolError) as e:
        parse_request(b"x" * (limits.MAX_REQUEST_BYTES + 1))
    assert e.value.code == "request_too_large"


def test_not_json():
    with pytest.raises(ProtocolError) as e:
        parse_request(b"\xff\xfe not json")
    assert e.value.code == "invalid_json"
