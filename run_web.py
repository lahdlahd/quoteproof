"""Entry point used by systemd for qp-web (runs with the qp-web venv's `python -I`).
Listens on 127.0.0.1 only; Caddy terminates HTTPS in front of it."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402

from qp.web import create_app  # noqa: E402

uvicorn.run(create_app(os.environ.get("QP_WEB_CONFIG", "/etc/qp-web/web.json")), host="127.0.0.1", port=8080,
            proxy_headers=True, forwarded_allow_ips="127.0.0.1", server_header=False, access_log=False,
            workers=1, limit_concurrency=64, timeout_keep_alive=5)
