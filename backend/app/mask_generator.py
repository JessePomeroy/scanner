"""Replaceable temporal generators for reviewable per-frame mask proposals."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Protocol
import uuid

from PIL import Image, ImageDraw

from app.mask_authoring import (
    MaskAuthoringFrame,
    MaskAuthoringPlan,
    MaskAuthoringPoint,
    MaskAuthoringRegion,
    load_mask_authoring_plan,
    representative_frame_indices,
)
from app.scan_metadata import FrameMetadata


class MaskGenerationError(RuntimeError):
    """Raised when a complete, bounded proposal set cannot be generated."""


@dataclass(frozen=True)
class GeneratedMaskFrame:
    frame_id: int
    image: str
    mask: str
    confidence: float
    method: str
    source_frame_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "image": self.image,
            "mask": self.mask,
            "confidence": self.confidence,
            "method": self.method,
            "source_frame_ids": list(self.source_frame_ids),
        }


@dataclass(frozen=True)
class MaskGenerationResult:
    generator: str
    source_revision: int
    output_dir: Path
    report_path: Path
    frames: tuple[GeneratedMaskFrame, ...]
    review_indices: tuple[int, ...]

    def report_payload(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "state": "awaiting_review",
            "generator": self.generator,
            "source_authoring_revision": self.source_revision,
            "mask_convention": "white_keep_black_exclude",
            "frame_count": len(self.frames),
            "review_indices": list(self.review_indices),
            "frames": [frame.as_dict() for frame in self.frames],
        }


class MaskGenerator(Protocol):
    """Boundary for deterministic, optical-flow, or model-backed generators."""

    identifier: str

    def generate(
        self,
        scan_root: Path,
        plan: MaskAuthoringPlan,
        frames: tuple[FrameMetadata, ...],
    ) -> MaskGenerationResult: ...


class PolygonInterpolationMaskGenerator:
    """Interpolate normalized user polygons without claiming image understanding."""

    identifier = "polygon_keyframe_interpolation_v1"

    def __init__(self, *, resampled_point_count: int = 128) -> None:
        if not 8 <= resampled_point_count <= 1024:
            raise ValueError("resampled_point_count must be from 8 through 1024")
        self.resampled_point_count = resampled_point_count

    def generate(
        self,
        scan_root: Path,
        plan: MaskAuthoringPlan,
        frames: tuple[FrameMetadata, ...],
    ) -> MaskGenerationResult:
        scan_root = scan_root.resolve()
        if not frames:
            raise MaskGenerationError("Cannot generate masks without frames")
        positions = {(frame.id, frame.image): index for index, frame in enumerate(frames)}
        anchors = sorted(
            ((positions[(selection.frame_id, selection.image)], selection) for selection in plan.representative_frames),
            key=lambda item: item[0],
        )
        masks_root = scan_root / "masks"
        if masks_root.is_symlink():
            raise MaskGenerationError(f"Masks directory is unsafe: {masks_root}")
        masks_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=masks_root, prefix=".proposed."))
        generated: list[GeneratedMaskFrame] = []
        try:
            for index, frame in enumerate(frames):
                regions, method, sources, confidence = self._regions_at(index, anchors, len(frames))
                output_name = Path(frame.image).name + ".png"
                output = staging / output_name
                _write_mask(output, regions, frame.resolution)
                generated.append(
                    GeneratedMaskFrame(
                        frame_id=frame.id,
                        image=frame.image,
                        mask=f"masks/proposed/{output_name}",
                        confidence=confidence,
                        method=method,
                        source_frame_ids=sources,
                    )
                )
            report_path = scan_root / "metadata" / "mask_generation.json"
            result = MaskGenerationResult(
                generator=self.identifier,
                source_revision=plan.revision,
                output_dir=masks_root / "proposed",
                report_path=report_path,
                frames=tuple(generated),
                review_indices=representative_frame_indices(len(frames)),
            )
            _publish_generation(
                masks_root,
                staging,
                report_path,
                result.report_payload(),
            )
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        return result

    def _regions_at(
        self,
        index: int,
        anchors: list[tuple[int, MaskAuthoringFrame]],
        frame_count: int,
    ) -> tuple[tuple[MaskAuthoringRegion, ...], str, tuple[int, ...], float]:
        for position, selection in anchors:
            if position == index:
                return selection.regions, "authored", (selection.frame_id,), 1.0

        lower = next(((position, frame) for position, frame in reversed(anchors) if position < index), None)
        upper = next(((position, frame) for position, frame in anchors if position > index), None)
        if lower is not None and upper is not None:
            lower_position, lower_frame = lower
            upper_position, upper_frame = upper
            if _compatible_topology(lower_frame.regions, upper_frame.regions):
                alpha = (index - lower_position) / (upper_position - lower_position)
                regions = tuple(
                    self._interpolate_region(left, right, alpha)
                    for left, right in zip(lower_frame.regions, upper_frame.regions)
                )
                return (
                    regions,
                    "interpolated",
                    (lower_frame.frame_id, upper_frame.frame_id),
                    0.8,
                )
            chosen_position, chosen = min((lower, upper), key=lambda item: abs(item[0] - index))
            del chosen_position
            return chosen.regions, "nearest_topology_fallback", (chosen.frame_id,), 0.25

        position, selected = lower or upper or anchors[0]
        distance_fraction = abs(index - position) / max(frame_count - 1, 1)
        confidence = max(0.2, 0.55 - 0.35 * distance_fraction)
        return selected.regions, "boundary_hold", (selected.frame_id,), confidence

    def _interpolate_region(
        self,
        left: MaskAuthoringRegion,
        right: MaskAuthoringRegion,
        alpha: float,
    ) -> MaskAuthoringRegion:
        left_points = _resample_polygon(left.points, self.resampled_point_count)
        right_points = _resample_polygon(right.points, self.resampled_point_count)
        aligned_right = _align_polygon(left_points, right_points)
        points = tuple(
            MaskAuthoringPoint(
                x=left_point.x + (right_point.x - left_point.x) * alpha,
                y=left_point.y + (right_point.y - left_point.y) * alpha,
            )
            for left_point, right_point in zip(left_points, aligned_right)
        )
        return MaskAuthoringRegion(operation=left.operation, points=points)


def generate_mask_proposals(
    scan_root: Path,
    frames: tuple[FrameMetadata, ...],
    *,
    generator: MaskGenerator | None = None,
) -> MaskGenerationResult | None:
    """Generate proposals when authoring exists; never promote them to capture masks."""
    plan = load_mask_authoring_plan(scan_root.resolve() / "metadata", frames)
    if plan is None:
        return None
    return (generator or PolygonInterpolationMaskGenerator()).generate(scan_root, plan, frames)


def _compatible_topology(
    left: tuple[MaskAuthoringRegion, ...],
    right: tuple[MaskAuthoringRegion, ...],
) -> bool:
    return len(left) == len(right) and all(
        left_region.operation == right_region.operation
        for left_region, right_region in zip(left, right)
    )


def _resample_polygon(
    points: tuple[MaskAuthoringPoint, ...],
    count: int,
) -> tuple[MaskAuthoringPoint, ...]:
    segments: list[float] = []
    perimeter = 0.0
    for index, point in enumerate(points):
        following = points[(index + 1) % len(points)]
        length = math.hypot(following.x - point.x, following.y - point.y)
        segments.append(length)
        perimeter += length
    if perimeter <= math.ulp(1.0):
        raise MaskGenerationError("Cannot resample a degenerate authoring polygon")
    output: list[MaskAuthoringPoint] = []
    segment_index = 0
    segment_start_distance = 0.0
    for sample_index in range(count):
        target = perimeter * sample_index / count
        while (
            segment_index + 1 < len(points)
            and target > segment_start_distance + segments[segment_index]
        ):
            segment_start_distance += segments[segment_index]
            segment_index += 1
        start = points[segment_index]
        end = points[(segment_index + 1) % len(points)]
        length = segments[segment_index]
        alpha = 0.0 if length == 0 else (target - segment_start_distance) / length
        output.append(
            MaskAuthoringPoint(
                x=start.x + (end.x - start.x) * alpha,
                y=start.y + (end.y - start.y) * alpha,
            )
        )
    return tuple(output)


def _align_polygon(
    reference: tuple[MaskAuthoringPoint, ...],
    candidate: tuple[MaskAuthoringPoint, ...],
) -> tuple[MaskAuthoringPoint, ...]:
    """Choose winding and cyclic offset with the smallest pointwise distance."""
    options = (candidate, tuple(reversed(candidate)))
    best: tuple[MaskAuthoringPoint, ...] | None = None
    best_cost = math.inf
    for option in options:
        for offset in range(len(option)):
            shifted = option[offset:] + option[:offset]
            cost = sum(
                (left.x - right.x) ** 2 + (left.y - right.y) ** 2
                for left, right in zip(reference, shifted)
            )
            if cost < best_cost:
                best = shifted
                best_cost = cost
    assert best is not None
    return best


def _write_mask(
    path: Path,
    regions: tuple[MaskAuthoringRegion, ...],
    resolution: tuple[int, int],
) -> None:
    width, height = resolution
    if width < 1 or height < 1 or width * height > 64_000_000:
        raise MaskGenerationError(f"Frame resolution is unsafe: {resolution}")
    image = Image.new("L", (width, height), color=0)
    draw = ImageDraw.Draw(image)
    for region in regions:
        polygon = [
            (round(point.x * (width - 1)), round(point.y * (height - 1)))
            for point in region.points
        ]
        draw.polygon(polygon, fill=255 if region.operation == "keep" else 0)
    image.save(path, format="PNG", optimize=False)


def _publish_generation(
    masks_root: Path,
    staging: Path,
    report_path: Path,
    report_payload: dict[str, object],
) -> None:
    """Publish a proposal directory and its provenance report as one locked generation."""
    destination = masks_root / "proposed"
    lock_path = masks_root.parent / "metadata" / ".mask_generation.lock"
    if destination.is_symlink() or report_path.is_symlink() or lock_path.is_symlink():
        raise MaskGenerationError("Mask proposal destination is unsafe")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    backup: Path | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if destination.exists():
            if not destination.is_dir():
                raise MaskGenerationError("Mask proposal destination is unsafe")
            backup = masks_root / f".proposed.backup.{uuid.uuid4().hex}"
            os.replace(destination, backup)
        try:
            os.replace(staging, destination)
            _write_json_atomic(report_path, report_payload)
        except BaseException:
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            if backup is not None:
                os.replace(backup, destination)
                backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, indent=2, sort_keys=True, allow_nan=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
