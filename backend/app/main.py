"""FastAPI entry point for the reconstruction backend."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import os
import shutil
from typing import BinaryIO, Iterator
from urllib.parse import quote
import uuid
import zipfile
from time import perf_counter

from fastapi import BackgroundTasks, Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.artifacts import (
    ArtifactUnavailableError,
    UnsafeArtifactPathError,
    list_downloadable_artifacts,
    open_downloadable_artifact,
    rebase_output_paths,
)
from app.blender_exporter import export_blender_formats
from app.colmap_runner import ColmapConfig, run_colmap_pipeline
from app.job_recovery import reconcile_interrupted_jobs
from app.jobs import InvalidScanIDError, JobStore
from app.mask_undistorter import convert_capture_mask_set
from app.mask_processor import validate_openmvs_masks
from app.openmvs_runner import (
    OpenMVSConfig,
    OpenMVSScopeMode,
    inspect_openmvs_dense_cloud,
    run_openmvs_pipeline,
    validate_openmvs_config_masks,
)
from app.reconstruction_region import (
    ReconstructionRegion,
    ReconstructionRegionError,
    ReconstructionRegionNotFoundError,
    ReconstructionRegionRevisionError,
    load_reconstruction_region,
    save_reconstruction_region,
)
from app.scan_package import validate_and_report_scan
from app.scan_validator import (
    ScanValidationError,
    find_scan_root,
)
from app.schemas import JobRecord, ScanArtifact
from app.sparse_review import publish_sparse_review_checkpoint
from app.storage import UnsafeArchiveError, safe_extract_zip
from app.upload_lifecycle import store_job_upload


@asynccontextmanager
async def lifespan(_: FastAPI):
    reconcile_interrupted_jobs(
        jobs,
        processing_dir=PROCESSING_DIR,
        completed_dir=COMPLETED_DIR,
        failed_dir=FAILED_DIR,
    )
    yield


app = FastAPI(title="3D Scan Reconstruction Backend", lifespan=lifespan)

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
    scope_mode: OpenMVSScopeMode = Query("auto_roi"),
    use_masks: bool = Query(False),
    review_scope: bool = Query(False),
) -> JobRecord:
    """Upload a scan package and optionally run reconstruction in the background."""
    if review_scope and not run_reconstruction:
        raise HTTPException(status_code=400, detail="Scope review requires reconstruction.")
    scan_id = str(uuid.uuid4())
    jobs.create(scan_id)
    incoming_zip = INCOMING_DIR / f"{scan_id}.zip"

    try:
        await store_job_upload(
            file,
            incoming_zip,
            scan_id=scan_id,
            jobs=jobs,
        )
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="Unable to store uploaded scan package",
        ) from error

    if run_reconstruction:
        background_tasks.add_task(
            process_scan,
            scan_id,
            incoming_zip,
            run_dense,
            run_openmvs,
            scope_mode,
            use_masks,
            review_scope,
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
    except InvalidScanIDError as error:
        raise HTTPException(status_code=400, detail="Invalid scan id") from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown scan id") from error


@app.get("/scans/{scan_id}/scope")
def get_scan_scope(scan_id: str) -> dict[str, object]:
    record = get_scan_status(scan_id)
    scan_root = _stored_scan_root(scan_id, record)
    try:
        region = load_reconstruction_region(scan_root)
    except ReconstructionRegionNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ReconstructionRegionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"scan_id": scan_id, "region": region.as_dict()}


@app.put("/scans/{scan_id}/scope")
def put_scan_scope(
    scan_id: str,
    payload: dict[str, object] = Body(...),
) -> dict[str, object]:
    record = get_scan_status(scan_id)
    if record.status != "processing" or record.stage != "awaiting_scope":
        raise HTTPException(
            status_code=409,
            detail="The scan is not awaiting reconstruction-scope review.",
        )
    scan_root = _active_scan_root(scan_id)
    try:
        region = ReconstructionRegion.from_dict(payload)
        region_path = save_reconstruction_region(scan_root, region)
    except ReconstructionRegionRevisionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ReconstructionRegionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    outputs = dict(record.outputs)
    outputs["reconstruction_region"] = str(region_path)
    jobs.update(
        scan_id,
        status="processing",
        stage="awaiting_scope",
        message=f"Reconstruction scope revision {region.revision} is ready.",
        outputs=outputs,
    )
    return {"scan_id": scan_id, "region": region.as_dict()}


@app.get("/scans/{scan_id}/artifacts", response_model=list[ScanArtifact])
def list_scan_artifacts(scan_id: str) -> list[ScanArtifact]:
    record = get_scan_status(scan_id)
    try:
        artifacts = list_downloadable_artifacts(
            record.outputs,
            allowed_package_roots=(PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR),
        )
    except ArtifactUnavailableError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except UnsafeArtifactPathError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return [
        ScanArtifact(
            name=artifact.name,
            relative_path=artifact.relative_path,
            filename=artifact.filename,
            byte_count=artifact.byte_count,
            media_type=artifact.media_type,
        )
        for artifact in artifacts
    ]


@app.get("/scans/{scan_id}/files/{relative_path:path}")
def download_scan_file(scan_id: str, relative_path: str) -> StreamingResponse:
    record = get_scan_status(scan_id)
    try:
        opened = open_downloadable_artifact(
            record.outputs,
            relative_path,
            allowed_package_roots=(PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR),
        )
    except ArtifactUnavailableError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except UnsafeArtifactPathError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    encoded_filename = quote(opened.descriptor.filename, safe="")
    try:
        return StreamingResponse(
            _stream_artifact(opened.file),
            media_type=opened.descriptor.media_type,
            headers={
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''" + encoded_filename
                )
            },
            background=BackgroundTask(opened.file.close),
        )
    except BaseException:
        opened.file.close()
        raise


def _stream_artifact(file: BinaryIO, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    try:
        while chunk := file.read(chunk_size):
            yield chunk
    finally:
        file.close()


def process_scan(
    scan_id: str,
    incoming_zip: Path,
    run_dense: bool,
    run_openmvs: bool,
    scope_mode: OpenMVSScopeMode = "auto_roi",
    use_masks: bool = False,
    review_scope: bool = False,
) -> None:
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
        colmap_config = ColmapConfig(use_gpu=True)
        colmap_output = run_colmap_pipeline(
            scan_root,
            colmap_config,
            include_dense=run_dense and not review_scope,
        )
        package.record_processing_step(
            "colmap",
            {
                "matcher": colmap_config.matcher,
                "use_gpu": colmap_config.use_gpu,
                "include_dense": run_dense and not review_scope,
                "review_scope": review_scope,
                "elapsed_seconds": perf_counter() - started_at,
                "output": str(colmap_output),
            },
        )
        outputs = {
            "colmap_output": str(colmap_output),
            "scan_report": str(package.report_path),
        }

        if review_scope:
            checkpoint_outputs = publish_sparse_review_checkpoint(
                scan_root,
                run_dense=run_dense,
                run_openmvs=run_openmvs,
                scope_mode=scope_mode,
                use_masks=use_masks,
                colmap_executable=colmap_config.executable,
            )
            # Publish the explicit sparse-preview name rather than two output
            # labels pointing at the same PLY artifact.
            outputs.pop("colmap_output", None)
            outputs.update({name: str(path) for name, path in checkpoint_outputs.items()})
            outputs["package_dir"] = str(processing_dir)
            package.record_processing_step(
                "scope_review_checkpoint",
                {
                    "state": "awaiting_scope",
                    "artifacts": {
                        name: str(path) for name, path in checkpoint_outputs.items()
                    },
                },
            )
            jobs.update(
                scan_id,
                status="processing",
                stage="awaiting_scope",
                message="Sparse reconstruction is ready for 3D scope review.",
                image_count=report.image_count,
                frame_count=report.frame_count,
                outputs=outputs,
            )
            return

        if run_openmvs:
            jobs.update(
                scan_id,
                status="processing",
                stage="meshing",
                message="Running OpenMVS mesh reconstruction.",
            )
            auto_masks = report.reconstruction_scope is not None
            mask_path = scan_root / "dense" / "masks" if (auto_masks or use_masks) else None
            mask_conversion = None
            if auto_masks:
                conversion_started_at = perf_counter()
                if mask_path.is_dir():
                    mask_conversion = validate_openmvs_masks(mask_path, scan_root / "dense" / "images")
                else:
                    mask_conversion = convert_capture_mask_set(scan_root)
                package.record_processing_step(
                    "mask_conversion",
                    {
                        "elapsed_seconds": perf_counter() - conversion_started_at,
                        "result": mask_conversion.as_dict(),
                    },
                )
            started_at = perf_counter()
            openmvs_config = OpenMVSConfig(
                scope_mode=scope_mode,
                mask_path=mask_path,
            )
            textured_mesh = run_openmvs_pipeline(scan_root, openmvs_config)
            density_budget = inspect_openmvs_dense_cloud(scan_root, openmvs_config)
            mask_validation = validate_openmvs_config_masks(scan_root, openmvs_config)
            package.record_processing_step(
                "openmvs",
                {
                    "elapsed_seconds": perf_counter() - started_at,
                    "output": str(textured_mesh),
                    "settings": openmvs_config.report_settings(),
                    "density_budget": density_budget.as_dict(),
                    "mask_validation": (
                        mask_validation.as_dict() if mask_validation is not None else None
                    ),
                    "automatic_mask_conversion": auto_masks,
                },
            )
            outputs["openmvs_dense_point_cloud"] = str(
                scan_root / "dense" / "scene_dense.ply"
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
        rebased_outputs = rebase_output_paths(
            outputs,
            old_root=processing_dir,
            new_root=COMPLETED_DIR / scan_id,
        )
        completed_dir = move_to_completed(scan_id, processing_dir)
        completed_scan_root = find_scan_root(completed_dir)
        rebased_outputs["package_dir"] = str(completed_dir)
        rebased_outputs["scan_report"] = str(
            completed_scan_root / "metadata" / "scan_report.json"
        )

        jobs.update(
            scan_id,
            status="complete",
            message="Reconstruction completed.",
            image_count=report.image_count,
            frame_count=report.frame_count,
            outputs=rebased_outputs,
        )
    except Exception as error:
        fail_processing(scan_id, processing_dir)
        jobs.update(scan_id, status="failed", message=str(error))


def _active_scan_root(scan_id: str) -> Path:
    workspace = PROCESSING_DIR / scan_id
    if workspace.is_symlink() or not workspace.is_dir():
        raise HTTPException(status_code=409, detail="The scope-review workspace is unavailable.")
    try:
        return find_scan_root(workspace)
    except ScanValidationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _stored_scan_root(scan_id: str, record: JobRecord) -> Path:
    known_candidates = (
        PROCESSING_DIR / scan_id,
        COMPLETED_DIR / scan_id,
        FAILED_DIR / scan_id,
    )
    raw_package = record.outputs.get("package_dir")
    if raw_package is not None:
        declared = Path(raw_package)
        for candidate in known_candidates:
            if declared.absolute() == candidate.absolute():
                candidates = (candidate,)
                break
        else:
            raise HTTPException(status_code=409, detail="The stored scan workspace is invalid.")
    else:
        candidates = known_candidates
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            return find_scan_root(candidate)
        except ScanValidationError:
            continue
    raise HTTPException(status_code=404, detail="The stored scan workspace is unavailable.")


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
