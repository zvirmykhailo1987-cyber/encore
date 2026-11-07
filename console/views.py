"""Page shells for the operator console. Every number on screen is fetched from the JSON API by the browser."""

import json
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

DASH = "—"
REFRESH_PREFIX = "Refresh spike, "
DOORS_PREFIX = "Doors open, "
STOPPED_PREFIX = "Django stopped during a spike"
# The doors-open runs ramp up for this long before the peak. tickets_admitted counts the ramp too.
RAMP_SECONDS = 20
# Below this share of the requested rate, the screen says the gates no longer keep up.
KEEPS_UP = 0.99


def _context(nav: str, **extra: object) -> dict:
    return {
        "nav": nav,
        # Only whether a token is needed. The token itself never reaches a template.
        "needs_token": bool(settings.ENCORE_OPERATOR_TOKEN),
        "stale_seconds": settings.ENCORE_IMPORT_STALE_SECONDS,
        **extra,
    }


def overview(request: HttpRequest) -> HttpResponse:
    return render(request, "console/overview.html", _context("imports"))


def run_detail(request: HttpRequest, pk: int) -> HttpResponse:
    return render(request, "console/run.html", _context("imports", run_id=pk))


def quarantine(request: HttpRequest, pk: int) -> HttpResponse:
    return render(request, "console/quarantine.html", _context("imports", run_id=pk))


def integration(request: HttpRequest) -> HttpResponse:
    return render(request, "console/integration.html", _context("integration"))


def benchmarks_path() -> Path:
    return Path(settings.BASE_DIR) / "docs" / "benchmarks.json"


def load_benchmarks(path: Path) -> tuple[list[dict] | None, str | None]:
    """Rows from the results file, or None when nothing was measured. A broken file is an error, not zero."""
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"docs/benchmarks.json could not be read: {type(exc).__name__}"
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        return None, "docs/benchmarks.json is not a list of results"
    return data, None


