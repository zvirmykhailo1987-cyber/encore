# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Who may call what. Every API route is closed unless it is named here as public."""

import re
import uuid
from pathlib import Path

import pytest
from django.core.management import call_command
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.test import APIClient

from access.models import ApiKey, digest
from imports.models import ImportRun, QuarantineRow
from ticketing.models import EventOwner, ScanConflict, Ticket
from ticketing.services import apply_ticket_versions

pytestmark = pytest.mark.django_db

#: The only routes a caller without a key may use, and why.
PUBLIC = {
    "api/v1/events/<str:event>/availability",  # what fans refresh; no personal data
    "api/v1/webhooks/ticketing",  # authenticated by an HMAC signature instead of a key
}


def api_routes(patterns=None, prefix=""):
    for entry in patterns if patterns is not None else get_resolver().url_patterns:
        if isinstance(entry, URLResolver):
            yield from api_routes(entry.url_patterns, prefix + str(entry.pattern))
        elif isinstance(entry, URLPattern):
            route = prefix + str(entry.pattern)
            if route.startswith("api/"):
                yield route


def concrete(route: str) -> str:
    return "/" + re.sub(r"<(?:int:)?(\w+)>", "1", re.sub(r"<str:(\w+)>", "x", route))


def client(key: str | None = None) -> APIClient:
    api = APIClient()
    if key:
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
    return api


@pytest.fixture
def keys(settings):
    settings.DEBUG = False
    settings.ENCORE_OPERATOR_TOKEN = ""
    return {
        "platform": ApiKey.issue("ops", "platform")[1],
        "festival": ApiKey.issue("festival staff", "organizer", "festival-a")[1],
        "club": ApiKey.issue("club staff", "organizer", "club-b")[1],
        "gate": ApiKey.issue("north gate", "gate", "festival-a")[1],
        "club_gate": ApiKey.issue("club door", "gate", "club-b")[1],
    }


# ---------------------------------------------------------------- closed by default


def test_every_api_route_refuses_a_caller_without_a_key(keys):
    routes = sorted(set(api_routes()))
    assert len(routes) >= 14, routes
    for route in routes:
        if route in PUBLIC:
            continue
        for method in ("get", "post"):
            status = getattr(client(), method)(concrete(route), {}, format="json").status_code
            assert status in (401, 405), f"{method.upper()} {route} answered {status} without a key"


def test_unknown_and_revoked_keys_are_refused(keys):
    assert client("enc_00000000_not-a-key").get("/api/v1/imports/runs").status_code == 401
    key = ApiKey.objects.get(name="ops")
    assert client(keys["platform"]).get("/api/v1/imports/runs").status_code == 200
    key.revoke()
    assert client(keys["platform"]).get("/api/v1/imports/runs").status_code == 401


def test_only_a_digest_is_stored_and_use_is_recorded(keys):
    stored = ApiKey.objects.get(name="ops")
    assert stored.digest == digest(keys["platform"]) != keys["platform"]
    assert keys["platform"].startswith(f"enc_{stored.prefix}_")
    assert stored.last_used_at is None
    client(keys["platform"]).get("/api/v1/imports/runs")
    stored.refresh_from_db()
    assert stored.last_used_at is not None


def test_the_debug_machine_is_open_only_while_no_key_exists(settings):
    settings.DEBUG = True
    settings.ENCORE_OPERATOR_TOKEN = ""
    assert client().get("/api/v1/imports/runs").status_code == 200
    ApiKey.issue("first key", "platform")
    assert client().get("/api/v1/imports/runs").status_code == 401


# ---------------------------------------------------------------- organizers see only themselves


def make_run(organizer: str, tmp_path: Path) -> ImportRun:
    run = ImportRun.objects.create(source_path=str(tmp_path / f"{organizer}.csv"), organizer=organizer)
    QuarantineRow.objects.create(run=run, row_number=1, reason="bad_email", raw={"EMAIL_ADDR": "x"})
    return run


def test_an_organizer_key_lists_only_its_own_runs(keys, tmp_path):
    mine, theirs = make_run("festival-a", tmp_path), make_run("club-b", tmp_path)
    ids = [row["id"] for row in client(keys["festival"]).get("/api/v1/imports/runs").json()]
    assert ids == [mine.pk]
    everyone = [row["id"] for row in client(keys["platform"]).get("/api/v1/imports/runs").json()]
    assert sorted(everyone) == sorted([mine.pk, theirs.pk])


