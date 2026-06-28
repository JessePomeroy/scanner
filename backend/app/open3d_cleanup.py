"""Open3D cleanup helpers for reconstruction outputs."""

from __future__ import annotations

from pathlib import Path


def cleanup_outputs(scan_dir: Path) -> Path:
    """Clean intermediate reconstruction outputs."""
    try:
        import open3d as o3d
    except ImportError as error:
        raise RuntimeError("open3d is required for cleanup_outputs") from error

    fused_path = scan_dir / "dense" / "fused.ply"
    if not fused_path.exists():
        raise FileNotFoundError(f"Missing dense point cloud: {fused_path}")

    pcd = o3d.io.read_point_cloud(str(fused_path))
    pcd, _ = pcd.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    pcd.estimate_normals()

    cleaned_path = scan_dir / "dense" / "fused_cleaned.ply"
    o3d.io.write_point_cloud(str(cleaned_path), pcd)
    return cleaned_path
