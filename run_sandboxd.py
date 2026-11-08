"""Entry point used by systemd. Runs with `python3 -I`, so we add our own folder to sys.path explicitly."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sandboxd.server import main  # noqa: E402

main()