def test_another_organizers_run_answers_404_everywhere(keys, tmp_path):
    theirs = make_run("club-b", tmp_path)
    row = QuarantineRow.objects.get(run=theirs)
    festival = client(keys["festival"])
    assert festival.get(f"/api/v1/imports/runs/{theirs.pk}").status_code == 404
    assert festival.get(f"/api/v1/imports/runs/{theirs.pk}/quarantine").status_code == 404
    for action in ("pause", "resume", "rollback", "reconcile"):
        assert festival.post(f"/api/v1/imports/runs/{theirs.pk}/{action}").status_code == 404
    assert festival.post(f"/api/v1/imports/quarantine/{row.pk}/dismiss").status_code == 404
    row.refresh_from_db()
    assert row.status == QuarantineRow.Status.OPEN
    assert client(keys["club"]).get(f"/api/v1/imports/runs/{theirs.pk}").status_code == 200


def test_an_organizer_key_imports_for_itself_only(keys, settings, tmp_path, monkeypatch):
    monkeypatch.setattr("imports.views.run_import.delay", lambda pk: None)
    settings.ENCORE_IMPORT_SOURCE_DIR = tmp_path
    (tmp_path / "festival-a").mkdir()
    (tmp_path / "festival-a" / "fans.csv").write_text("CUST_ID\n", encoding="utf-8")
    (tmp_path / "shared.csv").write_text("CUST_ID\n", encoding="utf-8")
    festival = client(keys["festival"])
    assert [f["name"] for f in festival.get("/api/v1/imports/sources").json()] == ["fans.csv"]
    other = festival.post(
        "/api/v1/imports/runs", {"source": "fans.csv", "organizer": "club-b"}, format="json"
    )
    assert other.status_code == 403
    outside = festival.post("/api/v1/imports/runs", {"source": "shared.csv"}, format="json")
    assert outside.status_code == 400, "A file outside the organizer's folder does not exist for it"
    created = festival.post("/api/v1/imports/runs", {"source": "fans.csv"}, format="json")
    assert created.status_code == 202
    assert ImportRun.objects.get(pk=created.json()["id"]).organizer == "festival-a"


# ---------------------------------------------------------------- roles


def test_a_gate_key_scans_but_reads_no_imports_and_runs_no_operations(keys):
    EventOwner.objects.create(event_external_id="E", organizer="festival-a")
    apply_ticket_versions([("T1", "E", None, "valid", 1)])
    gate = client(keys["gate"])
    scan = gate.post("/api/v1/scans", {"ticket": "T1", "scan_id": str(uuid.uuid4())}, format="json")
    assert scan.status_code == 200
    assert gate.get("/api/v1/events/E/manifest").status_code == 200
    for path in ("/api/v1/imports/runs", "/api/v1/connector/dead-letters", "/api/v1/connector/health"):
        assert gate.get(path).status_code == 403, path
    assert gate.post("/api/v1/connector/faults", {"outage": True}, format="json").status_code == 403


def test_a_gate_cannot_see_or_burn_another_organizers_tickets(keys):
    EventOwner.objects.create(event_external_id="FEST", organizer="festival-a")
    EventOwner.objects.create(event_external_id="CLUB", organizer="club-b")
    apply_ticket_versions([("F1", "FEST", None, "valid", 1), ("C1", "CLUB", None, "valid", 1)])
    festival_gate = client(keys["gate"])
    # Another organizer's event and ticket answer exactly like ones that do not exist.
    assert festival_gate.get("/api/v1/events/CLUB/manifest").status_code == 404
    assert festival_gate.get("/api/v1/events/NOPE/manifest").status_code == 404
    foreign = festival_gate.post("/api/v1/scans", {"ticket": "C1", "scan_id": "s-1"}, format="json")
    missing = festival_gate.post("/api/v1/scans", {"ticket": "NOPE", "scan_id": "s-2"}, format="json")
    assert (
        (foreign.status_code, foreign.json())
        == (missing.status_code, missing.json())
        == (
            404,
            {"outcome": "unknown_ticket", "checked_in_at": None},
        )
    )
    upload = festival_gate.post(
        "/api/v1/scans/offline",
        {"device": "d1", "scans": [{"ticket": "C1", "scan_id": "s-3", "scanned_at": "2026-01-01T10:00:00Z"}]},
        format="json",
    )
    assert upload.json()["results"] == [{"scan_id": "s-3", "result": "conflict"}]
    assert ScanConflict.objects.get(scan_id="s-3").organizer == "festival-a"
    assert Ticket.objects.get(external_id="C1").checked_in_at is None, "The club's ticket was not burned"
    # Its own event works, and the club's own gate can admit its ticket.
    assert [t["ticket"] for t in festival_gate.get("/api/v1/events/FEST/manifest").json()["tickets"]] == [
        "F1"
    ]
    club = client(keys["club_gate"]).post("/api/v1/scans", {"ticket": "C1", "scan_id": "s-4"}, format="json")
    assert club.json()["outcome"] == "admitted"