def _number(value: object) -> float | None:
    """The file stores some measurements as text. A value that is not a number stays unknown."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _show(value: object, unit: str = "") -> str:
    number = _number(value)
    if number is None:
        return DASH
    text = f"{number:,.0f}" if number == int(number) else f"{number:,}"
    return f"{text}{unit}"


def _same(rows: list[dict], key: str) -> str:
    """One value when the rows agree, all of them when they do not. Nothing is averaged or dropped."""
    values = list(dict.fromkeys(str(row[key]) for row in rows if row.get(key) not in (None, "")))
    return "; ".join(values) or DASH


def _provenance(rows: list[dict]) -> dict:
    return {
        "machine": _same(rows, "machine"),
        "data": _same(rows, "data"),
        "measured": _same(rows, "measured"),
    }


def _import_row(row: dict) -> dict:
    seconds = _number(row.get("seconds_total"))
    return {
        "rows": _show(row.get("rows")),
        "minutes": f"{seconds / 60:,.1f} min" if seconds is not None else DASH,
        "seconds_total": _show(seconds, " s"),
        "rows_per_second": _show(row.get("rows_per_second")),
        "file_mb": _show(row.get("file_mb")),
        "worker_peak_mb": _show(row.get("worker_peak_mb")),
        "discrepancies": _show(row.get("discrepancies")),
        "quarantined": _show(row.get("quarantined")),
    }


def _percent(part: object, whole: object, digits: int = 1) -> str:
    top, bottom = _number(part), _number(whole)
    return f"{top / bottom * 100:.{digits}f}" if top is not None and bottom else DASH


def _admitted_per_second(row: dict) -> float | None:
    tickets, peak = _number(row.get("tickets_admitted")), _number(row.get("seconds_at_peak"))
    return tickets / (peak + RAMP_SECONDS) if tickets is not None and peak is not None else None


def _by_prefix(rows: list[dict], prefix: str) -> list[dict]:
    return [row for row in rows if str(row.get("test", "")).startswith(prefix)]


def _sort_value(row: dict, key: str) -> float:
    return _number(row.get(key)) or 0


def _fans_block(rows: list[dict]) -> dict:
    # Django directly goes first, so the comparison reads from no edge to the largest crowd behind it.
    ordered = sorted(rows, key=lambda r: (r.get("through") != "Django directly", _sort_value(r, "fans")))
    return {
        "columns": [{"fans": _show(r.get("fans")), "through": r.get("through") or DASH} for r in ordered],
        "table": [
            {
                "label": "Requests per second",
                "values": [_show(r.get("requests_per_second")) for r in ordered],
            },
            {"label": "p95, ms", "values": [_show(r.get("fan_p95_ms")) for r in ordered]},
            {"label": "p99, ms", "values": [_show(r.get("fan_p99_ms")) for r in ordered]},
            {"label": "Failed, %", "values": [_show(r.get("failed_percent")) for r in ordered]},
            {
                "label": "From cache, %",
                "note": "computed: served_from_cache / requests",
                "values": [_percent(r.get("served_from_cache"), r.get("requests")) for r in ordered],
            },
        ],
        "chart": [
            {
                "label": f"{_show(r.get('fans'))} {'no edge' if r.get('through') == 'Django directly' else 'edge'}",
                "p50": _number(r.get("fan_p50_ms")),
                "p95": _number(r.get("fan_p95_ms")),
                "p99": _number(r.get("fan_p99_ms")),
            }
            for r in ordered
        ],
    }


def _saturation_text(rows: list[dict]) -> str:
    """Say plainly where admitted stops following requested. It is a limit, neither good news nor hidden."""
    below = []
    for row in rows:
        requested, admitted = _number(row.get("scans_per_second_requested")), _admitted_per_second(row)
        if requested and admitted is not None and admitted < requested * KEEPS_UP:
            below.append(
                f"at {requested:,.0f} requested scans per second, about {admitted:,.0f} are admitted, "
                f"and gate p50 is {_show(row.get('gate_p50_ms'))} ms"
            )
    if not below:
        return "Admitted scans kept up with the requested rate in every run."
    return "Saturation: " + "; ".join(below) + "."


def _doors_block(rows: list[dict]) -> dict:
    ordered = sorted(rows, key=lambda r: _sort_value(r, "scans_per_second_requested"))
    equal = [
        _number(r.get("tickets_admitted")) is not None
        and _number(r.get("tickets_admitted")) == _number(r.get("outbox_messages"))
        for r in ordered
    ]
    return {
        "columns": [_show(r.get("scans_per_second_requested")) for r in ordered],
        "table": [
            {
                "label": "Admitted per second",
                "note": f"computed: tickets_admitted / (seconds_at_peak + {RAMP_SECONDS} s ramp)",
                "values": [
                    _show(round(a)) if (a := _admitted_per_second(r)) is not None else DASH for r in ordered
                ],
            },
            {"label": "Gate p50, ms", "values": [_show(r.get("gate_p50_ms")) for r in ordered]},
            {"label": "Gate p95, ms", "values": [_show(r.get("gate_p95_ms")) for r in ordered]},
            {"label": "Gate p99, ms", "values": [_show(r.get("gate_p99_ms")) for r in ordered]},
            {"label": "Gate failed, %", "values": [_show(r.get("gate_failed_percent")) for r in ordered]},
            {
                "label": "Unexpected responses",
                "values": [_show(r.get("unexpected_responses")) for r in ordered],
            },
            {"label": "Tickets admitted", "values": [_show(r.get("tickets_admitted")) for r in ordered]},
            {"label": "Outbox messages", "values": [_show(r.get("outbox_messages")) for r in ordered]},
            {
                "label": "Tickets = outbox",
                "values": ["equal" if same else "NOT equal" for same in equal],
            },
        ],
        "saturation": _saturation_text(ordered),
        "chart": [
            {
                "requested": _number(r.get("scans_per_second_requested")),
                "admitted": _admitted_per_second(r),
                "label": f"{_show(r.get('scans_per_second_requested'))}/s",
                "p50": _number(r.get("gate_p50_ms")),
                "p95": _number(r.get("gate_p95_ms")),
                "p99": _number(r.get("gate_p99_ms")),
            }
            for r in ordered
        ],
    }


def _stopped_block(row: dict) -> dict:
    return {
        "fans": _show(row.get("fans")),
        "failed": _show(row.get("failed_percent")),
        "requests": _show(row.get("requests")),
        "served_from_cache": _show(row.get("served_from_cache")),
        "from_cache": _percent(row.get("served_from_cache"), row.get("requests")),
        "unexpected": _show(row.get("unexpected_responses")),
        "p95": _show(row.get("fan_p95_ms")),
        "p99": _show(row.get("fan_p99_ms")),
    }


def shape_results(rows: list[dict]) -> dict:
    """Sort the file's flat list into the blocks of the page. Every value is read or computed from the file."""
    imports = sorted(
        (r for r in rows if _number(r.get("rows")) is not None), key=lambda r: _sort_value(r, "rows")
    )
    refresh, doors = _by_prefix(rows, REFRESH_PREFIX), _by_prefix(rows, DOORS_PREFIX)
    stopped = _by_prefix(rows, STOPPED_PREFIX)
    peak = [*refresh, *doors, *stopped]
    import_rows = [_import_row(row) for row in imports]
    fans = _fans_block(refresh) if refresh else None
    door_block = _doors_block(doors) if doors else None
    return {
        "imports": import_rows,
        "import_table": [
            {"label": label, "values": [row[key] for row in import_rows], "notes": notes}
            for label, key, notes in (
                ("Total time", "minutes", [row["seconds_total"] for row in import_rows]),
                ("Rows per second", "rows_per_second", None),
                ("File MB", "file_mb", None),
                ("Worker peak MB", "worker_peak_mb", None),
                ("Discrepancies", "discrepancies", None),
                ("Quarantined", "quarantined", None),
            )
        ],
        "import_includes": _same(imports, "includes"),
        "import_provenance": _provenance(imports),
        "fans": fans,
        "doors": door_block,
        "stopped": _stopped_block(stopped[0]) if stopped else None,
        "peak_provenance": _provenance(peak),
        "refresh_seconds": _same(peak, "seconds_between_refreshes"),
        "not_shown": len(rows) - len(imports) - len(peak),
        "chart": {
            "imports": [
                {"label": _show(row.get("rows")) + " rows", "value": _number(row.get("rows_per_second"))}
                for row in imports
            ],
            "fans": fans["chart"] if fans else [],
            "doors": door_block["chart"] if door_block else [],
        },
    }


def benchmarks(request: HttpRequest) -> HttpResponse:
    rows, error = load_benchmarks(benchmarks_path())
    return render(
        request,
        "console/benchmarks.html",
        _context("benchmarks", results=shape_results(rows) if rows else None, error=error),
    )
