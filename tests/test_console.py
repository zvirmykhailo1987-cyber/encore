import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from console import views
from imports.models import ImportRun

STATIC = Path(settings.BASE_DIR) / "static" / "console"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    ["/", "/runs/1", "/runs/1/quarantine", "/integration", "/load-results"],
)
def test_pages_answer_on_an_empty_database(client, url):
    assert ImportRun.objects.count() == 0
    response = client.get(url)
    assert response.status_code == 200
    assert b"<main" in response.content


@pytest.mark.django_db
def test_pages_carry_no_sample_data_and_no_external_host(client):
    for name in ("console-overview", "console-integration", "console-benchmarks"):
        html = client.get(reverse(name)).content.decode()
        assert "Sample data" not in html
        assert "Preview state" not in html
        assert not re.search(r"""(?:src|href)=["']https?://""", html)


@pytest.mark.django_db
def test_the_api_the_console_reads_answers_on_an_empty_database(client):
    assert client.get("/api/v1/imports/runs").json() == []
    health = client.get("/api/v1/connector/health").json()
    assert health["breaker"] == "closed"
    assert health["cursor_lag"] is None
    assert health["metrics"] == []


@pytest.mark.django_db
def test_a_run_with_zero_rows_has_no_progress_to_divide_by(client):
    run = ImportRun.objects.create(source_path="/app/data/empty.csv")
    assert client.get(f"/runs/{run.pk}").status_code == 200
    payload = client.get(f"/api/v1/imports/runs/{run.pk}").json()
    assert payload["rows_read"] == 0
    assert payload["progress"] is None
    assert payload["speed_rows_per_second"] is None


def test_static_files_are_served_without_a_collectstatic_step(client):
    for name in ("console.css", "core.js", "run.js", "fonts/geist.woff2"):
        response = client.get(f"/static/console/{name}")
        assert response.status_code == 200, name
        assert b"".join(response.streaming_content)


def test_static_view_cannot_leave_its_folder():
    client = Client(raise_request_exception=False)
    for url in ("/static/../manage.py", "/static/%2e%2e/manage.py", "/static/console/../../manage.py"):
        assert client.get(url).status_code in (400, 404)


def test_scripts_never_assign_html_from_a_string():
    """Quarantined rows are untrusted input. API data may enter the page only as text."""
    forbidden = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function")
    scripts = list(STATIC.glob("*.js"))
    assert scripts
    for script in scripts:
        assert not forbidden.search(script.read_text(encoding="utf-8")), script.name


def test_static_assets_reference_no_external_host():
    for path in [*STATIC.glob("*.js"), *STATIC.glob("*.css")]:
        text = path.read_text(encoding="utf-8").replace("http://www.w3.org/2000/svg", "")
        assert not re.search(r"https?://", text), path.name


@pytest.mark.django_db
def test_load_results_say_not_measured_when_there_is_no_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(views, "benchmarks_path", lambda: tmp_path / "benchmarks.json")
    html = client.get("/load-results").content.decode()
    assert "Not measured yet" in html


_PEAK = {
    "seconds_between_refreshes": 2,
    "seconds_at_peak": 60,
    "failed_percent": 0,
    "unexpected_responses": 0,
    "machine": "VM A; load generator in the same VM",
    "data": "synthetic",
    "measured": "2026-11-06",
}
IMPORT_ROW = {
    "test": "Import 1,000,000 rows",
    "rows": 1000000,
    "seconds_total": 268.5,
    "rows_per_second": 3724,
    "includes": "hashing, import, full reconciliation",
    "file_mb": 86.1,
    "worker_peak_mb": 168,
    "discrepancies": 0,
    "quarantined": 19761,
    "machine": "Host A",
    "data": "synthetic",
    "measured": "2026-11-06",
}
NO_EDGE = {
    **_PEAK,
    "test": "Refresh spike, 5,000 fans, no edge",
    "through": "Django directly",
    "fans": 5000,
    "requests": 1000,
    "requests_per_second": 1661,
    "served_from_cache": 0,
    "fan_p50_ms": 404.8,
    "fan_p95_ms": 1024.0,
    "fan_p99_ms": 1104.0,
}
EDGE = {
    **_PEAK,
    "test": "Refresh spike, 10,000 fans",
    "through": "edge cache",
    "fans": 10000,
    "requests": 1000,
    "requests_per_second": 4190,
    "served_from_cache": 989,
    "fan_p50_ms": 0.3,
    "fan_p95_ms": 0.8,
    "fan_p99_ms": 1.8,
}


def _doors(requested: int, tickets: int, outbox: int | None = None, **extra) -> dict:
    return {
        **_PEAK,
        "test": f"Doors open, 10,000 fans and {requested:,} scans per second",
        "through": "edge cache",
        "fans": 10000,
        "scans_per_second_requested": requested,
        "requests": 1000,
        "served_from_cache": 900,
        "tickets_admitted": tickets,
        "outbox_messages": tickets if outbox is None else outbox,
        "gate_p50_ms": 8.2,
        "gate_p95_ms": 70.4,
        "gate_p99_ms": 555.7,
        "gate_failed_percent": 0.002,
        **extra,
    }


STOPPED = {
    **_PEAK,
    "test": "Django stopped during a spike, 5,000 fans",
    "through": "edge cache",
    "fans": 5000,
    "requests": 183943,
    "served_from_cache": 183943,
    "fan_p50_ms": 0.4,
    "fan_p95_ms": 0.7,
    "fan_p99_ms": 1.0,
}
SAMPLE_RESULTS = [IMPORT_ROW, EDGE, NO_EDGE, _doors(300, 24000), _doors(1000, 67173), STOPPED]


