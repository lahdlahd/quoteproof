# Source Index format: `qp.source_index.v1`

The trusted indexer (`images/indexer/indexer.py`) turns one uploaded document into this JSON, inside the sandbox.

- It records **what is in the file and where**, never what anything means.
- All values are **strings or null**. No floats are emitted, so money is never rounded by JSON.

## Top level

| Field | Meaning |
|---|---|
| `schema` | Always `qp.source_index.v1` |
| `source_sha256`, `source_bytes` | Hash and size of the exact bytes the indexer read |
| `declared_type` / `detected_type` | `csv` / `xlsx` / `pdf`. A mismatch is an error |
| `ok`, `error` | `ok=false` → `error.code` explains why, and no cells or lines are included |
| `truncated` | True if any limit was hit (also listed in `warnings`) |
| `warnings[]` | `{code, detail, ref}`. Each one becomes a review item in Milestone 2 |
| `cells[]` | For CSV and XLSX |
| `lines[]` | For PDF |
| `csv` / `workbook` / `pdf` | Per-format metadata |

## Cells

- **CSV:** `{ref:"R4C3", row, col, value}`. The value is the exact text from the file (e.g. `"₦85,000.00"`). A value starting with `=` is plain text and is never evaluated.
- **XLSX:** `{ref:"Quote!D4", sheet, row, col, formula, value, value_type, number_format, hidden}`
  - `formula`: the formula text, e.g. `"=B4*C4"`, or null for a constant.
  - `value`: the **cached result** saved in the file, which is what the supplier saw, or the constant. Null if a formula has no cached result.
  - `value_type`: `n` number, `s` text, `b` boolean, `d` date, `e` error (e.g. `#DIV/0!`).

## Lines (PDF)

`{ref:"p1:L6", page, line, text, bbox:[x0,top,x1,bottom]}`. Coordinates are strings with 2 decimal places.

## Warning codes produced by the indexer

| Code | Meaning |
|---|---|
| `FORMULA_NO_CACHED_VALUE` | Formula with no saved result |
| `FORMULA_ERROR` | Saved result is an error value |
| `EXTERNAL_REFERENCE` | Formula points to another workbook |
| `EXTERNAL_LINKS` | Workbook has external links |
| `RECALC_ON_LOAD` | Workbook asks to recalculate on open, so saved values may be stale |
| `HIDDEN_CONTENT` | Non-empty cell in a hidden sheet, row or column |
| `NO_TEXT_LAYER` | PDF with no text (scanned); OCR is not supported |
| `ENCODING_FALLBACK` | CSV wasn't UTF-8 and was read as Windows-1252 |
| `TEXT_TRUNCATED`, `CELL_LIMIT`, `PAGE_LIMIT`, `WARNINGS_TRUNCATED` | A limit was hit |
| `EMPTY_DOCUMENT`, `PAGE_UNREADABLE` | Nothing usable on the document or a page |

`POSSIBLY_STALE` and `FORMULA_NOT_CHECKED` are produced in Milestone 2 by **trusted code in qp-worker**, which re-checks simple formulas. They are not produced by the indexer.

## Error codes (`ok=false`)

`TYPE_MISMATCH`, `MACRO_CONTENT`, `XLSX_UNREADABLE`, `XLSX_TOO_LARGE_UNCOMPRESSED`, `XLSX_TOO_COMPLEX`, `PDF_ENCRYPTED`, `PDF_UNREADABLE`, `FILE_TOO_LARGE`, `UNSUPPORTED_TYPE`, `INDEXER_OUT_OF_MEMORY`, `INDEXER_EXCEPTION`. Error details never include document content.

## Limits

| Limit | Value |
|---|---|
| File size | 10 MB |
| Cells | 50,000 |
| PDF pages | 50 |
| Text per cell or line | 2,000 characters |
| XLSX uncompressed size | 60 MB |
| XLSX zip members | 5,000 |
