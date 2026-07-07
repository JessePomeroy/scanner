"""Scan package preparation, validation, and reporting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any

from app.report_writer import write_scan_report
from app.scan_validator import ScanValidationReport, find_scan_root, validate_scan_package
from app.storage import safe_extract_zip


@dataclass(frozen=True)
class PreparedScanPackage:
    scan_root: Path
    validation: ScanValidationReport
    report_path: Path

    @property
    def scan_id(self) -> str:
        return self.validation.scan_id or self.scan_root.name

    @property
    def metadata_dir(self) -> Path:
        return self.scan_root / "metadata"

    @property
    def manifest_path(self) -> Path:
        return self.metadata_dir / "manifest.json"

    @property
    def processing_path(self) -> Path:
        return self.metadata_dir / "processing.json"

    def record_processing_step(self, name: str, values: dict[str, Any]) -> Path:
        """Append or replace processing metadata for a named reconstruction step."""
        payload = _read_json_object(self.processing_path)
        steps = payload.setdefault("steps", {})
        steps[name] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **values,
        }
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.processing_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return self.processing_path


def scan_id_from_path(path: Path) -> str:
    """Derive a stable scan id from a zip or directory path."""
    name = path.name
    if name.lower().endswith(".zip"):
        name = name[:-4]
    return name.replace(" ", "_")


def prepare_scan_source(scan: Path, destination: Path, *, reset: bool = True) -> Path:
    """Copy or extract a scan package into destination and return its scan root."""
    if reset and destination.exists():
        shutil.rmtree(destination)

    destination.mkdir(parents=True, exist_ok=True)

    if scan.suffix.lower() == ".zip":
        safe_extract_zip(scan, destination)
    elif scan.is_dir():
        target = destination / scan.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(scan, target)
    else:
        raise ValueError(f"Scan path is not a zip or directory: {scan}")

    return find_scan_root(destination)


def validate_and_report_scan(scan_root: Path) -> PreparedScanPackage:
    """Validate a scan root and write its scan_report.json."""
    validation = validate_scan_package(scan_root)
    write_manifest(scan_root, validation)
    report_path = write_scan_report(scan_root, validation)
    return PreparedScanPackage(
        scan_root=scan_root,
        validation=validation,
        report_path=report_path,
    )


def write_manifest(scan_root: Path, validation: ScanValidationReport) -> Path:
    """Write a stable package manifest for downstream tools."""
    metadata_dir = scan_root / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    session = _read_json_object(metadata_dir / "session.json")
    frames = _read_json_array(metadata_dir / "frames.json")
    video_metadata = _read_json_array(metadata_dir / "video.json")
    image_files = sorted((scan_root / "images").glob("*"))
    depth_files = sorted((scan_root / "depth").glob("*")) if (scan_root / "depth").is_dir() else []
    motion_path = metadata_dir / "imu.json"

    manifest = {
        "schema_version": "0.3.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scan_id": validation.scan_id,
        "scan_mode": validation.scan_mode,
        "app_version": session.get("app_version"),
        "build_version": session.get("build_version"),
        "device": session.get("device"),
        "file_counts": {
            "images": len([path for path in image_files if path.is_file()]),
            "depth": len([path for path in depth_files if path.is_file()]),
            "videos": validation.video_count,
            "frames": len(frames),
            "imu_samples": len(_read_json_array(motion_path)) if motion_path.exists() else 0,
            "video_metadata_entries": len(video_metadata),
        },
        "sensors": {
            "camera": True,
            "arkit_tracking": True,
            "lidar_depth": bool(session.get("uses_lidar")),
            "arkit_mesh": bool(session.get("uses_arkit_mesh")),
            "imu": motion_path.exists(),
            "video": bool(video_metadata) or validation.video_count > 0,
        },
        "object_scan": {
            "object_center_world": validation.object_center_world,
            "object_radius_meters": validation.object_radius_meters,
        },
        "limitations": [
            "depth frames are optional and absent on non-LiDAR devices",
            "video capture is optional and may be absent from photogrammetry-first scans",
            "automatic object crop requires ARKit-to-COLMAP coordinate alignment",
            "dense reconstruction requires a CUDA-capable COLMAP build",
        ],
    }

    path = metadata_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return path


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_json_array(path: Path) -> list[Any]:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []
