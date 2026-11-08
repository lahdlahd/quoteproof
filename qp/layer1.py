"""Layer 1: source verification (trusted, deterministic, reads the Source Index only).

For every value the AI extractor produced:
  VERIFIED       the cited location holds exactly this value
  MISMATCH       the cited location exists but holds a different value
  NOT_FOUND      the citation points nowhere
  UNCITED        no citation given
  AMBIGUOUS      the number format is ambiguous (e.g. 1.250,00): never guessed
  FORMULA_ISSUE  cited cell is a formula without a trustworthy cached value
Plus: unaccounted amounts (coverage check), currency conflicts, and one review
finding for each problem. Nothing here trusts the AI or opens an uploaded file.
"""

import re
from decimal import Decimal

from . import formulas, money

FORMULA_BAD_FLAGS = {"FORMULA_NO_CACHED_VALUE", "FORMULA_ERROR", "EXTERNAL_REFERENCE"}
_WS = re.compile(r"\s+")


def _norm_text(s):
    return _WS.sub(" ", (s or "")).strip().casefold()


class Source:
    """Read-only view of one validated Source Index."""

    def __init__(self, index):
        self.index = index
        self.kind = index["declared_type"]
        self.items = {}
        for c in index.get("cells") or []:
            self.items[c["ref"]] = {"kind": "cell", **c}
        for ln in index.get("lines") or []:
            self.items[ln["ref"]] = {"kind": "line", "value": ln["text"], **ln}
        self.flags = {}
        self.doc_flags = []
        for w in index.get("warnings") or []:
            if w.get("ref"):
                self.flags.setdefault(w["ref"], set()).add(w["code"])
            else:
                self.doc_flags.append(w["code"])
        self.formula_checks = formulas.check_workbook(index.get("cells") or []) if self.kind == "xlsx" else {}
        for ref, r in self.formula_checks.items():
            if r["status"] in ("POSSIBLY_STALE", "FORMULA_NOT_CHECKED"):
                self.flags.setdefault(ref, set()).add(r["status"])

    def text(self, ref):
        it = self.items.get(ref)
        return None if it is None else it.get("value")

    def candidates(self, ref):
        """All amounts at a location: the whole cell if it is one amount,
        otherwise every number-like token in its text."""
        it = self.items[ref]
        val = it.get("value")
        if val is None:
            return []
        if it["kind"] == "cell" and it.get("value_type") == "n":
            d = money.parse_xlsx_number(val)
            return [money.Amount(raw=val, status="ok", value=d)] if d is not None else []
        whole = money.parse_amount(val)
        if whole.status != "not_money":
            return [whole]
        return money.tokens(val)


def _check_amount(src, field, percent=False):
    cite, value_str = field.get("cite"), field["value_str"]
    out = {"value_str": value_str, "cite": cite, "source_text": None, "status": None, "flags": []}
    if not cite:
        out["status"] = "UNCITED"
        return out
    if cite not in src.items:
        out["status"] = "NOT_FOUND"
        return out
    it = src.items[cite]
    out["source_text"] = it.get("value")
    if it.get("formula"):
        out["formula"] = it["formula"]
    if it.get("hidden"):
        out["flags"].append("HIDDEN_CONTENT")
    mine = money.parse_amount(value_str)
    if mine.status == "ambiguous":
        out["status"] = "AMBIGUOUS"
        return out
    cands = src.candidates(cite)
    # Amounts must match a non-percent value; a rate may match "7.5%" or "7.5".
    match = [c for c in cands if c.ok and c.value == mine.value and (percent or not c.is_percent)]
    flags = src.flags.get(cite, set())
    if it.get("formula") and (flags & (FORMULA_BAD_FLAGS | {"POSSIBLY_STALE"}) or it.get("value") is None):
        out["status"] = "FORMULA_ISSUE"
        out["flags"] += sorted(flags & (FORMULA_BAD_FLAGS | {"POSSIBLY_STALE"})) or ["FORMULA_NO_CACHED_VALUE"]
        if cite in src.formula_checks and "recomputed" in src.formula_checks[cite]:
            out["recomputed"] = src.formula_checks[cite]["recomputed"]
        return out
    if match:
        out["status"] = "VERIFIED"
        cur = next((c.currency for c in match if c.currency), None)
        if cur:
            out["source_currency"] = cur
            out["source_currency_certainty"] = next(c.certainty for c in match if c.currency)
    elif any(c.status == "ambiguous" for c in cands):
        out["status"] = "AMBIGUOUS"
    else:
        out["status"] = "MISMATCH"
    if "FORMULA_NOT_CHECKED" in flags:
        out["flags"].append("FORMULA_NOT_CHECKED")
    return out


