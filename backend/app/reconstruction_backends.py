"""Backend registry for reconstruction command planning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.alicevision_runner import AliceVisionConfig, build_alicevision_plan
from app.colmap_runner import ColmapConfig, build_colmap_dense_commands, build_colmap_sparse_commands
from app.meshroom_runner import MeshroomConfig, build_meshroom_plan
from app.openmvs_runner import (
    OpenMVSConfig,
    OpenMVSPointCloudSource,
    OpenMVSScopeMode,
    build_openmvs_commands,
    openmvs_mesh_input_cloud_path,
)
from app.reconstruction_plan import CommandPlan


SUPPORTED_BACKENDS = ("colmap_openmvs", "meshroom", "alicevision")


@dataclass(frozen=True)
class BackendPlanConfig:
    backend: str = "colmap_openmvs"
    matcher: str = "sequential_matcher"
    use_gpu: bool = True
    include_dense: bool = True
    include_openmvs: bool = True
    meshroom_pipeline: str = "photogrammetry"
    alicevision_sensor_database: Path | None = None
    openmvs_point_cloud_source: OpenMVSPointCloudSource = "openmvs_densify"
    openmvs_scope_mode: OpenMVSScopeMode = "auto_roi"


def build_backend_plan(scan_dir: Path, config: BackendPlanConfig | None = None) -> CommandPlan:
    """Build a command plan for a supported reconstruction backend."""
    config = config or BackendPlanConfig()

    if config.backend == "colmap_openmvs":
        return build_colmap_openmvs_plan(scan_dir, config)

    if config.backend == "meshroom":
        return build_meshroom_plan(
            scan_dir,
            MeshroomConfig(pipeline=config.meshroom_pipeline),
        )

    if config.backend == "alicevision":
        return build_alicevision_plan(
            scan_dir,
            AliceVisionConfig(sensor_database=config.alicevision_sensor_database),
        )

    supported = ", ".join(SUPPORTED_BACKENDS)
    raise ValueError(f"Unsupported reconstruction backend '{config.backend}'. Supported backends: {supported}")


def build_colmap_openmvs_plan(scan_dir: Path, config: BackendPlanConfig) -> CommandPlan:
    if config.include_openmvs and not config.include_dense:
        raise ValueError("OpenMVS planning requires COLMAP dense commands. Set include_dense=True or include_openmvs=False.")

    scan_dir = scan_dir.resolve()
    colmap_config = ColmapConfig(
        matcher=config.matcher,
        use_gpu=config.use_gpu,
        geometric_consistency=True,
    )
    commands = build_colmap_sparse_commands(scan_dir, colmap_config)
    commands.append(build_model_converter_command(scan_dir, colmap_config))

    if config.include_dense:
        commands.extend(build_colmap_dense_commands(scan_dir, colmap_config))

    openmvs_config: OpenMVSConfig | None = None
    if config.include_openmvs:
        openmvs_config = OpenMVSConfig(
            point_cloud_source=config.openmvs_point_cloud_source,
            scope_mode=config.openmvs_scope_mode,
        )
        commands.extend(build_openmvs_commands(scan_dir, openmvs_config))

    outputs = {
        "sparse_model": scan_dir / "sparse" / "0",
        "sparse_point_cloud": scan_dir / "sparse" / "sparse_points.ply",
    }
    if config.include_dense:
        outputs["dense_point_cloud"] = scan_dir / "dense" / "fused.ply"
    if config.include_openmvs:
        assert openmvs_config is not None
        outputs["openmvs_dense_point_cloud"] = openmvs_mesh_input_cloud_path(
            scan_dir, openmvs_config
        )
        outputs["textured_mesh"] = scan_dir / "dense" / "scene_textured.obj"

    openmvs_note = (
        "OpenMVS meshing explicitly reuses InterfaceCOLMAP's view-aware import "
        "of the COLMAP fused cloud; DensifyPointCloud is skipped."
        if openmvs_config is not None
        and openmvs_config.point_cloud_source == "colmap_fused"
        else (
            "OpenMVS dense reconstruction uses explicit automatic ROI and "
            "visibility filtering defaults."
            if openmvs_config is not None
            else "OpenMVS commands are omitted from this plan."
        )
    )

    return CommandPlan(
        backend="colmap_openmvs",
        scan_root=scan_dir,
        commands=commands,
        outputs=outputs,
        notes=[
            "Primary production reconstruction path.",
            "Run dense COLMAP and OpenMVS on native Linux with CUDA-capable tools.",
            openmvs_note,
        ],
        settings={
            "matcher": config.matcher,
            "use_gpu": config.use_gpu,
            "openmvs": (
                openmvs_config.report_settings()
                if openmvs_config is not None
                else None
            ),
        },
    )


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