def test_a_gate_key_has_its_own_higher_limit(keys, monkeypatch):
    from django.core.cache import cache
    from rest_framework.throttling import SimpleRateThrottle

    monkeypatch.setattr(
        SimpleRateThrottle, "THROTTLE_RATES", {"key": "1/min", "gate-key": "3/min", "no-key": "100000/min"}
    )
    cache.clear()
    gate = client(keys["gate"])
    assert [gate.get("/api/v1/whoami").status_code for _ in range(4)] == [200, 200, 200, 429]
    festival = client(keys["festival"])
    assert [festival.get("/api/v1/whoami").status_code for _ in range(2)] == [200, 429]


def test_a_forged_forwarded_for_header_does_not_reset_the_limit(keys, monkeypatch):
    from django.core.cache import cache
    from rest_framework.throttling import SimpleRateThrottle

    monkeypatch.setattr(
        SimpleRateThrottle,
        "THROTTLE_RATES",
        {"key": "100000/min", "gate-key": "100000/min", "no-key": "2/min"},
    )
    cache.clear()
    answers = [
        client().get("/api/v1/imports/runs", HTTP_X_FORWARDED_FOR=f"10.0.0.{n}, 203.0.113.9").status_code
        for n in range(4)
    ]
    assert answers == [401, 401, 429, 429], "The edge's own entry, not the caller's, decides the bucket"


def test_starting_and_undoing_imports_is_logged(keys, settings, tmp_path, monkeypatch):
    from access.models import AccessRecord

    monkeypatch.setattr("imports.views.run_import.delay", lambda pk: None)
    settings.ENCORE_IMPORT_SOURCE_DIR = tmp_path
    (tmp_path / "festival-a").mkdir()
    (tmp_path / "festival-a" / "fans.csv").write_text("CUST_ID\n", encoding="utf-8")
    festival = client(keys["festival"])
    run_id = festival.post("/api/v1/imports/runs", {"source": "fans.csv"}, format="json").json()["id"]
    festival.post(f"/api/v1/imports/runs/{run_id}/rollback")
    festival.get("/api/v1/imports/runs")
    logged = list(AccessRecord.objects.order_by("at").values_list("method", "path"))
    assert logged == [("POST", "/api/v1/imports/runs"), ("POST", f"/api/v1/imports/runs/{run_id}/rollback")]


def test_runs_show_the_file_name_not_the_server_path(keys, tmp_path):
    run = make_run("festival-a", tmp_path)
    body = client(keys["festival"]).get(f"/api/v1/imports/runs/{run.pk}").json()
    assert body["source_path"] == "festival-a.csv"


def test_an_organizer_key_cannot_scan_or_run_the_connector(keys):
    festival = client(keys["festival"])
    data = {"ticket": "T1", "scan_id": str(uuid.uuid4())}
    assert festival.post("/api/v1/scans", data, format="json").status_code == 403
    assert festival.get("/api/v1/events/E/manifest").status_code == 403
    assert festival.get("/api/v1/connector/dead-letters").status_code == 403


def test_whoami_tells_a_client_what_its_key_can_do(keys):
    assert client(keys["festival"]).get("/api/v1/whoami").json() == {
        "scope": "organizer",
        "organizer": "festival-a",
    }
    assert client(keys["gate"]).get("/api/v1/whoami").json() == {"scope": "gate", "organizer": "festival-a"}
    assert client().get("/api/v1/whoami").status_code == 401


def test_availability_stays_public_and_is_never_rate_limited(keys, monkeypatch):
    from django.core.cache import cache
    from rest_framework.throttling import SimpleRateThrottle

    monkeypatch.setattr(
        SimpleRateThrottle, "THROTTLE_RATES", {"key": "1/min", "gate-key": "1/min", "no-key": "1/min"}
    )
    cache.clear()
    assert {client().get("/api/v1/events/E/availability").status_code for _ in range(5)} == {200}


# ---------------------------------------------------------------- the command people use


def test_key_command_issues_lists_and_revokes(capsys):
    call_command(
        "api_key", "issue", "--scope", "organizer", "--organizer", "festival-a", "--name", "box office"
    )
    plaintext = capsys.readouterr().out.strip().splitlines()[-1]
    key = ApiKey.objects.get(digest=digest(plaintext))
    call_command("api_key", "list")
    assert key.prefix in capsys.readouterr().out
    call_command("api_key", "revoke", key.prefix)
    key.refresh_from_db()
    assert key.revoked_at is not None


def test_key_command_refuses_an_organizer_key_without_an_organizer():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("api_key", "issue", "--scope", "organizer", "--name", "x")
    with pytest.raises(CommandError):
        call_command("api_key", "issue", "--scope", "gate", "--name", "a gate belongs to an organizer")
    with pytest.raises(CommandError):
        call_command("api_key", "issue", "--scope", "platform", "--organizer", "festival-a", "--name", "x")


