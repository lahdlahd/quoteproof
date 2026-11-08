"""Trusted document indexer (our fixed code). Runs ONLY inside the sandbox on the VM.

Reads /input/document (+ /input/meta.json) and prints one JSON "Source Index"
to stdout: a neutral, uninterpreted record of what is in the file, with exact
locations. It never decides what a value means (that is the AI extractor's job)
and never evaluates formulas.

Schema: qp.source_index.v1 (see docs/SOURCE_INDEX.md)
"""

import csv
import datetime as dt
import hashlib
import io
import json
import re
import resource
import sys
import zipfile

SCHEMA = "qp.source_index.v1"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CELLS = 50_000
MAX_PAGES = 50
MAX_TEXT = 2_000
MAX_WARNINGS = 1_000
MAX_XLSX_UNCOMPRESSED = 60 * 1024 * 1024
MAX_XLSX_MEMBERS = 5_000
CSV_DELIMS = ",;\t|"
RECALC_RE = re.compile(rb'<(?:\w+:)?calcPr\b[^>]*\bfullCalcOnLoad\s*=\s*"(?:1|true)"')


class DocError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


class Index:
    def __init__(self, data, declared):
        self.out = {
            "schema": SCHEMA,
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "source_bytes": len(data),
            "declared_type": declared,
            "detected_type": None,
            "ok": False,
            "error": None,
            "truncated": False,
            "warnings": [],
        }

    def warn(self, code, detail="", ref=None):
        w = self.out["warnings"]
        if len(w) < MAX_WARNINGS:
            w.append({"code": code, "detail": detail, "ref": ref})
        elif not any(x["code"] == "WARNINGS_TRUNCATED" for x in w):
            w.append({"code": "WARNINGS_TRUNCATED", "detail": f"more than {MAX_WARNINGS}", "ref": None})
            self.out["truncated"] = True

    def text(self, s, ref):
        if len(s) > MAX_TEXT:
            self.warn("TEXT_TRUNCATED", f"cut to {MAX_TEXT} chars", ref)
            self.out["truncated"] = True
            return s[:MAX_TEXT]
        return s


# ------------------------------------------------------------------ type check
def detect_type(data):
    if b"%PDF-" in data[:1024]:
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "zip"
    if b"\x00" in data[:65536]:
        return "binary"
    return "text"


# ------------------------------------------------------------------ CSV
def index_csv(ix, data):
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
        enc = "utf-8"
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
        enc = "cp1252"
        ix.warn("ENCODING_FALLBACK", "file is not UTF-8; decoded as Windows-1252")
    sample = text[:65536]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=CSV_DELIMS).delimiter
    except csv.Error:
        delim = ","
    csv.field_size_limit(1_000_000)
    cells, rows = [], 0
    for r, row in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delim), start=1):
        rows = r
        for c, raw in enumerate(row, start=1):
            if raw == "":
                continue
            if len(cells) >= MAX_CELLS:
                ix.out["truncated"] = True
                ix.warn("CELL_LIMIT", f"stopped at {MAX_CELLS} cells", f"R{r}C{c}")
                break
            ref = f"R{r}C{c}"
            cells.append({"ref": ref, "row": r, "col": c, "value": ix.text(raw, ref)})
        if ix.out["truncated"] and len(cells) >= MAX_CELLS:
            break
    ix.out["csv"] = {"delimiter": delim, "encoding": enc, "rows": rows}
    ix.out["cells"] = cells
    if not cells:
        ix.warn("EMPTY_DOCUMENT", "no non-empty cells")


# ------------------------------------------------------------------ XLSX
def fmt_value(v):
    """Render a cell value as an exact string (never a float in the JSON)."""
    if v is None:
        return None, None
    if isinstance(v, bool):
        return ("TRUE" if v else "FALSE"), "b"
    if isinstance(v, int):
        return str(v), "n"
    if isinstance(v, float):
        return repr(v), "n"
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat(), "d"
    if isinstance(v, dt.timedelta):
        return str(v.total_seconds()), "n"
    return str(v), "s"


