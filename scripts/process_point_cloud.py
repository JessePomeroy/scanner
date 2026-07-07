#!/usr/bin/env python3
"""Process a PLY/point-cloud file with an optional cleanup backend."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.point_cloud_processor import (  # noqa: E402
    PointCloudProcessingConfig,
    SUPPORTED_POINT_CLOUD_PROCESSORS,
    build_processing_summary,
    process_point_cloud,
    write_processing_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--processor", choices=SUPPORTED_POINT_CLOUD_PROCESSORS, default="open3d")
    parser.add_argument("--voxel-size", type=float, default=None)
    parser.add_argument("--no-normals", action="store_true")
    parser.add_argument("--no-outlier-filter", action="store_true")
    parser.add_argument("--outlier-neighbors", type=int, default=20)
    parser.add_argument("--outlier-std-ratio", type=float, default=2.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    config = PointCloudProcessingConfig(
        processor=args.processor,
        voxel_size=args.voxel_size,
        estimate_normals=not args.no_normals,
        statistical_outlier_neighbors=None if args.no_outlier_filter else args.outlier_neighbors,
        statistical_outlier_std_ratio=None if args.no_outlier_filter else args.outlier_std_ratio,
    )

    report_path = args.report or args.output.with_suffix(args.output.suffix + ".processing.json")
    if args.dry_run:
        summary = build_processing_summary(args.input, args.output, config)
        print(f"Processor: {summary['processor']}")
        print(f"Input: {summary['input']}")
        print(f"Output: {summary['output']}")
        print(f"Report: {report_path}")
        write_processing_report(args.input, args.output, report_path, config, dry_run=True)
        return

    output = process_point_cloud(args.input, args.output, config)
    write_processing_report(args.input, output, report_path, config, dry_run=False)
    print(f"Output: {output}")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
