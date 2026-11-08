"""T1 trusted core: money parsing, schemas, formula re-checks, Layer 1, Layer 2, comparison.

Uses saved Source Indexes (fixtures/indexes/) so it also runs on the VM, where the
indexer libraries are intentionally not installed.
"""
import copy
import json
import os
from decimal import Decimal

import pytest

from qp import compare, formulas, layer1, layer2, money, schemas

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def load(name):
    with open(os.path.join(FIX, "indexes", name + ".json"), encoding="utf-8") as f:
        idx = json.load(f)
    with open(os.path.join(FIX, "extractions", name + ".json"), encoding="utf-8") as f:
        ex = json.load(f)
    return schemas.validate_index(idx), schemas.validate_extraction(ex)


def run(name, cur, mutate=None, overrides=None):
    idx, ex = load(name)
    if mutate:
        mutate(ex)
    l1 = layer1.verify(idx, ex, cur)
    return l1, layer2.reconcile(ex, l1, overrides)


def codes(findings):
    return {f["code"] for f in findings}


# ------------------------------------------------------------------ money
@pytest.mark.parametrize("text,value,cur,cert", [
    ("₦85,000.00", "85000.00", "NGN", "certain"), ("NGN 1,585,625.00", "1585625.00", "NGN", "certain"),
    ("N85,000.00", "85000.00", "NGN", "possible"), ("$1,200.50", "1200.50", "USD", "possible"),
    ("USD 1,200", "1200", "USD", "certain"), ("1,200.00 USD", "1200.00", "USD", "certain"),
    ("US$5", "5", "USD", "certain"), ("(1,000.00)", "-1000.00", None, None), ("10", "10", None, None),
])
def test_parse_amount_ok(text, value, cur, cert):
    a = money.parse_amount(text)
    assert a.ok and a.value == Decimal(value) and a.currency == cur and a.certainty == cert


@pytest.mark.parametrize("text", ["1.250,00", "1,25", "1.250", "12.345"])
def test_ambiguous_formats_never_guessed(text):
    a = money.parse_amount(text)
    assert a.status == "ambiguous" and a.value is None


def test_floats_rejected_everywhere():
    with pytest.raises(TypeError):
        money.parse_amount(1.5)
    with pytest.raises(schemas.SchemaError):
        schemas.validate({"a": 1.5}, {"a": "str"})


def test_tokens_ignore_codes_dates_and_units():
    toks = [t.raw for t in money.tokens("Quotation BB-0091 dated 2026-11-07: Cement 50kg 200 x 9,500.00")]
    assert toks == ["200", "9,500.00"]


def test_rounding_half_up():
    assert money.q2(Decimal("2.005")) == Decimal("2.01") and money.q2(Decimal("2.004")) == Decimal("2.00")


# ------------------------------------------------------------------ schemas
def test_extraction_rejects_extra_keys_and_bad_amounts():
    _, ex = load("ngn_quote.csv")
    bad = copy.deepcopy(ex)
    bad["line_items"][0]["discount_hidden"] = "x"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_extraction(bad)
    bad = copy.deepcopy(ex)
    bad["line_items"][0]["unit_price"]["value_str"] = "about 85k"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_extraction(bad)
    bad = copy.deepcopy(ex)
    bad["line_items"][1]["id"] = "L1"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_extraction(bad)


def test_index_schema_rejects_unexpected_fields():
    idx, _ = load("ngn_quote.csv")
    idx = copy.deepcopy(idx)
    idx["cells"][0]["note"] = "x"
    with pytest.raises(schemas.SchemaError):
        schemas.validate_index(idx)


# ------------------------------------------------------------------ formulas
def _cells(formula, cached, a="2", b="3"):
    return [{"ref": "S!A1", "sheet": "S", "row": 1, "col": 1, "formula": None, "value": a, "value_type": "n"},
            {"ref": "S!B1", "sheet": "S", "row": 1, "col": 2, "formula": None, "value": b, "value_type": "n"},
            {"ref": "S!C1", "sheet": "S", "row": 1, "col": 3, "formula": formula, "value": cached, "value_type": "n"}]


