"""Dry-run planners for experimental neural reconstruction backends."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORTED_NEURAL_BACKENDS = (
    "mast3r_slam",
    "depth_anything",
    "lingbot",
    "gaussian_splatting",
)


@dataclass(frozen=True)
class NeuralBackendConfig:
    backend: str
    mast3r_slam_config: str = "config/base.yaml"
    depth_anything_encoder: str = "vits"
    splat_method: str = "splatfacto"
    splat_matching_method: str = "sequential"


@dataclass(frozen=True)
class NeuralBackendPlan:
    backend: str
    scan_root: Path
    commands: list[list[str]]
    inputs: dict[str, Any]
    outputs: dict[str, Path]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "scan_root": str(self.scan_root),
            "commands": self.commands,
            "inputs": self.inputs,
            "outputs": {key: str(path) for key, path in self.outputs.items()},
            "notes": self.notes,
        }


def build_neural_backend_plan(scan_root: Path, config: NeuralBackendConfig) -> NeuralBackendPlan:
    """Build a non-executing neural backend experiment plan."""
    if config.backend not in SUPPORTED_NEURAL_BACKENDS:
        supported = ", ".join(SUPPORTED_NEURAL_BACKENDS)
        raise ValueError(f"Unsupported neural backend '{config.backend}'. Supported backends: {supported}")

    scan_root = scan_root.resolve()
    image_dir = scan_root / "images"
    video_paths = _video_paths(scan_root)
    inputs = {
        "image_dir": str(image_dir),
        "image_count": _image_count(image_dir),
        "video_paths": [str(path) for path in video_paths],
        "video_count": len(video_paths),
    }

    if config.backend == "mast3r_slam":
        dataset_path = video_paths[0] if video_paths else image_dir
        return NeuralBackendPlan(
            backend=config.backend,
            scan_root=scan_root,
            commands=[
                [
                    "python",
                    "main.py",
                    "--dataset",
                    str(dataset_path),
                    "--config",
                    config.mast3r_slam_config,
                ]
            ],
            inputs=inputs,
            outputs={
                "workspace": scan_root / "neural" / "mast3r_slam",
            },
            notes=[
                "Experimental only; run inside a separate MASt3R-SLAM checkout and Python environment.",
                "Prefer video input when available; image folders can be used as fallback.",
                "Use reduced resolution/frame counts first on RTX 3070-class hardware.",
            ],
        )

    if config.backend == "depth_anything":
        return NeuralBackendPlan(
            backend=config.backend,
            scan_root=scan_root,
            commands=[
                [
                    "python",
                    "run.py",
                    "--encoder",
                    config.depth_anything_encoder,
                    "--img-path",
                    str(image_dir),
                    "--outdir",
                    str(scan_root / "neural" / "depth_anything"),
                ]
            ],
            inputs=inputs,
            outputs={
                "depth_dir": scan_root / "neural" / "depth_anything",
            },
            notes=[
                "Experimental depth-estimation support path, not a textured mesh replacement.",
                "Use outputs for scan diagnostics, object isolation, preview depth, or later DA3 experiments.",
                "Run inside a separate Depth Anything checkout and Python environment.",
                "Depth Anything V2 Small is Apache-2.0; larger V2 checkpoints are non-commercial.",
            ],
        )

    if config.backend == "gaussian_splatting":
        workspace = scan_root / "neural" / "gaussian_splatting"
        nerfstudio_data = workspace / "nerfstudio"
        export_dir = workspace / "exports" / "splat"
        config_path = (
            workspace
            / "outputs"
            / "<scene>"
            / config.splat_method
            / "<timestamp>"
            / "config.yml"
        )
        process_source_type = "video" if video_paths else "images"
        process_source = video_paths[0] if video_paths else image_dir

        commands = [
            [
                "ns-process-data",
                process_source_type,
                "--data",
                str(process_source),
                "--output-dir",
                str(nerfstudio_data),
                "--matching-method",
                config.splat_matching_method,
            ],
            [
                "ns-train",
                config.splat_method,
                "--data",
                str(nerfstudio_data),
                "--output-dir",
                str(workspace / "outputs"),
            ],
            [
                "ns-export",
                "gaussian-splat",
                "--load-config",
                str(config_path),
                "--output-dir",
                str(export_dir),
            ],
        ]

        return NeuralBackendPlan(
            backend=config.backend,
            scan_root=scan_root,
            commands=commands,
            inputs={
                **inputs,
                "preferred_source": str(process_source),
                "preferred_source_type": process_source_type,
            },
            outputs={
                "workspace": workspace,
                "nerfstudio_data": nerfstudio_data,
                "train_outputs": workspace / "outputs",
                "splat_export": export_dir,
            },
            notes=[
                "Viewer-focused Gaussian splat path; this does not produce an editable textured mesh.",
                "Run inside a CUDA-enabled Nerfstudio environment on the Windows RTX workstation.",
                "The export command needs the real config.yml path printed by ns-train.",
                "Start with scene scans or object scans with strong multi-angle coverage and static lighting.",
            ],
        )

    return NeuralBackendPlan(
        backend=config.backend,
        scan_root=scan_root,
        commands=[],
        inputs=inputs,
        outputs={
            "workspace": scan_root / "neural" / "lingbot",
        },
        notes=[
            "Lingbot-style workflow is UI-driven: launch the local app and drop in a video.",
            "This path needs video capture; image-only scan packages are not enough for the current workflow.",
            "Treat output as a point-cloud/viewer experiment, not Blender-ready textured mesh generation.",
        ],
    )


def write_neural_backend_report(plan: NeuralBackendPlan, path: Path) -> Path:
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        **plan.to_dict(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def _video_paths(scan_root: Path) -> list[Path]:
    video_metadata_path = scan_root / "metadata" / "video.json"
    try:
        video_metadata = json.loads(video_metadata_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        video_metadata = []

    paths: list[Path] = []
    if isinstance(video_metadata, list):
        for item in video_metadata:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if isinstance(path, str):
                candidate = scan_root / path
                if candidate.is_file():
                    paths.append(candidate)

    if paths:
        return paths

    video_dir = scan_root / "video"
    if not video_dir.is_dir():
        return []

    return sorted(
        path
        for path in video_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".mov", ".mp4", ".m4v"}
    )


def _image_count(image_dir: Path) -> int:
    if not image_dir.is_dir():
        return 0

    return len(
        [
            path
            for path in image_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".heic", ".png"}
        ]
    )
