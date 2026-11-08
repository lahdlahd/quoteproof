"""The docker command line is fixed; requests cannot influence it."""
import subprocess
import sys

import pytest

from sandboxd import limits
from sandboxd.dockerrun import CappedReader, Docker, DockerError, sanitize

IMG = "sha256:" + "b" * 64
NAME = "qp-" + "c" * 32
JOB = "11111111-2222-4333-8444-555555555555"


def argv(profile="extractor", runtime="runsc", **kw):
    return Docker("/usr/bin/docker").build_argv(profile, name=NAME, job_id=JOB, runtime=runtime,
                                                image_id=kw.get("image", IMG),
                                                input_dir=kw.get("input_dir", "/srv/qp/staging/x/in"))


@pytest.mark.parametrize("runtime", ["runsc", "runc"])
def test_required_isolation_flags_present(runtime):
    a = argv(runtime=runtime)
    joined = " ".join(a)
    for flag in ["--network none", "--read-only", "--user 65534:65534", "--cap-drop ALL",
                 "--security-opt no-new-privileges", "--memory 256m",
                 "--memory-swap 256m", "--cpus 0.5", f"--runtime {runtime}", "--pull never",
                 "--log-driver none"]:
        assert flag in joined, flag
    assert "target=/input,readonly" in joined
    assert a[-2:] == [IMG, "extractor"]


def test_process_limit_runc_uses_cgroup_pids_limit():
    a = argv(runtime="runc")
    assert a[a.index("--pids-limit") + 1] == "32"
    assert not any(x.startswith("nproc=") for x in a)


def test_process_limit_gvisor_uses_nproc_inside_and_host_ceiling():
    a = argv(runtime="runsc")
    assert a.count("--pids-limit") == 1
    assert a[a.index("--pids-limit") + 1] == str(limits.GVISOR_HOST_PIDS_CEILING)
    assert "nproc=32:32" in a
    # the sandbox's own process limit must stay strict, never the host ceiling
    assert limits.EXPECTED_PIDS_LIMIT == 32 < limits.GVISOR_HOST_PIDS_CEILING


def test_forbidden_flags_absent():
    joined = " ".join(argv())
    for bad in ["--privileged", "--cap-add", "docker.sock", "--network host", "--pid host",
                "--ipc host", "-e ", "--env", "seccomp=unconfined", "--volume", " -v "]:
        assert bad not in joined, bad


def test_every_profile_builds_with_limits():
    for name, prof in limits.PROFILES.items():
        a = argv(profile=name, runtime="runc")
        assert a[a.index("--pids-limit") + 1] == str(prof["pids"])
        assert a[a.index("--memory") + 1] == prof["memory"]
        g = argv(profile=name, runtime="runsc")
        assert f"nproc={prof['pids']}:{prof['pids']}" in g
        assert g[g.index("--memory") + 1] == prof["memory"]


@pytest.mark.parametrize("kw", [{"runtime": "kata"}, {"runtime": "runc --privileged"},
                                {"image": "python:latest"}, {"image": "sha256:short"}])
def test_bad_runtime_or_image_rejected(kw):
    with pytest.raises(DockerError):
        argv(**kw)


def test_capped_reader_head_cap_and_drain():
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdout.write('y'*300000)"],
                         stdout=subprocess.PIPE)
    r = CappedReader(p.stdout, 1000, keep="head")
    r.start(); p.wait(); r.join()
    assert len(r.data) == 1000 and r.total == 300000 and r.over_cap


def test_capped_reader_tail():
    p = subprocess.Popen([sys.executable, "-c", "print('A'*5000 + 'END')"], stdout=subprocess.PIPE)
    r = CappedReader(p.stdout, 10, keep="tail")
    r.start(); p.wait(); r.join()
    assert r.data.endswith(b"END\n")


def test_sanitize_redacts_secrets():
    assert "SHOULDNOTLEAK" not in sanitize("boom api_key=SHOULDNOTLEAK more")
