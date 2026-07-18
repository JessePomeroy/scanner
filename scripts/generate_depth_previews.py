#!/usr/bin/env python3
"""Generate bounded representative depth previews with depth-anything.cpp."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.depth_preview import (  # noqa: E402
    load_preview_frames,
    run_depth_previews,
    select_representative_frames,
)
from app.scan_package import prepare_scan_source, scan_id_from_path, validate_and_report_scan  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path, help="Scan zip or extracted scan directory.")
    parser.add_argument("--runtime", type=Path, required=True, help="Path to the da3-cli binary.")
    parser.add_argument("--model", type=Path, required=True, help="Path to a compatible GGUF model.")
    parser.add_argument("--maximum-frames", type=int, default=12)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Workspace. Defaults to DepthPreviews/<scan-id>.",
    )
    args = parser.parse_args()

    scan_id = scan_id_from_path(args.scan)
    work_dir = (args.work_dir or Path("DepthPreviews") / scan_id).resolve()
    source_dir = work_dir / "source"
    scan_path = args.scan.resolve()
    if scan_path == source_dir or source_dir in scan_path.parents:
        parser.error("--work-dir/source must not contain the input scan")
    if args.scan.is_dir() and (scan_path == work_dir or scan_path in work_dir.parents):
        parser.error("--work-dir must not be the scan directory or inside it")

    scan_root = prepare_scan_source(args.scan, source_dir, reset=True)
    package = validate_and_report_scan(scan_root)
    selected = select_representative_frames(
        load_preview_frames(scan_root),
        maximum=args.maximum_frames,
    )
    output_dir = work_dir / "previews"
    report_path = work_dir / "depth_preview_report.json"
    report = run_depth_previews(
        runtime=args.runtime,
        model=args.model,
        frames=selected,
        output_dir=output_dir,
        report_path=report_path,
        timeout_seconds=args.timeout_seconds,
        threads=args.threads,
    )
    package.record_processing_step(
        "depth_previews",
        {
            "status": "complete",
            "selected_frame_count": report["selected_frame_count"],
            "report": str(report_path),
        },
    )

    print(f"Depth previews: {report['selected_frame_count']} representative frames")
    print(f"Report: {report_path}")
    print(f"Preview directory: {output_dir}")


if __name__ == "__main__":
    main()

