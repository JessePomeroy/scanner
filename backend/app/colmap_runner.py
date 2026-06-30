"""COLMAP command runner for local reconstruction jobs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class ColmapConfig:
    executable: str = "colmap"
    matcher: str = "sequential_matcher"
    single_camera: bool = True
    use_gpu: bool = True
    geometric_consistency: bool = True


def run_command(command: list[str], cwd: Path | None = None) -> None:
    """Run a reconstruction command and fail on non-zero exit."""
    subprocess.run(command, cwd=cwd, check=True)


def build_colmap_commands(scan_dir: Path, config: ColmapConfig | None = None) -> list[list[str]]:
    """Build the COLMAP command sequence for a scan directory."""
    return build_colmap_sparse_commands(scan_dir, config) + build_colmap_dense_commands(scan_dir, config)


def build_colmap_sparse_commands(scan_dir: Path, config: ColmapConfig | None = None) -> list[list[str]]:
    """Build the COLMAP sparse reconstruction command sequence."""
    config = config or ColmapConfig()
    image_path = scan_dir / "images"
    database_path = scan_dir / "database.db"
    sparse_path = scan_dir / "sparse"

    feature_extractor = [
        config.executable,
        "feature_extractor",
        "--database_path",
        str(database_path),
        "--image_path",
        str(image_path),
        "--ImageReader.single_camera",
        "1" if config.single_camera else "0",
        "--FeatureExtraction.use_gpu",
        "1" if config.use_gpu else "0",
    ]

    matcher = [
        config.executable,
        config.matcher,
        "--database_path",
        str(database_path),
        "--FeatureMatching.use_gpu",
        "1" if config.use_gpu else "0",
    ]

    mapper = [
        config.executable,
        "mapper",
        "--database_path",
        str(database_path),
        "--image_path",
        str(image_path),
        "--output_path",
        str(sparse_path),
    ]

    return [
        feature_extractor,
        matcher,
        mapper,
    ]


def build_colmap_dense_commands(scan_dir: Path, config: ColmapConfig | None = None) -> list[list[str]]:
    """Build the COLMAP dense reconstruction command sequence."""
    config = config or ColmapConfig()
    image_path = scan_dir / "images"
    sparse_path = scan_dir / "sparse"
    dense_path = scan_dir / "dense"

    image_undistorter = [
        config.executable,
        "image_undistorter",
        "--image_path",
        str(image_path),
        "--input_path",
        str(sparse_path / "0"),
        "--output_path",
        str(dense_path),
        "--output_type",
        "COLMAP",
    ]

    patch_match_stereo = [
        config.executable,
        "patch_match_stereo",
        "--workspace_path",
        str(dense_path),
        "--workspace_format",
        "COLMAP",
        "--PatchMatchStereo.geom_consistency",
        "true" if config.geometric_consistency else "false",
    ]

    stereo_fusion = [
        config.executable,
        "stereo_fusion",
        "--workspace_path",
        str(dense_path),
        "--workspace_format",
        "COLMAP",
        "--input_type",
        "geometric" if config.geometric_consistency else "photometric",
        "--output_path",
        str(dense_path / "fused.ply"),
    ]

    return [
        image_undistorter,
        patch_match_stereo,
        stereo_fusion,
    ]


def run_colmap_sparse_pipeline(scan_dir: Path, config: ColmapConfig | None = None) -> Path:
    """Run feature extraction, matching, and sparse mapping."""
    scan_dir = scan_dir.resolve()
    (scan_dir / "sparse").mkdir(exist_ok=True)

    for command in build_colmap_sparse_commands(scan_dir, config):
        run_command(command)

    return scan_dir / "sparse" / "0"


def run_colmap_dense_pipeline(scan_dir: Path, config: ColmapConfig | None = None) -> Path:
    """Run image undistortion, dense stereo, and dense point cloud fusion."""
    scan_dir = scan_dir.resolve()
    (scan_dir / "dense").mkdir(exist_ok=True)

    for command in build_colmap_dense_commands(scan_dir, config):
        run_command(command)

    return scan_dir / "dense" / "fused.ply"


def export_sparse_point_cloud(scan_dir: Path, config: ColmapConfig | None = None) -> Path:
    """Export the sparse COLMAP model to a PLY point cloud."""
    config = config or ColmapConfig()
    scan_dir = scan_dir.resolve()
    sparse_model = scan_dir / "sparse" / "0"
    output_path = scan_dir / "sparse" / "sparse_points.ply"

    run_command(
        [
            config.executable,
            "model_converter",
            "--input_path",
            str(sparse_model),
            "--output_path",
            str(output_path),
            "--output_type",
            "PLY",
        ]
    )

    return output_path


def run_colmap_pipeline(
    scan_dir: Path,
    config: ColmapConfig | None = None,
    *,
    include_dense: bool = True,
) -> Path:
    """Run COLMAP and return the most complete output path produced."""
    run_colmap_sparse_pipeline(scan_dir, config)

    if include_dense:
        return run_colmap_dense_pipeline(scan_dir, config)

    return export_sparse_point_cloud(scan_dir, config)
