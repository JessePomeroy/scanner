"""Reconcile job records with scan workspaces after an interrupted backend process."""

from __future__ import annotations

from pathlib import Path
import shutil

from app.artifacts import discover_standard_output_paths
from app.jobs import JobStore
from app.scan_validator import find_scan_root, validate_scan_package
from app.schemas import JobRecord, JobStatus


_INTERRUPTED_WORKSPACE_OUTPUT = "interrupted_workspace"
_STANDARD_RESULT_OUTPUTS = {"colmap_output", "scan_report", "textured_mesh"}


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
                record, _ = _preserve_processing_workspace(
                    jobs,
                    record,
                    processing_path,
                    failed_dir,
                )
            reconciled.append(_recover_completed_job(jobs, record, completed_path))
            continue

        failed_path = _recorded_interrupted_workspace(record, failed_dir)
        if processing_path.exists():
            record, failed_path = _preserve_processing_workspace(
                jobs,
                record,
                processing_path,
                failed_dir,
            )
        elif failed_path is None:
            existing_failed_path = failed_dir / record.scan_id
            if existing_failed_path.is_dir():
                failed_path = existing_failed_path

        outputs = dict(record.outputs)
        outputs.pop(_INTERRUPTED_WORKSPACE_OUTPUT, None)
        if failed_path is not None and failed_path.is_dir():
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
    for name in _STANDARD_RESULT_OUTPUTS:
        outputs.pop(name, None)
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

    recovered_outputs = discover_standard_output_paths(scan_root)
    if terminal_status == "complete":
        outputs.update(recovered_outputs)
    elif "scan_report" in recovered_outputs:
        outputs["scan_report"] = recovered_outputs["scan_report"]

    return jobs.update(
        record.scan_id,
        status=terminal_status,
        message=message,
        image_count=report.image_count,
        frame_count=report.frame_count,
        outputs=outputs,
    )


def _preserve_processing_workspace(
    jobs: JobStore,
    record: JobRecord,
    processing_path: Path,
    failed_dir: Path,
) -> tuple[JobRecord, Path]:
    failed_dir.mkdir(parents=True, exist_ok=True)
    destination = _recorded_interrupted_workspace(record, failed_dir)
    if destination is None or destination.exists():
        destination = _available_failed_destination(failed_dir, record.scan_id)

    outputs = dict(record.outputs)
    outputs[_INTERRUPTED_WORKSPACE_OUTPUT] = str(destination)
    record = jobs.update(
        record.scan_id,
        status=record.status,
        message="Backend restart is preserving an interrupted processing workspace.",
        outputs=outputs,
    )
    shutil.move(str(processing_path), str(destination))
    return record, destination


def _recorded_interrupted_workspace(
    record: JobRecord,
    failed_dir: Path,
) -> Path | None:
    raw_path = record.outputs.get(_INTERRUPTED_WORKSPACE_OUTPUT)
    if raw_path is None:
        return None

    candidate = Path(raw_path)
    expected_prefix = f"{record.scan_id}-interrupted-"
    if (
        candidate.parent.resolve() != failed_dir.resolve()
        or (
            candidate.name != record.scan_id
            and not candidate.name.startswith(expected_prefix)
        )
    ):
        return None
    return candidate


def _available_failed_destination(failed_dir: Path, scan_id: str) -> Path:
    destination = failed_dir / scan_id
    suffix = 1
    while destination.exists():
        destination = failed_dir / f"{scan_id}-interrupted-{suffix}"
        suffix += 1
    return destination
