#!/usr/bin/env python3
"""Inspect and validate an extracted scan package."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.scan_validator import find_scan_root, validate_scan_package  # noqa: E402
from app.scan_metadata import FrameMetadata, load_scan_metadata  # noqa: E402


def _format_counts(counts: Counter[str]) -> str:
    return ",".join(f"{key}:{counts[key]}" for key in sorted(counts)) or "none"


def _metadata_resolution_counts(frames: tuple[FrameMetadata, ...]) -> Counter[str]:
    return Counter(
        f"{frame.image_source or 'legacy'}@{frame.resolution[0]}x{frame.resolution[1]}"
        for frame in frames
    )


def _verify_image_dimensions(
    scan_root: Path,
    frames: tuple[FrameMetadata, ...],
) -> Counter[str]:
    actual_counts: Counter[str] = Counter()
    mismatches: list[str] = []
    for frame in frames:
        image_path = scan_root / frame.image
        try:
            with Image.open(image_path) as image:
                actual_resolution = image.size
                image.verify()
        except (OSError, UnidentifiedImageError) as error:
            raise SystemExit(f"Image decode failed for {frame.image}: {error}") from error

        source = frame.image_source or "legacy"
        actual_counts[f"{source}@{actual_resolution[0]}x{actual_resolution[1]}"] += 1
        if actual_resolution != frame.resolution:
            mismatches.append(
                f"{frame.image}: metadata={frame.resolution[0]}x{frame.resolution[1]} "
                f"actual={actual_resolution[0]}x{actual_resolution[1]}"
            )

    if mismatches:
        raise SystemExit("Image resolution mismatch: " + "; ".join(mismatches))
    return actual_counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan_dir", type=Path)
    parser.add_argument(
        "--verify-images",
        action="store_true",
        help="decode every packaged image and compare its dimensions with frames.json",
    )
    args = parser.parse_args()

    scan_root = find_scan_root(args.scan_dir)
    report = validate_scan_package(scan_root)
    metadata = load_scan_metadata(scan_root / "metadata")
    source_counts = Counter(frame.image_source or "legacy" for frame in metadata.frames)
    fallback_reasons = Counter(
        frame.high_resolution_capture_failure
        for frame in metadata.frames
        if frame.high_resolution_capture_failure is not None
    )

    print(f"scan_dir={report.scan_dir}")
    print(f"scan_id={report.scan_id}")
    print(f"scan_mode={report.scan_mode}")
    print(f"images={report.image_count}")
    print(f"frames={report.frame_count}")
    print(f"videos={report.video_count}")
    print(f"video_metadata_entries={report.video_metadata_count}")
    print(
        "high_resolution_capture_enabled="
        f"{report.high_resolution_frame_capture_enabled}"
    )
    print(f"configured_video_resolution={report.configured_video_resolution}")
    print(f"high_resolution_images={report.high_resolution_image_count}")
    print(f"fallback_images={report.fallback_image_count}")
    print(f"image_sources={_format_counts(source_counts)}")
    print(
        "metadata_resolutions_by_source="
        f"{_format_counts(_metadata_resolution_counts(metadata.frames))}"
    )
    print(f"fallback_reasons={_format_counts(fallback_reasons)}")
    if args.verify_images:
        actual_counts = _verify_image_dimensions(scan_root, metadata.frames)
        print(f"decoded_resolutions_by_source={_format_counts(actual_counts)}")
        print("image_decode_and_dimension_check=passed")
    print(f"integrity_warnings={','.join(report.integrity_warnings) or 'none'}")
    print(f"object_center_world={report.object_center_world}")
    print(f"object_radius_meters={report.object_radius_meters}")


if __name__ == "__main__":
    main()
