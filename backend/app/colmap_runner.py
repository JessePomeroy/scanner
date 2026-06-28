"""COLMAP command runner for local reconstruction jobs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class ColmapConfig:
    executable: str = "colmap"
    matcher: str = "exhaustive_matcher"
    single_camera: bool = True
    use_gpu: bool = True
    geometric_consistency: bool = True


def run_command(command: list[str], cwd: Path | None = None) -> None:
    """Run a reconstruction command and fail on non-zero exit."""
    subprocess.run(command, cwd=cwd, check=True)


def build_colmap_commands(scan_dir: Path, config: ColmapConfig | None = None) -> list[list[str]]:
    """Build the COLMAP command sequence for a scan directory."""
    config = config or ColmapConfig()
    image_path = scan_dir / "images"
    database_path = scan_dir / "database.db"
    sparse_path = scan_dir / "sparse"
    dense_path = scan_dir / "dense"

    feature_extractor = [
        config.executable,
        "feature_extractor",
        "--database_path",
        str(database_path),
        "--image_path",
        str(image_path),
        "--ImageReader.single_camera",
        "1" if config.single_camera else "0",
        "--SiftExtraction.use_gpu",
        "1" if config.use_gpu else "0",
    ]

    matcher = [
        config.executable,
        config.matcher,
        "--database_path",
        str(database_path),
        "--SiftMatching.use_gpu",
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
        feature_extractor,
        matcher,
        mapper,
        image_undistorter,
        patch_match_stereo,
        stereo_fusion,
    ]


def run_colmap_pipeline(scan_dir: Path, config: ColmapConfig | None = None) -> Path:
    """Run the COLMAP pipeline for an extracted scan package."""
    scan_dir = scan_dir.resolve()
    (scan_dir / "sparse").mkdir(exist_ok=True)
    (scan_dir / "dense").mkdir(exist_ok=True)

    for command in build_colmap_commands(scan_dir, config):
        run_command(command)

    return scan_dir / "dense" / "fused.ply"
