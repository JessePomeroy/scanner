#!/usr/bin/env python3
"""Run the GPU reconstruction workflow for WSL2/Windows workstations."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.colmap_runner import (  # noqa: E402
    ColmapConfig,
    build_colmap_dense_commands,
    build_colmap_sparse_commands,
)
from app.openmvs_runner import OpenMVSConfig, build_openmvs_commands  # noqa: E402
from app.report_writer import object_scan_summary, write_scan_report  # noqa: E402
from app.scan_validator import find_scan_root, validate_scan_package  # noqa: E402
from app.storage import safe_extract_zip  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path, help="Scan zip or extracted scan directory.")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("ScannerOutputs"),
        help="Output folder, preferably under /mnt/c/... for Windows Blender access.",
    )
    parser.add_argument("--matcher", default="exhaustive_matcher")
    parser.add_argument("--skip-dense", action="store_true")
    parser.add_argument("--skip-openmvs", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print and report commands without executing them.")
    args = parser.parse_args()

    initial_scan_id = scan_id_from_path(args.scan)
    run_dir = (args.output_root / initial_scan_id).resolve()
    source_dir = run_dir / "source"
    logs_dir = run_dir / "logs"

    run_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    prepare_source(args.scan, source_dir)

    scan_root = find_scan_root(source_dir)
    validation_report = validate_scan_package(scan_root)
    scan_id = validation_report.scan_id if validation_report and validation_report.scan_id else initial_scan_id
    if scan_id != initial_scan_id:
        final_run_dir = (args.output_root / scan_id).resolve()
        if final_run_dir != run_dir:
            final_run_dir.parent.mkdir(parents=True, exist_ok=True)
            if final_run_dir.exists():
                shutil.rmtree(final_run_dir)
            shutil.move(str(run_dir), str(final_run_dir))
            run_dir = final_run_dir
            source_dir = run_dir / "source"
            logs_dir = run_dir / "logs"
            scan_root = find_scan_root(source_dir)
            validation_report = validate_scan_package(scan_root)

    colmap_config = ColmapConfig(
        matcher=args.matcher,
        use_gpu=True,
        geometric_consistency=True,
    )
    commands: list[list[str]] = []
    commands.extend(build_colmap_sparse_commands(scan_root, colmap_config))
    commands.append(build_model_converter_command(scan_root, colmap_config))

    if not args.skip_dense:
        commands.extend(build_colmap_dense_commands(scan_root, colmap_config))

    if not args.skip_openmvs:
        commands.extend(build_openmvs_commands(scan_root, OpenMVSConfig()))

    command_log = logs_dir / "commands.log"
    outputs = expected_outputs(scan_root, include_dense=not args.skip_dense, include_openmvs=not args.skip_openmvs)

    package_report_path = write_scan_report(scan_root, validation_report)

    for command in commands:
        run_command(command, command_log=command_log, dry_run=args.dry_run)

    if not args.dry_run:
        package_report_path = write_scan_report(scan_root, validation_report)

    package_report = json.loads(package_report_path.read_text())
    object_summary = object_scan_summary(validation_report)
    reconstruction_report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scan_id": scan_id,
        "scan_path": str(args.scan),
        "run_dir": str(run_dir),
        "scan_root": str(scan_root),
        "dry_run": args.dry_run,
        "validation": validation_summary(validation_report),
        "scan_report": str(package_report_path),
        "object_scan": object_summary,
        "warnings": package_report.get("warnings", []),
        "commands": commands,
        "outputs": {key: str(path) for key, path in outputs.items()},
        "notes": [
            "Use this runner inside WSL2 with a CUDA-enabled COLMAP build.",
            "Open output paths through Windows if output-root is under /mnt/c.",
        ],
    }

    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(reconstruction_report, indent=2))

    print(f"Report: {report_path}")
    for key, path in outputs.items():
        print(f"{key}: {path}")


def scan_id_from_path(path: Path) -> str:
    name = path.name
    if name.lower().endswith(".zip"):
        name = name[:-4]
    return name.replace(" ", "_")


def prepare_source(scan: Path, source_dir: Path) -> None:
    if any(source_dir.iterdir()):
        shutil.rmtree(source_dir)
        source_dir.mkdir(parents=True)

    if scan.suffix.lower() == ".zip":
        safe_extract_zip(scan, source_dir)
    elif scan.is_dir():
        shutil.copytree(scan, source_dir / scan.name)
    else:
        raise SystemExit(f"Scan path is not a zip or directory: {scan}")


def build_model_converter_command(scan_root: Path, config: ColmapConfig) -> list[str]:
    return [
        config.executable,
        "model_converter",
        "--input_path",
        str(scan_root / "sparse" / "0"),
        "--output_path",
        str(scan_root / "sparse" / "sparse_points.ply"),
        "--output_type",
        "PLY",
    ]


def run_command(command: list[str], *, command_log: Path, dry_run: bool) -> None:
    line = shell_join(command)
    with command_log.open("a") as log:
        log.write(line + "\n")

    print(line)
    if dry_run:
        return

    subprocess.run(command, check=True)


def expected_outputs(scan_root: Path, *, include_dense: bool, include_openmvs: bool) -> dict[str, Path]:
    outputs = {
        "sparse_model": scan_root / "sparse" / "0",
        "sparse_point_cloud": scan_root / "sparse" / "sparse_points.ply",
    }

    if include_dense:
        outputs["dense_point_cloud"] = scan_root / "dense" / "fused.ply"

    if include_openmvs:
        outputs["textured_mesh"] = scan_root / "dense" / "scene_textured.obj"

    return outputs


def validation_summary(report: Any) -> dict[str, Any] | None:
    if report is None:
        return None

    return {
        "image_count": report.image_count,
        "frame_count": report.frame_count,
        "scan_mode": report.scan_mode,
        "object_center_world": report.object_center_world,
        "object_radius_meters": report.object_radius_meters,
    }


def shell_join(command: list[str]) -> str:
    return " ".join(quote(part) for part in command)


def quote(value: str) -> str:
    if value and all(character.isalnum() or character in "-_./:=+" for character in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    main()
