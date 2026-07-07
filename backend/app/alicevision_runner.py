"""Experimental direct AliceVision command planner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.reconstruction_plan import CommandPlan


@dataclass(frozen=True)
class AliceVisionConfig:
    executable_prefix: str = "aliceVision_"
    workspace_name: str = "alicevision"
    sensor_database: Path | None = None

    def executable(self, name: str) -> str:
        return f"{self.executable_prefix}{name}"


def build_alicevision_plan(scan_dir: Path, config: AliceVisionConfig | None = None) -> CommandPlan:
    """Build an experimental direct AliceVision photogrammetry command chain.

    Meshroom is the safer production entry point. This planner exists so the
    Windows workstation can dry-run and tune a direct AliceVision path after the
    exact installed binary version is known.
    """
    config = config or AliceVisionConfig()
    scan_dir = scan_dir.resolve()
    image_path = scan_dir / "images"
    workspace = scan_dir / config.workspace_name
    camera_init = workspace / "cameraInit.sfm"
    features = workspace / "features"
    image_matching = workspace / "imageMatching.txt"
    matches = workspace / "matches"
    sfm = workspace / "sfm.abc"
    dense = workspace / "dense"
    depth_maps = workspace / "depthMaps"
    filtered_depth = workspace / "depthMapsFiltered"
    mesh = workspace / "mesh.obj"
    filtered_mesh = workspace / "mesh_filtered.obj"
    textured = workspace / "textured"

    camera_init_command = [
        config.executable("cameraInit"),
        "--imageFolder",
        str(image_path),
        "--output",
        str(camera_init),
    ]
    if config.sensor_database is not None:
        camera_init_command.extend(["--sensorDatabase", str(config.sensor_database)])

    commands = [
        camera_init_command,
        [
            config.executable("featureExtraction"),
            "--input",
            str(camera_init),
            "--output",
            str(features),
        ],
        [
            config.executable("imageMatching"),
            "--input",
            str(camera_init),
            "--featuresFolders",
            str(features),
            "--output",
            str(image_matching),
        ],
        [
            config.executable("featureMatching"),
            "--input",
            str(camera_init),
            "--featuresFolders",
            str(features),
            "--imagePairsList",
            str(image_matching),
            "--output",
            str(matches),
        ],
        [
            config.executable("incrementalSfM"),
            "--input",
            str(camera_init),
            "--featuresFolders",
            str(features),
            "--matchesFolders",
            str(matches),
            "--output",
            str(sfm),
        ],
        [
            config.executable("prepareDenseScene"),
            "--input",
            str(sfm),
            "--output",
            str(dense),
        ],
        [
            config.executable("depthMapEstimation"),
            "--input",
            str(sfm),
            "--imagesFolder",
            str(dense),
            "--output",
            str(depth_maps),
        ],
        [
            config.executable("depthMapFiltering"),
            "--input",
            str(sfm),
            "--depthMapsFolder",
            str(depth_maps),
            "--output",
            str(filtered_depth),
        ],
        [
            config.executable("meshing"),
            "--input",
            str(sfm),
            "--depthMapsFolder",
            str(filtered_depth),
            "--output",
            str(mesh),
        ],
        [
            config.executable("meshFiltering"),
            "--input",
            str(mesh),
            "--output",
            str(filtered_mesh),
        ],
        [
            config.executable("texturing"),
            "--input",
            str(sfm),
            "--imagesFolder",
            str(dense),
            "--inputMesh",
            str(filtered_mesh),
            "--output",
            str(textured),
        ],
    ]

    return CommandPlan(
        backend="alicevision",
        scan_root=scan_dir,
        commands=commands,
        outputs={
            "sfm": sfm,
            "mesh": mesh,
            "filtered_mesh": filtered_mesh,
            "textured_output": textured,
        },
        notes=[
            "Experimental direct AliceVision plan; prefer Meshroom until the installed binary version is verified.",
            "Command names and options may need adjustment for the exact AliceVision release installed on Windows/WSL2.",
            "A CUDA-capable NVIDIA GPU is recommended for full dense reconstruction.",
        ],
    )
