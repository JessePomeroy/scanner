#!/usr/bin/env python3
"""Plan an experimental neural reconstruction backend without running it."""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.neural_backend_planner import (  # noqa: E402
    DEFAULT_SPLAT_DELIVERY_FORMATS,
    NeuralBackendConfig,
    SUPPORTED_NEURAL_BACKENDS,
    SUPPORTED_SPLAT_DELIVERY_FORMATS,
    build_neural_backend_plan,
    write_neural_backend_report,
)
from app.scan_package import prepare_scan_source, scan_id_from_path, validate_and_report_scan  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path, help="Scan zip or extracted scan directory.")
    parser.add_argument("--backend", choices=SUPPORTED_NEURAL_BACKENDS, required=True)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Persistent planning workspace. Defaults to NeuralPlans/<scan_id>/<backend>.",
    )
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--mast3r-slam-config", default="config/base.yaml")
    parser.add_argument("--depth-anything-encoder", default="vits")
    parser.add_argument("--splat-method", default="splatfacto")
    parser.add_argument("--splat-matching-method", default="sequential")
    parser.add_argument(
        "--splat-delivery-format",
        action="append",
        choices=SUPPORTED_SPLAT_DELIVERY_FORMATS,
        dest="splat_delivery_formats",
        help=(
            "Repeat to select post-export formats. Defaults to SOG and standalone HTML. "
            "gaussian-glb is a KHR_gaussian_splatting asset, not a mesh."
        ),
    )
    args = parser.parse_args()

    scan_id = scan_id_from_path(args.scan)
    work_dir = args.work_dir or Path("NeuralPlans") / scan_id / args.backend
    source_dir = work_dir / "source"
    scan_path = args.scan.resolve()
    source_path = source_dir.resolve()
    if scan_path == source_path or source_path in scan_path.parents:
        parser.error("--work-dir/source must not contain the input scan")
    if args.scan.is_dir():
        if source_path == scan_path or scan_path in source_path.parents:
            parser.error("--work-dir must not be the scan directory or inside the scan directory")

    work_dir.mkdir(parents=True, exist_ok=True)

    scan_root = prepare_scan_source(args.scan, source_dir, reset=True)
    package = validate_and_report_scan(scan_root)
    plan = build_neural_backend_plan(
        scan_root,
        NeuralBackendConfig(
            backend=args.backend,
            mast3r_slam_config=args.mast3r_slam_config,
            depth_anything_encoder=args.depth_anything_encoder,
            splat_method=args.splat_method,
            splat_matching_method=args.splat_matching_method,
            splat_delivery_formats=tuple(
                args.splat_delivery_formats or DEFAULT_SPLAT_DELIVERY_FORMATS
            ),
        ),
    )

    report_path = args.report or package.metadata_dir / f"{args.backend}_neural_plan.json"
    write_neural_backend_report(plan, report_path)

    print(f"Backend: {plan.backend}")
    print(f"Report: {report_path}")
    print(f"Images: {plan.inputs['image_count']}")
    print(f"Videos: {plan.inputs['video_count']}")
    if plan.commands:
        print("Commands:")
        for command in plan.commands:
            print(shlex.join(command))
    else:
        print("Commands: none; see report notes")
    print(f"Work directory: {work_dir}")


if __name__ == "__main__":
    main()
