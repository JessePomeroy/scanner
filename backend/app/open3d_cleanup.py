"""Open3D cleanup helpers for reconstruction outputs."""

from __future__ import annotations

from pathlib import Path

from app.point_cloud_processor import PointCloudProcessingConfig, process_point_cloud


def cleanup_outputs(scan_dir: Path) -> Path:
    """Clean intermediate reconstruction outputs."""
    fused_path = scan_dir / "dense" / "fused.ply"
    if not fused_path.exists():
        raise FileNotFoundError(f"Missing dense point cloud: {fused_path}")

    cleaned_path = scan_dir / "dense" / "fused_cleaned.ply"
    return process_point_cloud(
        fused_path,
        cleaned_path,
        PointCloudProcessingConfig(
            processor="open3d",
            estimate_normals=True,
            statistical_outlier_neighbors=20,
            statistical_outlier_std_ratio=2.0,
        ),
    )