def formula_text(v):
    """openpyxl returns str for normal formulas, objects for array/data-table formulas."""
    if isinstance(v, str):
        return v
    text = getattr(v, "text", None)
    return text if isinstance(text, str) else f"<{type(v).__name__}>"


def precheck_zip(data):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DocError("XLSX_UNREADABLE", "not a valid zip container")
    infos = zf.infolist()
    names = {i.filename for i in infos}
    if len(infos) > MAX_XLSX_MEMBERS:
        raise DocError("XLSX_TOO_COMPLEX", f"{len(infos)} zip members")
    total = sum(i.file_size for i in infos)
    if total > MAX_XLSX_UNCOMPRESSED:
        raise DocError("XLSX_TOO_LARGE_UNCOMPRESSED", f"{total} bytes uncompressed")
    if "xl/workbook.xml" not in names:
        raise DocError("TYPE_MISMATCH", "zip file is not an .xlsx workbook")
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise DocError("MACRO_CONTENT", "workbook contains macros (.xlsm content is not accepted)")
    return names


def index_xlsx(ix, data):
    import openpyxl  # imported here so CSV/PDF runs don't need it

    names = precheck_zip(data)
    wb_f = openpyxl.load_workbook(io.BytesIO(data), data_only=False)  # formulas
    wb_v = openpyxl.load_workbook(io.BytesIO(data), data_only=True)   # cached results

    # Read the real setting from the file: openpyxl substitutes fullCalcOnLoad=True
    # when the workbook has no <calcPr>, which would be a false warning.
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        wb_xml = zf.read("xl/workbook.xml")[:2_000_000]
    recalc = bool(RECALC_RE.search(wb_xml))
    ext_links = len(getattr(wb_f, "_external_links", []) or [])
    if not ext_links:
        ext_links = sum(1 for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml"))
    if recalc:
        ix.warn("RECALC_ON_LOAD", "workbook asks to be recalculated when opened; saved results may be stale")
    if ext_links:
        ix.warn("EXTERNAL_LINKS", f"{ext_links} external workbook link(s)")

    sheets, cells = [], []
    for ws_f in wb_f.worksheets:
        ws_v = wb_v[ws_f.title]
        state = ws_f.sheet_state
        sheets.append({"name": ws_f.title, "state": state,
                       "max_row": ws_f.max_row, "max_col": ws_f.max_column})
        hidden_rows = {r for r, d in ws_f.row_dimensions.items() if d.hidden}
        hidden_cols = set()
        for d in ws_f.column_dimensions.values():
            if d.hidden and d.min and d.max:
                hidden_cols.update(range(d.min, min(d.max, 16384) + 1))

        # _cells holds only cells that exist in the file (avoids iterating huge empty ranges)
        for (r, c) in sorted(ws_f._cells):
            cf = ws_f._cells[(r, c)]
            if cf.value is None:
                continue
            if len(cells) >= MAX_CELLS:
                ix.out["truncated"] = True
                ix.warn("CELL_LIMIT", f"stopped at {MAX_CELLS} cells", f"{ws_f.title}!{cf.coordinate}")
                break
            ref = f"{ws_f.title}!{cf.coordinate}"
            is_formula = cf.data_type == "f"
            formula = formula_text(cf.value) if is_formula else None
            cached_cell = ws_v._cells.get((r, c))
            cached_raw = cached_cell.value if cached_cell is not None else None
            if is_formula:
                value, vtype = fmt_value(cached_raw)
                if cached_cell is not None and cached_cell.data_type == "e":
                    vtype = "e"
            else:
                value, vtype = fmt_value(cf.value)
                if cf.data_type == "e":
                    vtype = "e"
            hidden = state != "visible" or r in hidden_rows or c in hidden_cols
            entry = {
                "ref": ref, "sheet": ws_f.title, "row": r, "col": c,
                "formula": ix.text(formula, ref) if formula else None,
                "value": ix.text(value, ref) if value is not None else None,
                "value_type": vtype,
                "number_format": cf.number_format,
                "hidden": hidden,
            }
            cells.append(entry)

            if is_formula and value is None:
                ix.warn("FORMULA_NO_CACHED_VALUE", "formula has no saved result", ref)
            if vtype == "e":
                ix.warn("FORMULA_ERROR", f"cell shows error {value}", ref)
            if formula and "[" in formula and "]" in formula:
                ix.warn("EXTERNAL_REFERENCE", "formula refers to another workbook", ref)
            if hidden:
                ix.warn("HIDDEN_CONTENT", "non-empty cell in a hidden sheet, row or column", ref)
        if len(cells) >= MAX_CELLS:
            break

    ix.out["workbook"] = {"sheets": sheets, "recalc_on_load": recalc, "external_link_count": ext_links}
    ix.out["cells"] = cells
    if not cells:
        ix.warn("EMPTY_DOCUMENT", "no non-empty cells")


# ------------------------------------------------------------------ PDF
def index_pdf(ix, data):
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except PDFPasswordIncorrect:
        raise DocError("PDF_ENCRYPTED", "password-protected PDF")
    except Exception as e:  # malformed PDFs raise many different types
        raise DocError("PDF_UNREADABLE", type(e).__name__)
    lines = []
    with pdf:
        total_pages = len(pdf.pages)
        if total_pages > MAX_PAGES:
            ix.out["truncated"] = True
            ix.warn("PAGE_LIMIT", f"indexed first {MAX_PAGES} of {total_pages} pages")
        for pno, page in enumerate(pdf.pages[:MAX_PAGES], start=1):
            try:
                page_lines = page.extract_text_lines(strip=True, return_chars=False)
            except Exception as e:
                ix.warn("PAGE_UNREADABLE", type(e).__name__, f"p{pno}")
                continue
            for lno, ln in enumerate(page_lines, start=1):
                ref = f"p{pno}:L{lno}"
                lines.append({
                    "ref": ref, "page": pno, "line": lno,
                    "text": ix.text(ln["text"], ref),
                    "bbox": [f"{ln['x0']:.2f}", f"{ln['top']:.2f}", f"{ln['x1']:.2f}", f"{ln['bottom']:.2f}"],
                })
    ix.out["pdf"] = {"pages": total_pages, "pages_indexed": min(total_pages, MAX_PAGES)}
    ix.out["lines"] = lines
    if not lines:
        ix.warn("NO_TEXT_LAYER", "no extractable text (scanned PDFs need OCR, which is not supported)")


# ------------------------------------------------------------------ main
def build_index(data, declared):
    ix = Index(data, declared)
    try:
        if len(data) > MAX_FILE_BYTES:
            raise DocError("FILE_TOO_LARGE", f"{len(data)} bytes")
        if declared not in ("csv", "xlsx", "pdf"):
            raise DocError("UNSUPPORTED_TYPE", declared)
        kind = detect_type(data)
        detected = {"pdf": "pdf", "zip": "xlsx", "text": "csv"}.get(kind, kind)
        ix.out["detected_type"] = detected
        if detected != declared:
            raise DocError("TYPE_MISMATCH", f"declared {declared}, content looks like {detected}")
        {"csv": index_csv, "xlsx": index_xlsx, "pdf": index_pdf}[declared](ix, data)
        ix.out["ok"] = True
    except DocError as e:
        ix.out["error"] = {"code": e.code, "detail": e.detail}
    except MemoryError:
        ix.out["error"] = {"code": "INDEXER_OUT_OF_MEMORY", "detail": ""}
    except Exception as e:  # never leak document content in the message
        ix.out["error"] = {"code": "INDEXER_EXCEPTION", "detail": type(e).__name__}
    if not ix.out["ok"]:
        ix.out.pop("cells", None)
        ix.out.pop("lines", None)
    return ix.out


def main(argv):
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    doc_path = argv[1] if len(argv) > 1 and argv[1] != "index" else "/input/document"
    meta_path = argv[2] if len(argv) > 2 else "/input/meta.json"
    with open(meta_path, encoding="utf-8") as f:
        declared = json.load(f).get("declared_type")
    with open(doc_path, "rb") as f:
        data = f.read(MAX_FILE_BYTES + 1)
    sys.stdout.write(json.dumps(build_index(data, declared), ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main(sys.argv)
