"""OpenMVS command runner for textured mesh reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class OpenMVSConfig:
    interface_colmap: str = "InterfaceCOLMAP"
    densify_point_cloud: str = "DensifyPointCloud"
    reconstruct_mesh: str = "ReconstructMesh"
    refine_mesh: str = "RefineMesh"
    texture_mesh: str = "TextureMesh"


def run_command(command: list[str], cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def build_openmvs_commands(scan_dir: Path, config: OpenMVSConfig | None = None) -> list[list[str]]:
    """Build a conventional OpenMVS command sequence from COLMAP dense output."""
    config = config or OpenMVSConfig()
    dense_dir = scan_dir / "dense"
    scene = dense_dir / "scene.mvs"
    dense_scene = dense_dir / "scene_dense.mvs"
    mesh_scene = dense_dir / "scene_mesh.mvs"
    refined_scene = dense_dir / "scene_mesh_refined.mvs"
    textured_scene = dense_dir / "scene_textured.mvs"

    return [
        [
            config.interface_colmap,
            "-i",
            str(dense_dir),
            "-o",
            str(scene),
        ],
        [
            config.densify_point_cloud,
            str(scene),
            "-o",
            str(dense_scene),
        ],
        [
            config.reconstruct_mesh,
            str(dense_scene),
            "-o",
            str(mesh_scene),
        ],
        [
            config.refine_mesh,
            str(mesh_scene),
            "-o",
            str(refined_scene),
        ],
        [
            config.texture_mesh,
            str(refined_scene),
            "-o",
            str(textured_scene),
            "--export-type",
            "obj",
        ],
    ]


def run_openmvs_pipeline(scan_dir: Path, config: OpenMVSConfig | None = None) -> Path:
    """Run the OpenMVS mesh and texturing pipeline."""
    scan_dir = scan_dir.resolve()

    for command in build_openmvs_commands(scan_dir, config):
        run_command(command)

    return scan_dir / "dense" / "scene_textured.obj"
