# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

import datetime as dt
import re
from pathlib import Path

from django.conf import settings
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from access.audit import Audited
from access.permissions import ImportsAccess, principal
from crm.models import DEFAULT_ORGANIZER

from .mapping import RowDefect
from .models import ImportBatch, ImportRun, QuarantineRow
from .pipeline import RunBusy, reprocess_quarantined, rollback_run
from .tasks import reconcile_task, run_import

RECONCILE_EXPIRY_HOURS = 6
ORGANIZER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class RunSerializer(serializers.ModelSerializer):
    # The file name only: where the server keeps its files is not the caller's business.
    source_path = serializers.SerializerMethodField()

    class Meta:
        model = ImportRun
        exclude = ["lease_token"]

    def get_source_path(self, run: ImportRun) -> str:
        return Path(run.source_path).name


class QuarantineSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuarantineRow
        fields = ["id", "row_number", "reason", "detail", "raw", "status"]


def visible_runs(request: Request):
    """An organizer key sees that organizer's runs only. Anything else answers 404, as if it did not exist."""
    who = principal(request)
    runs = ImportRun.objects.all()
    return (
        runs if who is not None and who.is_platform else runs.filter(organizer=who.organizer if who else None)
    )


def source_root(request: Request) -> Path:
    """The platform sees the whole drop folder. An organizer sees its own subfolder only."""
    root = Path(settings.ENCORE_IMPORT_SOURCE_DIR).resolve()
    who = principal(request)
    return root if who is not None and who.is_platform else root / str(who.organizer if who else "-")


def resolve_source(name: str, root: Path | None = None) -> Path | None:
    """Sources are named relative to one folder. A request can never read an arbitrary server path."""
    root = (root or Path(settings.ENCORE_IMPORT_SOURCE_DIR)).resolve()
    try:
        candidate = (root / name).resolve()
        return candidate if candidate.is_relative_to(root) and candidate.is_file() else None
    except (ValueError, OSError):
        return None


class RunListView(Audited, APIView):
    permission_classes = [ImportsAccess]
    # Starting an import writes contacts. Listing runs shows no personal data.
    audited_methods = frozenset({"POST"})

    def get(self, request: Request) -> Response:
        return Response(RunSerializer(visible_runs(request)[:50], many=True).data)

    def post(self, request: Request) -> Response:
        path = resolve_source(str(request.data.get("source", "")), source_root(request))
        if path is None:
            return Response({"error": "Unknown source file"}, status=400)
        who = principal(request)
        requested = request.data.get("organizer")
        if who is not None and not who.is_platform:
            if requested and requested != who.organizer:
                return Response({"error": "This key imports for its own organizer only"}, status=403)
            requested = who.organizer
        organizer = str(requested or DEFAULT_ORGANIZER)
        if not ORGANIZER.match(organizer):
            return Response({"error": "An organizer is lower case letters, digits, - and _"}, status=400)
        if path.name.endswith(".mongoexport.jsonl"):
            source_format = "mongo"
        else:
            source_format = "jsonl" if path.suffix == ".jsonl" else "csv"
        run = ImportRun.objects.create(
            organizer=organizer,
            source_path=str(path),
            source_format=source_format,
            dry_run=bool(request.data.get("dry_run")),
        )
        run_import.delay(run.pk)
        return Response(RunSerializer(run).data, status=202)


def run_payload(run: ImportRun) -> dict:
    """The run plus what the console needs. Speed and time left come from committed batches only."""
    recent = list(ImportBatch.objects.filter(run=run)[:80])
    window = recent[:20]
    busy_ms = sum(batch.duration_ms for batch in window)
    speed = round(sum(batch.rows for batch in window) * 1000 / busy_ms) if busy_ms else None
    bytes_per_ms = None
    if len(window) >= 2 and busy_ms:
        bytes_per_ms = (window[0].last_offset - window[-1].last_offset) / max(
            1, sum(batch.duration_ms for batch in window[:-1])
        )
    seconds_left = None
    if bytes_per_ms and run.status == ImportRun.Status.RUNNING and run.source_size:
        seconds_left = round((run.source_size - run.checkpoint_offset) / bytes_per_ms / 1000)
    reasons = (
        QuarantineRow.objects.filter(run=run, status=QuarantineRow.Status.OPEN)
        .values("reason")
        .annotate(count=Count("pk"))
        .order_by("-count")
    )
    latest = QuarantineRow.objects.filter(run=run).order_by("-row_number").first()
    heartbeat_age = (timezone.now() - run.heartbeat_at).total_seconds() if run.heartbeat_at else None
    return {
        **RunSerializer(run).data,
        "reconciling": run.reconcile_started_at is not None,
        "speed_rows_per_second": speed,
        "seconds_left": seconds_left,
        "progress": run.checkpoint_offset / run.source_size if run.source_size else None,
        "heartbeat_age_seconds": heartbeat_age,
        "quarantine_open": [{"reason": row["reason"], "count": row["count"]} for row in reasons],
        "quarantine_latest": {"row": latest.row_number, "reason": latest.reason} if latest else None,
        "batches": [
            {
                "number": batch.number,
                "rows": batch.rows,
                "quarantined": batch.quarantined,
                "last_row": batch.last_row,
                "last_offset": batch.last_offset,
                "rows_per_second": round(batch.rows * 1000 / batch.duration_ms)
                if batch.duration_ms
                else None,
                "committed_at": batch.committed_at,
            }
            for batch in recent
        ],
    }


