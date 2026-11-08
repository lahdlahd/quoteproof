"""Layer 2: arithmetic reconciliation (trusted Decimal engine).

Uses ONLY values that passed Layer 1 (or values a human entered or corrected
in Layer 3). Every figure it returns carries a cost label:
  Quoted      stated in the document and source-verified
  Calculated  computed here from Quoted/Estimated inputs
  Estimated   entered by a reviewer, not found in the document ("user-entered")
  Unknown     missing or not verified -- never treated as zero
Rounding: 2 decimal places, ROUND_HALF_UP. A difference of up to 0.01 is
"ROUNDING"; anything larger is "FAILS".
"""

from decimal import Decimal

from . import layer1, money

TOL = Decimal("0.01")
QUOTED, CALCULATED, ESTIMATED, UNKNOWN = "Quoted", "Calculated", "Estimated", "Unknown"


def fig(value, label, **extra):
    """A labelled figure. value is a 2-dp string or None (Unknown)."""
    return {"value": None if value is None else str(money.q2(value)), "label": label, **extra}


class Values:
    """Resolves a path to (Decimal|None, label) from Layer 1 + reviewer overrides.

    overrides: {path: {"value_str": "...", "kind": "corrected"|"user_entered"}}
      corrected    -> a reviewer correction that cites a location and re-verified: Quoted
      user_entered -> not in the document: Estimated
    """

    def __init__(self, l1, overrides=None):
        self.l1, self.overrides = l1, overrides or {}

    def get(self, path):
        o = self.overrides.get(path)
        if o is not None:
            if o.get("value_str") is None:
                return None, UNKNOWN
            a = money.parse_amount(o["value_str"])
            if not a.ok:
                return None, UNKNOWN
            return a.value, (QUOTED if o.get("kind") == "corrected" else ESTIMATED)
        d = layer1.decimal_of(self.l1["values"].get(path))
        return (d, QUOTED) if d is not None else (None, UNKNOWN)


def _compare(expected, stated):
    diff = abs(money.q2(expected) - money.q2(stated))
    status = "RECONCILES" if diff == 0 else ("ROUNDING" if diff <= TOL else "FAILS")
    return {"status": status, "expected": str(money.q2(expected)), "stated": str(money.q2(stated)),
            "difference": str(money.q2(money.q2(stated) - money.q2(expected)))}


def _calc_label(*labels):
    return CALCULATED + (" (includes estimate)" if ESTIMATED in labels else "")


