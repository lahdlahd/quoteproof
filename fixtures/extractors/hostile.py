# Simulates AI-written code that has been manipulated by a poisoned quote.
# Every attempt is harmless and bounded; it reports what happened as JSON.
# Run through the sandbox, ALL attempts must fail.
import json
import os
import socket
import urllib.request

r = {}
try:
    urllib.request.urlopen("https://api.vultrinference.com/v1/models", timeout=3)
    r["exfiltrate_https"] = "SUCCEEDED"
except Exception as e:
    r["exfiltrate_https"] = "blocked: " + type(e).__name__
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3)
    r["raw_tcp"] = "SUCCEEDED"
except OSError as e:
    r["raw_tcp"] = "blocked: " + type(e).__name__
secretish = [k for k in os.environ if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "CANARY"))]
r["secret_env_vars_visible"] = secretish
for path in ("/srv/qp/uploads", "/etc/qp", "/var/run/docker.sock", "/run/qp-sandboxd"):
    r["exists:" + path] = os.path.exists(path)
for path in ("/input/overwrite_attempt", "/opt/qp/entrypoint.py", "/etc/passwd"):
    try:
        with open(path, "a") as f:
            f.write("x")
        r["write:" + path] = "SUCCEEDED"
    except OSError as e:
        r["write:" + path] = "blocked: " + (os.strerror(e.errno) if e.errno else type(e).__name__)
print(json.dumps(r))
