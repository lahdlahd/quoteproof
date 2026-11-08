"""Re-check simple XLSX formulas against their cached results (clarification 4).

Supported, and nothing else: + - * /, parentheses, numeric constants, cell
references on the same sheet (A1, $B$7) and SUM(range) over the same sheet.
The formula is parsed by a strict tokenizer + recursive-descent parser and
evaluated with Decimal. It is never passed to eval().

Result per formula cell:
  CHECKED_OK         recomputed value equals the cached value (to the cent)
  POSSIBLY_STALE     recomputed value differs from the cached value
  FORMULA_NOT_CHECKED outside the simple set, or an input is missing/non-numeric
"""

import re
from decimal import Decimal, DivisionByZero, InvalidOperation

from . import money

_TOKEN = re.compile(r"\s*(?:(\$?[A-Z]{1,3}\$?\d{1,7}(?::\$?[A-Z]{1,3}\$?\d{1,7})?)|(\d+(?:\.\d+)?)|(SUM)\b|([-+*/(),]))",
                    re.IGNORECASE)


class NotSimple(Exception):
    pass


def _col_num(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _split_ref(ref):
    m = re.fullmatch(r"\$?([A-Z]{1,3})\$?(\d{1,7})", ref, re.IGNORECASE)
    return _col_num(m.group(1)), int(m.group(2))


def tokenize(formula):
    if not formula.startswith("="):
        raise NotSimple("not a formula")
    s, pos, out = formula[1:], 0, []
    while pos < len(s):
        if s[pos:].strip() == "":
            break
        m = _TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise NotSimple(f"unsupported text at {pos}")
        ref, num, fn, op = m.groups()
        if ref:
            out.append(("range" if ":" in ref else "ref", ref.upper()))
        elif num:
            out.append(("num", Decimal(num)))
        elif fn:
            out.append(("fn", "SUM"))
        else:
            out.append(("op", op))
        pos = m.end()
    if "!" in formula or "[" in formula:
        raise NotSimple("cross-sheet or external reference")
    return out


class _Parser:
    def __init__(self, toks, lookup):
        self.t, self.i, self.lookup = toks, 0, lookup

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, kind=None, val=None):
        tok = self.peek()
        if tok[0] is None or (kind and tok[0] != kind) or (val and tok[1] != val):
            raise NotSimple("unexpected token")
        self.i += 1
        return tok

    def expr(self):
        v = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            r = self.term()
            v = v + r if op == "+" else v - r
        return v

    def term(self):
        v = self.unary()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            r = self.unary()
            if op == "*":
                v = v * r
            else:
                try:
                    v = v / r
                except (DivisionByZero, InvalidOperation):
                    raise NotSimple("division by zero")
        return v

    def unary(self):
        if self.peek() == ("op", "-"):
            self.take()
            return -self.unary()
        if self.peek() == ("op", "+"):
            self.take()
            return self.unary()
        return self.atom()

    def atom(self):
        kind, val = self.peek()
        if kind == "num":
            self.take()
            return val
        if kind == "ref":
            self.take()
            return self.lookup(*_split_ref(val))
        if kind == "fn":
            self.take()
            self.take("op", "(")
            total = Decimal(0)
            while True:
                k, v = self.peek()
                if k == "range":
                    self.take()
                    a, b = v.split(":")
                    (c1, r1), (c2, r2) = _split_ref(a), _split_ref(b)
                    if (r2 - r1 + 1) * (c2 - c1 + 1) > 10_000:
                        raise NotSimple("range too large")
                    for r in range(min(r1, r2), max(r1, r2) + 1):
                        for c in range(min(c1, c2), max(c1, c2) + 1):
                            total += self.lookup(c, r, blank_is_zero=True)
                else:
                    total += self.expr()
                if self.peek() == ("op", ","):
                    self.take()
                    continue
                self.take("op", ")")
                return total
        if (kind, val) == ("op", "("):
            self.take()
            v = self.expr()
            self.take("op", ")")
            return v
        raise NotSimple("unexpected token")


def evaluate(formula, lookup):
    toks = tokenize(formula)
    p = _Parser(toks, lookup)
    v = p.expr()
    if p.i != len(toks):
        raise NotSimple("trailing tokens")
    return v


def check_workbook(cells):
    """cells: XLSX cells from a validated Source Index. Returns {ref: status}."""
    by_pos = {(c["sheet"], c["col"], c["row"]): c for c in cells}
    results = {}
    for c in cells:
        if not c.get("formula"):
            continue
        sheet = c["sheet"]

        def lookup(col, row, blank_is_zero=False, _sheet=sheet):
            ref_cell = by_pos.get((_sheet, col, row))
            if ref_cell is None or ref_cell.get("value") in (None, ""):
                if blank_is_zero:
                    return Decimal(0)
                raise NotSimple("referenced cell is empty")
            if ref_cell.get("value_type") not in ("n", None):
                if blank_is_zero and ref_cell.get("value_type") == "s":
                    return Decimal(0)  # SUM ignores text, as spreadsheets do
                raise NotSimple("referenced cell is not a number")
            d = money.parse_xlsx_number(ref_cell["value"])
            if d is None:
                raise NotSimple("referenced cell is not a number")
            return d

        if c.get("value") is None or c.get("value_type") != "n":
            continue  # no cached number: handled by the indexer's own flags
        try:
            recomputed = evaluate(c["formula"], lookup)
        except NotSimple:
            results[c["ref"]] = {"status": "FORMULA_NOT_CHECKED"}
            continue
        cached = money.parse_xlsx_number(c["value"])
        if cached is not None and money.q2(recomputed) == money.q2(cached):
            results[c["ref"]] = {"status": "CHECKED_OK"}
        else:
            results[c["ref"]] = {"status": "POSSIBLY_STALE", "recomputed": str(money.q2(recomputed)),
                                 "cached": c["value"]}
    return results