def reconcile(extraction, l1, overrides=None):
    v = Values(l1, overrides)
    checks, findings = [], []

    # ---- per line: qty x unit_price = line_total
    lines = []
    for li in extraction["line_items"]:
        p = f"line_items.{li['id']}"
        q, ql = v.get(p + ".qty")
        u, ul = v.get(p + ".unit_price")
        t, tl = v.get(p + ".line_total")
        if q is not None and u is not None:
            expected = money.q2(q * u)
            if t is not None:
                c = _compare(expected, t)
                checks.append({"check": "line", "id": li["id"], **c})
                total = fig(t, tl)
            else:
                checks.append({"check": "line", "id": li["id"], "status": "NOT_CHECKED",
                               "reason": "line total unknown"})
                total = fig(expected, _calc_label(ql, ul))
        else:
            checks.append({"check": "line", "id": li["id"], "status": "NOT_CHECKED",
                           "reason": "quantity or unit price unknown"})
            total = fig(t, tl) if t is not None else fig(None, UNKNOWN)
        lines.append({"id": li["id"], "description": li["description"]["value_str"],
                      "qty": {"value": str(q), "label": ql} if q is not None else fig(None, UNKNOWN),
                      "unit_price": fig(u, ul) if u is not None else fig(None, UNKNOWN),
                      "line_total": total})

    # ---- subtotal: sum of lines vs stated subtotal
    line_vals = [Decimal(x["line_total"]["value"]) if x["line_total"]["value"] is not None else None for x in lines]
    unknown_lines = [x["id"] for x in lines if x["line_total"]["value"] is None]
    items_sum = None if unknown_lines or not lines else sum(line_vals, Decimal(0))
    # ---- charges
    charges = []
    for c in extraction["charges"]:
        a, al = v.get(f"charges.{c['id']}.amount")
        r, _rl = v.get(f"charges.{c['id']}.rate")
        charges.append({"id": c["id"], "kind": c["kind"], "description": c["description"]["value_str"],
                        "amount": fig(a, al) if a is not None else fig(None, UNKNOWN),
                        "rate": None if r is None else str(r)})

    def amt(ch):
        return None if ch["amount"]["value"] is None else Decimal(ch["amount"]["value"])

    non_tax = [ch for ch in charges if ch["kind"] in ("delivery", "installation", "other")]
    discounts = [ch for ch in charges if ch["kind"] == "discount"]
    taxes = [ch for ch in charges if ch["kind"] == "tax"]
    stated_tax, stl = v.get("stated.tax")

    # ---- subtotal: sum of lines vs stated subtotal. Some suppliers put delivery etc. ABOVE
    # their "Subtotal" line (e.g. =SUM(D4:D6) including a delivery row). Both bases are tried and
    # the one used is reported; charges inside the subtotal are then not added again.
    stated_sub, ssl = v.get("stated.subtotal")
    charges_in_subtotal = False
    if items_sum is not None and stated_sub is not None:
        res = _compare(items_sum, stated_sub)
        res["base"] = "line totals"
        nt_vals = [amt(x) for x in non_tax] + [-abs(amt(x)) if amt(x) is not None else None for x in discounts]
        if res["status"] == "FAILS" and nt_vals and all(x is not None for x in nt_vals):
            alt = _compare(items_sum + sum(nt_vals, Decimal(0)), stated_sub)
            if alt["status"] != "FAILS":
                res, charges_in_subtotal = dict(alt, base="line totals + charges"), True
        checks.append({"check": "subtotal", **res})
    elif stated_sub is not None:
        checks.append({"check": "subtotal", "status": "NOT_CHECKED", "reason": f"unknown line totals {unknown_lines}"})

    base = stated_sub if stated_sub is not None else items_sum

    # ---- tax rate check, only where a rate is stated
    for ch in taxes:
        if ch["rate"] is None:
            continue
        rate, tax_amt = Decimal(ch["rate"]), amt(ch)
        if base is None or tax_amt is None:
            checks.append({"check": "tax_rate", "id": ch["id"], "status": "NOT_CHECKED", "reason": "base or tax unknown"})
            continue
        extra = [amt(x) for x in non_tax] + [-abs(amt(x)) if amt(x) is not None else None for x in discounts]
        bases = [("subtotal", base)]
        if all(e is not None for e in extra) and extra and not charges_in_subtotal:
            bases.append(("subtotal + charges - discounts", base + sum(extra, Decimal(0))))
        results = [(name, _compare(money.q2(b * rate / 100), tax_amt)) for name, b in bases]
        good = next(((n, r) for n, r in results if r["status"] != "FAILS"), None)
        checks.append({"check": "tax_rate", "id": ch["id"], "rate": ch["rate"],
                       **(dict(good[1], base=good[0]) if good else dict(results[-1][1], base=results[-1][0]))})

    # ---- grand total
    stated_gt, gtl = v.get("stated.grand_total")
    tax_total = None
    if taxes:
        if all(amt(x) is not None for x in taxes):
            tax_total = sum((amt(x) for x in taxes), Decimal(0))
    elif stated_tax is not None:
        tax_total = stated_tax
    components_unknown = ([x["id"] for x in non_tax + discounts + taxes if amt(x) is None]
                          + (["subtotal"] if base is None else []))
    calc_gt = None
    if not components_unknown:
        in_sub = Decimal(0) if charges_in_subtotal else (
            sum((amt(x) for x in non_tax), Decimal(0)) - sum((abs(amt(x)) for x in discounts), Decimal(0)))
        calc_gt = base + in_sub + (tax_total or Decimal(0))
    if stated_gt is not None:
        if calc_gt is not None:
            checks.append({"check": "grand_total", **_compare(calc_gt, stated_gt)})
        else:
            checks.append({"check": "grand_total", "status": "NOT_CHECKED",
                           "reason": f"unknown components {components_unknown}"})

    # ---- AI cross-check: the extractor's own arithmetic is shown, never trusted
    ai = extraction.get("ai_arithmetic") or {}
    ai_check = {}
    for key, trusted in (("subtotal", items_sum), ("grand_total", calc_gt)):
        claimed = ai.get(key)
        if claimed is None:
            ai_check[key] = {"status": "NOT_PROVIDED"}
            continue
        a = money.parse_amount(claimed)
        if not a.ok or trusted is None:
            ai_check[key] = {"status": "NOT_COMPARABLE", "ai_value": claimed}
        else:
            ai_check[key] = {"status": "AGREES" if money.q2(a.value) == money.q2(trusted) else "DISAGREES",
                             "ai_value": claimed, "trusted_value": str(money.q2(trusted))}
            if ai_check[key]["status"] == "DISAGREES":
                findings.append({"code": "AI_ARITHMETIC_UNVERIFIED", "severity": "review",
                                 "detail": f"extractor says {key}={claimed}, trusted engine {money.q2(trusted)}"})

    for x, c in ((x, next((c for c in checks if c["check"] == "line" and c["id"] == x["id"]), None)) for x in lines):
        if c and c["status"] == "NOT_CHECKED":
            empty = all(x[k]["value"] is None for k in ("qty", "unit_price", "line_total"))
            findings.append({"code": "LINE_WITHOUT_AMOUNTS" if empty else "LINE_NOT_CHECKED", "severity": "review",
                             "path": f"line_items.{x['id']}", "id": x["id"],
                             "detail": (f"'{x['description']}' has no verified quantity, price or total; totals stay Unknown "
                                        "until a reviewer corrects it or marks it as not a line item") if empty
                             else f"'{x['description']}': {c['reason']}"})
    for c in checks:
        if c["status"] == "FAILS":
            findings.append({"code": "RECONCILIATION_FAILED", "severity": "review", "check": c["check"],
                             "id": c.get("id"),
                             "detail": f"{c['check']}: expected {c['expected']}, stated {c['stated']} (difference {c['difference']})"})

    failed = sum(1 for c in checks if c["status"] == "FAILS")
    not_checked = sum(1 for c in checks if c["status"] == "NOT_CHECKED")
    status = "FAILS" if failed else ("INCOMPLETE" if not_checked or not checks else "RECONCILES")
    return {
        "status": status,
        "checks": checks,
        "lines": lines,
        "charges": charges,
        "totals": {
            "items_subtotal": fig(items_sum, _calc_label(*(x["line_total"]["label"] for x in lines)))
            if items_sum is not None else fig(None, UNKNOWN, unknown=unknown_lines),
            "stated_subtotal": fig(stated_sub, ssl) if stated_sub is not None else fig(None, UNKNOWN),
            "tax": fig(tax_total, QUOTED) if tax_total is not None else fig(None, UNKNOWN),
            "grand_total_calculated": fig(calc_gt, CALCULATED) if calc_gt is not None
            else fig(None, UNKNOWN, unknown=components_unknown),
            "grand_total_stated": fig(stated_gt, gtl) if stated_gt is not None else fig(None, UNKNOWN),
        },
        "ai_cross_check": ai_check,
        "findings": findings,
    }
