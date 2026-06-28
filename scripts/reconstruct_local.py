#!/usr/bin/env python3
"""Validate a scan package and optionally run local reconstruction commands."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.colmap_runner import run_colmap_pipeline  # noqa: E402
from app.openmvs_runner import run_openmvs_pipeline  # noqa: E402
from app.scan_validator import find_scan_root, validate_scan_package  # noqa: E402
from app.storage import safe_extract_zip  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path, help="Scan zip or extracted scan directory.")
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--run-colmap", action="store_true")
    parser.add_argument("--run-openmvs", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temporary:
        work_dir = args.work_dir or Path(temporary) / "scan"
        work_dir.mkdir(parents=True, exist_ok=True)

        if args.scan.suffix.lower() == ".zip":
            safe_extract_zip(args.scan, work_dir)
        elif args.scan.is_dir():
            destination = work_dir / args.scan.name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(args.scan, destination)
        else:
            raise SystemExit(f"Scan path is not a zip or directory: {args.scan}")

        scan_root = find_scan_root(work_dir)
        report = validate_scan_package(scan_root)
        print(f"Validated {report.image_count} images and {report.frame_count} frames.")

        if args.run_colmap:
            fused = run_colmap_pipeline(scan_root)
            print(f"COLMAP fused point cloud: {fused}")

        if args.run_openmvs:
            textured = run_openmvs_pipeline(scan_root)
            print(f"OpenMVS textured mesh: {textured}")

        if args.work_dir is None:
            print("No --work-dir was provided; extracted files were temporary.")
        else:
            print(f"Work directory: {work_dir}")


if __name__ == "__main__":
    main()
