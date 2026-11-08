"""Store one uploaded document the way qp-web will (prototype of the upload path).

Run AS qp-web, reading the document from stdin:
  sudo -u qp-web python3 -I /opt/qp/app/tools/qp_put_upload.py --type csv < file.csv

  * name on disk = random UUID (the original filename is never used in a path)
  * temp file opened O_CREAT|O_EXCL|O_NOFOLLOW, SHA-256 computed while streaming,
    size-capped, fsync'd, chmod 0640, then atomically renamed into place.
Prints JSON: {document_id, sha256, bytes, declared_type}
"""

import argparse
import hashlib
import json
import os
import sys
import uuid

UPLOADS = "/srv/qp/uploads"
MAX_BYTES = 10 * 1024 * 1024
FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", required=True, choices=["csv", "xlsx", "pdf"])
    ap.add_argument("--uploads", default=UPLOADS)
    args = ap.parse_args()

    doc_id = str(uuid.uuid4())
    tmp_name = f".tmp-{doc_id}"
    dfd = os.open(args.uploads, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fd = os.open(tmp_name, FLAGS, 0o600, dir_fd=dfd)
    digest, total = hashlib.sha256(), 0
    try:
        src = sys.stdin.buffer
        while True:
            chunk = src.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise SystemExit(json.dumps({"ok": False, "error": "file_too_large"}))
            digest.update(chunk)
            os.write(fd, chunk)
        os.fsync(fd)
        os.fchmod(fd, 0o640)
    except BaseException:
        os.close(fd)
        os.unlink(tmp_name, dir_fd=dfd)
        os.close(dfd)
        raise
    os.close(fd)
    os.rename(tmp_name, doc_id, src_dir_fd=dfd, dst_dir_fd=dfd)
    os.fsync(dfd)
    os.close(dfd)
    print(json.dumps({"ok": True, "document_id": doc_id, "sha256": digest.hexdigest(),
                      "bytes": total, "declared_type": args.type}))


if __name__ == "__main__":
    main()
