"""Automatic one-box object reconstruction with measured, bounded native presets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
import subprocess
from time import perf_counter
from typing import Callable, Literal

from app.colmap_runner import (
    ColmapConfig, build_colmap_dense_commands, prepare_colmap_output_directories,
    prepare_colmap_sparse_execution, record_colmap_intake,
)
from app.heavy_work import guarded, native_kwargs
from app.mask_authoring import load_mask_authoring_plan, single_box_selection
from app.mask_generator import MaskGenerationError, generate_mask_proposals
from app.mask_processor import stage_openmvs_texture_masks
from app.mask_review import accept_automatic_masks
from app.mask_undistorter import convert_capture_mask_set
from app.openmvs_runner import (
    OpenMVSConfig, build_openmvs_commands, inspect_openmvs_dense_cloud, validate_openmvs_config_masks,
)
from app.scan_metadata import load_scan_metadata
from app.scan_package import PreparedScanPackage, validate_and_report_scan
from app.texture_quality import write_texture_report


ObjectPresetName = Literal["preview", "detail"]


@dataclass(frozen=True)
class ObjectPreset:
    prepared_image_max_size: int
    dense_max_resolution: int
    point_warning_limit: int
    point_hard_limit: int
    time_limit_seconds: int


OBJECT_PRESETS: dict[ObjectPresetName, ObjectPreset] = {
    "preview": ObjectPreset(1600, 960, 200_000, 1_000_000, 1800),
    "detail": ObjectPreset(3200, 1920, 1_000_000, 3_000_000, 2700),
}


@guarded("Automatic single-box object reconstruction")
def reconstruct_object(
    package: PreparedScanPackage,
    preset_name: ObjectPresetName,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Generate/validate masks, align the scene, then reconstruct only the selected object.

    No human approval or hand-drawn 3D region is implied. All original photos are
    retained, including unregistered views. GPU worker memory is released before
    native reconstruction; service-level memory caps are configured separately.
    """
    preset = OBJECT_PRESETS[preset_name]
    root = package.scan_root
    report = package.validation
    if report.scan_mode != "object_scan":
        raise ValueError("Automatic object presets require an Object scan")
    if report.reconstruction_scope is not None:
        raise ValueError("Single-box processing requires a new mask draft without active capture masks; existing masks are not overwritten")
    if report.image_count < 3:
        raise ValueError("Object reconstruction requires at least three photos")
    metadata = load_scan_metadata(root / "metadata")
    plan = load_mask_authoring_plan(root / "metadata", metadata.frames)
    if plan is None:
        raise ValueError("Automatic object processing requires a single_box selection")
    single_box_selection(plan)
    for path in (root / "database.db", root / "sparse", root / "dense", root / "object_logs"):
        if path.exists() or path.is_symlink():
            raise ValueError("Automatic object processing requires a fresh reconstruction workspace")
    started = perf_counter()
    deadline = started + preset.time_limit_seconds
    notify = progress or (lambda message: None)
    notify("Tracking the selected object through the capture with SAM2.")
    generation = generate_mask_proposals(root, metadata.frames)
    if generation is None:
        raise MaskGenerationError("SAM2 did not generate a mask set")
    decision = accept_automatic_masks(root)
    package = validate_and_report_scan(root)
    package.record_processing_step("mask_generation", {
        "state":decision["state"], "generator":generation.generator,
        "human_reviewed":False, "source_authoring_revision":generation.source_revision,
        "frame_count":len(generation.frames), "elapsed_seconds":perf_counter()-started,
    })
    outputs = {"mask_generation_report":str(generation.report_path)}
    for index, path in enumerate(generation.review_masks):
        outputs[f"mask_review_{index}"] = str(root / path)
    logs = root / "object_logs"
    logs.mkdir()
    commands: list[dict[str, object]] = []
    settings = {
        "preset":preset_name, **asdict(preset), "cpu_threads":4,
        "feature_max_image_size":1600, "feature_max_count":4096,
        "number_views":4, "number_views_fuse":2, "maximum_texture_size":4096,
        "photo_selection":"all_capture_photos", "mask_policy":"single_box_automatic_v1",
        "camera_alignment":"scene_geometry", "memory_limit":"configured by service, not this Python function",
    }

    def execute(phase: str, command: list[str], cwd: Path = root) -> None:
        remaining = deadline-perf_counter()
        if remaining <= 0:
            raise TimeoutError("Object reconstruction exceeded its time limit")
        notify(f"Object reconstruction: {phase}.")
        record: dict[str, object] = {"phase":phase, "argv":command, "state":"running"}
        commands.append(record)
        package.record_processing_step("automatic_object", {"settings":settings, "commands":commands})
        phase_started = perf_counter()
        with (logs / f"{len(commands):02d}-{phase}.log").open("x") as log:
            try:
                result = subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=remaining, **native_kwargs())
            except subprocess.TimeoutExpired as error:
                record.update(state="timed_out", elapsed_seconds=perf_counter()-phase_started)
                package.record_processing_step("automatic_object", {"settings":settings, "commands":commands})
                raise TimeoutError(f"Object {phase} exceeded the reconstruction time limit") from error
        record.update(state="succeeded" if result.returncode == 0 else "failed",
                      returncode=result.returncode, elapsed_seconds=perf_counter()-phase_started)
        package.record_processing_step("automatic_object", {"settings":settings, "commands":commands})
        if result.returncode:
            raise RuntimeError(f"Object {phase} failed; diagnostics preserved in object_logs")

    colmap = ColmapConfig(matcher="exhaustive_matcher", use_gpu=True)
    prepare_colmap_output_directories(root)
    sparse, groups = prepare_colmap_sparse_execution(root, colmap)
    for index, command in enumerate(sparse[:-2]):
        execute(f"features-{index+1}", command + ["--FeatureExtraction.num_threads","4",
                "--FeatureExtraction.max_image_size","1600", "--SiftExtraction.max_num_features","4096"])
    record_colmap_intake(root, groups, colmap)
    execute("matching", sparse[-2] + ["--FeatureMatching.num_threads","4"])
    execute("mapping", sparse[-1] + ["--Mapper.num_threads","4", "--Mapper.multiple_models","0"])
    intake = record_colmap_intake(root, groups, colmap, include_registration=True)
    if intake["registered_image_count"] < max(3, math.ceil(report.image_count*.8)):
        raise RuntimeError("Too few photos aligned for automatic object reconstruction; original capture is preserved")
    execute("undistortion", build_colmap_dense_commands(root,colmap)[0] +
            ["--max_image_size",str(preset.prepared_image_max_size), "--num_threads","4"])

    def export_cameras(source: Path, destination: Path) -> None:
        execute("mask-camera-export", [colmap.executable,"model_converter","--input_path",str(source),
                "--output_path",str(destination),"--output_type","TXT"])

    notify("Aligning generated masks to the reconstructed cameras.")
    conversion = convert_capture_mask_set(root, colmap_executable=colmap.executable, model_exporter=export_cameras)
    package.record_processing_step("mask_conversion", conversion.as_dict())
    config = OpenMVSConfig(scope_mode="auto_roi", resolution_level=0,
        max_resolution=preset.dense_max_resolution, number_views=4, number_views_fuse=2,
        mask_path=root/"dense/masks", texture_use_masks=True,
        point_warning_limit=preset.point_warning_limit, point_hard_limit=preset.point_hard_limit)
    validate_openmvs_config_masks(root, config)
    for command in build_openmvs_commands(root, config):
        phase = command[0]
        if phase == config.reconstruct_mesh:
            budget = inspect_openmvs_dense_cloud(root, config)
            package.record_processing_step("density_budget", budget.as_dict())
        if phase == config.texture_mesh:
            stage_openmvs_texture_masks(root/"dense/masks", root/"dense/images")
            command += ["--max-texture-size","4096"]
        execute(phase, command + ["--max-threads","4"], cwd=root/"dense")
    mesh = root / "dense/scene_textured.obj"
    quality = write_texture_report(mesh, root/"dense/texture_quality.json")
    package.record_processing_step("automatic_object", {
        "state":"succeeded", "settings":settings, "commands":commands,
        "elapsed_seconds":perf_counter()-started, "openmvs":config.report_settings(),
        "texture_quality":quality,
    })
    outputs.update(textured_mesh=str(mesh), openmvs_dense_point_cloud=str(root/"dense/scene_dense.ply"),
                   colmap_intake=str(root/"metadata/colmap_intake.json"),
                   processing_report=str(root/"metadata/processing.json"))
    return outputs
