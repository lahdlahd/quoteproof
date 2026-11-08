"""Trusted indexer: exact values, locations, formulas vs cached values, and flags.

NOTE: here the indexer runs directly (our own code on our own fixtures).
On the VM it only ever runs inside the sandbox.
"""
import json
import os
import sys

import pytest

# On the VM host these libraries are intentionally NOT installed (they live only in the
# indexer image), so these tests are skipped there; the indexer is tested in-sandbox by step 7.
pytest.importorskip("openpyxl")
pytest.importorskip("pdfplumber")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "images", "indexer"))
import indexer  # noqa: E402

FIX = os.path.join(ROOT, "fixtures")


def idx(name, declared):
    with open(os.path.join(FIX, name), "rb") as f:
        return indexer.build_index(f.read(), declared)


def cell(o, ref):
    return next(c for c in o["cells"] if c["ref"] == ref)


def codes(o):
    return {(w["code"], w["ref"]) for w in o["warnings"]}


def test_csv_ngn_values_are_exact_strings():
    o = idx("ngn_quote.csv", "csv")
    assert o["ok"] and o["schema"] == "qp.source_index.v1"
    assert cell(o, "R4C3")["value"] == "₦85,000.00"
    assert cell(o, "R8C4")["value"] == "₦1,585,625.00"
    assert cell(o, "R9C2")["value"].startswith("=SUM")  # kept as plain text, never evaluated
    assert o["csv"]["delimiter"] == ","


def test_xlsx_formula_and_cached_value_preserved():
    o = idx("usd_quote.xlsx", "xlsx")
    d4 = cell(o, "Quote!D4")
    assert d4["formula"] == "=B4*C4" and d4["value"] == "3598" and d4["value_type"] == "n"
    assert cell(o, "Quote!C4")["value"] == "899.5"
    assert cell(o, "Quote!D7")["formula"] == "=SUM(D4:D6)" and cell(o, "Quote!D7")["value"] == "4153"
    # LibreOffice re-saves '"$"#,##0.00' as '\$#,##0.00'; the indexer must report the file as-is
    assert d4["number_format"] == "\\$#,##0.00"


def test_xlsx_hidden_row_flagged():
    o = idx("usd_quote.xlsx", "xlsx")
    assert cell(o, "Quote!D6")["hidden"] is True
    assert ("HIDDEN_CONTENT", "Quote!D6") in codes(o)


def test_recalc_flag_only_when_file_says_so():
    assert ("RECALC_ON_LOAD", None) not in codes(idx("usd_quote.xlsx", "xlsx"))        # LibreOffice-saved
    assert ("RECALC_ON_LOAD", None) in codes(idx("usd_quote_nocache.xlsx", "xlsx"))    # openpyxl-saved


def test_missing_cached_value_flagged_and_value_unknown():
    o = idx("usd_quote_nocache.xlsx", "xlsx")
    d4 = cell(o, "Quote!D4")
    assert d4["formula"] == "=B4*C4" and d4["value"] is None
    assert ("FORMULA_NO_CACHED_VALUE", "Quote!D4") in codes(o)


def test_errors_external_refs_hidden_sheet():
    o = idx("errors.xlsx", "xlsx")
    c = codes(o)
    assert ("FORMULA_ERROR", "Errors!B3") in c and cell(o, "Errors!B3")["value"] == "#DIV/0!"
    assert ("EXTERNAL_REFERENCE", "Errors!B4") in c
    assert ("HIDDEN_CONTENT", "Hidden!B1") in c
    assert o["workbook"]["external_link_count"] == 1


def test_macro_rejected():
    o = idx("macro.xlsx", "xlsx")
    assert not o["ok"] and o["error"]["code"] == "MACRO_CONTENT" and "cells" not in o


def test_pdf_lines_with_page_locations():
    o = idx("ngn_quote.pdf", "pdf")
    refs = {l["ref"]: l["text"] for l in o["lines"]}
    assert refs["p1:L6"] == "Total 2,500,000.00"
    assert refs["p2:L1"].startswith("Page 2: Terms")
    assert all(isinstance(v, str) for l in o["lines"] for v in l["bbox"])


def test_pdf_without_text_flagged():
    o = idx("scanned_like.pdf", "pdf")
    assert o["ok"] and ("NO_TEXT_LAYER", None) in codes(o)


def test_type_mismatch_and_binary_rejected():
    assert idx("pdf_pretending_to_be.xlsx", "xlsx")["error"]["code"] == "TYPE_MISMATCH"
    assert idx("binary.csv", "csv")["error"]["code"] == "TYPE_MISMATCH"


def test_no_floats_anywhere_in_output():
    for name, t in [("ngn_quote.csv", "csv"), ("usd_quote.xlsx", "xlsx"), ("ngn_quote.pdf", "pdf")]:
        text = json.dumps(idx(name, t))
        def walk(x):
            if isinstance(x, float):
                raise AssertionError(f"float found in {name}")
            if isinstance(x, dict):
                for v in x.values():
                    walk(v)
            if isinstance(x, list):
                for v in x:
                    walk(v)
        walk(json.loads(text))


def test_zip_bomb_like_workbook_rejected(tmp_path):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("xl/workbook.xml", "<x/>")
        z.writestr("xl/worksheets/sheet1.xml", "0" * (indexer.MAX_XLSX_UNCOMPRESSED + 1))
    o = indexer.build_index(buf.getvalue(), "xlsx")
    assert o["error"]["code"] == "XLSX_TOO_LARGE_UNCOMPRESSED"


def test_cell_limit_truncates_with_flag(monkeypatch):
    monkeypatch.setattr(indexer, "MAX_CELLS", 5)
    o = idx("ngn_quote.csv", "csv")
    assert o["truncated"] is True and len(o["cells"]) == 5
    assert any(w["code"] == "CELL_LIMIT" for w in o["warnings"])