@pytest.mark.parametrize("formula,cached,status", [
    ("=A1*B1", "6", "CHECKED_OK"), ("=SUM(A1:B1)", "5", "CHECKED_OK"), ("=(A1+B1)/2", "2.5", "CHECKED_OK"),
    ("=A1*B1", "7", "POSSIBLY_STALE"), ("=VLOOKUP(A1,B1,1)", "2", "FORMULA_NOT_CHECKED"),
    ("=Other!A1*2", "4", "FORMULA_NOT_CHECKED"), ("=A1/0", "0", "FORMULA_NOT_CHECKED"),
])
def test_formula_recheck(formula, cached, status):
    assert formulas.check_workbook(_cells(formula, cached))["S!C1"]["status"] == status


def test_formula_never_uses_eval():
    with pytest.raises(formulas.NotSimple):
        formulas.tokenize("=__import__('os')")


# ------------------------------------------------------------------ Layer 1
def test_l1_csv_all_verified_and_currency_detected():
    l1, _ = run("ngn_quote.csv", "NGN")
    assert l1["summary"] == {"amounts_total": 11, "amounts_verified": 11, "unaccounted": 0}
    assert l1["currency"]["detected"] == "NGN"


def test_l1_pdf_token_level_citations():
    l1, _ = run("ngn_quote.pdf", "NGN")
    assert l1["summary"]["amounts_verified"] == l1["summary"]["amounts_total"] == 8


def test_l1_hidden_delivery_surfaces_as_unaccounted():
    l1, _ = run("usd_quote.xlsx", "USD")
    assert [u["ref"] for u in l1["unaccounted"]] == ["Quote!D6"] and l1["unaccounted"][0]["hidden"]
    assert {"UNACCOUNTED_AMOUNT", "HIDDEN_CONTENT"} <= codes(l1["findings"])


def test_l1_statuses_mismatch_notfound_uncited():
    def m(ex):
        ex["line_items"][0]["unit_price"]["value_str"] = "86000.00"   # wrong value
        ex["line_items"][1]["unit_price"]["cite"] = "R99C9"           # points nowhere
        ex["line_items"][1]["line_total"]["cite"] = None              # no citation
    l1, l2 = run("ngn_quote.csv", "NGN", m)
    v = l1["values"]
    assert v["line_items.L1.unit_price"]["status"] == "MISMATCH"
    assert v["line_items.L2.unit_price"]["status"] == "NOT_FOUND"
    assert v["line_items.L2.line_total"]["status"] == "UNCITED"
    # an unverified value is never used by Layer 2
    assert l2["lines"][0]["unit_price"]["label"] == "Unknown"


def test_l1_ambiguous_value_goes_to_review():
    def m(ex):
        ex["charges"][0]["amount"]["value_str"] = "25.000,00"
    l1, _ = run("ngn_quote.csv", "NGN", m)
    assert l1["values"]["charges.C1.amount"]["status"] == "AMBIGUOUS"


def test_l1_formula_without_cache_is_formula_issue():
    idx, ex = load("usd_quote.xlsx")
    with open(os.path.join(FIX, "indexes", "usd_quote_nocache.xlsx.json"), encoding="utf-8") as f:
        nocache = schemas.validate_index(json.load(f))
    l1 = layer1.verify(nocache, ex, "USD")
    assert l1["values"]["line_items.L1.line_total"]["status"] == "FORMULA_ISSUE"


def test_l1_currency_conflict_blocks():
    l1, _ = run("ngn_quote.csv", "USD")
    assert "CURRENCY_CONFLICT" in codes(l1["findings"])


