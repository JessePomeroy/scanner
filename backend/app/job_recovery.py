"""Reconcile job records with scan workspaces after an interrupted backend process."""

from __future__ import annotations

from pathlib import Path
import shutil

from app.jobs import JobStore
from app.scan_validator import find_scan_root, validate_scan_package
from app.schemas import JobRecord, JobStatus


def reconcile_interrupted_jobs(
    jobs: JobStore,
    *,
    processing_dir: Path,
    completed_dir: Path,
    failed_dir: Path,
) -> list[JobRecord]:
    """Resolve every active job without deleting partial or completed scan data."""
    reconciled: list[JobRecord] = []
    for record in jobs.list_active():
        processing_path = processing_dir / record.scan_id
        completed_path = completed_dir / record.scan_id

        if completed_path.is_dir():
            if processing_path.exists():
                _move_interrupted_workspace(
                    processing_path,
                    failed_dir,
                    record.scan_id,
                )
            reconciled.append(_recover_completed_job(jobs, record, completed_path))
            continue

        failed_path = failed_dir / record.scan_id
        if processing_path.exists():
            failed_path = _move_interrupted_workspace(
                processing_path,
                failed_dir,
                record.scan_id,
            )

        outputs = dict(record.outputs)
        if failed_path.is_dir():
            outputs["package_dir"] = str(failed_path)
        reconciled.append(
            jobs.update(
                record.scan_id,
                status="failed",
                message=(
                    "Backend restarted before the job finished. "
                    "Partial data was preserved; submit the scan again to retry."
                ),
                outputs=outputs,
            )
        )

    return reconciled


def _recover_completed_job(
    jobs: JobStore,
    record: JobRecord,
    completed_path: Path,
) -> JobRecord:
    outputs = dict(record.outputs)
    outputs["package_dir"] = str(completed_path)

    try:
        scan_root = find_scan_root(completed_path)
        report = validate_scan_package(scan_root)
    except Exception as error:
        return jobs.update(
            record.scan_id,
            status="failed",
            message=f"Completed scan recovery validation failed: {error}",
            outputs=outputs,
        )

    report_path = scan_root / "metadata" / "scan_report.json"
    if report_path.is_file():
        outputs["scan_report"] = str(report_path)

    terminal_status: JobStatus
    message: str
    if record.stage == "validating":
        terminal_status = "validated"
        message = "Recovered validated scan after backend restart."
    elif record.stage == "exporting":
        terminal_status = "complete"
        message = "Recovered completed reconstruction after backend restart."
    else:
        terminal_status = "failed"
        message = (
            "Completed scan files were preserved, but the saved lifecycle stage "
            f"{record.stage!r} cannot prove processing finished."
        )

    return jobs.update(
        record.scan_id,
        status=terminal_status,
        message=message,
        image_count=report.image_count,
        frame_count=report.frame_count,
        outputs=outputs,
    )


def _move_interrupted_workspace(
    processing_path: Path,
    failed_dir: Path,
    scan_id: str,
) -> Path:
    failed_dir.mkdir(parents=True, exist_ok=True)
    destination = failed_dir / scan_id
    suffix = 1
    while destination.exists():
        destination = failed_dir / f"{scan_id}-interrupted-{suffix}"
        suffix += 1
    shutil.move(str(processing_path), str(destination))
    return destination
