"""FastAPI entry point for the reconstruction backend."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import uuid
import zipfile
from time import perf_counter

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.blender_exporter import export_blender_formats
from app.colmap_runner import ColmapConfig, run_colmap_pipeline
from app.jobs import JobStore
from app.openmvs_runner import run_openmvs_pipeline
from app.scan_package import validate_and_report_scan
from app.scan_validator import (
    ScanValidationError,
    find_scan_root,
)
from app.schemas import JobRecord
from app.storage import UnsafeArchiveError, safe_extract_zip


app = FastAPI(title="3D Scan Reconstruction Backend")

BASE_DIR = Path(os.environ.get("SCANNER_SCANS_DIR", "scans"))
INCOMING_DIR = BASE_DIR / "incoming"
PROCESSING_DIR = BASE_DIR / "processing"
COMPLETED_DIR = BASE_DIR / "completed"
FAILED_DIR = BASE_DIR / "failed"
JOBS_DIR = BASE_DIR / "jobs"

for directory in [INCOMING_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR, JOBS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

jobs = JobStore(JOBS_DIR)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return a lightweight health response for local development."""
    return {"status": "ok"}


@app.get("/scans", response_model=list[JobRecord])
def list_scan_jobs(limit: int = Query(50, ge=1, le=200)) -> list[JobRecord]:
    """Return recent scan jobs, newest first."""
    return jobs.list(limit=limit)


@app.post("/scans", response_model=JobRecord)
async def upload_scan(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    run_reconstruction: bool = Query(False),
    run_dense: bool = Query(False),
    run_openmvs: bool = Query(False),
) -> JobRecord:
    """Upload a scan package and optionally run reconstruction in the background."""
    scan_id = str(uuid.uuid4())
    record = jobs.create(scan_id)
    incoming_zip = INCOMING_DIR / f"{scan_id}.zip"

    incoming_zip.write_bytes(await file.read())

    if run_reconstruction:
        background_tasks.add_task(
            process_scan,
            scan_id,
            incoming_zip,
            run_dense,
            run_openmvs,
        )
        return jobs.update(
            scan_id,
            status="processing",
            stage="queued",
            message="Scan queued for processing.",
        )

    processing_dir: Path | None = None
    try:
        jobs.update(
            scan_id,
            status="processing",
            stage="validating",
            message="Validating scan package.",
        )
        processing_dir = prepare_processing_dir(scan_id, incoming_zip)
        scan_root = find_scan_root(processing_dir)
        package = validate_and_report_scan(scan_root)
        report = package.validation
        completed_dir = move_to_completed(scan_id, processing_dir)
        completed_scan_root = find_scan_root(completed_dir)
        outputs = {
            "package_dir": str(completed_dir),
            "scan_report": str(completed_scan_root / "metadata" / "scan_report.json"),
        }

        return jobs.update(
            scan_id,
            status="validated",
            message="Scan package validated. Reconstruction was not requested.",
            image_count=report.image_count,
            frame_count=report.frame_count,
            outputs=outputs,
        )
    except (ScanValidationError, UnsafeArchiveError, zipfile.BadZipFile) as error:
        fail_processing(scan_id, processing_dir)
        return jobs.update(scan_id, status="failed", message=str(error))


@app.get("/scans/{scan_id}", response_model=JobRecord)
def get_scan_status(scan_id: str) -> JobRecord:
    try:
        return jobs.read(scan_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown scan id") from error


@app.get("/scans/{scan_id}/files/{relative_path:path}")
def download_scan_file(scan_id: str, relative_path: str) -> FileResponse:
    record = get_scan_status(scan_id)
    package_dir = record.outputs.get("package_dir")
    if package_dir is None:
        raise HTTPException(status_code=404, detail="No package directory available")

    base = Path(package_dir).resolve()
    target = (base / relative_path).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(target)


def process_scan(scan_id: str, incoming_zip: Path, run_dense: bool, run_openmvs: bool) -> None:
    processing_dir: Path | None = None
    try:
        jobs.update(
            scan_id,
            status="processing",
            stage="validating",
            message="Validating scan package.",
        )
        processing_dir = prepare_processing_dir(scan_id, incoming_zip)
        scan_root = find_scan_root(processing_dir)
        package = validate_and_report_scan(scan_root)
        report = package.validation

        jobs.update(
            scan_id,
            status="processing",
            stage="reconstructing",
            message="Running COLMAP reconstruction.",
        )
        started_at = perf_counter()
        colmap_output = run_colmap_pipeline(
            scan_root,
            ColmapConfig(use_gpu=False),
            include_dense=run_dense,
        )
        package.record_processing_step(
            "colmap",
            {
                "matcher": ColmapConfig().matcher,
                "use_gpu": False,
                "include_dense": run_dense,
                "elapsed_seconds": perf_counter() - started_at,
                "output": str(colmap_output),
            },
        )
        outputs = {
            "colmap_output": str(colmap_output),
            "scan_report": str(package.report_path),
        }

        if run_openmvs:
            jobs.update(
                scan_id,
                status="processing",
                stage="meshing",
                message="Running OpenMVS mesh reconstruction.",
            )
            started_at = perf_counter()
            textured_mesh = run_openmvs_pipeline(scan_root)
            package.record_processing_step(
                "openmvs",
                {
                    "elapsed_seconds": perf_counter() - started_at,
                    "output": str(textured_mesh),
                },
            )
            outputs["textured_mesh"] = str(textured_mesh)

        jobs.update(
            scan_id,
            status="processing",
            stage="exporting",
            message="Preparing Blender-friendly outputs.",
        )
        export_blender_formats(scan_root)
        package = validate_and_report_scan(scan_root)
        completed_dir = move_to_completed(scan_id, processing_dir)
        completed_scan_root = find_scan_root(completed_dir)
        outputs["package_dir"] = str(completed_dir)
        outputs["scan_report"] = str(completed_scan_root / "metadata" / "scan_report.json")

        jobs.update(
            scan_id,
            status="complete",
            message="Reconstruction completed.",
            image_count=report.image_count,
            frame_count=report.frame_count,
            outputs=outputs,
        )
    except Exception as error:
        fail_processing(scan_id, processing_dir)
        jobs.update(scan_id, status="failed", message=str(error))


def prepare_processing_dir(scan_id: str, incoming_zip: Path) -> Path:
    processing_dir = PROCESSING_DIR / scan_id
    if processing_dir.exists():
        shutil.rmtree(processing_dir)

    processing_dir.mkdir(parents=True)
    safe_extract_zip(incoming_zip, processing_dir)
    return processing_dir


def move_to_completed(scan_id: str, processing_dir: Path) -> Path:
    completed_dir = COMPLETED_DIR / scan_id
    if completed_dir.exists():
        shutil.rmtree(completed_dir)
    shutil.move(str(processing_dir), str(completed_dir))
    return completed_dir


def fail_processing(scan_id: str, processing_dir: Path | None) -> None:
    if processing_dir is None or not processing_dir.exists():
        return

    failed_dir = FAILED_DIR / scan_id
    if failed_dir.exists():
        shutil.rmtree(failed_dir)
    shutil.move(str(processing_dir), str(failed_dir))
