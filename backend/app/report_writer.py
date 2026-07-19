"""Write scan diagnostics for capture packages and reconstruction outputs."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.scan_validator import ScanValidationReport


def write_scan_report(
    scan_dir: Path,
    validation: ScanValidationReport,
    *,
    output_path: Path | None = None,
) -> Path:
    """Create a scan_report.json with capture and reconstruction diagnostics."""
    scan_dir = scan_dir.resolve()
    output_path = output_path or scan_dir / "metadata" / "scan_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    frames = _read_json(scan_dir / "metadata" / "frames.json", fallback=[])
    session = _read_json(scan_dir / "metadata" / "session.json", fallback={})
    manifest = _read_json(scan_dir / "metadata" / "manifest.json", fallback={})
    processing = _read_json(scan_dir / "metadata" / "processing.json", fallback={})
    video_metadata = _read_json(scan_dir / "metadata" / "video.json", fallback=[])

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scan_id": validation.scan_id,
        "scan_dir": str(scan_dir),
        "manifest": manifest_summary(manifest),
        "capture": capture_summary(frames, session, validation, video_metadata),
        "package_integrity": package_integrity_summary(validation),
        "object_scan": object_scan_summary(validation),
        "processing": processing_summary(processing),
        "reconstruction": reconstruction_summary(scan_dir),
    }
    report["warnings"] = [
        *capture_warnings(report["capture"], report["object_scan"]),
        *validation.integrity_warnings,
    ]

    output_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    return output_path


def capture_summary(
    frames: list[dict[str, Any]],
    session: dict[str, Any],
    validation: ScanValidationReport,
    video_metadata: list[dict[str, Any]],
) -> dict[str, Any]:
    blur_scores = _number_values(frame.get("blur_score") for frame in frames)
    movement_speeds = _number_values(frame.get("movement_speed_meters_per_second") for frame in frames)
    movement_deltas = _number_values(frame.get("movement_delta_meters") for frame in frames)
    rotations = _number_values(frame.get("rotation_delta_degrees") for frame in frames)
    tracking_states = _histogram(frame.get("tracking_state") for frame in frames)
    resolutions = _histogram(_resolution_key(frame.get("resolution")) for frame in frames)

    return {
        "scan_mode": validation.scan_mode,
        "image_count": validation.image_count,
        "frame_count": validation.frame_count,
        "video_count": validation.video_count,
        "video_metadata_count": len(video_metadata),
        "device": session.get("device"),
        "app_version": session.get("app_version"),
        "build_version": session.get("build_version"),
        "imu_sample_count": session.get("imu_sample_count"),
        "session_video_count": session.get("video_count"),
        "uses_lidar": session.get("uses_lidar"),
        "uses_arkit_mesh": session.get("uses_arkit_mesh"),
        "capture_duration_seconds": session.get("capture_duration_seconds"),
        "scene_coverage": session.get("scene_coverage"),
        "rejected_frame_count": session.get("rejected_frame_count"),
        "rejected_tracking_count": session.get("rejected_tracking_count"),
        "rejected_blur_count": session.get("rejected_blur_count"),
        "rejected_motion_count": session.get("rejected_motion_count"),
        "blur": _stats(blur_scores),
        "movement_speed_meters_per_second": _stats(movement_speeds),
        "movement_delta_meters": _stats(movement_deltas),
        "rotation_delta_degrees": _stats(rotations),
        "tracking_states": tracking_states,
        "resolutions": resolutions,
    }


def object_scan_summary(validation: ScanValidationReport) -> dict[str, Any]:
    has_object_center = validation.object_center_world is not None
    has_object_radius = validation.object_radius_meters is not None

    return {
        "is_object_scan": validation.scan_mode == "object_scan",
        "object_center_world": validation.object_center_world,
        "object_radius_meters": validation.object_radius_meters,
        "ready_for_manual_radius_crop": has_object_center and has_object_radius,
        "automatic_crop_status": (
            "needs_arkit_to_colmap_alignment"
            if has_object_center and has_object_radius
            else "missing_object_center_or_radius"
        ),
    }


def package_integrity_summary(validation: ScanValidationReport) -> dict[str, Any]:
    return {
        "validated_image_references": validation.frame_count,
        "validated_video_references": validation.video_metadata_count,
        "session_image_count": validation.session_image_count,
        "session_video_count": validation.session_video_count,
        "warnings": list(validation.integrity_warnings),
        "mask_authoring": validation.mask_authoring,
    }


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest.get("schema_version"),
        "file_counts": manifest.get("file_counts", {}),
        "sensors": manifest.get("sensors", {}),
    }


def processing_summary(processing: dict[str, Any]) -> dict[str, Any]:
    steps = processing.get("steps")
    return {
        "updated_at": processing.get("updated_at"),
        "steps": steps if isinstance(steps, dict) else {},
    }


def reconstruction_summary(scan_dir: Path) -> dict[str, Any]:
    sparse_model = scan_dir / "sparse" / "0"
    dense_point_cloud = scan_dir / "dense" / "fused.ply"
    textured_obj = scan_dir / "dense" / "scene_textured.obj"

    return {
        "sparse_model_exists": sparse_model.is_dir(),
        "sparse_point_count": _ply_vertex_count(scan_dir / "sparse" / "sparse_points.ply"),
        "dense_point_cloud_exists": dense_point_cloud.is_file(),
        "dense_point_count": _ply_vertex_count(dense_point_cloud),
        "textured_mesh_exists": textured_obj.is_file(),
    }


def capture_warnings(capture: dict[str, Any], object_scan: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    frame_count = capture.get("frame_count") or 0
    rejected_count = capture.get("rejected_frame_count")
    rejected_tracking_count = capture.get("rejected_tracking_count")
    rejected_blur_count = capture.get("rejected_blur_count")
    blur_mean = capture.get("blur", {}).get("mean")
    blur_min = capture.get("blur", {}).get("min")
    speed_max = capture.get("movement_speed_meters_per_second", {}).get("max")
    scene_coverage = capture.get("scene_coverage")

    if frame_count < 40:
        warnings.append("low_frame_count")
    if (
        isinstance(rejected_count, int)
        and isinstance(rejected_tracking_count, int)
        and frame_count > 0
        and rejected_tracking_count > frame_count
    ):
        warnings.append("tracking_loss_during_capture")
    if (
        isinstance(rejected_count, int)
        and isinstance(rejected_blur_count, int)
        and frame_count > 0
        and rejected_blur_count > max(10, frame_count // 4)
    ):
        warnings.append("many_blurry_rejected_frames")
    if isinstance(blur_mean, (int, float)) and blur_mean < 0.28:
        warnings.append("low_average_blur_score")
    if isinstance(blur_min, (int, float)) and blur_min < 0.18:
        warnings.append("very_blurry_accepted_frames")
    if isinstance(speed_max, (int, float)) and speed_max > 0.75:
        warnings.append("fast_camera_motion")
    if capture.get("scan_mode") == "scene_scan" and isinstance(scene_coverage, dict):
        coverage_score = scene_coverage.get("score")
        disconnected_jump_count = scene_coverage.get("disconnected_jump_count")
        if (
            isinstance(coverage_score, (int, float))
            and math.isfinite(coverage_score)
            and coverage_score < 0.55
        ):
            warnings.append("low_scene_coverage")
        if (
            isinstance(disconnected_jump_count, int)
            and not isinstance(disconnected_jump_count, bool)
            and disconnected_jump_count > 0
        ):
            warnings.append("disconnected_scene_passes")
    if object_scan["is_object_scan"] and not object_scan["ready_for_manual_radius_crop"]:
        warnings.append("object_scan_missing_subject_tap")

    return warnings


def _read_json(path: Path, *, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def _number_values(values: Any) -> list[float]:
    result: list[float] = []
    for value in values:
        if isinstance(value, (int, float)):
            result.append(float(value))
    return result


def _stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
        }

    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def _histogram(values: Any) -> dict[str, int]:
    histogram: dict[str, int] = {}
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        histogram[value] = histogram.get(value, 0) + 1
    return histogram


def _resolution_key(value: Any) -> str | None:
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, int) for item in value)
    ):
        return f"{value[0]}x{value[1]}"
    return None


def _ply_vertex_count(path: Path) -> int | None:
    if not path.is_file():
        return None

    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            for line in file:
                line = line.strip()
                if line.startswith("element vertex "):
                    return int(line.rsplit(" ", 1)[1])
                if line == "end_header":
                    return None
    except OSError:
        return None

    return None
