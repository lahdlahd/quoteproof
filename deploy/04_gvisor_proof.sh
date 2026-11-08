#!/usr/bin/env bash
# STEP 4 - gVisor proof tests (a), (b), (c). Tests (d), (e), (f) run later via the
# supervisor self-test and step 7. Writes evidence to /root/qp-evidence/.
#   sudo bash deploy/04_gvisor_proof.sh
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
ts="$(date -u +%Y%m%dT%H%M%SZ)"
ev="/root/qp-evidence/gvisor-proof-${ts}.txt"
exec > >(tee "$ev") 2>&1
fail=0
check() { if [[ "$2" == "PASS" ]]; then echo "PASS  $1"; else echo "FAIL  $1"; fail=1; fi; }

echo "gVisor proof - $(date -u)"; echo "runsc: $(runsc --version 2>&1 | head -1)"
docker pull -q ubuntu:24.04 >/dev/null

echo; echo "(a) runtime registered"
runtimes="$(docker info --format '{{json .Runtimes}}' | jq -r 'keys|join(",")')"
echo "    docker runtimes: ${runtimes}"
[[ ",${runtimes}," == *",runsc,"* ]] && check "(a) runsc registered in Docker" PASS || check "(a) runsc registered in Docker" FAIL

echo; echo "(b) kernel identity"
host_k="$(uname -r)"
gv_k="$(docker run --rm --runtime=runsc --network none ubuntu:24.04 uname -r 2>&1)"
rc_k="$(docker run --rm --runtime=runc  --network none ubuntu:24.04 uname -r 2>&1)"
echo "    host kernel:            ${host_k}"
echo "    runc container kernel:  ${rc_k}   (expected: same as host)"
echo "    runsc container kernel: ${gv_k}   (expected: different, gVisor's emulated kernel)"
[[ -n "$gv_k" && "$gv_k" != "$host_k" && "$rc_k" == "$host_k" ]] && check "(b) sandbox kernel differs from host" PASS || check "(b) sandbox kernel differs from host" FAIL

echo; echo "(c) dmesg"
gv_d="$(docker run --rm --runtime=runsc --network none --user 65534:65534 ubuntu:24.04 dmesg 2>&1 | head -5)"
rc_d="$(docker run --rm --runtime=runc  --network none --user 65534:65534 ubuntu:24.04 dmesg 2>&1 | head -3)"
echo "    runsc dmesg:"; sed 's/^/      /' <<<"$gv_d"
echo "    runc dmesg:";  sed 's/^/      /' <<<"$rc_d"
grep -qi gvisor <<<"$gv_d" && check "(c) dmesg inside runsc shows gVisor" PASS || check "(c) dmesg inside runsc shows gVisor" FAIL
grep -qiE "not permitted|denied|failed" <<<"$rc_d" && check "(c) dmesg under runc is refused" PASS || check "(c) dmesg under runc is refused" FAIL

echo; echo "(smoke) hardened flags accepted by runsc"
out="$(docker run --rm --runtime=runsc --network none --read-only --user 65534:65534 --cap-drop ALL \
      --security-opt no-new-privileges --pids-limit 32 --memory 256m --memory-swap 256m --cpus 0.5 \
      --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m --log-driver none ubuntu:24.04 sh -c 'id -u; touch /var/tmp/x 2>&1 || true' 2>&1)"
echo "$out" | sed 's/^/    /'
grep -q "^65534$" <<<"$out" && grep -qi "read-only" <<<"$out" && check "(smoke) non-root + read-only under runsc" PASS || check "(smoke) non-root + read-only under runsc" FAIL

if [[ -f /root/qp-evidence/gvisor-timebox-start ]]; then
  mins=$(( ( $(date -u +%s) - $(cat /root/qp-evidence/gvisor-timebox-start) ) / 60 ))
  echo; echo "gVisor timebox: ${mins} of 90 minutes used"
  (( mins > 90 )) && echo "TIMEBOX EXCEEDED: switch to runc-hardened (see docs/MILESTONE1.md, fallback)."
fi
echo; echo "Evidence saved to ${ev}"
if (( fail )); then echo "RESULT: gVisor proof FAILED - do not claim gVisor. Paste this output to Claude."; exit 1; fi
echo "RESULT: gVisor proof (a)(b)(c) PASSED"
