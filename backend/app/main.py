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
from app.colmap_runner import ColmapConfig, run_colmap_dense_pipeline, run_colmap_pipeline
from app.density_budget import inspect_ply_point_budget
from app.job_recovery import reconcile_interrupted_jobs
from app.jobs import InvalidScanIDError, JobClaimError, JobStore
from app.mask_undistorter import convert_capture_mask_set
from app.mask_generator import MaskGenerationError, generate_mask_proposals
from app.mask_processor import stage_colmap_fusion_masks, validate_openmvs_masks
from app.mask_profiles import MaskProfileName, mask_stage_profile
from app.mask_review import (
    MaskReviewBlockedError,
    MaskReviewError,
    approve_mask_review,
    load_mask_review,
    reject_mask_review,
)
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
from app.reconstruction_region_application import (
    record_region_application,
    verify_point_cloud_in_region,
    write_openmvs_roi_file,
)
from app.scan_package import validate_and_report_scan
from app.scan_metadata import load_scan_metadata
from app.scan_validator import (
    ScanValidationError,
    find_scan_root,
)
from app.schemas import JobRecord, ScanArtifact
from app.sparse_review import load_sparse_review_checkpoint, publish_sparse_review_checkpoint
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
    mask_profile: MaskProfileName = Query("scene_geometry"),
    review_scope: bool = Query(False),
) -> JobRecord:
    """Upload a scan package and optionally run reconstruction in the background."""
    if review_scope and not run_reconstruction:
        raise HTTPException(status_code=400, detail="Scope review requires reconstruction.")
    if review_scope and (not run_dense or not run_openmvs):
        raise HTTPException(
            status_code=400,
            detail="Scope review requires dense and OpenMVS reconstruction.",
        )
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
            mask_profile,
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


@app.get("/scans/{scan_id}/mask-review")
def get_scan_mask_review(scan_id: str) -> dict[str, object]:
    record = get_scan_status(scan_id)
    scan_root = _stored_scan_root(scan_id, record)
    try:
        return load_mask_review(scan_root)
    except MaskReviewError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/scans/{scan_id}/mask-review/approve", response_model=JobRecord)