def _results_page(client, monkeypatch, tmp_path, rows) -> str:
    target = tmp_path / "benchmarks.json"
    target.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(views, "benchmarks_path", lambda: target)
    return client.get("/load-results").content.decode()


@pytest.mark.django_db
def test_import_block_shows_the_numbers_from_the_file(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, SAMPLE_RESULTS)
    assert "Not measured yet" not in html
    for shown in ("1,000,000", "4.5 min", "268.5 s", "3,724", "86.1", "168", "19,761"):
        assert shown in html, shown


@pytest.mark.django_db
def test_fans_block_compares_no_edge_first_and_computes_the_cache_share(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, SAMPLE_RESULTS)
    assert html.index("Django directly") < html.index("edge cache")
    assert "computed: served_from_cache / requests" in html
    assert ">98.9<" in html
    assert ">0.0<" in html
    for shown in ("1,661", "4,190", "1,024", "1,104", "0.8", "1.8"):
        assert shown in html, shown


@pytest.mark.django_db
def test_doors_block_computes_admitted_rate_with_the_ramp(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, SAMPLE_RESULTS)
    assert "tickets_admitted / (seconds_at_peak + 20 s ramp)" in html
    assert ">300<" in html  # 24,000 / 80
    assert ">840<" in html  # 67,173 / 80
    for shown in ("8.2", "70.4", "555.7", "0.002", "67,173"):
        assert shown in html, shown


@pytest.mark.django_db
def test_saturation_is_stated_plainly(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, SAMPLE_RESULTS)
    assert "Saturation: at 1,000 requested scans per second, about 840 are admitted" in html


@pytest.mark.django_db
def test_no_saturation_is_not_claimed_when_the_rate_keeps_up(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, [_doors(300, 24000)])
    assert "Saturation:" not in html
    assert "kept up with the requested rate" in html


@pytest.mark.django_db
def test_unequal_outbox_is_not_shown_as_equal(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, [_doors(300, 24000, outbox=23999)])
    assert "NOT equal" in html


@pytest.mark.django_db
def test_stopped_block_says_scans_need_django_and_were_not_measured(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, SAMPLE_RESULTS)
    assert "183,943" in html
    assert "last known numbers" in html
    assert "do not work during this time, because they need Django" in html
    assert "ran no scans, so it is not a measurement" in html


@pytest.mark.django_db
def test_page_says_what_the_measurement_is(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, SAMPLE_RESULTS)
    for text in (
        "One run on a laptop with synthetic data",
        "not evidence for production",
        "k6 load generator runs in the same VM",
        "about once every 2 seconds",
        "about one second old",
        "bash scripts/run_benchmarks.sh --stack-only python scripts/spike_bench.py &lt;name&gt; edge 10000",
    ):
        assert text in html, text


@pytest.mark.django_db
def test_missing_blocks_and_values_stay_unknown(client, monkeypatch, tmp_path):
    row = {key: value for key, value in IMPORT_ROW.items() if key != "worker_peak_mb"}
    row["quarantined"] = "n/a"
    html = _results_page(client, monkeypatch, tmp_path, [row])
    assert html.count('<td class="r m nw">—</td>') == 2
    assert "No refresh results in the file" in html
    assert "No doors-open results in the file" in html
    assert "No result for this test in the file" in html


@pytest.mark.django_db
def test_zero_requests_do_not_divide_by_zero(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, [{**NO_EDGE, "requests": 0}])
    assert "From cache, %" in html


@pytest.mark.django_db
def test_file_values_are_escaped(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, [{**IMPORT_ROW, "machine": "<b>box</b>"}])
    assert "&lt;b&gt;box&lt;/b&gt;" in html
    assert "<b>box</b>" not in html


@pytest.mark.django_db
def test_results_the_page_has_no_block_for_are_declared(client, monkeypatch, tmp_path):
    html = _results_page(client, monkeypatch, tmp_path, [*SAMPLE_RESULTS, {"test": "Something else"}])
    assert "1 other result in the file not shown" in html


@pytest.mark.django_db
def test_the_committed_results_file_renders(client):
    html = client.get("/load-results").content.decode()
    if views.benchmarks_path().is_file():
        assert "could not be shown" not in html
        assert "Not measured yet" not in html
        assert "10,000,000" in html
        rows = json.loads(views.benchmarks_path().read_text(encoding="utf-8"))
        ten_million = next(r for r in rows if r.get("rows") == 10_000_000)
        assert f"{ten_million['seconds_total'] / 60:.1f} min" in html
        assert "Fans refreshing" in html
        assert "other result" not in html


@pytest.mark.django_db
def test_a_broken_results_file_is_an_error_not_an_empty_page(client, monkeypatch, tmp_path):
    target = tmp_path / "benchmarks.json"
    target.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(views, "benchmarks_path", lambda: target)
    html = client.get("/load-results").content.decode()
    assert "could not be shown" in html
    assert "Not measured yet" not in html


@pytest.mark.django_db
def test_operator_token_is_never_written_into_a_page(client, settings):
    marker = "-".join(["operator", "value", "for", "this", "test"])
    settings.ENCORE_OPERATOR_TOKEN = marker
    html = client.get("/").content.decode()
    assert marker not in html
    assert 'data-needs-token="1"' in html
