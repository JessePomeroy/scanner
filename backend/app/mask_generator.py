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

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

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
    keep_fraction: float
    centroid: tuple[float, float] | None
    safety_dilation_pixels: int

    def as_dict(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "image": self.image,
            "mask": self.mask,
            "confidence": self.confidence,
            "method": self.method,
            "source_frame_ids": list(self.source_frame_ids),
            "keep_fraction": self.keep_fraction,
            "centroid": list(self.centroid) if self.centroid is not None else None,
            "safety_dilation_pixels": self.safety_dilation_pixels,
        }


@dataclass(frozen=True)
class MaskGenerationResult:
    state: str
    generator: str
    source_revision: int
    output_dir: Path
    report_path: Path
    frames: tuple[GeneratedMaskFrame, ...]
    review_indices: tuple[int, ...]
    review_masks: tuple[str, ...]
    blocking_issues: tuple[dict[str, object], ...]
    warnings: tuple[dict[str, object], ...]

    def report_payload(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "state": self.state,
            "generator": self.generator,
            "source_authoring_revision": self.source_revision,
            "mask_convention": "white_keep_black_exclude",
            "frame_count": len(self.frames),
            "review_indices": list(self.review_indices),
            "review_masks": list(self.review_masks),
            "quality": {
                "blocking_issue_count": len(self.blocking_issues),
                "warning_count": len(self.warnings),
                "blocking_issues": list(self.blocking_issues),
                "warnings": list(self.warnings),
            },
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
        review_staging = Path(tempfile.mkdtemp(dir=masks_root, prefix=".review."))
        generated: list[GeneratedMaskFrame] = []
        review_indices = representative_frame_indices(len(frames))
        review_index_set = set(review_indices)
        review_masks: list[str] = []
        try:
            for index, frame in enumerate(frames):
                regions, method, sources, confidence = self._regions_at(index, anchors, len(frames))
                output_name = Path(frame.image).name + ".png"
                output = staging / output_name
                keep_fraction, centroid, dilation_pixels = _write_mask(
                    output,
                    regions,
                    frame.resolution,
                )
                if index in review_index_set:
                    review_output = review_staging / output_name
                    _write_review_preview(scan_root / frame.image, output, review_output)
                    review_masks.append(f"masks/review/{output_name}")
                generated.append(
                    GeneratedMaskFrame(
                        frame_id=frame.id,
                        image=frame.image,
                        mask=f"masks/proposed/{output_name}",
                        confidence=confidence,
                        method=method,
                        source_frame_ids=sources,
                        keep_fraction=keep_fraction,
                        centroid=centroid,
                        safety_dilation_pixels=dilation_pixels,
                    )
                )
            blocking_issues, warnings = _evaluate_quality(generated)
            report_path = scan_root / "metadata" / "mask_generation.json"
            result = MaskGenerationResult(
                state="needs_correction" if blocking_issues else "awaiting_review",
                generator=self.identifier,
                source_revision=plan.revision,
                output_dir=masks_root / "proposed",
                report_path=report_path,
                frames=tuple(generated),
                review_indices=review_indices,
                review_masks=tuple(review_masks),
                blocking_issues=blocking_issues,
                warnings=warnings,
            )
            _publish_generation(
                masks_root,
                staging,
                review_staging,
                report_path,
                result.report_payload(),
            )
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if review_staging.exists():
                shutil.rmtree(review_staging, ignore_errors=True)
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
) -> tuple[float, tuple[float, float] | None, int]:
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
    dilation_pixels = min(32, max(2, round(min(width, height) * 0.01)))
    image = image.filter(ImageFilter.MaxFilter(dilation_pixels * 2 + 1))
    image.save(path, format="PNG", optimize=False)
    values = np.asarray(image, dtype=np.uint8) > 0
    kept_y, kept_x = np.nonzero(values)
    keep_fraction = float(values.mean())
    centroid = None
    if kept_x.size:
        centroid = (
            float((kept_x.mean() + 0.5) / width),
            float((kept_y.mean() + 0.5) / height),
        )
    return keep_fraction, centroid, dilation_pixels