# ---------------------------------------------------------------- limits and the access log


def test_callers_without_a_key_are_slowed_down(keys, monkeypatch):
    from django.core.cache import cache
    from rest_framework.throttling import SimpleRateThrottle

    monkeypatch.setattr(
        SimpleRateThrottle,
        "THROTTLE_RATES",
        {"key": "100000/min", "gate-key": "100000/min", "no-key": "3/min"},
    )
    cache.clear()
    answers = [client().get("/api/v1/imports/runs").status_code for _ in range(5)]
    assert answers == [401, 401, 401, 429, 429]
    assert client(keys["platform"]).get("/api/v1/imports/runs").status_code == 200, (
        "A valid key is not blocked"
    )


def test_each_key_has_its_own_limit(keys, monkeypatch):
    from django.core.cache import cache
    from rest_framework.throttling import SimpleRateThrottle

    monkeypatch.setattr(
        SimpleRateThrottle,
        "THROTTLE_RATES",
        {"key": "2/min", "gate-key": "100000/min", "no-key": "100000/min"},
    )
    cache.clear()
    festival, club = client(keys["festival"]), client(keys["club"])
    assert [festival.get("/api/v1/imports/runs").status_code for _ in range(3)] == [200, 200, 429]
    assert club.get("/api/v1/imports/runs").status_code == 200


def test_personal_data_reads_and_changes_are_logged_with_the_key(keys, tmp_path):
    from access.models import AccessRecord

    run = make_run("festival-a", tmp_path)
    row = QuarantineRow.objects.get(run=run)
    festival = client(keys["festival"])
    festival.get(f"/api/v1/imports/runs/{run.pk}/quarantine")
    festival.post(f"/api/v1/imports/quarantine/{row.pk}/dismiss")
    client(keys["club"]).get(f"/api/v1/imports/runs/{run.pk}/quarantine")
    festival.get("/api/v1/imports/runs")  # no personal data: not logged
    records = list(AccessRecord.objects.order_by("at"))
    prefix = ApiKey.objects.get(name="festival staff").prefix
    assert [(r.key_prefix, r.method, r.status) for r in records] == [
        (prefix, "GET", 200),
        (prefix, "POST", 200),
        (ApiKey.objects.get(name="club staff").prefix, "GET", 404),
    ]
    assert records[0].organizer == "festival-a"


def test_old_access_records_are_purged(settings, capsys):
    import datetime as dt

    from django.utils import timezone

    from access.models import AccessRecord

    settings.ENCORE_AUDIT_RETENTION_DAYS = 30
    old = AccessRecord.objects.create(scope="platform", key_prefix="x", method="GET", path="/", status=200)
    AccessRecord.objects.filter(pk=old.pk).update(at=timezone.now() - dt.timedelta(days=31))
    AccessRecord.objects.create(scope="platform", key_prefix="y", method="GET", path="/", status=200)
    call_command("api_key", "purge-audit")
    assert list(AccessRecord.objects.values_list("key_prefix", flat=True)) == ["y"]


# ---------------------------------------------------------------- the published contract matches the code


def test_openapi_documents_every_route_with_the_scope_the_code_enforces():
    import yaml
    from django.urls import resolve

    from access import permissions

    spec = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml").read_text("utf-8")
    )
    names = {
        permissions.Authenticated: "any key",
        permissions.PlatformOnly: "platform",
        permissions.ImportsAccess: "organizer or platform",
        permissions.GateAccess: "gate or platform",
    }
    documented = {"/api/v1" + re.sub(r"\{\w+\}", "x", path) for path in spec["paths"]}
    for route in set(api_routes()):
        assert any(re.fullmatch(re.escape(d).replace("x", "[^/]+"), concrete(route)) for d in documented), (
            route
        )
    for path, methods in spec["paths"].items():
        url = "/api/v1" + re.sub(r"\{\w+\}", "1", path).replace("/1/1", "/1/pause")
        view = resolve(url).func
        cls = getattr(view, "view_class", None)
        for method, operation in methods.items():
            if cls is None or operation.get("security") == []:
                assert operation.get("security") == [], f"{path} is open in code, so the spec must say so"
                continue
            perm = cls.permission_classes[0]
            if perm is permissions.Public:
                assert operation.get("security") == [], path
                continue
            scope = operation.get("x-scope", "").split(";")[0]
            assert scope == names[perm], (
                f"{method.upper()} {path}: spec says {scope!r}, code enforces {names[perm]!r}"
            )
            logged = "logged" in operation.get("x-scope", "")
            from access.audit import Audited

            audited = issubclass(cls, Audited) and (
                cls.audited_methods is None or method.upper() in cls.audited_methods
            )
            assert logged == audited, f"{method.upper()} {path}: access log flag differs from the code"