def approve_scan_mask_review(scan_id: str) -> JobRecord:
    record = get_scan_status(scan_id)
    if record.status != "processing" or record.stage != "awaiting_scope":
        raise HTTPException(status_code=409, detail="The scan is not awaiting review.")
    scan_root = _active_scan_root(scan_id)
    try:
        approve_mask_review(scan_root)
        validate_and_report_scan(scan_root)
    except MaskReviewBlockedError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (MaskReviewError, ScanValidationError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    outputs = dict(record.outputs)
    report_path = outputs.pop("mask_generation_report", None)
    if report_path is not None:
        outputs["mask_review_report"] = report_path
    return jobs.update(
        scan_id,
        status="processing",
        stage="awaiting_scope",
        message="Mask review approved. Set or confirm the 3D region, then continue.",
        outputs=outputs,
    )


@app.post("/scans/{scan_id}/mask-review/reject", response_model=JobRecord)
def reject_scan_mask_review(scan_id: str) -> JobRecord:
    record = get_scan_status(scan_id)
    if record.status != "processing" or record.stage != "awaiting_scope":
        raise HTTPException(status_code=409, detail="The scan is not awaiting review.")
    scan_root = _active_scan_root(scan_id)
    try:
        reject_mask_review(scan_root)
    except MaskReviewError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return jobs.update(
        scan_id,
        status="processing",
        stage="awaiting_scope",
        message="Mask proposals rejected. Correct the saved scan masks and upload a new job.",
    )


@app.post("/scans/{scan_id}/resume", response_model=JobRecord)
def resume_scan_job(scan_id: str, background_tasks: BackgroundTasks) -> JobRecord:
    """Continue an awaiting job from its saved sparse model and reviewed region."""
    record = get_scan_status(scan_id)
    if record.status != "processing" or record.stage != "awaiting_scope":
        raise HTTPException(status_code=409, detail="The scan is not awaiting scope review.")
    scan_root = _active_scan_root(scan_id)
    try:
        load_reconstruction_region(scan_root)
        checkpoint = load_sparse_review_checkpoint(scan_root)
    except (ReconstructionRegionError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not checkpoint.run_dense or not checkpoint.run_openmvs:
        raise HTTPException(
            status_code=409,
            detail="This checkpoint cannot produce a region-scoped textured mesh.",
        )
    if record.outputs.get("mask_generation_report"):
        raise HTTPException(
            status_code=409,
            detail="Generated mask proposals are awaiting sampled review before reconstruction can resume.",
        )
    try:
        claimed = jobs.claim(
            scan_id,
            expected_status="processing",
            expected_stage="awaiting_scope",
            status="processing",
            stage="reconstructing",
            message="Reviewed region saved. Continuing dense reconstruction.",
        )
    except JobClaimError as error:
        raise HTTPException(status_code=409, detail="This scan has already resumed.") from error
    background_tasks.add_task(resume_scoped_scan, scan_id)
    return claimed


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
    mask_profile: MaskProfileName = "scene_geometry",
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
        profile = mask_stage_profile(mask_profile)
        proposal_outputs: dict[str, str] = {}
        mask_generation = None
        if getattr(report, "mask_authoring", None) is not None:
            metadata = load_scan_metadata(scan_root / "metadata")
            mask_generation = generate_mask_proposals(scan_root, metadata.frames)
        if mask_generation is not None:
            if not review_scope:
                raise MaskGenerationError(
                    "Post-capture mask proposals require scope review before reconstruction."
                )
            package.record_processing_step(
                "mask_generation",
                {
                    "state": mask_generation.state,
                    "generator": mask_generation.generator,
                    "source_authoring_revision": mask_generation.source_revision,
                    "frame_count": len(mask_generation.frames),
                    "report": str(mask_generation.report_path),
                },
            )
            proposal_outputs["mask_generation_report"] = str(mask_generation.report_path)
            for review_number, review_path in enumerate(mask_generation.review_masks):
                proposal_outputs[f"mask_review_{review_number}"] = str(scan_root / review_path)

        jobs.update(
            scan_id,
            status="processing",
            stage="reconstructing",
            message="Running COLMAP reconstruction.",
        )
        started_at = perf_counter()
        masks_available = report.reconstruction_scope is not None or use_masks
        capture_mask_path = (
            scan_root / "masks" / "capture"
            if report.reconstruction_scope is not None
            else None
        )
        dense_mask_path = scan_root / "dense" / "masks" if masks_available else None
        colmap_fusion_mask_path = (
            scan_root / "dense" / "colmap_masks" if masks_available else None
        )
        mask_conversion = None

        def prepare_dense_masks() -> None:
            nonlocal mask_conversion
            assert dense_mask_path is not None
            conversion_started_at = perf_counter()
            if report.reconstruction_scope is not None:
                if dense_mask_path.is_dir():
                    mask_conversion = validate_openmvs_masks(
                        dense_mask_path, scan_root / "dense" / "images"
                    )
                else:
                    mask_conversion = convert_capture_mask_set(scan_root)
            else:
                mask_conversion = validate_openmvs_masks(
                    dense_mask_path, scan_root / "dense" / "images"
                )
            package.record_processing_step(
                "mask_conversion",
                {
                    "elapsed_seconds": perf_counter() - conversion_started_at,
                    "result": mask_conversion.as_dict(),
                },
            )
            if profile.colmap_stereo_fusion:
                assert colmap_fusion_mask_path is not None
                stage_colmap_fusion_masks(
                    dense_mask_path,
                    scan_root / "dense" / "images",
                    colmap_fusion_mask_path,
                )

        package.record_processing_step(
            "mask_profile",
            profile.as_dict(masks_available=masks_available),
        )
        colmap_config = ColmapConfig(
            use_gpu=True,
            feature_mask_path=(
                capture_mask_path
                if masks_available and profile.colmap_features
                else None
            ),
            stereo_fusion_mask_path=(
                colmap_fusion_mask_path
                if masks_available and profile.colmap_stereo_fusion
                else None
            ),
        )
        colmap_output = run_colmap_pipeline(
            scan_root,
            colmap_config,
            include_dense=run_dense and not review_scope,
            after_undistort=(
                prepare_dense_masks
                if run_dense and not review_scope and dense_mask_path is not None
                else None
            ),
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
                "mask_profile": profile.name,
                "feature_masks": colmap_config.feature_mask_path is not None,
                "stereo_fusion_masks": colmap_config.stereo_fusion_mask_path is not None,
            },
        )
        outputs = {
            "colmap_output": str(colmap_output),
            "scan_report": str(package.report_path),
            **proposal_outputs,
        }

        if review_scope:
            checkpoint_outputs = publish_sparse_review_checkpoint(
                scan_root,
                run_dense=run_dense,
                run_openmvs=run_openmvs,
                scope_mode=scope_mode,
                use_masks=use_masks,
                mask_profile=profile.name,
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
                message=(
                    "Mask proposals need correction before reconstruction can continue."
                    if mask_generation is not None and mask_generation.state == "needs_correction"
                    else "Sparse reconstruction is ready for mask and 3D scope review."
                ),
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
            mask_path = (
                dense_mask_path
                if masks_available and profile.openmvs_densification
                else None
            )
            started_at = perf_counter()
            openmvs_config = OpenMVSConfig(
                scope_mode=scope_mode,
                mask_path=mask_path,
                texture_use_masks=(
                    masks_available and profile.openmvs_texturing
                ),
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


def resume_scoped_scan(scan_id: str) -> None:
    """Resume dense work without repeating sparse feature matching or mapping."""
    processing_dir = PROCESSING_DIR / scan_id
    try:
        scan_root = find_scan_root(processing_dir)
        checkpoint = load_sparse_review_checkpoint(scan_root)
        region = load_reconstruction_region(scan_root)
        package = validate_and_report_scan(scan_root)
        report = package.validation

        profile = mask_stage_profile(checkpoint.mask_profile)
        masks_available = report.reconstruction_scope is not None or checkpoint.use_masks
        dense_mask_path = scan_root / "dense" / "masks" if masks_available else None
        colmap_fusion_mask_path = (
            scan_root / "dense" / "colmap_masks" if masks_available else None
        )
        mask_conversion = None

        def prepare_dense_masks() -> None:
            nonlocal mask_conversion
            assert dense_mask_path is not None
            conversion_started_at = perf_counter()
            if report.reconstruction_scope is not None and not dense_mask_path.is_dir():
                mask_conversion = convert_capture_mask_set(scan_root)
            else:
                mask_conversion = validate_openmvs_masks(
                    dense_mask_path, scan_root / "dense" / "images"
                )
            package.record_processing_step(
                "mask_conversion",
                {
                    "elapsed_seconds": perf_counter() - conversion_started_at,
                    "result": mask_conversion.as_dict(),
                },
            )
            if profile.colmap_stereo_fusion:
                assert colmap_fusion_mask_path is not None
                stage_colmap_fusion_masks(
                    dense_mask_path,
                    scan_root / "dense" / "images",
                    colmap_fusion_mask_path,
                )

        package.record_processing_step(
            "mask_profile",
            profile.as_dict(masks_available=masks_available),
        )

        colmap_config = ColmapConfig(
            use_gpu=True,
            stereo_fusion_mask_path=(
                colmap_fusion_mask_path
                if masks_available and profile.colmap_stereo_fusion
                else None
            ),
        )
        started_at = perf_counter()
        colmap_output = run_colmap_dense_pipeline(
            scan_root,
            colmap_config,
            after_undistort=(prepare_dense_masks if dense_mask_path is not None else None),
        )
        package.record_processing_step(
            "colmap_resume",
            {
                "reused_sparse_model": True,
                "elapsed_seconds": perf_counter() - started_at,
                "output": str(colmap_output),
                "mask_profile": profile.name,
                "stereo_fusion_masks": colmap_config.stereo_fusion_mask_path is not None,
            },
        )

        jobs.update(
            scan_id,
            status="processing",
            stage="meshing",
            message="Applying the reviewed region and reconstructing the textured mesh.",
        )
        auto_masks = report.reconstruction_scope is not None
        mask_path = (
            dense_mask_path
            if masks_available and profile.openmvs_densification
            else None
        )

        roi_path = write_openmvs_roi_file(scan_root, region)
        openmvs_config = OpenMVSConfig(
            scope_mode=checkpoint.scope_mode,
            mask_path=mask_path,
            texture_use_masks=(masks_available and profile.openmvs_texturing),
            region_path=roi_path,
        )
        started_at = perf_counter()
        textured_mesh = run_openmvs_pipeline(scan_root, openmvs_config)
        dense_dir = scan_root / "dense"
        unscoped_budget = inspect_ply_point_budget(
            dense_dir / "scene_dense_unscoped.ply",
            warning_limit=openmvs_config.point_warning_limit,
            hard_limit=openmvs_config.point_hard_limit,
        )
        scoped_verification = verify_point_cloud_in_region(
            dense_dir / "scene_dense.ply",
            region,
            point_hard_limit=openmvs_config.point_hard_limit,
        )
        mesh_verification = verify_point_cloud_in_region(
            dense_dir / "scene_mesh.ply",
            region,
            point_hard_limit=openmvs_config.point_hard_limit,
        )
        application_path = record_region_application(
            scan_root,
            region,
            roi_path=roi_path,
            unscoped_point_count=unscoped_budget.point_count,
            scoped_verification=scoped_verification,
            mesh_verification=mesh_verification,
        )
        package.record_processing_step(
            "openmvs",
            {
                "elapsed_seconds": perf_counter() - started_at,
                "output": str(textured_mesh),
                "settings": openmvs_config.report_settings(),
                "unscoped_density_budget": unscoped_budget.as_dict(),
                "scoped_dense_verification": scoped_verification.as_dict(),
                "mesh_verification": mesh_verification.as_dict(),
                "automatic_mask_conversion": auto_masks,
            },
        )
        outputs = {
            "colmap_output": str(colmap_output),
            "openmvs_unscoped_dense_point_cloud": str(dense_dir / "scene_dense_unscoped.ply"),
            "openmvs_dense_point_cloud": str(dense_dir / "scene_dense.ply"),
            "textured_mesh": str(textured_mesh),
            "reconstruction_region": str(scan_root / "metadata" / "reconstruction_region.json"),
            "reconstruction_region_application": str(application_path),
            "scan_report": str(package.report_path),
        }

        jobs.update(
            scan_id,
            status="processing",
            stage="exporting",
            message="Preparing Blender-friendly outputs.",
        )
        export_blender_formats(scan_root)
        validate_and_report_scan(scan_root)
        completed_target = COMPLETED_DIR / scan_id
        rebased_outputs = rebase_output_paths(
            outputs,
            old_root=processing_dir,
            new_root=completed_target,
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
            message="Reviewed-region reconstruction completed.",
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
