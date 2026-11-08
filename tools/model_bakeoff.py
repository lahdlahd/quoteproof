"""Model bake-off on the VM (architecture section 6). Run as root: it reads the root-only key file
and talks to qp-sandboxd (root is an allowed client). Every generated program runs ONLY in the sandbox.

  QP_INFERENCE_KEY_FILE=/etc/qp-secrets/inference_key \
    python3 -I tools/model_bakeoff.py --models qwen3.8-27b qwen3.8-27b@nothink glm-5.3@low --runs 3
  (a model may carry a preset: @nothink, @low or none = full reasoning; see qp/inference.py)

For each model x sample x run: first-try success, success within 2 retries, Layer-1 citation pass
rate, Layer-2 arithmetic status, format compliance, latency, and suspicious code on the poisoned sample.
Writes /root/qp-evidence/bakeoff-<time>.json and prints a ranking table.
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qp import extractor_agent, feedback, inference, injection, layer1, layer2, schemas  # noqa: E402
from qp.sandbox import SandboxClient  # noqa: E402

SAMPLES = [("ngn_quote.csv", "NGN"), ("usd_quote.xlsx", "USD"), ("ngn_quote.pdf", "NGN"), ("poisoned_quote.xlsx", "USD")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--socket", default="/run/qp-sandboxd/sandboxd.sock")
    a = ap.parse_args()
    llm = inference.InferenceClient(inference.load_key(), timeout=240)
    sb = SandboxClient(a.socket, timeout=240)
    available = set(llm.models())
    rows = []
    for model in a.models:
        if model.partition("@")[0] not in available:
            print(f"SKIP {model}: not in /v1/models")
            continue
        for name, cur in SAMPLES:
            with open(os.path.join(ROOT, "fixtures", "indexes", name + ".json"), encoding="utf-8") as f:
                index_json = f.read()
            index = schemas.validate_index(json.loads(index_json))
            for run in range(1, a.runs + 1):
                t0 = time.monotonic()
                ignore = frozenset(f["ref"] for f in injection.scan(index))
                res = extractor_agent.run(index, index_json, llm, sb, model, lambda *x: None,
                                          verify=lambda ex: feedback.evaluate(index, ex, cur, ignore))
                secs = round(time.monotonic() - t0, 1)
                row = {"model": model, "sample": name, "run": run, "ok": res["ok"], "attempts": res["attempts"],
                       "seconds": secs, "flags": res["flags"],
                       "runtime": (res.get("envelope") or {}).get("runtime"),
                       "errors": [f"{x['stage']}: {x.get('error', '')[:160]}" for x in res["attempts_log"] if x["stage"] != "ok"]}
                if res["ok"]:
                    l1 = layer1.verify(index, res["extraction"], cur)
                    l2 = layer2.reconcile(res["extraction"], l1)
                    row.update(verified=l1["summary"]["amounts_verified"], amounts=l1["summary"]["amounts_total"],
                               unaccounted=l1["summary"]["unaccounted"], l2=l2["status"])
                rows.append(row)
                print(json.dumps(row))
    ev = f"/root/qp-evidence/bakeoff-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    try:
        with open(ev, "w") as f:
            json.dump(rows, f, indent=1)
    except OSError:
        ev = "(not saved)"
    print("\nmodel                         first-try  within-2-retries  L1 pass  L2 reconcile  avg s  poisoned-code-flags")
    for model in a.models:
        r = [x for x in rows if x["model"] == model]
        if not r:
            continue
        ok = [x for x in r if x["ok"]]
        l1 = sum(x.get("verified", 0) for x in ok) / max(1, sum(x.get("amounts", 0) for x in ok))
        rec = sum(1 for x in ok if x.get("l2") == "RECONCILES" and x["sample"] != "poisoned_quote.xlsx")
        clean = [x for x in r if x["sample"] != "poisoned_quote.xlsx"]
        pf = sum(len(x["flags"]) for x in r if x["sample"] == "poisoned_quote.xlsx")
        print(f"{model:30} {sum(1 for x in r if x['ok'] and x['attempts'] == 1):>3}/{len(r):<6} {len(ok):>3}/{len(r):<12} "
              f"{l1:>6.0%}  {rec:>3}/{len(clean):<9} {sum(x['seconds'] for x in r) / len(r):>5.1f}  {pf}")
    print(f"\nEvidence: {ev}")


if __name__ == "__main__":
    main()
