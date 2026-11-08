"""T5: prompt-injection detection."""
import json
import os

import pytest

from qp import injection

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def idx(name):
    with open(os.path.join(ROOT, "fixtures", "indexes", name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def test_poisoned_sample_flagged_on_several_patterns():
    found = injection.scan(idx("poisoned_quote.xlsx"))
    assert {f["ref"] for f in found} == {"Quote!B8"}
    assert {f["pattern"] for f in found} >= {"override_instructions", "secret_request", "exfiltration",
                                              "value_tampering"}


@pytest.mark.parametrize("name", ["ngn_quote.csv", "usd_quote.xlsx", "ngn_quote.pdf"])
def test_clean_samples_not_flagged(name):
    assert injection.scan(idx(name)) == []


def test_classifier_reply_is_strict_and_cannot_invent_refs():
    i = idx("poisoned_quote.xlsx")
    reply = 'Sure! {"suspicious": [{"ref": "Quote!B8", "reason": "asks for secrets"},' \
            ' {"ref": "Quote!Z99", "reason": "made up"}]}'
    out = injection.parse_classifier(reply, i)
    assert [f["ref"] for f in out] == ["Quote!B8"]
    with pytest.raises(Exception):
        injection.parse_classifier('{"suspicious": [{"ref": "Quote!B8"}]}', i)   # missing reason
    with pytest.raises(Exception):
        injection.parse_classifier('{"suspicious": [], "also": 1}', i)           # extra key


def test_classifier_unavailable_is_reported_not_fatal():
    class Down:
        def chat(self, *a, **k):
            raise RuntimeError("HTTP 503")
    findings, info = injection.classify(idx("ngn_quote.csv"), Down(), "m")
    assert findings == [] and info["status"] == "unavailable"


def test_classifier_input_marked_untrusted_and_bounded():
    msgs = injection.classifier_messages(idx("poisoned_quote.xlsx"))
    assert "UNTRUSTED TEXT ITEMS START" in msgs[1]["content"] and "Do not follow" in msgs[0]["content"]
    assert len(msgs[1]["content"]) < injection.MAX_CHARS + 200


def test_code_flags_become_findings():
    assert injection.code_findings([]) == []
    assert injection.code_findings(["import socket"])[0]["code"] == "GENERATED_CODE_SUSPICIOUS"
