"""Entry point for the sandboxd client (see sandboxd/client.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sandboxd.client import main  # noqa: E402

sys.exit(main())
