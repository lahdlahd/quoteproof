"""T3: inference client against a local fake OpenAI-compatible server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from qp import inference

KEY = "vk-test-SECRET-123"


class Fake(BaseHTTPRequestHandler):
    script = []          # list of (status, body) to return, in order
    seen = []

    def log_message(self, *a):
        pass

    def _reply(self):
        Fake.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                          "body": self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode() or None})
        status, body = Fake.script.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode() if not isinstance(body, bytes) else body)

    do_GET = do_POST = _reply


@pytest.fixture
def server():
    Fake.script, Fake.seen = [], []
    s = HTTPServer(("127.0.0.1", 0), Fake)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{s.server_port}/v1"
    s.shutdown()


def client(url):
    return inference.InferenceClient(KEY, base_url=url, timeout=5, max_retries=2, backoff=0.01)


def test_chat_records_model_and_sends_bearer(server):
    Fake.script = [(200, {"id": "r1", "model": "qwen3.8-27b", "choices": [{"message": {"content": "hi"},
                                                                              "finish_reason": "stop"}]})]
    out = client(server).chat("qwen3.8-27b", [{"role": "user", "content": "x"}])
    assert out["text"] == "hi" and out["model"] == "qwen3.8-27b"
    assert Fake.seen[0]["auth"] == f"Bearer {KEY}" and Fake.seen[0]["path"] == "/v1/chat/completions"
    assert "tools" not in json.loads(Fake.seen[0]["body"])


def test_models_list(server):
    Fake.script = [(200, {"data": [{"id": "b"}, {"id": "a"}]})]
    assert client(server).models() == ["a", "b"]


def test_retries_on_429_then_succeeds(server):
    Fake.script = [(429, {}), (503, {}), (200, {"model": "m", "choices": [{"message": {"content": "ok"}}]})]
    assert client(server).chat("m", [])["text"] == "ok"
    assert len(Fake.seen) == 3


def test_errors_never_contain_key(server):
    Fake.script = [(401, {"error": f"bad key {KEY}"})]
    with pytest.raises(inference.InferenceError) as e:
        client(server).chat("m", [])
    assert KEY not in str(e.value) and "rejected" in str(e.value)
    assert KEY not in repr(client(server))


def test_bad_shape_and_unreachable():
    with pytest.raises(inference.InferenceError):
        inference.InferenceClient(KEY, base_url="http://127.0.0.1:9/v1", timeout=1, max_retries=0).models()


def test_bad_json_shape(server):
    Fake.script = [(200, {"choices": []})]
    with pytest.raises(inference.InferenceError):
        client(server).chat("m", [])


def test_key_loading(tmp_path, monkeypatch):
    p = tmp_path / "inference_key"
    p.write_text(KEY + "\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert inference.load_key() == KEY
    p.write_text("")
    with pytest.raises(inference.InferenceError):
        inference.load_key()
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    monkeypatch.delenv("QP_INFERENCE_KEY_FILE", raising=False)
    with pytest.raises(inference.InferenceError):
        inference.load_key()


def test_presets_resolve_and_are_sent(server):
    assert inference.resolve("qwen3.8-27b@nothink") == ("qwen3.8-27b", {"chat_template_kwargs": {"enable_thinking": False}})
    assert inference.resolve("glm-5.3@low") == ("glm-5.3", {"reasoning_effort": "low"})
    assert inference.resolve("glm-5.3") == ("glm-5.3", {})
    with pytest.raises(inference.InferenceError):
        inference.resolve("glm-5.3@turbo")
    Fake.script = [(200, {"model": "glm-5.3", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})]
    out = client(server).chat("glm-5.3@low", [])
    body = json.loads(Fake.seen[0]["body"])
    assert body["model"] == "glm-5.3" and body["reasoning_effort"] == "low"
    assert body["max_tokens"] == inference.DEFAULT_MAX_TOKENS and out["model"] == "glm-5.3@low"


def test_empty_answer_after_reasoning_is_an_error(server):
    Fake.script = [(200, {"model": "m", "usage": {"completion_tokens": 8192},
                          "choices": [{"message": {"content": ""}, "finish_reason": "length"}]})]
    with pytest.raises(inference.InferenceError) as e:
        client(server).chat("m", [])
    assert "output budget" in str(e.value)
