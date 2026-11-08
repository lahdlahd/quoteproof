#!/usr/bin/env python3
"""TEST-ONLY stand-in for the docker CLI (this workspace has no Docker daemon).

It simulates container lifecycle (run / inspect / kill / rm) so the supervisor's
real code paths can be exercised. It does NOT provide any isolation; the
containment self-test can only be passed on the real VM.

Behaviour of `run` depends on the image's mode argument:
  index       -> runs the real indexer.py on <mount>/document
  extractor   -> runs <mount>/main.py with python (unsandboxed!) in a temp cwd
  selftest-*  -> runs the real probe script (unsandboxed, so it should FAIL checks)
Special FAKE_BEHAVIOUR env var (set by tests): flood | hang | crash
"""

import json
import os
import signal
import subprocess
import sys

STATE = os.environ.get("FAKE_DOCKER_STATE", "/tmp/fake_docker_state")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(STATE, exist_ok=True)


def state_path(name):
    return os.path.join(STATE, name + ".json")


def save(name, obj):
    with open(state_path(name), "w") as f:
        json.dump(obj, f)


def load(name):
    with open(state_path(name)) as f:
        return json.load(f)


def cmd_run(args):
    name = args[args.index("--name") + 1]
    runtime = args[args.index("--runtime") + 1]
    mount = next((a for a in args if a.startswith("type=bind,")), None)
    src = dict(kv.split("=", 1) for kv in mount.split(",") if "=" in kv)["source"] if mount else None
    image, mode = args[-2], args[-1]
    st = {"State": {"ExitCode": None, "OOMKilled": False}, "HostConfig": {"Runtime": runtime},
          "Image": image, "Id": "f" * 64, "argv": args}
    behaviour = os.environ.get("FAKE_BEHAVIOUR", "")
    if behaviour == "flood":
        cmd = [sys.executable, "-c", "import sys\nfor _ in range(400): sys.stdout.write('x'*65536)"]
    elif behaviour == "hang":
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    elif behaviour == "crash":
        cmd = [sys.executable, "-c", "import sys; sys.stderr.write('boom api_key=SHOULDNOTLEAK'); sys.exit(3)"]
    elif mode == "index":
        cmd = [sys.executable, os.path.join(ROOT, "images/indexer/indexer.py"),
               os.path.join(src, "document"), os.path.join(src, "meta.json")]
    elif mode == "extractor":
        cmd = [sys.executable, "-I", os.path.join(src, "main.py")]
    elif mode.startswith("selftest"):
        cmd = [sys.executable, os.path.join(ROOT, "images/runner/selftest_probe.py"),
               "sleep" if mode == "selftest-sleep" else "probe"]
    else:
        print("unknown mode", file=sys.stderr)
        return 125
    proc = subprocess.Popen(cmd, cwd="/tmp")
    st["pid"] = proc.pid
    save(name, st)
    rc = proc.wait()
    st = load(name)
    st["State"]["ExitCode"] = 137 if rc < 0 else rc
    save(name, st)
    return st["State"]["ExitCode"]


def main():
    args = sys.argv[1:]
    if not args:
        return 1
    c = args[0]
    if c == "run":
        return cmd_run(args[1:])
    if c == "inspect":
        name = args[-1]
        if not os.path.exists(state_path(name)):
            print(f"Error: No such object: {name}", file=sys.stderr)
            return 1
        print(json.dumps([load(name)]))
        return 0
    if c == "kill":
        try:
            os.kill(load(args[-1])["pid"], signal.SIGKILL)
        except (OSError, KeyError, FileNotFoundError):
            pass
        return 0
    if c == "rm":
        try:
            os.remove(state_path(args[-1]))
        except FileNotFoundError:
            pass
        return 0
    if c == "info":
        print(json.dumps({"runc": {}, "runsc": {}}))
        return 0
    if c == "image":
        return 0
    if c == "ps":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
