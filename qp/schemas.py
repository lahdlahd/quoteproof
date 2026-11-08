"""Strict, dependency-free validation for every JSON that crosses a trust boundary.

Rules everywhere: no unknown keys, no floats, bounded strings and lists.
A spec is one of:
  "str" / "str?"            string (or null with ?), max 2,000 chars unless (str, n)
  ("str", n) / ("str?", n)  string with a custom max length
  "int" / "bool"            exact type (bool is not accepted as int)
  ("enum", [...])           one of the listed strings
  ("enum?", [...])          same, or null
  ("list", spec, max_len)   list of items
  {key: spec, "key?": spec} object with exactly these keys; "key?" = optional key
  ("nullable", spec)        spec or null
"""

from . import money


class SchemaError(ValueError):
    def __init__(self, path, msg):
        super().__init__(f"{path or '$'}: {msg}")
        self.path, self.msg = path, msg


def validate(value, spec, path="$"):
    if isinstance(value, float):
        raise SchemaError(path, "floats are not allowed (send numbers as strings)")
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise SchemaError(path, "expected object")
        required = {k for k in spec if not k.endswith("?")}
        optional = {k[:-1] for k in spec if k.endswith("?")}
        extra = set(value) - required - optional
        if extra:
            raise SchemaError(path, f"unexpected keys {sorted(extra)}")
        missing = required - set(value)
        if missing:
            raise SchemaError(path, f"missing keys {sorted(missing)}")
        for k, v in value.items():
            validate(v, spec[k] if k in spec else spec[k + "?"], f"{path}.{k}")
        return value
    if isinstance(spec, str):
        spec = (spec, 2000) if spec.startswith("str") else (spec,)
    kind = spec[0]
    if kind in ("str", "str?"):
        if value is None and kind == "str?":
            return value
        if not isinstance(value, str):
            raise SchemaError(path, "expected string")
        if len(value) > spec[1]:
            raise SchemaError(path, f"string longer than {spec[1]}")
        return value
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise SchemaError(path, "expected integer")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise SchemaError(path, "expected boolean")
        return value
    if kind in ("enum", "enum?"):
        if value is None and kind == "enum?":
            return value
        if value not in spec[1]:
            raise SchemaError(path, f"must be one of {spec[1]}")
        return value
    if kind == "list":
        if not isinstance(value, list):
            raise SchemaError(path, "expected list")
        if len(value) > spec[2]:
            raise SchemaError(path, f"more than {spec[2]} items")
        for i, item in enumerate(value):
            validate(item, spec[1], f"{path}[{i}]")
        return value
    if kind == "nullable":
        return value if value is None else validate(value, spec[1], path)
    raise AssertionError(f"bad spec {spec!r}")


# ------------------------------------------------------------------ Source Index (from the indexer)
_WARNING = {"code": ("str", 64), "detail": ("str?", 300), "ref": ("str?", 64)}
_CELL_CSV = {"ref": ("str", 64), "row": "int", "col": "int", "value": "str?"}
_CELL_XLSX = {"ref": ("str", 64), "sheet": ("str", 64), "row": "int", "col": "int", "formula": "str?",
              "value": "str?", "value_type": ("str?", 8), "number_format": ("str?", 128), "hidden": "bool"}
_LINE = {"ref": ("str", 64), "page": "int", "line": "int", "text": "str",
         "bbox": ("list", ("str", 16), 4)}
_ERROR = ("nullable", {"code": ("str", 64), "detail?": ("str?", 300)})
_INDEX_BASE = {
    "schema": ("enum", ["qp.source_index.v1"]),
    "source_sha256": ("str", 64), "source_bytes": "int",
    "declared_type": ("enum", ["csv", "xlsx", "pdf"]), "detected_type": ("enum?", ["csv", "xlsx", "pdf", "binary"]),  # indexer.build_index mapping
    "ok": "bool", "error": _ERROR, "truncated": "bool",
    "warnings": ("list", _WARNING, 2000),
}
MAX_CELLS, MAX_LINES = 50_000, 50 * 400