@pytest.mark.parametrize("det,ex_says,user,proj,doc_cur,comparable,expect", [
    ({"detected": "NGN"}, "NGN", "NGN", "NGN", "NGN", True, set()),
    ({"detected": "POSSIBLE_USD"}, "USD", "USD", "USD", "USD", True, {"CURRENCY_POSSIBLE_ONLY"}),
    ({"detected": "NGN"}, "USD", "NGN", "NGN", "NGN", True, {"CURRENCY_DISAGREEMENT"}),
    ({"detected": "USD"}, "USD", "USD", "NGN", "USD", False, {"CURRENCY_NOT_COMPARABLE"}),
    ({"detected": "MIXED"}, "NGN", "NGN", "NGN", "NGN", False, {"CURRENCY_MIXED"}),
])
def test_currency_three_way_check(det, ex_says, user, proj, doc_cur, comparable, expect):
    cur, comp, findings = layer1.resolve_currency(det, ex_says, user, proj)
    assert (cur, comp) == (doc_cur, comparable) and expect <= codes(findings)


# ------------------------------------------------------------------ Layer 2
def test_l2_csv_reconciles_including_vat_base():
    _, l2 = run("ngn_quote.csv", "NGN")
    assert l2["status"] == "RECONCILES"
    tax = next(c for c in l2["checks"] if c["check"] == "tax_rate")
    assert tax["status"] == "RECONCILES" and tax["base"] == "subtotal + charges - discounts"
    assert l2["totals"]["grand_total_calculated"] == {"value": "1585625.00", "label": "Calculated"}
    assert l2["ai_cross_check"]["grand_total"]["status"] == "AGREES"


def test_l2_hidden_charge_breaks_subtotal_by_exactly_75():
    _, l2 = run("usd_quote.xlsx", "USD")
    sub = next(c for c in l2["checks"] if c["check"] == "subtotal")
    assert (sub["status"], sub["difference"]) == ("FAILS", "75.00")
    assert l2["totals"]["tax"]["label"] == "Unknown"


def test_l2_rounding_vs_failure():
    def m1(ex):
        ex["line_items"][0]["line_total"]["value_str"] = "850000.01"
    def m2(ex):
        ex["line_items"][0]["line_total"]["value_str"] = "850000.02"
    idx, _ = load("ngn_quote.csv")
    # make the source agree with the modified value so Layer 1 still verifies it
    for val, mut, expected in (("₦850,000.01", m1, "ROUNDING"), ("₦850,000.02", m2, "FAILS")):
        i2 = copy.deepcopy(idx)
        next(c for c in i2["cells"] if c["ref"] == "R4C4")["value"] = val
        _, ex = load("ngn_quote.csv")
        mut(ex)
        l2 = layer2.reconcile(ex, layer1.verify(i2, ex, "NGN"))
        assert next(c for c in l2["checks"] if c["id"] == "L1")["status"] == expected


def test_l2_unknown_is_never_zero():
    def m(ex):
        ex["charges"][0]["amount"]["cite"] = None   # delivery becomes unverified
    _, l2 = run("ngn_quote.csv", "NGN", m)
    assert l2["charges"][0]["amount"] == {"value": None, "label": "Unknown"}
    gt = next(c for c in l2["checks"] if c["check"] == "grand_total")
    assert gt["status"] == "NOT_CHECKED" and l2["totals"]["grand_total_calculated"]["value"] is None


def test_l2_reviewer_values_are_labelled():
    def m(ex):
        ex["charges"][0]["amount"]["cite"] = None
    _, l2 = run("ngn_quote.csv", "NGN", m,
                overrides={"charges.C1.amount": {"value_str": "25000.00", "kind": "user_entered"}})
    assert l2["charges"][0]["amount"] == {"value": "25000.00", "label": "Estimated"}


def test_l2_ai_arithmetic_disagreement_is_flagged():
    def m(ex):
        ex["ai_arithmetic"]["grand_total"] = "1500000.00"
    _, l2 = run("ngn_quote.csv", "NGN", m)
    assert l2["ai_cross_check"]["grand_total"]["status"] == "DISAGREES"
    assert "AI_ARITHMETIC_UNVERIFIED" in codes(l2["findings"])


