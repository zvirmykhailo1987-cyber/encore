from celery import shared_task
from django.conf import settings

from .models import ImportRun
from .pipeline import RunBusy, execute_run, reap_stale_runs
from .reconcile import reconcile_run


@shared_task(name="imports.run_import", acks_late=True)
def run_import(run_id: int) -> None:
    try:
        execute_run(run_id)
    except RunBusy:
        # A redelivered task for a run that is already handled has nothing left to do.
        return


@shared_task(name="imports.reconcile")
def reconcile_task(run_id: int) -> None:
    run = ImportRun.objects.get(pk=run_id)
    try:
        report = reconcile_run(run)
        # A run rolled back in the meantime keeps its own state: the report no longer describes it.
        ImportRun.objects.filter(pk=run_id, status=ImportRun.Status.COMPLETED).update(
            reconciliation=report, reconcile_error=""
        )
    except Exception as exc:
        ImportRun.objects.filter(pk=run_id).update(reconcile_error=f"{type(exc).__name__}: {exc}"[:300])
        raise
    finally:
        ImportRun.objects.filter(pk=run_id).update(reconcile_started_at=None)


@shared_task(name="imports.reap_stale_runs")
def reap_stale() -> list[int]:
    stale = reap_stale_runs()
    if settings.ENCORE_IMPORT_AUTO_RESUME:
        for run in ImportRun.objects.filter(
            pk__in=stale,
            status=ImportRun.Status.INTERRUPTED,
            resume_count__lt=settings.ENCORE_IMPORT_MAX_AUTO_RESUMES,
        ):
            run_import.delay(run.pk)
    return stale
