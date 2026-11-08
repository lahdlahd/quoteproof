"""Judges the containment self-test.

The probe inside the sandbox only *collects observations*. The verdicts are
made here, outside the sandbox, by comparing observations with expectations.
Each test is PASS, FAIL or INCONCLUSIVE; only PASS counts as passed.
"""

import errno
import json

from . import limits

ALLOWED_ENV = {"PATH", "HOME", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"}
FORBIDDEN_ENV = {"VULTR_INFERENCE_API_KEY", "QP_INFERENCE_API_KEY", "INFERENCE_API_KEY", "QP_CANARY"}
EROFS_NAME = errno.errorcode[errno.EROFS]


def _result(name, verdict, expected, observed):
    return {"test": name, "verdict": verdict, "expected": expected, "observed": observed}


def evaluate(*, probe_env, sleep_env, canary, host_kernel, configured_runtime, isolation_mode):
    results = []
    probe = None
    if probe_env.get("status") == "ok" and probe_env.get("stdout"):
        try:
            probe = json.loads(probe_env["stdout"])
        except ValueError:
            probe = None

    if probe is None:
        results.append(_result("probe_ran", "FAIL", "probe container exits 0 with JSON",
                               {"status": probe_env.get("status"), "stderr_tail": probe_env.get("stderr_tail")}))
    else:
        results += [
            _network(probe),
            _secrets(probe, probe_env["stdout"], canary),
            _read_only(probe),
            _pids(probe),
            _privileges(probe),
            _runtime(probe, probe_env, host_kernel, configured_runtime, isolation_mode),
        ]
    results.append(_timeout(sleep_env))
    results.append(_lifecycle(probe_env, sleep_env))

    passed = all(r["verdict"] == "PASS" for r in results)
    return {"passed": passed, "isolation_mode": isolation_mode, "tests": results}


def _network(p):
    n = p.get("network", {})
    observed = {"dns": n.get("dns"), "tcp_1.1.1.1:443": n.get("tcp"), "interfaces": n.get("interfaces")}
    ok = (n.get("dns", {}).get("ok") is False and n.get("tcp", {}).get("ok") is False
          and n.get("interfaces") == ["lo"])
    return _result("network_blocked", "PASS" if ok else "FAIL",
                   "DNS fails, TCP connect fails, only interface 'lo'", observed)


def _host_path_reachable(v):
    """A host path counts as safe ONLY if it is absent, or is a folder the probe could
    list and found completely empty (e.g. the base image's own empty /home).
    Files, links, non-empty folders and unlistable folders all count as reachable."""
    if not v.get("exists"):
        return False
    return v.get("dir_entries") != 0


def _secrets(p, raw_stdout, canary):
    env = p.get("env", {})
    keys = set(env)
    canary_leaked = canary in raw_stdout
    unexpected = sorted(keys - ALLOWED_ENV)
    forbidden = sorted(keys & FORBIDDEN_ENV)
    host_paths = p.get("host_paths", {})
    reachable = sorted(k for k, v in host_paths.items() if _host_path_reachable(v))
    ok = not canary_leaked and not unexpected and not forbidden and not reachable
    return _result("secret_isolation", "PASS" if ok else "FAIL",
                   "canary absent; env only " + ",".join(sorted(ALLOWED_ENV)) + "; host secret paths absent (an empty image folder is allowed)",
                   {"canary_found_in_output": canary_leaked, "env_keys": sorted(keys),
                    "unexpected_env": unexpected, "forbidden_env": forbidden,
                    "host_paths_reachable": reachable})


def _read_only(p):
    w = p.get("writes", {})
    probe_dir = w.get("/probe/qp_write_test", {})
    others = {k: v for k, v in w.items() if k not in ("/probe/qp_write_test", "/tmp/qp_write_test")}
    tmp = w.get("/tmp/qp_write_test", {})
    ok = (probe_dir.get("ok") is False and probe_dir.get("errno") == EROFS_NAME
          and all(v.get("ok") is False for v in others.values())
          and tmp.get("ok") is True)
    return _result("read_only_filesystem", "PASS" if ok else "FAIL",
                   f"/probe (world-writable dir in image) fails with {EROFS_NAME}; /etc,/usr,/,/input fail; /tmp succeeds",
                   w)


def _pids(p):
    pr = p.get("pids", {})
    cap = limits.EXPECTED_PIDS_LIMIT + 5
    ok = pr.get("refused") is True and pr.get("started", cap + 1) <= cap
    return _result("process_limit", "PASS" if ok else "FAIL",
                   f"spawning refused before {cap} children (pids limit {limits.EXPECTED_PIDS_LIMIT})", pr)


def _privileges(p):
    s = p.get("proc_status", {})
    uid_ok = s.get("Uid", "").split()[:1] == ["65534"]
    cap_ok = s.get("CapEff") is not None and int(s.get("CapEff", "1"), 16) == 0
    nnp = s.get("NoNewPrivs")
    if uid_ok and cap_ok and nnp == "1":
        verdict = "PASS"
    elif uid_ok and cap_ok and nnp is None:
        verdict = "INCONCLUSIVE"  # kernel/runtime did not report the field
    else:
        verdict = "FAIL"
    return _result("privileges", verdict, "uid 65534, CapEff 0, NoNewPrivs 1",
                   {k: s.get(k) for k in ("Uid", "Gid", "CapEff", "CapBnd", "NoNewPrivs", "Seccomp")})


def _runtime(p, probe_env, host_kernel, configured_runtime, isolation_mode):
    ident = p.get("identity", {})
    dmesg = p.get("dmesg", {})
    observed = {
        "configured_runtime": configured_runtime,
        "docker_reported_runtime": probe_env.get("runtime"),
        "host_kernel": host_kernel,
        "sandbox_kernel": ident.get("release"),
        "dmesg_rc": dmesg.get("rc"),
        "dmesg_head": dmesg.get("head"),
    }
    runtime_ok = probe_env.get("runtime") == configured_runtime
    if isolation_mode == "gvisor":
        kernel_differs = ident.get("release") not in (None, host_kernel)
        gvisor_dmesg = any("gvisor" in line.lower() for line in dmesg.get("head", []))
        observed.update({"kernel_differs": kernel_differs, "dmesg_mentions_gvisor": gvisor_dmesg})
        ok = runtime_ok and kernel_differs and gvisor_dmesg
        expected = "Docker reports runsc; sandbox kernel != host kernel; dmesg shows gVisor"
    else:
        ok = runtime_ok
        observed["gvisor_active"] = False
        expected = "Docker reports runc (hardened mode, gVisor NOT active)"
    return _result("isolation_runtime", "PASS" if ok else "FAIL", expected, observed)


def _timeout(sleep_env):
    lim = limits.SLEEP_TEST_TIMEOUT_S
    d = sleep_env.get("duration_ms") or 0
    ok = (sleep_env.get("status") == "timeout" and sleep_env.get("timed_out") is True
          and d < (lim + 10) * 1000 and sleep_env.get("stdout") in ("", None))
    return _result("execution_timeout", "PASS" if ok else "FAIL",
                   f"'sleep 30' killed at about {lim}s", {
                       "status": sleep_env.get("status"), "duration_ms": d,
                       "exit_code": sleep_env.get("exit_code"), "stdout": sleep_env.get("stdout")})


def _lifecycle(probe_env, sleep_env):
    obs = {
        "probe_container_removed": probe_env.get("removed"),
        "sleep_container_removed": sleep_env.get("removed"),
        "probe_staging_removed": probe_env.get("staging_removed"),
        "sleep_staging_removed": sleep_env.get("staging_removed"),
    }
    ok = all(v is True for v in obs.values())
    return _result("lifecycle_cleanup", "PASS" if ok else "FAIL",
                   "containers and staging folders confirmed gone", obs)