# ------------------------------------------------------------------ comparison
def _docs():
    _, a = run("ngn_quote.csv", "NGN")
    _, b = run("ngn_quote.pdf", "NGN")
    _, c = run("usd_quote.xlsx", "USD")
    return [{"doc_id": "A", "supplier": "Adeyemi", "currency": "NGN", "comparable": True, "l2": a},
            {"doc_id": "B", "supplier": "Bello", "currency": "NGN", "comparable": True, "l2": b},
            {"doc_id": "C", "supplier": "Lakeside", "currency": "USD", "comparable": False, "l2": c}]


def test_compare_only_confirmed_groups_count_and_unknown_not_zero():
    groups = [{"group_id": "g1", "label": "Chair vs cement (test)", "status": "confirmed",
               "members": [{"doc_id": "A", "item_id": "L1"}, {"doc_id": "B", "item_id": "L1"}],
               "confirmed_by": "Ada (self-declared)", "confirmed_at": "2026-11-07T16:00:00Z"},
              {"group_id": "g2", "label": "proposed only", "status": "proposed",
               "members": [{"doc_id": "A", "item_id": "L2"}, {"doc_id": "B", "item_id": "L2"}]}]
    out = compare.compare(_docs(), groups, "NGN")
    assert [s["doc_id"] for s in out["suppliers"]] == ["A", "B"]
    assert out["not_comparable_currency"] == [{"doc_id": "C", "supplier": "Lakeside", "currency": "USD"}]
    # proposed group is not in totals
    assert {(x["doc_id"], x["item_id"]) for x in out["not_compared_yet"]} == {("A", "L2"), ("B", "L2")}
    assert all(x["equivalence"] == "Proposed by AI, not confirmed" for x in out["not_compared_yet"])
    # A states VAT, B does not: B's tax is Unknown, never zero, and B's total is incomplete
    tax = next(r for r in out["charges"] if r["kind"] == "tax")
    assert tax["cells"]["B"]["label"] == "Unknown" and tax["cells"]["B"]["value"] is None
    assert out["totals"]["B"]["complete"] is False and "tax: unknown" in out["totals"]["B"]["unknown_components"]
    assert out["totals"]["A"]["known_total"] == "985625.00"   # 850,000 + 25,000 + 110,625
    assert "No currency conversion performed" in out["note"]


def test_l2_charges_inside_subtotal_are_not_double_counted():
    """usd_quote.xlsx: Subtotal =SUM(D4:D6) includes the (hidden) delivery row."""
    def m(ex):
        ex["charges"].append({"id": "C1", "kind": "delivery", "description": {"value_str": "Delivery", "cite": "Quote!A6"},
                              "amount": {"value_str": "75", "cite": "Quote!D6"}, "rate": None})
    l1, l2 = run("usd_quote.xlsx", "USD", m)
    sub = next(c for c in l2["checks"] if c["check"] == "subtotal")
    assert (sub["status"], sub["base"]) == ("RECONCILES", "line totals + charges")
    assert l2["totals"]["grand_total_calculated"]["value"] == "4153.00"      # not 4228.00
    assert "HIDDEN_CONTENT" in codes(l1["findings"])                         # still flagged for review


def _add_note_row(ex):
    """Exactly what deepseek produced on the VM (7 Nov, M2 acceptance E6)."""
    ex["line_items"].append({"id": "L3", "description": {"value_str": "Note", "cite": "R9C1"},
                             "qty": None, "unit_price": None, "line_total": None})


def test_l2_line_without_amounts_is_a_review_finding_and_totals_stay_unknown():
    _, l2 = run("ngn_quote.csv", "NGN", _add_note_row)
    assert l2["status"] == "INCOMPLETE" and l2["totals"]["grand_total_calculated"]["value"] is None
    f = next(x for x in l2["findings"] if x["code"] == "LINE_WITHOUT_AMOUNTS")
    assert f["path"] == "line_items.L3" and "Note" in f["detail"]
