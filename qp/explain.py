"""T8: plain-English explanation, checked by trusted code.

The model receives ONLY verified facts (comparison output and assurance counters)
and writes a short summary. Trusted code then extracts every number in the text;
any number that is not in the verified facts rejects the summary, which is
regenerated (max 3 attempts). If none passes, no summary is shown at all.
"""

import json
import re
from decimal import Decimal, InvalidOperation

MAX_ATTEMPTS = 3
PROMPT = """Write a short plain-English summary (at most 150 words) of a supplier quotation comparison
for a procurement officer. Use ONLY the facts provided. Every number you write must appear in the
facts exactly (you may add thousands separators). Do not compute new numbers, do not round, do not
convert currencies. Say clearly which amounts are Unknown and which items are not yet compared.
Write plain text only, no markdown."""

_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def numbers_in(obj):
    out = set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (int, str)) and not isinstance(x, bool):
            for t in _NUM.findall(str(x)):
                try:
                    out.add(Decimal(t.replace(",", "")).normalize())
                except InvalidOperation:
                    pass
    walk(obj)
    return out


def check(text, facts):
    """Returns the list of numbers in text that are NOT in the facts (empty = pass)."""
    allowed = numbers_in(facts)
    bad = []
    for t in _NUM.findall(text or ""):
        try:
            d = Decimal(t.replace(",", "").rstrip(".")).normalize()
        except InvalidOperation:
            bad.append(t)
            continue
        if d not in allowed:
            bad.append(t)
    return bad


def explain(facts, llm, model):
    """Returns {status: accepted|rejected|unavailable, text, model, attempts, rejected_numbers}."""
    msgs = [{"role": "system", "content": PROMPT},
            {"role": "user", "content": "FACTS (verified):\n" + json.dumps(facts, ensure_ascii=False)}]
    bad, used_model = [], model
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = llm.chat(model, msgs, temperature=0.2)
        except Exception as e:  # noqa: BLE001
            return {"status": "unavailable", "text": None, "model": model, "attempts": attempt, "error": str(e)[:200]}
        used_model = resp.get("model") or model
        text = (resp["text"] or "").strip()
        bad = check(text, facts)
        if not bad and text:
            return {"status": "accepted", "text": text, "model": used_model, "attempts": attempt, "rejected_numbers": []}
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": f"Rejected: these numbers are not in the facts: {bad[:10]}. "
                                             "Rewrite using only numbers from the facts."}]
    return {"status": "rejected", "text": None, "model": used_model, "attempts": MAX_ATTEMPTS, "rejected_numbers": bad[:10]}


def facts_for(comparison, assurance):
    """The only data the explainer sees."""
    names = {s["doc_id"]: s["supplier"] for s in comparison["suppliers"]}
    return {
        "currency": comparison["currency"],
        "suppliers": [{"supplier": names[d], **{k: v for k, v in t.items()}} for d, t in comparison["totals"].items()],
        "compared_groups": [{"label": r["label"], "amounts": {names[d]: c.get("value") for d, c in r["cells"].items()}}
                            for r in comparison["rows"]],
        "charges": [{"kind": r["kind"], "amounts": {names[d]: [c.get("value"), c.get("label")]
                                                    for d, c in r["cells"].items()}} for r in comparison["charges"]],
        "not_compared_yet": len(comparison["not_compared_yet"]),
        "not_comparable_currency": [x["supplier"] for x in comparison["not_comparable_currency"]],
        "assurance": assurance,
    }