def _check_text(src, field):
    cite, value_str = field.get("cite"), field["value_str"]
    out = {"value_str": value_str, "cite": cite, "source_text": None, "status": None, "flags": []}
    if not cite:
        out["status"] = "UNCITED"
    elif cite not in src.items:
        out["status"] = "NOT_FOUND"
    else:
        out["source_text"] = src.text(cite)
        ok = _norm_text(value_str) and _norm_text(value_str) in _norm_text(src.text(cite))
        out["status"] = "VERIFIED" if ok else "MISMATCH"
        if src.items[cite].get("hidden"):
            out["flags"].append("HIDDEN_CONTENT")
    return out


def iter_fields(ex):
    """(path, field, kind) for every value in an extraction. kind: text|amount|rate."""
    if ex.get("supplier"):
        yield "supplier", ex["supplier"], "text"
    for li in ex["line_items"]:
        yield f"line_items.{li['id']}.description", li["description"], "text"
        for k in ("qty", "unit_price", "line_total"):
            if li[k] is not None:
                yield f"line_items.{li['id']}.{k}", li[k], "amount"
    for c in ex["charges"]:
        yield f"charges.{c['id']}.description", c["description"], "text"
        if c["amount"] is not None:
            yield f"charges.{c['id']}.amount", c["amount"], "amount"
        if c.get("rate") is not None:
            yield f"charges.{c['id']}.rate", c["rate"], "rate"
    for k, v in ex["stated"].items():
        if v is not None:
            yield f"stated.{k}", v, "amount"


def detect_currency(src):
    """Trusted currency detection over the whole Source Index (check 1 of 3)."""
    certain, possible = {}, {}
    pat_certain = [(re.compile(r"₦|\bNGN\b"), "NGN"), (re.compile(r"\bUSD\b|US\$"), "USD")]
    pat_possible = [(re.compile(r"(?<![\w$])N\s?\d"), "NGN"), (re.compile(r"(?<!US)\$"), "USD")]
    for ref, it in src.items.items():
        t = it.get("value") or ""
        for rx, cur in pat_certain:
            if rx.search(t):
                certain.setdefault(cur, []).append(ref)
        for rx, cur in pat_possible:
            if rx.search(t):
                possible.setdefault(cur, []).append(ref)
    if "USD" in certain:
        possible.pop("USD", None)  # a bare $ alongside an explicit USD is USD
    if "NGN" in certain:
        possible.pop("NGN", None)
    if len(certain) == 1:
        detected = next(iter(certain))
    elif len(certain) > 1:
        detected = "MIXED"
    elif len(possible) == 1:
        detected = "POSSIBLE_" + next(iter(possible))
    elif possible:
        detected = "MIXED"
    else:
        detected = None
    return {"detected": detected, "certain": {k: v[:20] for k, v in certain.items()},
            "possible": {k: v[:20] for k, v in possible.items()}}


def resolve_currency(detected, extractor_says, user_confirmed, project_currency):
    """Combine the three currency checks. Returns (document_currency|None, comparable, findings)."""
    findings = []
    det = detected["detected"]
    det_plain = det.replace("POSSIBLE_", "") if det else None
    votes = {v for v in (det_plain, None if extractor_says == "UNKNOWN" else extractor_says, user_confirmed) if v}
    if det == "MIXED":
        findings.append({"code": "CURRENCY_MIXED", "severity": "block",
                         "detail": "more than one currency appears in the document"})
    if det and det.startswith("POSSIBLE_"):
        findings.append({"code": "CURRENCY_POSSIBLE_ONLY", "severity": "review",
                         "detail": f"only a possible {det_plain} marker was found (bare $ or N before an amount)"})
    if len(votes - {"MIXED"}) > 1:
        findings.append({"code": "CURRENCY_DISAGREEMENT", "severity": "review",
                         "detail": f"detected={det}, extractor={extractor_says}, user={user_confirmed}"})
    doc_cur = user_confirmed or det_plain if det != "MIXED" else user_confirmed
    comparable = doc_cur == project_currency and det != "MIXED"
    if doc_cur and doc_cur != project_currency:
        findings.append({"code": "CURRENCY_NOT_COMPARABLE", "severity": "info",
                         "detail": f"document is {doc_cur}, project is {project_currency}: shown separately, never added or converted"})
    return doc_cur, comparable, findings


