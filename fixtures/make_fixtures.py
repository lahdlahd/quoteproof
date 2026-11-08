"""Generates small test documents for Milestone 1 (indexer + sandboxd tests).

These are TEST FIXTURES, not the demo quotations (those come later with an
answer key). Run:  python3 fixtures/make_fixtures.py
Needs: openpyxl, reportlab, and LibreOffice (soffice) to save cached formula results.
"""

import os
import shutil
import subprocess
import tempfile
import zipfile

import openpyxl
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HERE = os.path.dirname(os.path.abspath(__file__))


def p(name):
    return os.path.join(HERE, name)


def csv_ngn():
    with open(p("ngn_quote.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("Supplier,Adeyemi Office Supplies Ltd\n")
        f.write("Quote No,AOS-2026-114\n")
        f.write("Item,Qty,Unit Price (NGN),Line Total (NGN)\n")
        f.write('Office chair (mesh),10,"₦85,000.00","₦850,000.00"\n')
        f.write('Desk 1.4m,5,"₦120,000.00","₦600,000.00"\n')
        f.write('Delivery,,,"₦25,000.00"\n')
        f.write('VAT 7.5%,,,"₦110,625.00"\n')
        f.write('Grand Total,,,"₦1,585,625.00"\n')
        f.write("Note,=SUM(D4:D5) is plain text in a CSV\n")


def xlsx_usd(path, hidden_delivery=True):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Quote"
    ws.append(["Supplier", "Lakeside Equipment Inc."])
    ws.append(["Currency", "USD"])
    ws.append(["Item", "Qty", "Unit Price", "Line Total"])
    ws.append(["Laptop 14in", 4, 899.5, "=B4*C4"])
    ws.append(["Docking station", 4, 120, "=B5*C5"])
    ws.append(["Delivery", None, None, 75])
    ws.append(["Subtotal", None, None, "=SUM(D4:D6)"])
    ws["D4"].number_format = '"$"#,##0.00'
    if hidden_delivery:
        ws.row_dimensions[6].hidden = True
    wb.save(path)


def recalc_with_libreoffice(src, dst):
    """Open + save through LibreOffice so formula results are cached in the file."""
    out = tempfile.mkdtemp()
    subprocess.run(["soffice", "--headless", "--calc", "--convert-to", "xlsx", "--outdir", out, src],
                   check=True, capture_output=True, timeout=120)
    shutil.move(os.path.join(out, os.path.basename(src)), dst)
    shutil.rmtree(out)


def xlsx_errors(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Errors"
    ws.append(["Value", 10])
    ws.append(["Zero", 0])
    ws.append(["Divide", "=B1/B2"])
    ws.append(["External", "='[prices.xlsx]Sheet1'!A1"])
    hidden = wb.create_sheet("Hidden")
    hidden["A1"] = "Service fee"
    hidden["B1"] = 300
    hidden.sheet_state = "hidden"
    wb.save(path)


def xlsx_macro(path):
    xlsx_usd(path, hidden_delivery=False)
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("xl/vbaProject.bin", b"not really a macro")


def pdf_ngn(path):
    c = canvas.Canvas(path, pagesize=A4)
    y = 800
    for line in [
        "Bello Builders Ltd - Quotation BB-0091",
        "Currency: NGN",
        "Cement 50kg bag   200 x 9,500.00 = 1,900,000.00",
        "Sand (tonnes)      30 x 18,000.00 =   540,000.00",
        "Transport                              60,000.00",
        "Total                               2,500,000.00",
    ]:
        c.drawString(72, y, line)
        y -= 20
    c.showPage()
    c.drawString(72, 800, "Page 2: Terms - valid 30 days. Prices exclude VAT.")
    c.save()


def pdf_blank(path):
    c = canvas.Canvas(path, pagesize=A4)
    c.rect(100, 100, 200, 200)  # a drawing, no text: looks like a scanned page
    c.save()


def main():
    csv_ngn()
    tmp = p("_usd_tmp.xlsx")
    xlsx_usd(tmp)
    shutil.copy(tmp, p("usd_quote_nocache.xlsx"))      # openpyxl output: formulas, no cached values
    recalc_with_libreoffice(tmp, p("usd_quote.xlsx"))   # formulas + cached values
    os.remove(tmp)
    tmp = p("_err_tmp.xlsx")
    xlsx_errors(tmp)
    recalc_with_libreoffice(tmp, p("errors.xlsx"))
    os.remove(tmp)
    xlsx_macro(p("macro.xlsx"))
    pdf_ngn(p("ngn_quote.pdf"))
    pdf_blank(p("scanned_like.pdf"))
    shutil.copy(p("ngn_quote.pdf"), p("pdf_pretending_to_be.xlsx"))
    with open(p("binary.csv"), "wb") as f:
        f.write(b"Item,Qty\x00\x01\x02")
    print("fixtures written to", HERE)


if __name__ == "__main__":
    main()
