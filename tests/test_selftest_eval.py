"""The judge outside the sandbox: good observations pass, each bad one fails."""
import copy
import json

from sandboxd import limits, selftest_eval

CANARY = "QPCANARY-abc123"
HOST_KERNEL = "6.8.0-48-generic"

GOOD_PROBE = {
    "identity": {"release": "4.4.0", "version": "#1 SMP", "machine": "x86_64"},
    "proc_status": {"Uid": "65534\t65534\t65534\t65534", "Gid": "65534", "CapEff": "0000000000000000",
                    "CapBnd": "0000000000000000", "NoNewPrivs": "1", "Seccomp": "0"},
    "network": {"dns": {"ok": False, "errno": "EAI"}, "tcp": {"ok": False, "errno": "ENETUNREACH"},
                "interfaces": ["lo"]},
    "env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
    "pid1_env": {},
    "host_paths": {"/var/lib/qp-sandbox/canary": {"exists": False}, "/var/run/docker.sock": {"exists": False}},
    "writes": {"/probe/qp_write_test": {"ok": False, "errno": "EROFS"},
               "/etc/qp_write_test": {"ok": False, "errno": "EROFS"},
               "/usr/qp_write_test": {"ok": False, "errno": "EROFS"},
               "/qp_write_test": {"ok": False, "errno": "EROFS"},
               "/input/qp_write_test": {"ok": False, "errno": "EROFS"},
               "/tmp/qp_write_test": {"ok": True}},
    "pids": {"attempt_cap": 37, "started": 30, "refused": True, "error": "EAGAIN"},
    "dmesg": {"rc": 0, "head": ["[   0.000000] Starting gVisor..."]},
}


def envs(probe):
    p = {"status": "ok", "stdout": json.dumps(probe), "runtime": "runsc", "removed": True, "staging_removed": True}
    s = {"status": "timeout", "timed_out": True, "duration_ms": 5100, "stdout": "", "removed": True,
         "staging_removed": True, "exit_code": 137}
    return p, s


def run(probe, mode="gvisor", runtime="runsc", sleep_override=None):
    p, s = envs(probe)
    p["runtime"] = runtime
    if sleep_override:
        s.update(sleep_override)
    return selftest_eval.evaluate(probe_env=p, sleep_env=s, canary=CANARY, host_kernel=HOST_KERNEL,
                                  configured_runtime=runtime, isolation_mode=mode)


def verdict(rep, name):
    return next(t["verdict"] for t in rep["tests"] if t["test"] == name)


def test_good_observations_pass():
    rep = run(GOOD_PROBE)
    assert rep["passed"], [t for t in rep["tests"] if t["verdict"] != "PASS"]


def mutate(path, value):
    p = copy.deepcopy(GOOD_PROBE)
    d = p
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = value
    return p


def test_each_failure_is_detected():
    cases = [
        (("network", "tcp"), {"ok": True}, "network_blocked"),
        (("network", "interfaces"), ["eth0", "lo"], "network_blocked"),
        (("env", "VULTR_INFERENCE_API_KEY"), "sk-123", "secret_isolation"),
        (("env", "LEAK"), CANARY, "secret_isolation"),
        (("host_paths", "/var/run/docker.sock"), {"exists": True}, "secret_isolation"),
        (("writes", "/probe/qp_write_test"), {"ok": False, "errno": "EACCES"}, "read_only_filesystem"),
        (("writes", "/tmp/qp_write_test"), {"ok": False, "errno": "EROFS"}, "read_only_filesystem"),
        (("pids", "refused"), False, "process_limit"),
        (("proc_status", "CapEff"), "00000000a80425fb", "privileges"),
        (("identity", "release"), HOST_KERNEL, "isolation_runtime"),
        (("dmesg", "head"), ["dmesg: read kernel buffer failed: Operation not permitted"], "isolation_runtime"),
    ]
    for path, value, test in cases:
        rep = run(mutate(path, value))
        assert verdict(rep, test) == "FAIL", (path, test)
        assert rep["passed"] is False


def test_missing_nonewprivs_is_inconclusive_not_pass():
    p = copy.deepcopy(GOOD_PROBE)
    del p["proc_status"]["NoNewPrivs"]
    rep = run(p)
    assert verdict(rep, "privileges") == "INCONCLUSIVE" and rep["passed"] is False


def test_timeout_must_actually_fire():
    rep = run(GOOD_PROBE, sleep_override={"status": "ok", "timed_out": False, "stdout": '{"slept_full_30s": true}'})
    assert verdict(rep, "execution_timeout") == "FAIL"


def test_runc_mode_never_claims_gvisor():
    rep = run(GOOD_PROBE, mode="runc-hardened", runtime="runc")
    t = next(t for t in rep["tests"] if t["test"] == "isolation_runtime")
    assert t["verdict"] == "PASS" and t["observed"]["gvisor_active"] is False
    assert "gVisor NOT active" in t["expected"]


def test_wrong_runtime_reported_by_docker_fails():
    p, s = envs(GOOD_PROBE)
    p["runtime"] = "runc"
    rep = selftest_eval.evaluate(probe_env=p, sleep_env=s, canary=CANARY, host_kernel=HOST_KERNEL,
                                 configured_runtime="runsc", isolation_mode="gvisor")
    assert verdict(rep, "isolation_runtime") == "FAIL"


def test_pids_constant_matches_probe():
    import os, re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "images/runner/selftest_probe.py")).read()
    assert int(re.search(r"EXPECTED_PIDS_LIMIT = (\d+)", src).group(1)) == limits.EXPECTED_PIDS_LIMIT


# --- host paths: an empty image folder is fine, anything else is not -------------------
import pytest  # noqa: E402


@pytest.mark.parametrize("entry,expected", [
    ({"exists": False}, "PASS"),
    ({"exists": True, "dir_entries": 0}, "PASS"),                    # base image's empty /home
    ({"exists": True, "dir_entries": 1}, "FAIL"),                    # something is in there
    ({"exists": True, "list_error": "EACCES"}, "FAIL"),              # could not prove it is empty
    ({"exists": True}, "FAIL"),                                      # file / link / socket
    ({"exists": True, "content_head": "QPCANARY"}, "FAIL"),
])
def test_host_path_reachability(entry, expected):
    probe = copy.deepcopy(GOOD_PROBE)
    probe["host_paths"]["/home"] = entry
    verdict = {t["test"]: t["verdict"] for t in run(probe)["tests"]}["secret_isolation"]
    assert verdict == expected


def test_probe_reports_folder_entry_counts(tmp_path, monkeypatch):
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "selftest_probe", os.path.join(os.path.dirname(__file__), "..", "images", "runner", "selftest_probe.py"))
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    empty, full = tmp_path / "empty", tmp_path / "full"
    empty.mkdir(); full.mkdir(); (full / "x").write_text("x")
    monkeypatch.setattr(probe, "HOST_PATHS", [str(empty), str(full), str(tmp_path / "absent")])
    res = probe.host_paths()
    assert res[str(empty)] == {"exists": True, "dir_entries": 0}
    assert res[str(full)] == {"exists": True, "dir_entries": 1}
    assert res[str(tmp_path / "absent")] == {"exists": False}
