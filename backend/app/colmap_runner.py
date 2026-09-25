"""COLMAP command runner for local reconstruction jobs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import tempfile
from typing import Callable, NotRequired, TypedDict

from PIL import Image

from app.scan_validator import SUPPORTED_IMAGE_SUFFIXES
from app.heavy_work import guarded, native_kwargs


@dataclass(frozen=True)
class ColmapConfig:
    executable: str = "colmap"
    matcher: str = "sequential_matcher"
    single_camera: bool = True
    single_camera_per_folder: bool = False
    use_gpu: bool = True
    geometric_consistency: bool = True
    feature_mask_path: Path | None = None
    stereo_fusion_mask_path: Path | None = None


class ColmapIntakeReport(TypedDict):
    schema_version: str
    status: str
    input_image_count: int
    imported_image_count: int
    camera_count: int
    resolution_groups: dict[str, int]
    camera_sharing: NotRequired[str]
    registered_image_count: NotRequired[int]
    unregistered_image_count: NotRequired[int]


def run_command(command: list[str], cwd: Path | None = None) -> None:
    """Run a reconstruction command and fail on non-zero exit."""
    subprocess.run(command, cwd=cwd, check=True, **native_kwargs())


def build_colmap_commands(scan_dir: Path, config: ColmapConfig | None = None) -> list[list[str]]:
    """Build the COLMAP command sequence for a scan directory."""
    return build_colmap_sparse_commands(scan_dir, config) + build_colmap_dense_commands(scan_dir, config)


def build_colmap_sparse_commands(scan_dir: Path, config: ColmapConfig | None = None) -> list[list[str]]:
    """Build the COLMAP sparse reconstruction command sequence."""
    config = config or ColmapConfig()
    if config.single_camera and config.single_camera_per_folder:
        raise ValueError("COLMAP camera sharing cannot be both single and per-folder")
    image_path = scan_dir / "images"
    database_path = scan_dir / "database.db"
    sparse_path = scan_dir / "sparse"

    feature_extractor = [
        config.executable,
        "feature_extractor",
        "--database_path",
        str(database_path),
        "--image_path",
        str(image_path),
        "--ImageReader.single_camera",
        "1" if config.single_camera else "0",
        "--ImageReader.single_camera_per_folder",
        "1" if config.single_camera_per_folder else "0",
        "--FeatureExtraction.use_gpu",
        "1" if config.use_gpu else "0",
    ]
    if config.feature_mask_path is not None:
        feature_extractor.extend(
            ["--ImageReader.mask_path", str(config.feature_mask_path.resolve())]
        )

    matcher = [
        config.executable,
        config.matcher,
        "--database_path",
        str(database_path),
        "--FeatureMatching.use_gpu",
        "1" if config.use_gpu else "0",
    ]

    mapper = [
        config.executable,
        "mapper",
        "--database_path",
        str(database_path),
        "--image_path",
        str(image_path),
        "--output_path",
        str(sparse_path),
    ]

    return [
        feature_extractor,
        matcher,
        mapper,
    ]


def build_colmap_dense_commands(scan_dir: Path, config: ColmapConfig | None = None) -> list[list[str]]:
    """Build the COLMAP dense reconstruction command sequence."""
    config = config or ColmapConfig()
    image_path = scan_dir / "images"
    sparse_path = scan_dir / "sparse"
    dense_path = scan_dir / "dense"

    image_undistorter = [
        config.executable,
        "image_undistorter",
        "--image_path",
        str(image_path),
        "--input_path",
        str(sparse_path / "0"),
        "--output_path",
        str(dense_path),
        "--output_type",
        "COLMAP",
    ]

    patch_match_stereo = [
        config.executable,
        "patch_match_stereo",
        "--workspace_path",
        str(dense_path),
        "--workspace_format",
        "COLMAP",
        "--PatchMatchStereo.geom_consistency",
        "true" if config.geometric_consistency else "false",
    ]

    stereo_fusion = [
        config.executable,
        "stereo_fusion",
        "--workspace_path",
        str(dense_path),
        "--workspace_format",
        "COLMAP",
        "--input_type",
        "geometric" if config.geometric_consistency else "photometric",
        "--output_path",
        str(dense_path / "fused.ply"),
    ]
    if config.stereo_fusion_mask_path is not None:
        stereo_fusion.extend(
            ["--StereoFusion.mask_path", str(config.stereo_fusion_mask_path.resolve())]
        )

    return [
        image_undistorter,
        patch_match_stereo,
        stereo_fusion,
    ]


def prepare_colmap_output_directories(
    scan_dir: Path,
    *,
    include_dense: bool = True,
) -> None:
    """Create the output directories required by generated COLMAP commands."""
    scan_dir = scan_dir.resolve()
    (scan_dir / "sparse").mkdir(parents=True, exist_ok=True)
    if include_dense:
        (scan_dir / "dense").mkdir(parents=True, exist_ok=True)


@guarded('COLMAP sparse reconstruction')
def run_colmap_sparse_pipeline(scan_dir: Path, config: ColmapConfig | None = None) -> Path:
    """Run feature extraction, matching, and sparse mapping."""
    scan_dir = scan_dir.resolve()
    config = config or ColmapConfig()
    prepare_colmap_output_directories(scan_dir, include_dense=False)

    commands, groups = prepare_colmap_sparse_execution(scan_dir, config)
    for command in commands[:-2]:
        run_command(command)
    record_colmap_intake(scan_dir, groups, config)
    for command in commands[-2:]:
        run_command(command)
    record_colmap_intake(scan_dir, groups, config, include_registration=True)
    return scan_dir / "sparse" / "0"


def prepare_colmap_sparse_execution(
    scan_dir: Path, config: ColmapConfig
) -> tuple[list[list[str]], dict[str, list[str]]]:
    """Prepare replayable feature batches shared by API and GPU execution plans."""
    feature, matcher, mapper = build_colmap_sparse_commands(scan_dir, config)
    groups = inspect_colmap_images(scan_dir)
    # Separate reader instances give each resolution its own shared camera.
    # Image names remain unchanged, preserving masks and sequential matching's
    # filename order; folder regrouping would disrupt both contracts.
    features = []
    if config.single_camera and len(groups) > 1:
        list_directory = scan_dir / "metadata"
        if list_directory.is_symlink():
            raise ValueError("COLMAP image-list directory must not be a symbolic link")
        list_directory.mkdir(parents=True, exist_ok=True)
        for resolution, names in groups.items():
            image_list = list_directory / f"colmap_images_{resolution}.txt"
            content = "\n".join(names) + "\n"
            try:
                with image_list.open("x", encoding="utf-8") as file:
                    file.write(content)
            except FileExistsError:
                if image_list.is_symlink() or image_list.read_text(encoding="utf-8") != content:
                    raise ValueError(f"Conflicting COLMAP image list: {image_list}")
            features.append(feature + ["--image_list_path", str(image_list)])
    else:
        features.append(feature)
    return [*features, matcher, mapper], groups


def record_colmap_intake(
    scan_dir: Path, groups: dict[str, list[str]], config: ColmapConfig,
    *, include_registration: bool = False,
) -> ColmapIntakeReport:
    """Check native acceptance at the same stage boundary in every runner."""
    intake = verify_colmap_image_intake(scan_dir, groups)
    intake["camera_sharing"] = (
        "per_resolution" if config.single_camera and len(groups) > 1
        else "single" if config.single_camera
        else "per_folder" if config.single_camera_per_folder
        else "native_default"
    )
    if include_registration:
        model = scan_dir / "sparse" / "0" / "images.bin"
        try:
            with model.open("rb") as file:
                registered = struct.unpack("<Q", file.read(8))[0]
        except (OSError, struct.error) as error:
            raise ValueError("COLMAP did not produce a readable registered-image model") from error
        if registered < 2 or registered > intake["input_image_count"]:
            raise ValueError(f"Invalid COLMAP registered-image count: {registered}")
        intake["registered_image_count"] = registered
        intake["unregistered_image_count"] = intake["input_image_count"] - registered
        intake["status"] = "checks_passed" if registered == intake["input_image_count"] else "needs_review"
    _write_intake_report(scan_dir, intake)
    return intake


def inspect_colmap_images(scan_dir: Path) -> dict[str, list[str]]:
    """Decode supported inputs and group equal pixel dimensions without moving files."""
    images = scan_dir / "images"
    groups: dict[str, list[str]] = {}
    for path in sorted(images.rglob("*")):
        if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES or not path.is_file():
            continue
        relative = path.relative_to(images)
        if any((images / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
            raise ValueError(f"COLMAP input must not be a symbolic link: {relative}")
        name = relative.as_posix()
        if "\n" in name or "\r" in name:
            raise ValueError("COLMAP image names cannot contain line breaks")
        try:
            with Image.open(path) as image:
                image.load()
                width, height = image.size
        except OSError as error:
            raise ValueError(f"Unable to decode COLMAP input: {name}") from error
        groups.setdefault(f"{width}x{height}", []).append(name)
    if not groups:
        raise ValueError("No decodable COLMAP input images")
    return groups


def verify_colmap_image_intake(
    scan_dir: Path, groups: dict[str, list[str]]
) -> ColmapIntakeReport:
    """Reject native exit-zero skips before matching or expensive reconstruction."""
    expected = {name for names in groups.values() for name in names}
    database = scan_dir / "database.db"
    if database.is_symlink():
        raise ValueError("COLMAP database must not be a symbolic link")
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT name, camera_id FROM images").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ValueError("Unable to verify COLMAP image intake") from error
    imported = {name for name, _ in rows}
    missing, unexpected = sorted(expected - imported), sorted(imported - expected)
    if missing or unexpected or len(rows) != len(expected):
        raise ValueError(
            f"COLMAP image intake mismatch: missing {missing[:8]}, unexpected {unexpected[:8]} "
            f"({len(imported)}/{len(expected)} imported)"
        )
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            camera_rows = connection.execute(
                "SELECT images.name, cameras.width, cameras.height FROM images "
                "JOIN cameras ON images.camera_id = cameras.camera_id"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ValueError("Unable to verify COLMAP input camera dimensions") from error
    expected_dimensions = {
        name: tuple(int(dimension) for dimension in resolution.split("x"))
        for resolution, names in groups.items() for name in names
    }
    if len(camera_rows) != len(expected) or any(
        expected_dimensions[name] != (width, height) for name, width, height in camera_rows
    ):
        raise ValueError("COLMAP camera dimensions do not match the decoded inputs")
    return {
        "schema_version": "1.0",
        "status": "imported",
        "input_image_count": len(expected),
        "imported_image_count": len(rows),
        "camera_count": len({camera for _, camera in rows}),
        "resolution_groups": {key: len(names) for key, names in groups.items()},
    }


def _write_intake_report(scan_dir: Path, report: ColmapIntakeReport) -> None:
    destination = scan_dir / "metadata" / "colmap_intake.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(report, file, indent=2, sort_keys=True)
            file.write("\n")
            file.close()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


@guarded('COLMAP dense reconstruction')
def run_colmap_dense_pipeline(
    scan_dir: Path,
    config: ColmapConfig | None = None,
    *,
    after_undistort: Callable[[], None] | None = None,
) -> Path:
    """Run image undistortion, dense stereo, and dense point cloud fusion."""
    scan_dir = scan_dir.resolve()
    prepare_colmap_output_directories(scan_dir)

    for index, command in enumerate(build_colmap_dense_commands(scan_dir, config)):
        run_command(command)
        if index == 0 and after_undistort is not None:
            after_undistort()

    return scan_dir / "dense" / "fused.ply"


@guarded('COLMAP sparse export')
def export_sparse_point_cloud(scan_dir: Path, config: ColmapConfig | None = None) -> Path:
    """Export the sparse COLMAP model to a PLY point cloud."""
    config = config or ColmapConfig()
    scan_dir = scan_dir.resolve()
    sparse_model = scan_dir / "sparse" / "0"
    output_path = scan_dir / "sparse" / "sparse_points.ply"

    run_command(
        [
            config.executable,
            "model_converter",
            "--input_path",
            str(sparse_model),
            "--output_path",
            str(output_path),
            "--output_type",
            "PLY",
        ]
    )

    return output_path


@guarded('COLMAP reconstruction')
def run_colmap_pipeline(
    scan_dir: Path,
    config: ColmapConfig | None = None,
    *,
    include_dense: bool = True,
    after_undistort: Callable[[], None] | None = None,
) -> Path:
    """Run COLMAP and return the most complete output path produced."""
    run_colmap_sparse_pipeline(scan_dir, config)

    if include_dense:
        return run_colmap_dense_pipeline(
            scan_dir,
            config,
            after_undistort=after_undistort,
        )

    return export_sparse_point_cloud(scan_dir, config)
