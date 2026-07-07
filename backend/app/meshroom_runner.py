"""Meshroom/AliceVision command planner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.reconstruction_plan import CommandPlan


@dataclass(frozen=True)
class MeshroomConfig:
    executable: str = "meshroom_batch"
    pipeline: str = "photogrammetry"
    workspace_name: str = "meshroom"


def build_meshroom_plan(scan_dir: Path, config: MeshroomConfig | None = None) -> CommandPlan:
    """Build a Meshroom batch reconstruction plan.

    Meshroom is the preferred AliceVision entry point until the workstation
    binary version is known because it owns the pipeline graph and node wiring.
    """
    config = config or MeshroomConfig()
    scan_dir = scan_dir.resolve()
    image_path = scan_dir / "images"
    workspace = scan_dir / config.workspace_name
    output_path = workspace / "output"
    cache_path = workspace / "cache"
    project_path = workspace / "project.mg"

    command = [
        config.executable,
        "--input",
        str(image_path),
        "--pipeline",
        config.pipeline,
        "--output",
        str(output_path),
        "--cache",
        str(cache_path),
        "--save",
        str(project_path),
    ]

    return CommandPlan(
        backend="meshroom",
        scan_root=scan_dir,
        commands=[command],
        outputs={
            "published_output": output_path,
            "cache": cache_path,
            "project": project_path,
        },
        notes=[
            "Meshroom wraps AliceVision and is the recommended alternate photogrammetry backend to test first.",
            "A CUDA-capable NVIDIA GPU is recommended for the full Meshroom photogrammetry pipeline.",
        ],
    )
