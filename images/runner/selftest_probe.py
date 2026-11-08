"""Containment self-test probe (runs INSIDE the sandbox, written by us, never by the AI).

It only collects observations and prints them as JSON. It never decides
pass/fail: sandboxd judges the observations outside the sandbox
(sandboxd/selftest_eval.py). Every test is bounded and harmless:
no fork bombs, no memory exhaustion, no attempts to damage anything.
"""

import errno
import json
import os
import socket
import subprocess
import sys
import time

EXPECTED_PIDS_LIMIT = 32  # must match sandboxd/limits.py

HOST_PATHS = [
    "/var/lib/qp-sandbox/canary",
    "/etc/qp/sandboxd.json",
    "/srv/qp/uploads",
    "/srv/qp/staging",
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/qp-sandboxd/sandboxd.sock",
    "/opt/qp/app/sandboxd",
    "/root/.ssh",
    "/home",
]

WRITE_TARGETS = [
    "/probe/qp_write_test",   # directory is world-writable in the image: only a read-only mount can stop this
    "/etc/qp_write_test",
    "/usr/qp_write_test",
    "/qp_write_test",
    "/input/qp_write_test",
    "/tmp/qp_write_test",     # the one allowed writable path (small tmpfs)
]


def err_name(e):
    return errno.errorcode.get(e.errno, str(e.errno)) if getattr(e, "errno", None) else type(e).__name__


def network():
    out = {}
    try:
        socket.setdefaulttimeout(2)
        socket.getaddrinfo("api.vultrinference.com", 443)
        out["dns"] = {"ok": True}
    except OSError as e:
        out["dns"] = {"ok": False, "error": type(e).__name__, "errno": err_name(e)}
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(("1.1.1.1", 443))
        out["tcp"] = {"ok": True}
    except OSError as e:
        out["tcp"] = {"ok": False, "error": type(e).__name__, "errno": err_name(e)}
    finally:
        s.close()
    try:
        out["interfaces"] = sorted(name for _i, name in socket.if_nameindex())
    except OSError as e:
        out["interfaces"] = {"error": err_name(e)}
    return out


def env_info():
    pid1 = {}
    try:
        with open("/proc/1/environ", "rb") as f:
            for item in f.read().split(b"\0"):
                if b"=" in item:
                    k, v = item.split(b"=", 1)
                    pid1[k.decode(errors="replace")] = v.decode(errors="replace")
    except OSError as e:
        pid1 = {"_error": err_name(e)}
    return dict(os.environ), pid1


def host_paths():
    res = {}
    for p in HOST_PATHS:
        entry = {"exists": os.path.lexists(p)}
        if entry["exists"] and os.path.isdir(p) and not os.path.islink(p):
            # Base images ship some standard folders (e.g. an empty /home). Report how
            # many entries a folder holds so the judge can tell "empty image folder"
            # from "host folder mounted in".
            try:
                entry["dir_entries"] = len(os.listdir(p))
            except OSError as e:
                entry["list_error"] = err_name(e)
        if entry["exists"] and os.path.isfile(p):
            try:
                with open(p, "rb") as f:
                    entry["content_head"] = f.read(256).decode(errors="replace")
            except OSError as e:
                entry["read_error"] = err_name(e)
        res[p] = entry
    return res


def writes():
    res = {}
    for p in WRITE_TARGETS:
        try:
            with open(p, "w") as f:
                f.write("x")
            os.unlink(p)
            res[p] = {"ok": True}
        except OSError as e:
            res[p] = {"ok": False, "errno": err_name(e)}
    return res


def pids():
    cap = EXPECTED_PIDS_LIMIT + 5
    children, refused, error = [], False, None
    try:
        for _ in range(cap):
            try:
                children.append(subprocess.Popen(["sleep", "3"], stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            except OSError as e:
                refused, error = True, err_name(e)
                break
    finally:
        for c in children:
            c.kill()
        for c in children:
            c.wait()
    return {"attempt_cap": cap, "started": len(children), "refused": refused, "error": error}


def proc_status():
    out = {}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in ("Uid", "Gid", "CapEff", "CapPrm", "CapBnd", "NoNewPrivs", "Seccomp"):
                    out[k] = v.strip()
    except OSError as e:
        out["_error"] = err_name(e)
    return out


def dmesg():
    try:
        p = subprocess.run(["dmesg"], capture_output=True, timeout=5)
        text = (p.stdout or p.stderr).decode(errors="replace").splitlines()
        return {"rc": p.returncode, "head": text[:8]}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"rc": None, "head": [f"dmesg unavailable: {type(e).__name__}"]}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if mode == "sleep":
        time.sleep(30)  # sandboxd must kill us at ~5 s; this line should never finish
        print(json.dumps({"slept_full_30s": True}))
        return
    env, pid1_env = env_info()
    u = os.uname()
    report = {
        "identity": {"release": u.release, "version": u.version, "machine": u.machine},
        "proc_status": proc_status(),
        "network": network(),
        "env": env,
        "pid1_env": pid1_env,
        "host_paths": host_paths(),
        "writes": writes(),
        "pids": pids(),
        "dmesg": dmesg(),
    }
    print(json.dumps(report))


if __name__ == "__main__":
    main()
