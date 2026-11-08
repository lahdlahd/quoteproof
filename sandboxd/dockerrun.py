"""Builds the fixed `docker run` command and manages one container's lifecycle.

Security notes:
  * The argv is built ONLY from limits.PROFILES, the configured runtime and
    image ID, and folder paths that sandboxd itself created. Nothing from a
    request reaches this list.
  * docker is called without a shell, with a minimal environment.
  * stdout/stderr are read with byte caps; going over a cap kills the
    container. A wall-clock timeout also kills it.
  * After every run: inspect (exit code, OOM flag, runtime, image), remove,
    then inspect again to prove the container no longer exists.
"""

import hashlib
import json
import os
import re
import subprocess
import threading
import time

from . import limits

IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NAME_RE = re.compile(r"^qp-[0-9a-f]{32}$")
DOCKER_ENV_KEYS = ("PATH", "HOME")


class DockerError(Exception):
    pass


class Docker:
    def __init__(self, docker_bin="/usr/bin/docker", home="/var/lib/qp-sandbox"):
        self.bin = docker_bin
        self.env = {"PATH": "/usr/bin:/bin", "HOME": home}

    def _run(self, args, timeout=30):
        return subprocess.run([self.bin, *args], capture_output=True, timeout=timeout,
                              env=self.env, stdin=subprocess.DEVNULL)

    # ---------- startup checks ----------
    def runtimes(self):
        p = self._run(["info", "--format", "{{json .Runtimes}}"])
        if p.returncode != 0:
            raise DockerError("docker info failed: " + p.stderr.decode(errors="replace")[-300:])
        return sorted(json.loads(p.stdout.decode() or "{}").keys())

    def image_exists(self, image_id):
        return self._run(["image", "inspect", "--format", "{{.Id}}", image_id]).returncode == 0

    def reap_leftovers(self):
        p = self._run(["ps", "-aq", "--filter", "label=qp.managed=1"])
        ids = p.stdout.decode().split()
        for cid in ids:
            self._run(["rm", "-f", cid])
        return ids

    # ---------- per-run ----------
    def build_argv(self, profile_name, *, name, job_id, runtime, image_id, input_dir):
        prof = limits.PROFILES[profile_name]
        if runtime not in limits.ISOLATION_MODES.values():
            raise DockerError(f"runtime not allowed: {runtime}")
        if not IMAGE_ID_RE.match(image_id):
            raise DockerError("image id must be an immutable sha256 id")
        if not NAME_RE.match(name):
            raise DockerError("bad container name")
        # See limits.GVISOR_HOST_PIDS_CEILING for why gVisor needs two separate limits.
        if runtime == "runsc":
            pids_args = ["--pids-limit", str(limits.GVISOR_HOST_PIDS_CEILING),
                         "--ulimit", f"nproc={prof['pids']}:{prof['pids']}"]
        else:
            pids_args = ["--pids-limit", str(prof["pids"])]
        argv = [
            self.bin, "run",
            "--name", name,
            "--label", "qp.managed=1",
            "--label", f"qp.job={job_id}",
            "--runtime", runtime,
            "--pull", "never",
            "--network", "none",
            "--read-only",
            "--tmpfs", f"/tmp:rw,nosuid,nodev,noexec,size={prof['tmpfs_size']}",
            "--user", "65534:65534",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            *pids_args,
            "--memory", prof["memory"],
            "--memory-swap", prof["memory"],
            "--cpus", prof["cpus"],
            "--ulimit", "nofile=64:64",
            "--ulimit", "core=0:0",
            "--log-driver", "none",
            "--hostname", "qp-sandbox",
            "--workdir", "/tmp",
        ]
        if input_dir is not None:
            argv += ["--mount", f"type=bind,source={input_dir},target=/input,readonly"]
        argv += [image_id, prof["mode"]]
        return argv

    def run(self, profile_name, *, name, job_id, runtime, image_id, input_dir):
        """Run one container to completion under fixed limits. Returns an envelope dict."""
        prof = limits.PROFILES[profile_name]
        argv = self.build_argv(profile_name, name=name, job_id=job_id, runtime=runtime,
                               image_id=image_id, input_dir=input_dir)
        started = time.monotonic()
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, env=self.env)
        out = CappedReader(proc.stdout, prof["stdout_cap"], keep="head")
        err = CappedReader(proc.stderr, limits.STDERR_CAP_BYTES, keep="tail")
        out.start()
        err.start()

        timed_out = False
        killed_for_output = False
        deadline = started + prof["timeout_s"]
        while proc.poll() is None:
            if out.over_cap and not killed_for_output:
                killed_for_output = True
                self._run(["kill", name])
            if time.monotonic() > deadline and not timed_out:
                timed_out = True
                self._run(["kill", name])
            if time.monotonic() > deadline + 15:
                proc.kill()  # the docker CLI itself is stuck: never wait forever
                break
            time.sleep(0.05)
        proc.wait(timeout=10)
        out.join(5)
        err.join(5)
        duration_ms = int((time.monotonic() - started) * 1000)

        info = self.inspect(name)
        self._run(["rm", "-f", name])
        removed = not self.exists(name)

        stdout = out.data
        try:
            stdout_text = stdout.decode("utf-8")
            valid_utf8 = True
        except UnicodeDecodeError:
            stdout_text = None
            valid_utf8 = False

        if timed_out:
            status = "timeout"
        elif killed_for_output or out.over_cap:
            status = "output_limit_exceeded"
            stdout_text = None
        elif info.get("oom_killed"):
            status = "oom_killed"
        elif not valid_utf8:
            status = "invalid_output"
        elif info.get("exit_code") != 0:
            status = "nonzero_exit"
        else:
            status = "ok"

        return {
            "status": status,
            "profile": profile_name,
            "exit_code": info.get("exit_code"),
            "oom_killed": info.get("oom_killed"),
            "timed_out": timed_out,
            "duration_ms": duration_ms,
            "timeout_s": prof["timeout_s"],
            "runtime": info.get("runtime"),
            "image_id": info.get("image_id"),
            "container_name": name,
            "container_id": info.get("container_id"),
            "removed": removed,
            "stdout_bytes": out.total,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest() if stdout_text is not None else None,
            "stdout": stdout_text,
            "stderr_tail": sanitize(err.data.decode("utf-8", errors="replace")),
        }

    def inspect(self, name):
        p = self._run(["inspect", name])
        if p.returncode != 0:
            return {}
        try:
            obj = json.loads(p.stdout.decode())[0]
        except (ValueError, IndexError):
            return {}
        state = obj.get("State", {})
        return {
            "exit_code": state.get("ExitCode"),
            "oom_killed": state.get("OOMKilled"),
            "runtime": obj.get("HostConfig", {}).get("Runtime"),
            "image_id": obj.get("Image"),
            "container_id": obj.get("Id"),
        }

    def exists(self, name):
        p = self._run(["inspect", name])
        if p.returncode == 0:
            return True
        msg = p.stderr.decode(errors="replace").lower()
        if "no such" in msg:
            return False
        # Unknown failure: be conservative and report that it may still exist.
        return True


class CappedReader(threading.Thread):
    """Reads a pipe in a thread, keeping at most `cap` bytes (head or tail)."""

    def __init__(self, pipe, cap, keep="head"):
        super().__init__(daemon=True)
        self.pipe, self.cap, self.keep = pipe, cap, keep
        self.buf = bytearray()
        self.total = 0
        self.over_cap = False

    def run(self):
        while True:
            chunk = self.pipe.read(64 * 1024)
            if not chunk:
                break
            self.total += len(chunk)
            if self.keep == "head":
                room = self.cap - len(self.buf)
                if room > 0:
                    self.buf += chunk[:room]
                if self.total > self.cap:
                    self.over_cap = True  # keep draining so the process isn't blocked
            else:
                self.buf += chunk
                if len(self.buf) > self.cap:
                    del self.buf[: len(self.buf) - self.cap]
                    self.over_cap = True

    @property
    def data(self):
        return bytes(self.buf)


_SECRETISH = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+")


def sanitize(text):
    """Strip obvious secret-looking assignments before stderr leaves sandboxd."""
    return _SECRETISH.sub(r"\1=[redacted]", text)


def new_name():
    return "qp-" + os.urandom(16).hex()