def _write_review_preview(source_path: Path, mask_path: Path, output_path: Path) -> None:
    """Render a bounded source-image overlay where excluded pixels are visibly red."""
    try:
        with Image.open(source_path) as source_image, Image.open(mask_path) as mask_image:
            source = source_image.convert("RGB")
            mask = mask_image.convert("L")
            if source.size != mask.size:
                raise MaskGenerationError(
                    f"Review image and mask dimensions differ: {source_path.name}"
                )
            source.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            mask = mask.resize(source.size, Image.Resampling.NEAREST)
    except (OSError, ValueError) as error:
        raise MaskGenerationError(f"Unable to render mask review image: {source_path}") from error

    excluded = Image.blend(source, Image.new("RGB", source.size, (150, 20, 20)), 0.62)
    preview = Image.composite(source, excluded, mask)
    outer = mask.filter(ImageFilter.MaxFilter(7))
    inner = mask.filter(ImageFilter.MinFilter(7))
    boundary = ImageChops.difference(outer, inner)
    preview.paste((0, 230, 255), mask=boundary)
    preview.save(output_path, format="PNG", optimize=False)


def _evaluate_quality(
    frames: list[GeneratedMaskFrame],
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    blocking: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    for frame in frames:
        if frame.keep_fraction <= 0:
            blocking.append({
                "code": "empty_mask",
                "frame_id": frame.frame_id,
                "message": "The proposal keeps no image pixels.",
            })
        elif frame.keep_fraction < 0.005:
            warnings.append({
                "code": "very_small_keep_area",
                "frame_id": frame.frame_id,
                "message": "The proposal keeps less than 0.5% of the frame.",
            })
        if frame.confidence < 0.5:
            warnings.append({
                "code": "low_generator_confidence",
                "frame_id": frame.frame_id,
                "message": f"The generator used {frame.method} at low confidence.",
            })

    for previous, current in zip(frames, frames[1:]):
        smaller = min(previous.keep_fraction, current.keep_fraction)
        larger = max(previous.keep_fraction, current.keep_fraction)
        if smaller > 0 and larger / smaller > 1.75:
            blocking.append({
                "code": "abrupt_area_change",
                "frame_ids": [previous.frame_id, current.frame_id],
                "message": "The kept area changes by more than 75% between adjacent frames.",
            })
        if previous.centroid is not None and current.centroid is not None:
            distance = math.dist(previous.centroid, current.centroid)
            if distance > 0.18:
                blocking.append({
                    "code": "abrupt_centroid_change",
                    "frame_ids": [previous.frame_id, current.frame_id],
                    "distance": distance,
                    "message": "The kept-region center jumps abruptly between adjacent frames.",
                })
    return tuple(blocking), tuple(warnings)


def _publish_generation(
    masks_root: Path,
    staging: Path,
    review_staging: Path,
    report_path: Path,
    report_payload: dict[str, object],
) -> None:
    """Publish a proposal directory and its provenance report as one locked generation."""
    destination = masks_root / "proposed"
    review_destination = masks_root / "review"
    lock_path = masks_root.parent / "metadata" / ".mask_generation.lock"
    if (
        destination.is_symlink()
        or review_destination.is_symlink()
        or report_path.is_symlink()
        or lock_path.is_symlink()
    ):
        raise MaskGenerationError("Mask proposal destination is unsafe")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    backup: Path | None = None
    review_backup: Path | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if destination.exists() and not destination.is_dir():
            raise MaskGenerationError("Mask proposal destination is unsafe")
        if review_destination.exists() and not review_destination.is_dir():
            raise MaskGenerationError("Mask review destination is unsafe")
        try:
            if destination.exists():
                backup = masks_root / f".proposed.backup.{uuid.uuid4().hex}"
                os.replace(destination, backup)
            if review_destination.exists():
                review_backup = masks_root / f".review.backup.{uuid.uuid4().hex}"
                os.replace(review_destination, review_backup)
            os.replace(staging, destination)
            os.replace(review_staging, review_destination)
            _write_json_atomic(report_path, report_payload)
        except BaseException:
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            if review_destination.exists():
                shutil.rmtree(review_destination, ignore_errors=True)
            if backup is not None:
                os.replace(backup, destination)
                backup = None
            if review_backup is not None:
                os.replace(review_backup, review_destination)
                review_backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        if review_backup is not None:
            shutil.rmtree(review_backup, ignore_errors=True)
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
