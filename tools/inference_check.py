"""Check Vultr Serverless Inference from the VM (run as root; the key file is root-only).

  QP_INFERENCE_KEY_FILE=/etc/qp-secrets/inference_key python3 -I tools/inference_check.py --list
  ... --ping qwen3.8-27b deepseek-v4.1-flash-1017      # one tiny request per model, with latency

Never prints the key.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qp import inference  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="note: listing models does NOT prove the key is valid")
    ap.add_argument("--ping", nargs="*", default=[])
    ap.add_argument("--base-url", default=inference.DEFAULT_BASE_URL)
    a = ap.parse_args()
    c = inference.InferenceClient(inference.load_key(), base_url=a.base_url, timeout=60, max_retries=1)
    try:
        if a.list or not a.ping:
            models = c.models()
            print(f"{len(models)} models available:")
            for m in models:
                print("  ", m)
        for m in a.ping:
            r = c.chat(m, [{"role": "user", "content": "Reply with exactly: OK"}], temperature=0, max_tokens=2048)
            print(f"ping {m}: served_by={r['model']} finish={r['finish_reason']} latency_ms={r['latency_ms']} "
                  f"reply={r['text'][:20]!r}")
    except inference.InferenceError as e:
        print("INFERENCE CHECK FAILED:", e)
        return 1
    print("INFERENCE CHECK OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
