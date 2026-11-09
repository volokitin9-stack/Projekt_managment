"""Text, key and date normalisation helpers."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Optional

# Cyrillic letters that look like Latin ones.  Used only for *matching keys*,
# never for displayed values, so "НАVAL" typed with a Cyrillic Н still matches.
_HOMOGLYPHS = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ё": "e",
})


def clean_text(value: Any) -> Optional[str]:
    """Trim and collapse whitespace; return None for blanks/placeholders."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = str(value).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if s in ("", "-", "—", "–", "n/a", "N/A", "#N/A", "None", "null"):
        return None
    return s


def norm_header(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    s = re.sub(r"[\s_]+", " ", s)
    return s.strip(" .:;,")


def norm_key(value: Any) -> str:
    """Aggressive normalisation used for identity comparisons."""
    s = clean_text(value) or ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.translate(_HOMOGLYPHS)
    s = re.sub(r"[\s\-_./\\,;:()\"']+", "", s)
    return s


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"), ("y", "m", "d")),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$"), ("d", "m", "y")),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2})$"), ("d", "m", "yy")),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), ("d", "m", "y")),
    (re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$"), ("y", "m", "d")),
]

EXCEL_EPOCH = date(1899, 12, 30)


class DateParseError(ValueError):
    pass


def parse_date(value: Any, allow_serial: bool = False) -> Optional[str]:
    """Return an ISO date string, None for blanks, raise DateParseError otherwise.

    Day-first is assumed for ambiguous ``01/02/2026`` strings (European/Russian
    convention); ISO strings are always unambiguous.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if allow_serial and 20000 < float(value) < 80000:
            return (EXCEL_EPOCH + timedelta(days=int(value))).isoformat()
        raise DateParseError(f"number {value!r} is not a date")
    s = clean_text(value)
    if s is None:
        return None
    for rx, order in _DATE_PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        parts = dict(zip(order, m.groups()))
        y = int(parts["y"]) if "y" in parts else 2000 + int(parts["yy"])
        try:
            return date(y, int(parts["m"]), int(parts["d"])).isoformat()
        except ValueError as exc:
            raise DateParseError(f"invalid date {s!r}: {exc}") from exc
    if allow_serial and re.fullmatch(r"\d{5}", s):
        return parse_date(int(s), allow_serial=True)
    raise DateParseError(f"unrecognised date {s!r}")


def looks_like_date(value: Any) -> bool:
    try:
        return parse_date(value) is not None
    except DateParseError:
        return False
