"""Fixed container profiles.

This file is the single source of truth for every sandbox setting.
Nothing in a request can change these values. If you change a value here,
the self-test expectations (EXPECTED_PIDS_LIMIT etc.) must still hold.
"""

KIB = 1024
MIB = 1024 * 1024

# --- request / payload limits -------------------------------------------
MAX_REQUEST_BYTES = 10 * MIB          # whole JSON request over the socket
MAX_UPLOAD_BYTES = 10 * MIB           # largest document we will stage
MAX_INDEX_JSON_BYTES = 9 * MIB        # Source Index passed to the extractor
MAX_CODE_BYTES = 64 * KIB             # AI-written extractor code
STDERR_CAP_BYTES = 16 * KIB           # only the tail is kept

# --- values the self-test probe also knows (must match images/runner) ---
EXPECTED_PIDS_LIMIT = 32

# --- process limits under gVisor (runsc) ----------------------------------
# Measured on the VM (7 Nov 2026): under runsc, Docker's --pids-limit counts
# gVisor's OWN host threads, not the sandboxed processes. At 32/64 the sandbox
# refused children after 1-17 and sometimes crashed (exit 2). So in gvisor mode:
#   * each profile's "pids" value is enforced INSIDE the sandbox as RLIMIT_NPROC
#     (--ulimit nproc=N:N), which gVisor's kernel enforces (measured: refused at 31
#     children + the probe itself = 32, every run);
#   * --pids-limit becomes an outer host ceiling for gVisor's own threads.
# runc-hardened mode is unchanged: --pids-limit = the profile's "pids" value.
GVISOR_HOST_PIDS_CEILING = 256
SLEEP_TEST_TIMEOUT_S = 5

# Each profile = one kind of container. image_key selects which of the two
# images (from /etc/qp/sandboxd.json) is used; mode is the fixed argument
# passed to the image's fixed entrypoint.
PROFILES = {
    "indexer": {
        "image_key": "indexer",
        "mode": "index",
        "memory": "512m",
        "cpus": "1.0",
        "pids": 64,
        "tmpfs_size": "16m",
        "timeout_s": 60,
        "stdout_cap": 8 * MIB,
    },
    "extractor": {
        "image_key": "runner",
        "mode": "extractor",
        "memory": "256m",
        "cpus": "0.5",
        "pids": EXPECTED_PIDS_LIMIT,
        "tmpfs_size": "16m",
        "timeout_s": 20,
        "stdout_cap": 2 * MIB,
    },
    "selftest_probe": {
        "image_key": "runner",
        "mode": "selftest-probe",
        "memory": "256m",
        "cpus": "0.5",
        "pids": EXPECTED_PIDS_LIMIT,
        "tmpfs_size": "16m",
        "timeout_s": 30,
        "stdout_cap": 256 * KIB,
    },
    "selftest_sleep": {
        "image_key": "runner",
        "mode": "selftest-sleep",
        "memory": "256m",
        "cpus": "0.5",
        "pids": EXPECTED_PIDS_LIMIT,
        "tmpfs_size": "16m",
        "timeout_s": SLEEP_TEST_TIMEOUT_S,
        "stdout_cap": 256 * KIB,
    },
}

# Isolation modes allowed in /etc/qp/sandboxd.json -> Docker runtime name.
ISOLATION_MODES = {
    "gvisor": "runsc",
    "runc-hardened": "runc",
}
