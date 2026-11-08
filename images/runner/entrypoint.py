"""Fixed entrypoint of the runner image (extractor + self-test).

1. Accepts only three fixed modes.
2. Sets in-process resource limits (second layer behind Docker's cgroup limits).
3. Replaces the environment with a minimal, secret-free one.
4. exec()s Python in isolated mode on a fixed target file.
"""

import os
import resource
import sys

TARGETS = {
    "extractor": ["/input/main.py"],
    "selftest-probe": ["/opt/qp/selftest_probe.py", "probe"],
    "selftest-sleep": ["/opt/qp/selftest_probe.py", "sleep"],
}

CLEAN_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}


def set_limit(res, value):
    soft, hard = resource.getrlimit(res)
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    resource.setrlimit(res, (value, value))


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in TARGETS:
        print('{"error":"unknown_mode"}')
        sys.exit(2)
    set_limit(resource.RLIMIT_CPU, 15)                 # seconds of CPU
    set_limit(resource.RLIMIT_FSIZE, 8 * 1024 * 1024)  # largest file it may write (in /tmp)
    set_limit(resource.RLIMIT_NPROC, 64)               # backup to the cgroup pids limit (32)
    set_limit(resource.RLIMIT_NOFILE, 64)
    set_limit(resource.RLIMIT_CORE, 0)
    target = TARGETS[sys.argv[1]]
    os.execve("/usr/local/bin/python3", ["python3", "-I", "-B", *target], CLEAN_ENV)


if __name__ == "__main__":
    main()
