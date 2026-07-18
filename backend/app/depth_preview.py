"""Generate bounded Depth Anything preview artifacts for representative scan frames."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import time
from typing import Any, Sequence

from app.benchmark_evidence import sha256_file


SCHEMA_VERSION = "0.1.0"


class DepthPreviewError(RuntimeError):
    """Raised when a depth preview run cannot produce trustworthy artifacts."""


@dataclass(frozen=True)
class PreviewFrame:
    frame_id: int
    image_path: Path


def select_representative_frames(
    frames: Sequence[PreviewFrame],
    *,
    maximum: int = 12,
) -> list[PreviewFrame]:
    """Select evenly spaced frames, retaining both scan endpoints."""
    if maximum <= 0:
        raise ValueError("maximum must be positive")
    if len(frames) <= maximum:
        return list(frames)
    if maximum == 1:
        return [frames[len(frames) // 2]]

    last = len(frames) - 1
    indices = [round(index * last / (maximum - 1)) for index in range(maximum)]
    return [frames[index] for index in indices]


def build_depth_preview_command(
    runtime: Path,
    model: Path,
    frame: PreviewFrame,
    output_dir: Path,
    *,
    threads: int | None = None,
) -> tuple[list[str], dict[str, Path]]:
    """Build one dependency-owned CLI call with scanner-owned output paths."""
    prefix = f"frame_{frame.frame_id:06d}"
    outputs = {
        "depth_pfm": output_dir / f"{prefix}_depth.pfm",
        "depth_png": output_dir / f"{prefix}_depth.png",
        "predicted_pose_json": output_dir / f"{prefix}_predicted_pose.json",
    }
    command = [
        str(runtime),
        "depth",
        "--model",
        str(model),
        "--input",
        str(frame.image_path),
        "--pfm",
        str(outputs["depth_pfm"]),
        "--png",
        str(outputs["depth_png"]),
        "--pose",
        str(outputs["predicted_pose_json"]),
    ]
    if threads is not None:
        if threads <= 0:
            raise ValueError("threads must be positive")
        command.extend(["--threads", str(threads)])
    return command, outputs


def load_preview_frames(scan_root: Path) -> list[PreviewFrame]:
    """Load validated frame IDs and package-local image paths in metadata order."""
    metadata_path = scan_root / "metadata" / "frames.json"
    try:
        values = json.loads(metadata_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise DepthPreviewError(f"Cannot read validated frame metadata: {metadata_path}") from error
    if not isinstance(values, list):
        raise DepthPreviewError("frames.json must contain an array")

    frames: list[PreviewFrame] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise DepthPreviewError(f"frames.json[{index}] must be an object")
        frame_id = value.get("id")
        image = value.get("image")
        if not isinstance(frame_id, int) or not isinstance(image, str):
            raise DepthPreviewError(f"frames.json[{index}] is missing id or image")
        image_path = (scan_root / image).resolve()
        images_dir = (scan_root / "images").resolve()
        if images_dir not in image_path.parents or not image_path.is_file():
            raise DepthPreviewError(f"Frame {frame_id} has an unsafe or missing image path")
        frames.append(PreviewFrame(frame_id=frame_id, image_path=image_path))
    return frames


def run_depth_previews(
    *,
    runtime: Path,
    model: Path,
    frames: Sequence[PreviewFrame],
    output_dir: Path,
    report_path: Path,
    timeout_seconds: float = 300,
    threads: int | None = None,
) -> dict[str, Any]:
    """Run bounded single-frame previews and write a reproducibility report."""
    runtime = runtime.resolve()
    model = model.resolve()
    if not runtime.is_file():
        raise DepthPreviewError(f"Depth Anything runtime is not a regular file: {runtime}")
    if not model.is_file():
        raise DepthPreviewError(f"Depth Anything model is not a regular file: {model}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not frames:
        raise DepthPreviewError("No frames were selected for depth previews")

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    for frame in frames:
        command, outputs = build_depth_preview_command(
            runtime,
            model,
            frame,
            output_dir,
            threads=threads,
        )
        frame_started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise DepthPreviewError(f"Depth preview failed for frame {frame.frame_id}: {error}") from error

        missing = [name for name, path in outputs.items() if not path.is_file()]
        if missing:
            raise DepthPreviewError(
                f"Depth preview frame {frame.frame_id} omitted expected outputs: {', '.join(missing)}"
            )
        results.append(
            {
                "frame_id": frame.frame_id,
                "source_image": str(frame.image_path),
                "source_sha256": sha256_file(frame.image_path),
                "elapsed_seconds": time.monotonic() - frame_started,
                "stdout": completed.stdout.strip(),
                "artifacts": {
                    name: {
                        "path": str(path),
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                    for name, path in outputs.items()
                },
            }
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "representative_depth_previews",
        "limitations": [
            "Outputs are advisory visual previews, not reconstruction quality scores.",
            "Depth is model-dependent and must not be interpreted as metres unless the model is metric.",
            "Predicted poses are retained for inspection and do not replace captured ARKit poses.",
        ],
        "runtime": {
            "path": str(runtime),
            "sha256": sha256_file(runtime),
        },
        "model": {
            "path": str(model),
            "sha256": sha256_file(model),
        },
        "selected_frame_count": len(results),
        "elapsed_seconds": time.monotonic() - started,
        "frames": results,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    return report

