"""Streaming source reader. Memory stays flat and every record reports the byte offset where it ends.

csv.reader pulls lines only as it needs them, so the byte count after each record is an exact restart
point, including for quoted fields that span several lines.
"""

import csv
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

csv.field_size_limit(1_000_000)


class SourceError(Exception):
    """The file cannot be read safely from here on. The run stops and keeps its checkpoint."""


@dataclass(slots=True)
class Record:
    row_number: int
    values: dict[str, str] | None
    end_offset: int
    defect: str | None = None
    raw: dict | None = None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def clean_for_storage(value: str) -> str:
    """Make a damaged value storable and visible. PostgreSQL rejects NUL and lone surrogates."""
    return value.replace("\x00", "\\x00").encode("utf-8", "backslashreplace").decode("utf-8")


def has_bad_encoding(value: str) -> bool:
    return "\x00" in value or any("\udc80" <= ch <= "\udcff" for ch in value)


class _LineFeed:
    def __init__(self, handle) -> None:
        self.handle = handle
        self.offset = handle.tell()

    def __iter__(self) -> Iterator[str]:
        for line in self.handle:
            self.offset += len(line)
            yield line.decode("utf-8", "surrogateescape")


def read_header(path: Path) -> tuple[list[str], int]:
    with path.open("rb") as handle:
        feed = _LineFeed(handle)
        row = next(csv.reader(feed), None)
        if not row:
            raise SourceError("The source file is empty")
        row[0] = row[0].removeprefix("﻿")
        return [column.strip() for column in row], feed.offset


def iter_csv(path: Path, header: list[str], offset: int, row_number: int) -> Iterator[Record]:
    with path.open("rb") as handle:
        handle.seek(offset)
        feed = _LineFeed(handle)
        reader = csv.reader(feed)
        while True:
            try:
                fields = next(reader)
            except StopIteration:
                return
            except csv.Error as exc:
                raise SourceError(f"CSV structure is broken after row {row_number}: {exc}") from exc
            if not fields:
                continue
            row_number += 1
            if len(fields) != len(header):
                raw = {"fields": [clean_for_storage(field) for field in fields]}
                yield Record(row_number, None, feed.offset, "column_count", raw)
                continue
            yield Record(row_number, dict(zip(header, fields, strict=True)), feed.offset)


def iter_jsonl(path: Path, offset: int, row_number: int) -> Iterator[Record]:
    with path.open("rb") as handle:
        handle.seek(offset)
        position = offset
        for line in handle:
            position += len(line)
            if not line.strip():
                continue
            row_number += 1
            try:
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeDecodeError):
                text = clean_for_storage(line.decode("utf-8", "surrogateescape"))[:2000]
                yield Record(row_number, None, position, "bad_json", {"line": text})
                continue
            values = {str(key): "" if value is None else str(value) for key, value in data.items()}
            yield Record(row_number, values, position)
