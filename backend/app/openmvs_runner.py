"""OpenMVS command runner for textured mesh reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any, Literal


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
    include_refine: bool = False

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

    def report_settings(self) -> dict[str, Any]:
        """Return stable, JSON-safe settings for reconstruction reports."""
        return {
            "scope_mode": self.scope_mode,
            "resolution_level": self.resolution_level,
            "max_resolution": self.max_resolution,
            "number_views": self.number_views,
            "number_views_fuse": self.number_views_fuse,
            "filter_point_cloud": self.filter_point_cloud,
            "estimate_roi": self.estimate_roi if self.scope_mode == "auto_roi" else 0,
            "crop_to_roi": self.scope_mode == "auto_roi",
            "roi_border": self.roi_border if self.scope_mode == "auto_roi" else 0,
            "mask_path": str(self.mask_path.resolve()) if self.mask_path is not None else None,
            "include_refine": self.include_refine,
        }


def run_command(command: list[str], cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def build_openmvs_commands(scan_dir: Path, config: OpenMVSConfig | None = None) -> list[list[str]]:
    """Build an OpenMVS dense, scoped mesh, and texture command sequence."""
    config = config or OpenMVSConfig()
    dense_dir = scan_dir / "dense"
    scene = dense_dir / "scene.mvs"
    dense_scene = dense_dir / "scene_dense.mvs"
    mesh_scene = dense_dir / "scene_mesh.mvs"
    refined_scene = dense_dir / "scene_mesh_refined.mvs"
    mesh_file = dense_dir / "scene_mesh.ply"
    refined_mesh_file = dense_dir / "scene_mesh_refined.ply"
    textured_scene = dense_dir / "scene_textured.mvs"

    densify = [
        config.densify_point_cloud,
        str(scene),
        "-o",
        str(dense_scene),
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
        str(config.estimate_roi if config.scope_mode == "auto_roi" else 0),
        "--crop-to-roi",
        "1" if config.scope_mode == "auto_roi" else "0",
        "--roi-border",
        str(config.roi_border if config.scope_mode == "auto_roi" else 0),
    ]
    if config.mask_path is not None:
        densify.extend(["--mask-path", str(config.mask_path.resolve())])

    commands = [
        [
            config.interface_colmap,
            "-i",
            str(dense_dir),
            "-o",
            str(scene),
        ],
        densify,
        [
            config.reconstruct_mesh,
            str(dense_scene),
            "-o",
            str(mesh_scene),
        ],
    ]

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

    commands.append(
        [
            config.texture_mesh,
            str(texture_input_scene),
            "-m",
            str(texture_input_mesh),
            "-o",
            str(textured_scene),
            "--export-type",
            "obj",
        ]
    )
    return commands


def run_openmvs_pipeline(scan_dir: Path, config: OpenMVSConfig | None = None) -> Path:
    """Run the OpenMVS mesh and texturing pipeline."""
    scan_dir = scan_dir.resolve()
    dense_dir = scan_dir / "dense"

    for command in build_openmvs_commands(scan_dir, config):
        # InterfaceCOLMAP stores image paths relative to the COLMAP dense
        # workspace. Every later OpenMVS command must resolve those paths from
        # the same directory rather than from the backend process directory.
        run_command(command, cwd=dense_dir)

    return dense_dir / "scene_textured.obj"
