#!/usr/bin/env python3
"""Plan reconstruction commands for one scan package without running them."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.reconstruction_backends import BackendPlanConfig, SUPPORTED_BACKENDS, build_backend_plan  # noqa: E402
from app.reconstruction_plan import shell_join, write_command_plan_report  # noqa: E402
from app.scan_package import prepare_scan_source, scan_id_from_path, validate_and_report_scan  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path, help="Scan zip or extracted scan directory.")
    parser.add_argument(
        "--backend",
        choices=SUPPORTED_BACKENDS,
        default="colmap_openmvs",
        help="Reconstruction backend to plan.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Persistent planning workspace. Defaults to ScannerPlans/<scan_id>/<backend>.",
    )
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--matcher", default="sequential_matcher")
    parser.add_argument("--no-gpu", action="store_true", help="Plan COLMAP commands with GPU flags disabled.")
    parser.add_argument("--sparse-only", action="store_true", help="Plan only sparse COLMAP commands.")
    parser.add_argument("--skip-openmvs", action="store_true", help="Skip OpenMVS commands for COLMAP/OpenMVS plans.")
    parser.add_argument("--meshroom-pipeline", default="photogrammetry")
    parser.add_argument("--alicevision-sensor-database", type=Path, default=None)
    args = parser.parse_args()

    scan_id = scan_id_from_path(args.scan)
    work_dir = args.work_dir or Path("ScannerPlans") / scan_id / args.backend
    work_dir.mkdir(parents=True, exist_ok=True)

    scan_root = prepare_scan_source(args.scan, work_dir, reset=False)
    package = validate_and_report_scan(scan_root)
    plan = build_backend_plan(
        scan_root,
        BackendPlanConfig(
            backend=args.backend,
            matcher=args.matcher,
            use_gpu=not args.no_gpu,
            include_dense=not args.sparse_only,
            include_openmvs=not args.skip_openmvs and not args.sparse_only,
            meshroom_pipeline=args.meshroom_pipeline,
            alicevision_sensor_database=args.alicevision_sensor_database,
        ),
    )

    report_path = args.report or package.metadata_dir / f"{args.backend}_plan.json"
    write_command_plan_report(
        plan,
        report_path,
        extra={
            "scan_id": package.scan_id,
            "scan_report": str(package.report_path),
            "work_dir": str(work_dir),
        },
    )

    print(f"Backend: {plan.backend}")
    print(f"Commands: {plan.command_count}")
    print(f"Report: {report_path}")
    for command in plan.commands:
        print(shell_join(command))

    print(f"Work directory: {work_dir}")


if __name__ == "__main__":
    main()
