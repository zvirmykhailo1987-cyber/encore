"""Legacy export columns to contact fields, and validation of one row.

A column missing from COLUMN_MAP is reported as unmapped. It is never guessed into a field.
"""

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass

from .source import has_bad_encoding

COLUMN_MAP = {
    "CUST_ID": "legacy_id",
    "EMAIL_ADDR": "email",
    "MOBILE": "phone",
    "FNAME": "first_name",
    "LNAME": "last_name",
    "TOWN": "city",
    "OPT_IN": "marketing_opt_in",
    "CREATED": "legacy_created_at",
}
REQUIRED_COLUMNS = ("CUST_ID",)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_PHONE_NOISE = re.compile(r"[\s\-().]")
_PHONE = re.compile(r"^\+?\d{7,15}$")
_TRUE = {"y", "yes", "1", "true"}
_FALSE = {"n", "no", "0", "false"}


class RowDefect(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail[:300]


@dataclass(slots=True)
class CleanRow:
    row_number: int
    src_uid: str
    row_hash: str
    fields: dict[str, object]


def _text(value: str, limit: int) -> str | None:
    value = value.strip()
    if not value:
        return None
    if len(value) > limit:
        raise RowDefect("too_long", f"{len(value)} characters, limit {limit}")
    return value


def _email(value: str) -> str | None:
    value = value.strip().lower()
    if not value:
        return None
    if len(value) > 254 or not _EMAIL.match(value):
        raise RowDefect("invalid_email", value)
    return value


def _phone(value: str) -> str | None:
    value = _PHONE_NOISE.sub("", value.strip())
    if not value:
        return None
    if not _PHONE.match(value):
        raise RowDefect("invalid_phone", value)
    return value


def _opt_in(value: str) -> bool | None:
    value = value.strip().lower()
    if not value:
        return None
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise RowDefect("invalid_opt_in", value)


def _date(value: str) -> dt.date | None:
    value = value.strip()
    if not value:
        return None
    if len(value) == 10 and value[4] == "-":
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            raise RowDefect("bad_date", value) from None
    for layout in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(value, layout).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise RowDefect("bad_date", value)


def clean_row(row_number: int, values: dict[str, str], system: str) -> CleanRow:
    for value in values.values():
        if has_bad_encoding(value):
            raise RowDefect("bad_encoding")
    legacy_id = values.get("CUST_ID", "").strip()
    if not legacy_id:
        raise RowDefect("missing_id")
    if len(legacy_id) > 100:
        raise RowDefect("too_long", "CUST_ID")
    fields: dict[str, object] = {
        "email": _email(values.get("EMAIL_ADDR", "")),
        "phone": _phone(values.get("MOBILE", "")),
        "first_name": _text(values.get("FNAME", ""), 120),
        "last_name": _text(values.get("LNAME", ""), 120),
        "city": _text(values.get("TOWN", ""), 120),
        "marketing_opt_in": _opt_in(values.get("OPT_IN", "")),
        "legacy_created_at": _date(values.get("CREATED", "")),
    }
    if fields["email"] is None and fields["phone"] is None:
        raise RowDefect("no_contact_channel", "Neither email nor phone")
    canonical = json.dumps(fields, sort_keys=True, default=str, ensure_ascii=False)
    return CleanRow(
        row_number=row_number,
        src_uid=f"{system}:{legacy_id}",
        row_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        fields=fields,
    )
