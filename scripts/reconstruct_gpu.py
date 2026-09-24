#!/usr/bin/env python3
"""Run the GPU reconstruction workflow on a native Linux RTX workstation."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.colmap_runner import (  # noqa: E402
    ColmapConfig,
    build_colmap_dense_commands,
    build_colmap_sparse_commands,
    prepare_colmap_output_directories,
)
from app.openmvs_runner import (  # noqa: E402
    OpenMVSConfig,
    build_openmvs_commands,
    inspect_openmvs_dense_cloud,
    openmvs_mesh_input_cloud_path,
    validate_openmvs_config_masks,
)
from app.mask_processor import validate_openmvs_masks  # noqa: E402
from app.mask_undistorter import convert_capture_mask_set  # noqa: E402
from app.report_writer import object_scan_summary, write_scan_report  # noqa: E402
from app.scan_package import prepare_scan_source, scan_id_from_path, validate_and_report_scan  # noqa: E402
from app.scan_metadata import load_scan_metadata  # noqa: E402
from app.scan_validator import find_scan_root  # noqa: E402
from PIL import Image  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path, help="Scan zip or extracted scan directory.")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("ScannerOutputs"),
        help="Output folder on a Linux-native filesystem.",
    )
    parser.add_argument(
        "--use-masks",
        action="store_true",
        help="Require a complete OpenMVS-ready mask set under dense/masks.",
    )
    parser.add_argument("--matcher", default="exhaustive_matcher")
    parser.add_argument(
        "--camera-sharing",
        choices=("single", "per-folder"),
        default="single",
        help=(
            "Share one COLMAP camera across every image, or one camera per "
            "image subfolder for grouped mixed-resolution captures."
        ),
    )
    parser.add_argument("--skip-dense", action="store_true")
    parser.add_argument("--skip-openmvs", action="store_true")
    parser.add_argument(
        "--openmvs-point-cloud-source",
        choices=("openmvs_densify", "colmap_fused"),
        default="openmvs_densify",
        help=(
            "Densify in OpenMVS or mesh InterfaceCOLMAP's view-aware import "
            "of COLMAP fused.ply."
        ),
    )
    parser.add_argument(
        "--scope-mode",
        choices=("auto_roi", "unbounded"),
        default="auto_roi",
        help=(
            "Use OpenMVS automatic ROI or retain the full point cloud; "
            "colmap_fused requires unbounded."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print and report commands without executing them.")
    args = parser.parse_args()
    if args.camera_sharing == "per-folder" and args.use_masks:
        parser.error("per-folder camera sharing does not yet support --use-masks")
    if (
        not args.skip_openmvs
        and args.openmvs_point_cloud_source == "colmap_fused"
        and args.scope_mode != "unbounded"
    ):
        parser.error("colmap_fused requires --scope-mode unbounded")
    if (
        not args.skip_openmvs
        and args.openmvs_point_cloud_source == "colmap_fused"
        and args.use_masks
    ):
        parser.error("colmap_fused does not yet support --use-masks")

    initial_scan_id = scan_id_from_path(args.scan)
    run_dir = (args.output_root / initial_scan_id).resolve()
    source_dir = run_dir / "source"
    logs_dir = run_dir / "logs"

    run_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    scan_root = prepare_scan_source(args.scan, source_dir)
    package = validate_and_report_scan(scan_root)
    validation_report = package.validation
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
            package = validate_and_report_scan(scan_root)
            validation_report = package.validation

    camera_groups = None
    if args.camera_sharing == "per-folder":
        camera_groups = group_images_by_resolution(scan_root)

    prepare_colmap_output_directories(scan_root, include_dense=not args.skip_dense)

    colmap_config = ColmapConfig(
        matcher=args.matcher,
        single_camera=args.camera_sharing == "single",
        single_camera_per_folder=args.camera_sharing == "per-folder",
        use_gpu=True,
        geometric_consistency=True,
    )
    commands: list[list[str]] = []
    commands.extend(build_colmap_sparse_commands(scan_root, colmap_config))
    commands.append(build_model_converter_command(scan_root, colmap_config))

    if not args.skip_dense:
        commands.extend(build_colmap_dense_commands(scan_root, colmap_config))

    auto_masks = validation_report.reconstruction_scope is not None
    mask_path = scan_root / "dense" / "masks" if (auto_masks or args.use_masks) else None
    openmvs_config = (
        OpenMVSConfig(
            point_cloud_source=args.openmvs_point_cloud_source,
            scope_mode=args.scope_mode,
            mask_path=mask_path,
        )
        if not args.skip_openmvs
        else OpenMVSConfig()
    )
    if not args.skip_openmvs:
        commands.extend(build_openmvs_commands(scan_root, openmvs_config))

    command_log = logs_dir / "commands.log"
    outputs = expected_outputs(
        scan_root,
        include_dense=not args.skip_dense,
        include_openmvs=not args.skip_openmvs,
        openmvs_config=openmvs_config,
    )

    package_report_path = package.report_path

    started_at = perf_counter()
    density_budget = None
    mask_validation = None
    mask_conversion = None
    openmvs_settings = (
        openmvs_config.report_settings() if not args.skip_openmvs else None
    )
    attempt = {
        "state": "planned" if args.dry_run else "running",
        "matcher": args.matcher,
        "camera_sharing": args.camera_sharing,
        "camera_groups": camera_groups,
        "dry_run": args.dry_run,
        "skip_dense": args.skip_dense,
        "skip_openmvs": args.skip_openmvs,
        "command_count": len(commands),
        "openmvs_settings": openmvs_settings,
    }
    package.record_processing_step("gpu_reconstruction", attempt)
    openmvs_commands = {
        OpenMVSConfig().interface_colmap,
        OpenMVSConfig().densify_point_cloud,
        OpenMVSConfig().reconstruct_mesh,
        OpenMVSConfig().refine_mesh,
        OpenMVSConfig().texture_mesh,
    }
    active_command: list[str] | None = None
    pending_command: list[str] | None = None
    failure_phase: str | None = None
    try:
        for command in commands:
            active_command = None
            pending_command = command
            failure_phase = "preflight"
            if (
                not args.dry_run
                and command[0] == openmvs_config.interface_colmap
                and auto_masks
            ):
                if mask_path.is_dir():
                    mask_conversion = validate_openmvs_masks(
                        mask_path, scan_root / "dense" / "images"
                    )
                else:
                    mask_conversion = convert_capture_mask_set(scan_root)
            if (
                not args.dry_run
                and command[0] == openmvs_config.densify_point_cloud
            ):
                mask_validation = validate_openmvs_config_masks(
                    scan_root, openmvs_config
                )
            if (
                not args.dry_run
                and command[0] == openmvs_config.reconstruct_mesh
            ):
                density_budget = inspect_openmvs_dense_cloud(
                    scan_root, openmvs_config
                )
            active_command = command
            failure_phase = "execution"
            run_command(
                command,
                command_log=command_log,
                dry_run=args.dry_run,
                cwd=scan_root / "dense" if command[0] in openmvs_commands else None,
            )
            active_command = None
            pending_command = None
            failure_phase = None
    except BaseException as error:
        try:
            package.record_processing_step(
                "gpu_reconstruction",
                {
                    **attempt,
                    "state": "failed",
                    "elapsed_seconds": perf_counter() - started_at,
                    "failed_command": active_command,
                    "blocked_command": (
                        pending_command if active_command is None else None
                    ),
                    "failure_phase": failure_phase,
                    "failure": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                },
            )
        except Exception:
            pass
        raise
    elapsed_seconds = perf_counter() - started_at
    package.record_processing_step(
        "gpu_reconstruction",
        {
            **attempt,
            "state": "planned" if args.dry_run else "complete",
            "elapsed_seconds": elapsed_seconds,
            "density_budget": density_budget.as_dict() if density_budget is not None else None,
            "mask_validation": mask_validation.as_dict() if mask_validation is not None else None,
            "mask_conversion": mask_conversion.as_dict() if mask_conversion is not None else None,
        },
    )

    if not args.dry_run:
        package_report_path = write_scan_report(scan_root, validation_report)
    else:
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
        "camera_sharing": args.camera_sharing,
        "camera_groups": camera_groups,
        "openmvs_settings": openmvs_settings,
        "density_budget": density_budget.as_dict() if density_budget is not None else None,
        "mask_validation": mask_validation.as_dict() if mask_validation is not None else None,
        "mask_conversion": mask_conversion.as_dict() if mask_conversion is not None else None,
        "outputs": {key: str(path) for key, path in outputs.items()},
        "notes": [
            "Use this runner on native Linux with a CUDA-enabled COLMAP build.",
            "Keep active reconstruction workspaces on a Linux-native filesystem.",
        ],
    }

    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(reconstruction_report, indent=2))

    print(f"Report: {report_path}")
    for key, path in outputs.items():
        print(f"{key}: {path}")


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


def group_images_by_resolution(scan_root: Path) -> dict[str, int]:
    """Group a validated copied scan by dimensions for per-folder intrinsics."""
    scan_root = scan_root.resolve()
    images_dir = scan_root / "images"
    metadata = load_scan_metadata(scan_root / "metadata")
    if metadata.reconstruction_scope is not None:
        raise ValueError("Per-folder camera grouping does not yet support capture masks")
    planned_moves: list[tuple[Path, Path]] = []
    group_counts: dict[str, int] = {}

    for frame in metadata.frames:
        source = (scan_root / frame.image).resolve()
        if source.parent != images_dir:
            raise ValueError(
                "Per-folder camera grouping requires flat validated image paths"
            )
        with Image.open(source) as image:
            actual_resolution = image.size
        if actual_resolution != frame.resolution:
            raise ValueError(
                f"Image resolution mismatch for {frame.image}: "
                f"metadata={frame.resolution}, actual={actual_resolution}"
            )
        group = f"{actual_resolution[0]}x{actual_resolution[1]}"
        destination = images_dir / group / source.name
        if destination.exists():
            raise FileExistsError(f"Camera-group destination exists: {destination}")
        planned_moves.append((source, destination))
        group_counts[group] = group_counts.get(group, 0) + 1

    for source, destination in planned_moves:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)

    return dict(sorted(group_counts.items()))


def run_command(
    command: list[str],
    *,
    command_log: Path,
    dry_run: bool,
    cwd: Path | None = None,
) -> None:
    line = shell_join(command)
    with command_log.open("a") as log:
        log.write(line + "\n")

    print(line)
    if dry_run:
        return

    subprocess.run(command, check=True, cwd=cwd)


def expected_outputs(
    scan_root: Path,
    *,
    include_dense: bool,
    include_openmvs: bool,
    openmvs_config: OpenMVSConfig | None = None,
) -> dict[str, Path]:
    outputs = {
        "sparse_model": scan_root / "sparse" / "0",
        "sparse_point_cloud": scan_root / "sparse" / "sparse_points.ply",
    }

    if include_dense:
        outputs["dense_point_cloud"] = scan_root / "dense" / "fused.ply"

    if include_openmvs:
        outputs["openmvs_dense_point_cloud"] = openmvs_mesh_input_cloud_path(
            scan_root, openmvs_config
        )
        outputs["textured_mesh"] = scan_root / "dense" / "scene_textured.obj"

    return outputs


def validation_summary(report: Any) -> dict[str, Any] | None:
    if report is None:
        return None

    return {
        "image_count": report.image_count,
        "frame_count": report.frame_count,
        "video_count": report.video_count,
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
