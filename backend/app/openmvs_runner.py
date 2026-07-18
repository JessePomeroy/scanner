"""OpenMVS command runner for textured mesh reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any, Literal

from app.density_budget import PointCloudBudgetResult, inspect_ply_point_budget
from app.mask_processor import (
    MaskValidationResult,
    stage_openmvs_texture_masks,
    validate_openmvs_masks,
)


OpenMVSScopeMode = Literal["auto_roi", "unbounded"]


@dataclass(frozen=True)
class OpenMVSConfig:
    interface_colmap: str = "InterfaceCOLMAP"
    densify_point_cloud: str = "DensifyPointCloud"
    reconstruct_mesh: str = "ReconstructMesh"
    refine_mesh: str = "RefineMesh"
    texture_mesh: str = "TextureMesh"
    scope_mode: OpenMVSScopeMode = "auto_roi"
    resolution_level: int = 1
    max_resolution: int = 2560
    number_views: int = 8
    number_views_fuse: int = 3
    filter_point_cloud: int = 1
    estimate_roi: float = 1.1
    roi_border: float = 10.0
    mask_path: Path | None = None
    mask_ignore_label: int = 0
    texture_use_masks: bool = False
    region_path: Path | None = None
    include_refine: bool = False
    point_warning_limit: int = 2_000_000
    point_hard_limit: int = 10_000_000

    def __post_init__(self) -> None:
        if self.scope_mode not in {"auto_roi", "unbounded"}:
            raise ValueError(f"Unsupported OpenMVS scope mode: {self.scope_mode}")
        if self.resolution_level < 0:
            raise ValueError("OpenMVS resolution_level must be non-negative")
        if self.max_resolution <= 0:
            raise ValueError("OpenMVS max_resolution must be positive")
        if self.number_views < 0:
            raise ValueError("OpenMVS number_views must be non-negative")
        if self.number_views_fuse < 1:
            raise ValueError("OpenMVS number_views_fuse must be at least 1")
        if self.filter_point_cloud < 0:
            raise ValueError("OpenMVS filter_point_cloud must be non-negative")
        if self.estimate_roi < 0:
            raise ValueError("OpenMVS estimate_roi must be non-negative")
        if self.roi_border < 0:
            raise ValueError("OpenMVS roi_border must be non-negative")
        if not 0 <= self.mask_ignore_label <= 255:
            raise ValueError("OpenMVS mask_ignore_label must be between 0 and 255")
        if self.point_warning_limit < 1:
            raise ValueError("OpenMVS point_warning_limit must be positive")
        if self.point_hard_limit < self.point_warning_limit:
            raise ValueError("OpenMVS point_hard_limit must be at least point_warning_limit")

    def report_settings(self) -> dict[str, Any]:
        """Return stable, JSON-safe settings for reconstruction reports."""
        automatic_roi = self.scope_mode == "auto_roi" and self.region_path is None
        return {
            "scope_mode": self.scope_mode,
            "resolution_level": self.resolution_level,
            "max_resolution": self.max_resolution,
            "number_views": self.number_views,
            "number_views_fuse": self.number_views_fuse,
            "filter_point_cloud": self.filter_point_cloud,
            "estimate_roi": self.estimate_roi if automatic_roi else 0,
            "crop_to_roi": automatic_roi or self.region_path is not None,
            "roi_border": self.roi_border if automatic_roi else 0,
            "mask_path": str(self.mask_path.resolve()) if self.mask_path is not None else None,
            "mask_ignore_label": self.mask_ignore_label if self.mask_path is not None else None,
            "texture_use_masks": self.texture_use_masks,
            "region_path": str(self.region_path.resolve()) if self.region_path is not None else None,
            "region_method": "openmvs_oriented_box" if self.region_path is not None else None,
            "include_refine": self.include_refine,
            "point_warning_limit": self.point_warning_limit,
            "point_hard_limit": self.point_hard_limit,
        }


def run_command(command: list[str], cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def build_openmvs_commands(scan_dir: Path, config: OpenMVSConfig | None = None) -> list[list[str]]:
    """Build an OpenMVS dense, scoped mesh, and texture command sequence."""
    config = config or OpenMVSConfig()
    dense_dir = scan_dir / "dense"
    scene = dense_dir / "scene.mvs"
    dense_scene = dense_dir / "scene_dense.mvs"
    unscoped_dense_scene = dense_dir / "scene_dense_unscoped.mvs"
    unscoped_dense_cloud = dense_dir / "scene_dense_unscoped.ply"
    mesh_scene = dense_dir / "scene_mesh.mvs"
    refined_scene = dense_dir / "scene_mesh_refined.mvs"
    mesh_file = dense_dir / "scene_mesh.ply"
    refined_mesh_file = dense_dir / "scene_mesh_refined.ply"
    textured_scene = dense_dir / "scene_textured.mvs"

    densify_output = unscoped_dense_scene if config.region_path is not None else dense_scene
    densify = [
        config.densify_point_cloud,
        str(scene),
        "-o",
        str(densify_output),
        "--resolution-level",
        str(config.resolution_level),
        "--max-resolution",
        str(config.max_resolution),
        "--number-views",
        str(config.number_views),
        "--number-views-fuse",
        str(config.number_views_fuse),
        "--filter-point-cloud",
        str(config.filter_point_cloud),
        "--estimate-roi",
        str(config.estimate_roi if config.scope_mode == "auto_roi" and config.region_path is None else 0),
        "--crop-to-roi",
        "1" if config.scope_mode == "auto_roi" and config.region_path is None else "0",
        "--roi-border",
        str(config.roi_border if config.scope_mode == "auto_roi" and config.region_path is None else 0),
    ]
    if config.mask_path is not None:
        densify.extend(
            [
                "--mask-path",
                str(config.mask_path.resolve()),
                "--ignore-mask-label",
                str(config.mask_ignore_label),
            ]
        )

    commands = [
        [
            config.interface_colmap,
            "-i",
            str(dense_dir),
            "-o",
            str(scene),
        ],
        densify,
    ]
    if config.region_path is not None:
        region_path = config.region_path.resolve()
        commands.append(
            [
                config.densify_point_cloud,
                str(unscoped_dense_scene),
                "--pointcloud-file",
                str(unscoped_dense_cloud),
                "--crop-roi-file",
                str(region_path),
                "-o",
                str(dense_scene),
            ]
        )
        commands.append(
            [
                config.reconstruct_mesh,
                str(unscoped_dense_scene),
                "--pointcloud-file",
                str(dense_dir / "scene_dense.ply"),
                "--import-roi-file",
                str(region_path),
                "--integrate-only-roi",
                "1",
                "--crop-to-roi",
                "1",
                "--roi-border",
                "0",
                "-o",
                str(mesh_scene),
            ]
        )
    else:
        commands.append(
            [
                config.reconstruct_mesh,
                str(dense_scene),
                "-o",
                str(mesh_scene),
            ]
        )

    texture_input_scene = mesh_scene
    texture_input_mesh = mesh_file
    if config.include_refine:
        commands.append(
            [
                config.refine_mesh,
                str(mesh_scene),
                "-o",
                str(refined_scene),
            ]
        )
        texture_input_scene = refined_scene
        texture_input_mesh = refined_mesh_file

    texture = [
            config.texture_mesh,
            str(texture_input_scene),
            "-m",
            str(texture_input_mesh),
            "-o",
            str(textured_scene),
            "--export-type",
            "obj",
        ]
    if config.texture_use_masks:
        texture.extend(["--ignore-mask-label", str(config.mask_ignore_label)])
    commands.append(texture)
    return commands


def run_openmvs_pipeline(scan_dir: Path, config: OpenMVSConfig | None = None) -> Path:
    """Run the OpenMVS mesh and texturing pipeline."""
    scan_dir = scan_dir.resolve()
    dense_dir = scan_dir / "dense"

    config = config or OpenMVSConfig()
    validate_openmvs_config_masks(scan_dir, config)
    if config.region_path is not None:
        validate_openmvs_region_capabilities(config)
    for command in build_openmvs_commands(scan_dir, config):
        if command[0] == config.texture_mesh and config.texture_use_masks:
            if config.mask_path is None:
                raise RuntimeError("OpenMVS texture masks require a validated mask path")
            stage_openmvs_texture_masks(config.mask_path, dense_dir / "images")
        # InterfaceCOLMAP stores image paths relative to the COLMAP dense
        # workspace. Every later OpenMVS command must resolve those paths from
        # the same directory rather than from the backend process directory.
        run_command(command, cwd=dense_dir)
        completed_densify = config.region_path is None or "--crop-roi-file" in command
        if command[0] == config.densify_point_cloud and completed_densify:
            inspect_openmvs_dense_cloud(scan_dir, config)

    return dense_dir / "scene_textured.obj"


def validate_openmvs_region_capabilities(config: OpenMVSConfig) -> None:
    """Fail before dense work when pinned manual-ROI command options are absent."""
    probes = (
        [config.densify_point_cloud, "--help", "--pointcloud-file", "probe.ply", "--crop-roi-file", "probe.roi"],
        [config.reconstruct_mesh, "--help", "--pointcloud-file", "probe.ply", "--import-roi-file", "probe.roi", "--integrate-only-roi", "1"],
    )
    for probe in probes:
        result = subprocess.run(probe, capture_output=True, text=True, check=False)
        diagnostic = f"{result.stdout}\n{result.stderr}".lower()
        if "unrecognized option" in diagnostic or "unrecognised option" in diagnostic or "unknown option" in diagnostic:
            raise RuntimeError(
                f"{probe[0]} does not support the manual reconstruction-region options required by this job"
            )


def validate_openmvs_config_masks(
    scan_dir: Path,
    config: OpenMVSConfig,
) -> MaskValidationResult | None:
    """Validate configured masks against COLMAP's undistorted images."""
    if config.mask_path is None:
        return None
    return validate_openmvs_masks(config.mask_path, scan_dir.resolve() / "dense" / "images")


def inspect_openmvs_dense_cloud(
    scan_dir: Path,
    config: OpenMVSConfig | None = None,
) -> PointCloudBudgetResult:
    """Inspect the OpenMVS dense PLY without loading its point payload."""
    config = config or OpenMVSConfig()
    return inspect_ply_point_budget(
        scan_dir.resolve() / "dense" / "scene_dense.ply",
        warning_limit=config.point_warning_limit,
        hard_limit=config.point_hard_limit,
    )
