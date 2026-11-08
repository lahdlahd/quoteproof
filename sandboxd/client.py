"""qpctl: tiny command-line client for qp-sandboxd (used by tests and, later, by qp-worker).

Examples (run as the qp-worker user):
  python3 -I /opt/qp/app/qpctl.py selftest
  python3 -I /opt/qp/app/qpctl.py index --document-id <uuid> --sha256 <hex> --type xlsx
  python3 -I /opt/qp/app/qpctl.py extract --index-file idx.json --code-file main.py
  python3 -I /opt/qp/app/qpctl.py raw '{"cmd":"bogus"}'      # for negative tests
"""

import argparse
import hashlib
import json
import socket
import sys
import uuid

DEFAULT_SOCKET = "/run/qp-sandboxd/sandboxd.sock"


def call(request, socket_path=DEFAULT_SOCKET, timeout=180):
    data = request if isinstance(request, bytes) else json.dumps(request).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(socket_path)
        s.sendall(data + b"\n")
        buf = bytearray()
        while True:
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n"):
                break
    return json.loads(buf.decode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qpctl")
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    p = sub.add_parser("index")
    p.add_argument("--document-id", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--job-id", default=None)
    e = sub.add_parser("extract")
    e.add_argument("--index-file", required=True)
    e.add_argument("--code-file", required=True)
    e.add_argument("--job-id", default=None)
    r = sub.add_parser("raw")
    r.add_argument("payload")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        req = {"cmd": "self_test"}
    elif args.cmd == "index":
        req = {"cmd": "index_document", "job_id": args.job_id or str(uuid.uuid4()),
               "document_id": args.document_id, "expected_sha256": args.sha256,
               "declared_type": args.type}
    elif args.cmd == "extract":
        index_json = open(args.index_file, encoding="utf-8").read()
        code = open(args.code_file, encoding="utf-8").read()
        req = {"cmd": "run_extractor", "job_id": args.job_id or str(uuid.uuid4()),
               "index_json": index_json,
               "index_sha256": hashlib.sha256(index_json.encode("utf-8")).hexdigest(),
               "code": code}
    else:
        req = args.payload.encode("utf-8")

    try:
        resp = call(req, args.socket)
    except PermissionError:
        print(json.dumps({"ok": False, "error": "socket_permission_denied"}))
        return 3
    except FileNotFoundError:
        print(json.dumps({"ok": False, "error": "socket_not_found"}))
        return 3
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
