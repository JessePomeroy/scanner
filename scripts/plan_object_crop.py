#!/usr/bin/env python3
"""Inspect object-scan metadata and print the next crop command."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.scan_package import prepare_scan_source, validate_and_report_scan  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path, help="Scan zip or extracted scan directory.")
    parser.add_argument("--point-cloud", type=Path, help="Optional sparse/dense PLY to crop.")
    parser.add_argument(
        "--colmap-center",
        type=float,
        nargs=3,
        metavar=("X", "Y", "Z"),
        help="Object center in COLMAP/OpenMVS coordinates once manually identified.",
    )
    parser.add_argument("--output", type=Path, default=Path("object_cropped.ply"))
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temporary:
        scan_root = prepare_scan_source(args.scan, Path(temporary))
        report = validate_and_report_scan(scan_root).validation

        print(f"scan_id: {report.scan_id or scan_root.name}")
        print(f"scan_mode: {report.scan_mode or 'unknown'}")
        print(f"object_center_world: {report.object_center_world}")
        print(f"object_radius_meters: {report.object_radius_meters}")

        if report.scan_mode != "object_scan":
            raise SystemExit("This scan is not marked as object_scan.")
        if report.object_center_world is None or report.object_radius_meters is None:
            raise SystemExit("Object crop metadata is incomplete. Re-scan in Object mode and tap the subject.")

        print()
        print("Automatic object cropping still needs ARKit-to-COLMAP coordinate alignment.")
        print("For now, pick the object center in the reconstructed point cloud, then crop by radius.")

        if args.point_cloud and args.colmap_center:
            command = [
                "python3",
                "scripts/crop_point_cloud.py",
                str(args.point_cloud),
                str(args.output),
                "--center",
                *[str(value) for value in args.colmap_center],
                "--radius",
                str(report.object_radius_meters),
            ]
            print()
            print("crop_command:")
            print(" ".join(command))
        else:
            print()
            print("example:")
            print(
                "python3 scripts/crop_point_cloud.py input.ply object_cropped.ply "
                f"--center X Y Z --radius {report.object_radius_meters}"
            )


if __name__ == "__main__":
    main()