def validate_index(idx):
    """Strictly validate an index produced by our own indexer. Chooses the cell
    shape from declared_type; anything unexpected is rejected."""
    if not isinstance(idx, dict):
        raise SchemaError("$", "expected object")
    spec = dict(_INDEX_BASE)
    t = idx.get("declared_type")
    if t == "csv":
        spec["csv?"] = ("nullable", {"delimiter": ("str", 4), "encoding": ("str", 32), "rows": "int"})
        spec["cells?"] = ("list", _CELL_CSV, MAX_CELLS)
    elif t == "xlsx":
        spec["workbook?"] = ("nullable", {
            "sheets": ("list", {"name": ("str", 64), "state": ("str", 16), "max_row": "int", "max_col": "int"}, 256),
            "recalc_on_load": "bool", "external_link_count": "int"})
        spec["cells?"] = ("list", _CELL_XLSX, MAX_CELLS)
    elif t == "pdf":
        spec["pdf?"] = ("nullable", {"pages": "int", "pages_indexed": "int"})
        spec["lines?"] = ("list", _LINE, MAX_LINES)
    return validate(idx, spec)


# ------------------------------------------------------------------ Extractor output (AI code)
EXTRACTION_SCHEMA_ID = "qp.extraction.v1"
FIELD = {"value_str": ("str", 200), "cite": ("str?", 64)}
CHARGE_KINDS = ["delivery", "installation", "tax", "discount", "other"]
EXTRACTION_SPEC = {
    "schema": ("enum", [EXTRACTION_SCHEMA_ID]),
    "supplier": ("nullable", FIELD),
    "currency": ("enum", ["NGN", "USD", "UNKNOWN"]),
    "line_items": ("list", {
        "id": ("str", 16),
        "description": FIELD,
        "qty": ("nullable", FIELD),
        "unit_price": ("nullable", FIELD),
        "line_total": ("nullable", FIELD),
    }, 500),
    "charges": ("list", {
        "id": ("str", 16),
        "kind": ("enum", CHARGE_KINDS),
        "description": FIELD,
        "amount": ("nullable", FIELD),
        "rate?": ("nullable", FIELD),
    }, 100),
    "stated": {"subtotal": ("nullable", FIELD), "tax": ("nullable", FIELD), "grand_total": ("nullable", FIELD)},
    "ai_arithmetic?": {"subtotal": ("str?", 40), "grand_total": ("str?", 40)},
}


def validate_extraction(obj):
    """Schema check plus content checks the generic validator cannot express:
    unique ids, and every numeric field must hold a plain amount string."""
    validate(obj, EXTRACTION_SPEC)
    ids = [x["id"] for x in obj["line_items"]] + [c["id"] for c in obj["charges"]]
    if len(ids) != len(set(ids)):
        raise SchemaError("$", "line item / charge ids must be unique")

    def amount(field, path, percent_ok=False):
        if field is None:
            return
        a = money.parse_amount(field["value_str"])
        if a.status == "ambiguous":
            return  # allowed through: Layer 1 flags it for review, never guesses
        if not a.ok or (a.is_percent and not percent_ok):
            raise SchemaError(path, f"value_str is not a plain amount: {field['value_str']!r}")

    for i, li in enumerate(obj["line_items"]):
        for k in ("qty", "unit_price", "line_total"):
            amount(li[k], f"$.line_items[{i}].{k}")
    for i, c in enumerate(obj["charges"]):
        amount(c["amount"], f"$.charges[{i}].amount")
        amount(c.get("rate"), f"$.charges[{i}].rate", percent_ok=True)
    for k, v in obj["stated"].items():
        amount(v, f"$.stated.{k}")
    if obj["line_items"] == [] and obj["charges"] == []:
        raise SchemaError("$", "no line items or charges extracted")
    return obj
