"""Vultr Serverless Inference client (OpenAI-compatible chat API). Used ONLY by qp-worker.

* The key is read from a systemd credential ($CREDENTIALS_DIRECTORY/inference_key),
  which systemd provides to qp-worker alone. For local development a file path in
  QP_INFERENCE_KEY_FILE is accepted. The key is never logged, stored or returned.
* Standard library only. Timeouts on every call; bounded retries on 429/5xx.
* Every response records the model ID that actually answered, for the timeline.
* No tool calling: we ask for plain text (a fenced code block or JSON) and validate it.
"""

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.vultrinference.com/v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class InferenceError(Exception):
    """Message is always safe to show and log: never contains the key or the prompt."""


# Model "presets" are chosen per model in /etc/qp-worker/worker.json as "<model-id>@<preset>".
# Measured on Vultr (7 Nov 2026): these models reason by default and can spend a whole
# output budget on hidden reasoning, returning no content. Each preset was accepted by all
# five candidate models; which one turns reasoning down differs per model family.
PRESETS = {
    "default": {},                                                    # full reasoning
    "low": {"reasoning_effort": "low"},                               # GLM / MiniMax: ~0-12 reasoning tokens
    "nothink": {"chat_template_kwargs": {"enable_thinking": False}},  # Qwen / DeepSeek: 0 reasoning tokens
}
DEFAULT_MAX_TOKENS = 32768   # Vultr's default chat output limit; leaves room for reasoning + answer


def resolve(spec):
    """'qwen3.8-27b@nothink' -> ('qwen3.8-27b', {...}). Unknown presets are rejected."""
    model, _, preset = (spec or "").partition("@")
    preset = preset or "default"
    if not model or preset not in PRESETS:
        raise InferenceError(f"bad model spec {spec!r}; presets: {sorted(PRESETS)}")
    return model, dict(PRESETS[preset])


def load_key():
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    path = os.path.join(cred_dir, "inference_key") if cred_dir else os.environ.get("QP_INFERENCE_KEY_FILE")
    if not path:
        raise InferenceError("no inference key configured (systemd credential 'inference_key' missing)")
    with open(path, encoding="utf-8") as f:
        key = f.read().strip()
    if not key or any(c.isspace() for c in key):
        raise InferenceError("inference key file is empty or malformed")
    return key


class InferenceClient:
    def __init__(self, key, base_url=DEFAULT_BASE_URL, timeout=420, max_retries=3, backoff=2.0):
        self._key = key
        self.base_url = base_url.rstrip("/")
        self.timeout, self.max_retries, self.backoff = timeout, max_retries, backoff

    def __repr__(self):
        return f"InferenceClient(base_url={self.base_url!r}, key=<hidden>)"

    def _request(self, method, path, body=None):
        url = self.base_url + path
        data = None if body is None else json.dumps(body).encode()
        last = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                "Accept": "application/json", "User-Agent": "quoteproof-worker/1"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise InferenceError("response too large")
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                code = e.code
                if code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else self.backoff * 2 ** attempt
                    time.sleep(min(delay, 30))
                    last = f"HTTP {code}"
                    continue
                if code in (401, 403):
                    raise InferenceError(f"HTTP {code}: inference key rejected") from None
                raise InferenceError(f"HTTP {code} from inference API") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = type(e).__name__
                if attempt < self.max_retries:
                    time.sleep(self.backoff * 2 ** attempt)
                    continue
                raise InferenceError(f"inference API unreachable ({last})") from None
            except (ValueError, UnicodeDecodeError):
                raise InferenceError("inference API returned invalid JSON") from None
        raise InferenceError(f"inference API failed after retries ({last})")

    def models(self):
        out = self._request("GET", "/models")
        return sorted(m.get("id") for m in out.get("data", []) if isinstance(m, dict) and m.get("id"))

    def chat(self, model, messages, temperature=0.1, max_tokens=DEFAULT_MAX_TOKENS):
        """model may carry a preset: 'qwen3.8-27b@nothink'."""
        model_id, extra = resolve(model)
        t0 = time.monotonic()
        out = self._request("POST", "/chat/completions", {
            "model": model_id, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, **extra})
        try:
            choice = out["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise InferenceError("unexpected response shape from inference API") from None
        usage = out.get("usage") or {}
        if not text.strip() and choice.get("finish_reason") == "length":
            raise InferenceError(f"model {model_id} used its whole output budget "
                                 f"({usage.get('completion_tokens')} tokens, mostly reasoning) and returned no answer")
        return {"text": text, "model": (out.get("model") or model_id) + (f"@{model.partition('@')[2]}" if "@" in model else ""),
                "finish_reason": choice.get("finish_reason"),
                "usage": usage, "latency_ms": int((time.monotonic() - t0) * 1000),
                "response_id": out.get("id")}