class RunDetailView(APIView):
    permission_classes = [ImportsAccess]

    def get(self, request: Request, pk: int) -> Response:
        return Response(run_payload(get_object_or_404(visible_runs(request), pk=pk)))


class SourceListView(APIView):
    """Files an operator can import, for the start form."""

    permission_classes = [ImportsAccess]

    def get(self, request: Request) -> Response:
        root = source_root(request)
        if not root.is_dir():
            return Response([])
        files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix in (".csv", ".jsonl"))
        return Response([{"name": p.name, "size": p.stat().st_size} for p in files])


class RunActionView(Audited, APIView):
    permission_classes = [ImportsAccess]

    def post(self, request: Request, pk: int, action: str) -> Response:
        run = get_object_or_404(visible_runs(request), pk=pk)
        if action == "pause":
            ImportRun.objects.filter(pk=pk, status=ImportRun.Status.RUNNING).update(pause_requested=True)
        elif action == "resume":
            if run.status not in ImportRun.RESUMABLE:
                return Response({"error": f"Run is {run.status}"}, status=409)
            run_import.delay(pk)
        elif action == "rollback":
            try:
                rollback_run(pk)
            except RunBusy as exc:
                return Response({"error": str(exc)}, status=409)
        elif action == "reconcile":
            if run.status != ImportRun.Status.COMPLETED or run.dry_run:
                return Response({"error": "Only a completed import can be reconciled"}, status=409)
            # A reconciliation reads the whole file, so it must not live inside an HTTP request.
            expired = timezone.now() - dt.timedelta(hours=RECONCILE_EXPIRY_HOURS)
            claimed = (
                ImportRun.objects.filter(pk=pk)
                .filter(Q(reconcile_started_at__isnull=True) | Q(reconcile_started_at__lt=expired))
                .update(reconcile_started_at=timezone.now())
            )
            if not claimed:
                return Response({"error": "A reconciliation is already running"}, status=409)
            reconcile_task.delay(pk)
            return Response(run_payload(ImportRun.objects.get(pk=pk)), status=202)
        else:
            return Response({"error": "Unknown action"}, status=404)
        return Response(RunSerializer(ImportRun.objects.get(pk=pk)).data)


class QuarantineListView(Audited, APIView):
    permission_classes = [ImportsAccess]

    def get(self, request: Request, pk: int) -> Response:
        run = get_object_or_404(visible_runs(request), pk=pk)
        rows = QuarantineRow.objects.filter(run=run)
        reason = request.query_params.get("reason")
        if reason:
            rows = rows.filter(reason=reason)
        total = rows.count()
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except ValueError:
            return Response({"error": "offset must be a number"}, status=400)
        page = QuarantineSerializer(rows[offset : offset + 200], many=True).data
        return Response(page, headers={"X-Total-Count": str(total)})


class QuarantineActionView(Audited, APIView):
    permission_classes = [ImportsAccess]

    def post(self, request: Request, row_id: int, action: str) -> Response:
        row = get_object_or_404(QuarantineRow.objects.filter(run__in=visible_runs(request)), pk=row_id)
        if action == "retry":
            corrected = request.data.get("corrected", {})
            if not isinstance(corrected, dict):
                return Response({"error": "corrected must be an object"}, status=400)
            try:
                row = reprocess_quarantined(row, {str(k): str(v) for k, v in corrected.items()})
            except RowDefect as defect:
                return Response({"error": defect.reason, "detail": defect.detail}, status=422)
            except RunBusy as exc:
                return Response({"error": str(exc)}, status=409)
        elif action == "dismiss":
            QuarantineRow.objects.filter(pk=row_id, status=QuarantineRow.Status.OPEN).update(
                status=QuarantineRow.Status.DISMISSED
            )
            row.refresh_from_db()
        else:
            return Response({"error": "Unknown action"}, status=404)
        return Response(QuarantineSerializer(row).data)
