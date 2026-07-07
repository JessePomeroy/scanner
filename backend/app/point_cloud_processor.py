"""Optional point-cloud processing backends."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORTED_POINT_CLOUD_PROCESSORS = ("open3d", "threecrate")


@dataclass(frozen=True)
class PointCloudProcessingConfig:
    processor: str = "open3d"
    voxel_size: float | None = None
    estimate_normals: bool = True
    statistical_outlier_neighbors: int | None = 20
    statistical_outlier_std_ratio: float | None = 2.0


def process_point_cloud(input_path: Path, output_path: Path, config: PointCloudProcessingConfig | None = None) -> Path:
    """Process a point cloud with Open3D or ThreeCrate."""
    config = config or PointCloudProcessingConfig()
    _validate_config(config)

    if not input_path.exists():
        raise FileNotFoundError(f"Missing point cloud: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if config.processor == "open3d":
        return _process_with_open3d(input_path, output_path, config)

    if config.processor == "threecrate":
        return _process_with_threecrate(input_path, output_path, config)

    raise AssertionError(f"Unhandled point-cloud processor: {config.processor}")


def build_processing_summary(input_path: Path, output_path: Path, config: PointCloudProcessingConfig) -> dict[str, Any]:
    """Return a JSON-serializable summary without importing optional libraries."""
    _validate_config(config)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "processor": config.processor,
        "input": str(input_path),
        "output": str(output_path),
        "voxel_size": config.voxel_size,
        "estimate_normals": config.estimate_normals,
        "statistical_outlier_neighbors": config.statistical_outlier_neighbors,
        "statistical_outlier_std_ratio": config.statistical_outlier_std_ratio,
        "notes": _processor_notes(config.processor),
    }


def write_processing_report(
    input_path: Path,
    output_path: Path,
    report_path: Path,
    config: PointCloudProcessingConfig,
    *,
    dry_run: bool,
) -> Path:
    payload = {
        **build_processing_summary(input_path, output_path, config),
        "dry_run": dry_run,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return report_path


def _process_with_open3d(input_path: Path, output_path: Path, config: PointCloudProcessingConfig) -> Path:
    try:
        import open3d as o3d
    except ImportError as error:
        raise RuntimeError("open3d is required for --processor open3d. Install it in the active Python env.") from error

    pcd = o3d.io.read_point_cloud(str(input_path))

    if config.voxel_size is not None and config.voxel_size > 0:
        pcd = pcd.voxel_down_sample(voxel_size=config.voxel_size)

    if config.statistical_outlier_neighbors is not None and config.statistical_outlier_std_ratio is not None:
        pcd, _ = pcd.remove_statistical_outlier(
            nb_neighbors=config.statistical_outlier_neighbors,
            std_ratio=config.statistical_outlier_std_ratio,
        )

    if config.estimate_normals:
        pcd.estimate_normals()

    o3d.io.write_point_cloud(str(output_path), pcd)
    return output_path


def _process_with_threecrate(input_path: Path, output_path: Path, config: PointCloudProcessingConfig) -> Path:
    try:
        import threecrate as tc
    except ImportError as error:
        raise RuntimeError(
            "threecrate is required for --processor threecrate. Install it separately with `pip install threecrate`."
        ) from error

    cloud = tc.read_point_cloud(str(input_path))

    if config.voxel_size is not None and config.voxel_size > 0:
        voxel_downsample = getattr(tc, "voxel_downsample", None)
        if voxel_downsample is None:
            raise RuntimeError("threecrate.voxel_downsample is unavailable in the installed ThreeCrate package.")
        cloud = voxel_downsample(cloud, voxel_size=config.voxel_size)

    if config.estimate_normals:
        estimate_normals = getattr(tc, "estimate_normals", None)
        if estimate_normals is None:
            raise RuntimeError("threecrate.estimate_normals is unavailable in the installed ThreeCrate package.")
        cloud = estimate_normals(cloud)

    write_point_cloud = getattr(tc, "write_point_cloud", None)
    if write_point_cloud is None:
        raise RuntimeError("threecrate.write_point_cloud is unavailable in the installed ThreeCrate package.")

    write_point_cloud(cloud, str(output_path))
    return output_path


def _validate_processor(processor: str) -> None:
    if processor not in SUPPORTED_POINT_CLOUD_PROCESSORS:
        supported = ", ".join(SUPPORTED_POINT_CLOUD_PROCESSORS)
        raise ValueError(f"Unsupported point-cloud processor '{processor}'. Supported processors: {supported}")


def _validate_config(config: PointCloudProcessingConfig) -> None:
    _validate_processor(config.processor)
    if (
        config.processor == "threecrate"
        and (config.statistical_outlier_neighbors is not None or config.statistical_outlier_std_ratio is not None)
    ):
        raise ValueError("ThreeCrate processing does not support the Open3D statistical outlier filter yet.")


def _processor_notes(processor: str) -> list[str]:
    if processor == "open3d":
        return [
            "Default cleanup path.",
            "Requires open3d only when processing is executed.",
        ]

    if processor == "threecrate":
        return [
            "Experimental optional processing path.",
            "Install threecrate separately; it is not a required project dependency.",
            "Compare output quality and runtime against Open3D before replacing defaults.",
        ]

    return []
