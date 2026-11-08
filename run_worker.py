"""Entry point used by systemd for qp-worker (runs with `python3 -I`)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qp.worker import main  # noqa: E402

main()