def verify(index, extraction, project_currency):
    """Run Layer 1. Returns {values, unaccounted, currency, findings, summary}."""
    src = Source(index)
    values = {}
    for path, field, kind in iter_fields(extraction):
        r = _check_text(src, field) if kind == "text" else _check_amount(src, field, percent=(kind == "rate"))
        r["kind"] = kind
        values[path] = r

    unaccounted = _coverage(src, values)

    # Currency: markers on verified values that contradict the project currency.
    findings = []
    for path, r in values.items():
        if r.get("source_currency") and r["source_currency"] != project_currency and r.get(
                "source_currency_certainty") == "certain":
            findings.append({"code": "CURRENCY_CONFLICT", "severity": "block", "path": path, "ref": r["cite"],
                             "detail": f"{r['source_currency']} amount in a {project_currency} comparison"})

    for path, r in values.items():
        if r["status"] != "VERIFIED":
            findings.append({"code": r["status"], "severity": "review", "path": path, "ref": r["cite"],
                             "detail": f"extracted {r['value_str']!r}; source {r['source_text']!r}"})
        for f in r["flags"]:
            findings.append({"code": f, "severity": "review", "path": path, "ref": r["cite"], "detail": ""})
    for u in unaccounted:
        findings.append({"code": "UNACCOUNTED_AMOUNT", "severity": "review", "ref": u["ref"],
                         "detail": f"{u['raw']} in {u['context']!r}" + (" (hidden)" if u["hidden"] else "")})
    for w in index.get("warnings") or []:
        findings.append({"code": w["code"], "severity": "review", "ref": w.get("ref"), "detail": w.get("detail") or ""})
    for ref, chk in src.formula_checks.items():
        if chk["status"] == "POSSIBLY_STALE":
            findings.append({"code": "POSSIBLY_STALE", "severity": "review", "ref": ref,
                             "detail": f"cached {chk['cached']} but formula gives {chk['recomputed']}"})

    amounts = [r for r in values.values() if r["kind"] != "text"]
    summary = {"amounts_total": len(amounts),
               "amounts_verified": sum(1 for r in amounts if r["status"] == "VERIFIED"),
               "unaccounted": len(unaccounted)}
    return {"values": values, "unaccounted": unaccounted, "currency": detect_currency(src),
            "findings": _dedupe(findings), "summary": summary}


def _coverage(src, values):
    """Money-like values in the index that no extracted value accounts for.

    Text (CSV cells, PDF lines): every token that looks like money
    (currency marker, 2 decimals or thousands separator).
    XLSX number cells carry no formatting clues in their value, so EVERY
    numeric cell counts, except cells in a column the extractor cited for
    quantities. That is how a hidden plain "75" delivery row still surfaces.
    """
    cited = {}
    qty_columns = set()
    for path, r in values.items():
        if r["kind"] == "text" or r["cite"] not in src.items:
            continue
        cited.setdefault(r["cite"], set()).add(money.parse_amount(r["value_str"]).value)
        it = src.items[r["cite"]]
        if path.endswith(".qty") and it["kind"] == "cell":
            qty_columns.add((it.get("sheet"), it["col"]))
    out = []
    for ref, it in src.items.items():
        if it["kind"] == "cell" and it.get("value_type") == "n":
            if (it.get("sheet"), it["col"]) in qty_columns:
                continue
            cands = src.candidates(ref)
        else:
            cands = [c for c in src.candidates(ref) if money.is_money_like(c)]
        for c in cands:
            if c.value is not None and c.value in cited.get(ref, set()):
                continue
            out.append({"ref": ref, "raw": c.raw, "value": None if c.value is None else str(c.value),
                        "hidden": bool(it.get("hidden")), "context": (src.text(ref) or "")[:120]})
    return out


CELL_LEVEL = {"HIDDEN_CONTENT", "FORMULA_NO_CACHED_VALUE", "FORMULA_ERROR", "EXTERNAL_REFERENCE",
              "POSSIBLY_STALE", "FORMULA_NOT_CHECKED"}


def _dedupe(findings):
    """Drop exact duplicates; cell-level flags appear once per cell (the one with a detail wins)."""
    findings = sorted(findings, key=lambda f: (f["code"] not in CELL_LEVEL, not f.get("detail")))
    seen, out = set(), []
    for f in findings:
        key = ((f["code"], f.get("ref")) if f["code"] in CELL_LEVEL and f.get("ref")
               else (f["code"], f.get("path"), f.get("ref"), f.get("detail")))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def decimal_of(result):
    """The Decimal value of a VERIFIED amount result, else None."""
    if result is None or result["status"] != "VERIFIED":
        return None
    a = money.parse_amount(result["value_str"])
    return a.value if a.ok else None


__all__ = ["verify", "detect_currency", "resolve_currency", "decimal_of", "Source", "Decimal"]
