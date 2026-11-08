"""Trusted money parsing. Decimal only; floats are never accepted.

Rules (approved architecture, "Currency rule" + Layer 1):
  * NGN and USD only, 2 decimal places, comma = thousands, dot = decimal.
  * Currency markers: ₦, NGN, $, USD, US$. An "N" directly before an amount is
    only a POSSIBLE NGN marker; a bare "$" is only a POSSIBLE USD marker.
  * Ambiguous formats such as 1.250,00 or 1,25 are never guessed.
"""

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
CURRENCIES = ("NGN", "USD")

# Ordered: longest markers first.
_MARKERS = [
    ("US$", "USD", "certain"), ("USD", "USD", "certain"), ("NGN", "NGN", "certain"),
    ("₦", "NGN", "certain"), ("$", "USD", "possible"),
]
_N_PREFIX = re.compile(r"^N(?=\s?\d)")
_STANDARD = re.compile(r"^\d{1,3}(,\d{3})+(\.\d{1,2})?$|^\d+(\.\d{1,2})?$")
_COMMA_DECIMAL = re.compile(r"^\d{1,3}(\.\d{3})+,\d{1,2}$|^\d+,\d{1,2}$")
_DOT_THREE = re.compile(r"^\d{1,3}\.\d{3}$")  # 1.250 : thousands or decimal? never guess


@dataclass
class Amount:
    raw: str
    status: str                       # ok | ambiguous | not_money
    value: Decimal | None = None      # set only when status == ok
    currency: str | None = None       # NGN / USD when a marker was present
    certainty: str | None = None      # certain | possible
    is_percent: bool = False
    notes: list = field(default_factory=list)

    @property
    def ok(self):
        return self.status == "ok"


def q2(d: Decimal) -> Decimal:
    """Round to 2 decimal places with the fixed rounding mode."""
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_amount(text) -> Amount:
    """Parse one cell value or one token. Never raises; never returns a float."""
    if isinstance(text, float):
        raise TypeError("floats are not accepted for money")
    if text is None:
        return Amount(raw="", status="not_money")
    raw = str(text)
    s = raw.strip().replace(" ", " ")
    if not s:
        return Amount(raw=raw, status="not_money")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.startswith("-"):
        negative, s = True, s[1:].strip()

    currency = certainty = None
    up = s.upper()
    for marker, cur, cert in _MARKERS:
        if up.startswith(marker):                       # "₦85,000.00", "USD 1,200.00"
            currency, certainty, s = cur, cert, s[len(marker):].strip()
            break
        if up.endswith(marker):                         # "1,200.00 USD", "85,000.00₦"
            currency, certainty, s = cur, cert, s[: -len(marker)].strip()
            break
    if currency is None and _N_PREFIX.match(s):
        currency, certainty, s = "NGN", "possible", s[1:].strip()
    if s.startswith("-") and not negative:
        negative, s = True, s[1:].strip()

    is_percent = s.endswith("%")
    if is_percent:
        s = s[:-1].strip()

    if _STANDARD.match(s):
        try:
            v = Decimal(s.replace(",", ""))
        except InvalidOperation:
            return Amount(raw=raw, status="not_money")
        if negative:
            v = -v
        a = Amount(raw=raw, status="ok", value=v, currency=currency, certainty=certainty, is_percent=is_percent)
        if currency and certainty == "possible":
            a.notes.append(f"possible_{currency}")
        return a
    if _COMMA_DECIMAL.match(s) or _DOT_THREE.match(s):
        return Amount(raw=raw, status="ambiguous", currency=currency, certainty=certainty,
                      is_percent=is_percent, notes=["ambiguous_number_format"])
    return Amount(raw=raw, status="not_money")


def parse_xlsx_number(value: str):
    """XLSX cached numbers come from the indexer as plain decimal strings
    (e.g. "3598" or "0.30000000000000004"). Float noise beyond 1e-9 of the
    2-dp value is removed; anything else is returned exactly."""
    try:
        d = Decimal(value)
    except (InvalidOperation, TypeError):
        return None
    r = q2(d)
    if d != r and abs(d - r) < Decimal("1e-9"):
        return r
    return d


def strict_decimal(value_str: str) -> Decimal:
    """For extractor output: value_str must be a plain decimal string after
    removing currency markers and thousands separators. Raises ValueError."""
    a = parse_amount(value_str)
    if not a.ok or a.is_percent:
        raise ValueError(f"not a plain amount: {value_str!r}")
    return a.value


# Tokens inside free text (PDF lines). A token must not touch letters, digits,
# hyphens or slashes, so "BB-0091", "50kg" and "2026-11-07" are not tokens.
_TOKEN = re.compile(
    r"(?<![\w\-/.,])"
    r"(\(?-?(?:US\$|\$|₦|NGN\s?|USD\s?|N(?=\d))?\d[\d,]*(?:\.\d+)?%?\)?)"
    r"(?![\w\-/])"
)


def tokens(text: str):
    """All number-like tokens in a line of text, each parsed."""
    out = []
    for m in _TOKEN.finditer(text or ""):
        tok = m.group(1).rstrip(".,")
        out.append(parse_amount(tok))
    return out


def is_money_like(a: Amount) -> bool:
    """Used by the Layer 1 coverage check: decides which index values are
    amounts the extractor should have accounted for."""
    if a.is_percent:
        return False
    if a.status == "ambiguous":
        return True
    if not a.ok:
        return False
    if a.currency:
        return True
    raw = a.raw.strip()
    return bool(re.search(r"\.\d{2}\b", raw) or re.search(r"\d,\d{3}", raw))
