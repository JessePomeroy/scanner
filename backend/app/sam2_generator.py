"""One-box mask generation in an isolated, explicitly installed SAM2 runtime."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image

from app.heavy_work import guarded, native_kwargs
from app.mask_authoring import MaskAuthoringPlan, single_box_selection
from app.mask_generator import (
    GeneratedMaskFrame, MaskGenerationError, MaskGenerationResult,
    _evaluate_quality, _publish_generation, _select_review_indices, _write_review_preview,
)
from app.mask_processor import validate_capture_mask_png
from app.scan_metadata import FrameMetadata


SAM2_SOURCE_COMMIT = "2b90b9f5ceec907a1c18123530e92e794ad901a4"
SAM2_CHECKPOINT_SHA256 = "6d1aa6f30de5c92224f8172114de081d104bbd23dd9dc5c58996f0cad5dc4d38"
SAM2_MODEL_CONFIG = "configs/sam2.1/sam2.1_hiera_s.yaml"
SAM2_CHECKPOINT_URL = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt"
SAM2_MAX_FRAMES = 300
BACKEND_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Sam2Runtime:
    python: Path
    checkpoint: Path
    timeout_seconds: int = 600

    @classmethod
    def configured(cls) -> Sam2Runtime:
        return cls(
            python=Path(os.environ.get("SCANNER_SAM2_PYTHON", str(BACKEND_ROOT / ".venv-sam2/bin/python"))),
            checkpoint=Path(os.environ.get("SCANNER_SAM2_CHECKPOINT", str(BACKEND_ROOT / "models/sam2.1_hiera_small.pt"))),
        )

    def validate(self) -> None:
        if not self.python.is_file() or not os.access(self.python, os.X_OK):
            raise MaskGenerationError("SAM2 worker is not installed; follow the SAM2 backend setup in README.md")
        if self.timeout_seconds <= 0:
            raise MaskGenerationError("SAM2 worker timeout must be positive")
        verify_checkpoint(self.checkpoint)


def verify_checkpoint(path: Path) -> None:
    """Only the pinned public checkpoint is accepted; uploads cannot select model code."""
    if path.is_symlink() or not path.is_file():
        raise MaskGenerationError("SAM2 checkpoint is missing or unsafe; follow the SAM2 backend setup in README.md")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != SAM2_CHECKPOINT_SHA256:
        raise MaskGenerationError("SAM2 checkpoint SHA256 does not match the pinned model")


def validate_sam2_inputs(
    scan_root: Path, plan: MaskAuthoringPlan, frames: tuple[FrameMetadata, ...],
) -> tuple[int, tuple[float, float, float, float]]:
    selected, box = single_box_selection(plan)
    if not 1 <= len(frames) <= SAM2_MAX_FRAMES:
        raise MaskGenerationError(f"SAM2 supports 1–{SAM2_MAX_FRAMES} photos per scan")
    if len({frame.id for frame in frames}) != len(frames) or len({frame.image for frame in frames}) != len(frames):
        raise MaskGenerationError("SAM2 frame identities must be unique")
    positions = {(frame.id, frame.image): index for index, frame in enumerate(frames)}
    if (selected.frame_id, selected.image) not in positions:
        raise MaskGenerationError("SAM2 selection is not associated with a capture frame")
    if (scan_root / "images").is_symlink():
        raise MaskGenerationError("SAM2 image directory is unsafe")
    for index, frame in enumerate(frames):
        relative = Path(frame.image)
        if len(relative.parts) != 2 or relative.parts[0] != "images" or relative.name in {".", ".."}:
            raise MaskGenerationError("SAM2 images must be flat capture-image paths")
        source = scan_root / relative
        if source.is_symlink() or not source.is_file():
            raise MaskGenerationError("SAM2 source image is missing or unsafe")
        width, height = frame.resolution
        if width < 1 or height < 1 or width * height > 64_000_000:
            raise MaskGenerationError("SAM2 source image dimensions are unsafe")
        if not math.isfinite(frame.timestamp) or (index and frame.timestamp <= frames[index-1].timestamp):
            raise MaskGenerationError("SAM2 capture frames must be ordered by strictly increasing timestamps")
    return positions[(selected.frame_id, selected.image)], box


class Sam2MaskGenerator:
    identifier = "sam2_1_small_temporal_v1"

    def __init__(self, runtime: Sam2Runtime | None = None) -> None:
        self.runtime = runtime or Sam2Runtime.configured()

    @guarded("SAM2 one-box mask generation")
    def generate(
        self, scan_root: Path, plan: MaskAuthoringPlan, frames: tuple[FrameMetadata, ...],
    ) -> MaskGenerationResult:
        scan_root = scan_root.resolve()
        seed_index, _ = validate_sam2_inputs(scan_root, plan, frames)
        self.runtime.validate()
        masks_root = scan_root / "masks"
        if masks_root.is_symlink():
            raise MaskGenerationError("SAM2 mask workspace is unsafe")
        masks_root.mkdir(exist_ok=True)
        metadata = scan_root / "metadata"
        if metadata.is_symlink() or not metadata.is_dir():
            raise MaskGenerationError("SAM2 metadata workspace is unsafe")
        staging = Path(tempfile.mkdtemp(prefix=".proposed.", dir=masks_root))
        reviews = Path(tempfile.mkdtemp(prefix=".review.", dir=masks_root))
        generated: list[GeneratedMaskFrame] = []
        try:
            with tempfile.TemporaryDirectory(prefix="scanner-sam2-") as temporary:
                work = Path(temporary)
                worker_report = work / "result.json"
                command = [str(self.runtime.python.absolute()), "-I", str(Path(__file__).with_name("sam2_worker.py")),
                           "--scan-root", str(scan_root), "--work-dir", str(work),
                           "--mask-dir", str(staging), "--checkpoint", str(self.runtime.checkpoint.absolute())]
                # Keep worker diagnostics in the failed/completed package, not just the server journal.
                log_path = metadata / "sam2_worker.log"
                if log_path.is_symlink():
                    raise MaskGenerationError("SAM2 log destination is unsafe")
                try:
                    with log_path.open("a", encoding="utf-8") as log:
                        completed = subprocess.run(command, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                                                   timeout=self.runtime.timeout_seconds, **native_kwargs())
                except subprocess.TimeoutExpired as error:
                    raise MaskGenerationError("SAM2 worker exceeded its time limit; original capture is preserved") from error
                except OSError as error:
                    raise MaskGenerationError("Unable to start the installed SAM2 worker") from error
                if completed.returncode != 0:
                    raise MaskGenerationError("SAM2 worker failed; inspect metadata/sam2_worker.log in the preserved package")
                if worker_report.is_symlink() or not worker_report.is_file() or worker_report.stat().st_size > 1024*1024:
                    raise MaskGenerationError("SAM2 worker did not produce a bounded result report")
                worker = json.loads(worker_report.read_text())
                if not isinstance(worker, dict) or worker.get("frame_ids") != [frame.id for frame in frames]:
                    raise MaskGenerationError("SAM2 worker result does not cover the exact capture frames")
                expected = {Path(frame.image).name + ".png" for frame in frames}
                if {path.name for path in staging.iterdir()} != expected:
                    raise MaskGenerationError("SAM2 produced an incomplete mask set")
                for frame in frames:
                    name = Path(frame.image).name + ".png"
                    mask_path = staging / name
                    if mask_path.is_symlink() or not mask_path.is_file():
                        raise MaskGenerationError("SAM2 produced an unsafe mask file")
                    validate_capture_mask_png(mask_path, frame.resolution)
                    with Image.open(mask_path) as image:
                        values = np.asarray(image.convert("L")) > 0
                    count = int(np.count_nonzero(values))
                    height, width = values.shape
                    centroid = None if count == 0 else (
                        float(np.dot(np.arange(width)+.5, values.sum(axis=0)) / count / width),
                        float(np.dot(np.arange(height)+.5, values.sum(axis=1)) / count / height),
                    )
                    generated.append(GeneratedMaskFrame(
                        frame.id, frame.image, f"masks/proposed/{name}", None,
                        "sam2_box_seed" if frame.id == frames[seed_index].id else "sam2_temporal",
                        (frames[seed_index].id,), count / values.size, centroid, 0,
                    ))
            blocking, warnings = _evaluate_quality(generated)
            # Motion/area changes are triage hints, not proof that the selected object was lost.
            warnings = (*warnings, *(item for item in blocking if item["code"] != "empty_mask"))
            blocking = tuple(item for item in blocking if item["code"] == "empty_mask")
            indices = _select_review_indices(generated)
            preview_paths = []
            for index in indices:
                frame = generated[index]
                name = Path(frame.mask).name
                _write_review_preview(scan_root / frame.image, staging / name, reviews / name)
                preview_paths.append(f"masks/review/{name}")
            result = MaskGenerationResult(
                "needs_correction" if blocking else "awaiting_review", self.identifier, plan.revision,
                masks_root / "proposed", metadata / "mask_generation.json", tuple(generated),
                indices, tuple(preview_paths), blocking, tuple(warnings),
            )
            payload = result.report_payload()
            payload["model"] = worker
            payload["confidence_note"] = "No calibrated per-frame confidence; technical validation is not human approval or segmentation accuracy."
            _publish_generation(masks_root, staging, reviews, result.report_path, payload)
            return result
        finally:
            for temporary in (staging, reviews):
                if temporary.exists():
                    shutil.rmtree(temporary)
