#!/usr/bin/env python3
"""Inspect and validate an extracted scan package."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.scan_validator import find_scan_root, validate_scan_package  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan_dir", type=Path)
    args = parser.parse_args()

    scan_root = find_scan_root(args.scan_dir)
    report = validate_scan_package(scan_root)

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
    print(f"integrity_warnings={','.join(report.integrity_warnings) or 'none'}")
    print(f"object_center_world={report.object_center_world}")
    print(f"object_radius_meters={report.object_radius_meters}")


if __name__ == "__main__":
    main()
