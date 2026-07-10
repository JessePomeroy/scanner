"""Owned result-artifact discovery and download path validation."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import mimetypes
import os
from pathlib import Path
import stat as stat_module
from typing import BinaryIO, Mapping, Sequence


class ArtifactUnavailableError(FileNotFoundError):
    """Raised when a job has no usable package directory or artifact file."""


class UnsafeArtifactPathError(ValueError):
    """Raised when a persisted or requested artifact path escapes ownership."""


@dataclass(frozen=True)
class ArtifactDescriptor:
    """A single downloadable file owned by a completed or failed package."""

    name: str
    relative_path: str
    filename: str
    byte_count: int
    media_type: str


@dataclass
class OpenedArtifact:
    """An already-authorized artifact inode held open for response streaming."""

    descriptor: ArtifactDescriptor
    file: BinaryIO


def list_downloadable_artifacts(
    outputs: Mapping[str, str],
    *,
    allowed_package_roots: Sequence[Path],
) -> list[ArtifactDescriptor]:
    """Describe declared output files that still exist inside the package."""
    lexical_package, resolved_package = _package_directory(
        outputs,
        allowed_package_roots=allowed_package_roots,
    )
    artifacts: list[ArtifactDescriptor] = []
    seen_paths: set[str] = set()

    for name, raw_path in sorted(outputs.items()):
        if name == "package_dir":
            continue
        relative_path = _declared_output_relative_path(
            raw_path,
            lexical_package=lexical_package,
            resolved_package=resolved_package,
        )
        if relative_path is None or relative_path in seen_paths:
            continue

        try:
            descriptor, target_stat = _open_owned_regular_file(
                lexical_package,
                resolved_package,
                Path(relative_path),
            )
        except (ArtifactUnavailableError, UnsafeArtifactPathError):
            continue
        os.close(descriptor)

        filename = Path(relative_path).name
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        artifacts.append(
            ArtifactDescriptor(
                name=name,
                relative_path=relative_path,
                filename=filename,
                byte_count=target_stat.st_size,
                media_type=media_type,
            )
        )
        seen_paths.add(relative_path)

    return artifacts


def open_downloadable_artifact(
    outputs: Mapping[str, str],
    relative_path: str,
    *,
    allowed_package_roots: Sequence[Path],
) -> OpenedArtifact:
    """Open one published output without following replaceable pathnames."""
    lexical_package, resolved_package = _package_directory(
        outputs,
        allowed_package_roots=allowed_package_roots,
    )
    path_parts = relative_path.split("/")
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or "\0" in relative_path
        or any(part in {"", ".", ".."} for part in path_parts)
    ):
        raise UnsafeArtifactPathError("Invalid artifact path")

    relative = Path(*path_parts)
    if _contains_symlink(lexical_package, relative):
        raise UnsafeArtifactPathError("Artifact path must not contain symbolic links")

    published = {
        artifact.relative_path: artifact
        for artifact in list_downloadable_artifacts(
            outputs,
            allowed_package_roots=allowed_package_roots,
        )
    }
    artifact = published.get(relative.as_posix())
    if artifact is None:
        raise ArtifactUnavailableError("Artifact file is not a published job output")

    descriptor, target_stat = _open_owned_regular_file(
        lexical_package,
        resolved_package,
        relative,
    )
    try:
        file = os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        os.close(descriptor)
        raise
    return OpenedArtifact(
        descriptor=ArtifactDescriptor(
            name=artifact.name,
            relative_path=artifact.relative_path,
            filename=artifact.filename,
            byte_count=target_stat.st_size,
            media_type=artifact.media_type,
        ),
        file=file,
    )


def rebase_output_paths(
    outputs: Mapping[str, str],
    *,
    old_root: Path,
    new_root: Path,
) -> dict[str, str]:
    """Move owned output declarations from a processing root to its final root."""
    lexical_old_root = _absolute_lexical(old_root)
    resolved_old_root = lexical_old_root.resolve()
    rebased: dict[str, str] = {}

    for name, raw_path in outputs.items():
        declared = Path(raw_path)
        if ".." in declared.parts:
            raise UnsafeArtifactPathError(f"Output '{name}' contains parent traversal")
        lexical_target = _absolute_lexical(
            declared if declared.is_absolute() else lexical_old_root / declared
        )
        try:
            relative = lexical_target.relative_to(lexical_old_root)
        except ValueError as error:
            raise UnsafeArtifactPathError(
                f"Output '{name}' is outside the processing directory"
            ) from error
        if not relative.parts or _contains_symlink(lexical_old_root, relative):
            raise UnsafeArtifactPathError(f"Output '{name}' is not an owned result path")

        resolved_target = lexical_target.resolve()
        if resolved_old_root not in resolved_target.parents:
            raise UnsafeArtifactPathError(
                f"Output '{name}' resolves outside the processing directory"
            )
        rebased[name] = str(new_root / relative)

    return rebased


def discover_standard_output_paths(scan_root: Path) -> dict[str, str]:
    """Recover known single-file results from a completed reconstruction tree."""
    lexical_root = _absolute_lexical(scan_root)
    resolved_root = lexical_root.resolve()
    candidates: list[tuple[str, Path]] = [
        ("scan_report", Path("metadata/scan_report.json")),
        ("textured_mesh", Path("dense/scene_textured.obj")),
    ]
    dense_cloud = Path("dense/fused.ply")
    sparse_cloud = Path("sparse/sparse_points.ply")
    for cloud in (dense_cloud, sparse_cloud):
        try:
            descriptor, _ = _open_owned_regular_file(lexical_root, resolved_root, cloud)
        except (ArtifactUnavailableError, UnsafeArtifactPathError):
            continue
        os.close(descriptor)
        candidates.append(("colmap_output", cloud))
        break

    outputs: dict[str, str] = {}
    for name, relative in candidates:
        try:
            descriptor, _ = _open_owned_regular_file(
                lexical_root,
                resolved_root,
                relative,
            )
        except (ArtifactUnavailableError, UnsafeArtifactPathError):
            continue
        os.close(descriptor)
        outputs[name] = str(resolved_root / relative)
    return outputs


def _package_directory(
    outputs: Mapping[str, str],
    *,
    allowed_package_roots: Sequence[Path],
) -> tuple[Path, Path]:
    raw_package = outputs.get("package_dir")
    if raw_package is None:
        raise ArtifactUnavailableError("No package directory available")

    lexical_package = _absolute_lexical(Path(raw_package))
    if lexical_package.is_symlink():
        raise UnsafeArtifactPathError("Package directory must not be a symbolic link")
    resolved_package = lexical_package.resolve()
    if not resolved_package.is_dir():
        raise ArtifactUnavailableError("Package directory not found")

    resolved_roots = [Path(root).resolve() for root in allowed_package_roots]
    if not any(root in resolved_package.parents for root in resolved_roots):
        raise UnsafeArtifactPathError("Package directory is outside scanner storage")
    return lexical_package, resolved_package


def _declared_output_relative_path(
    raw_path: str,
    *,
    lexical_package: Path,
    resolved_package: Path,
) -> str | None:
    declared = Path(raw_path)
    if ".." in declared.parts:
        return None
    candidates = [_absolute_lexical(declared)]
    if not declared.is_absolute():
        candidates.append(_absolute_lexical(lexical_package / declared))

    for lexical_target in candidates:
        try:
            relative = lexical_target.relative_to(lexical_package)
        except ValueError:
            continue
        if not relative.parts or _contains_symlink(lexical_package, relative):
            continue

        resolved_target = lexical_target.resolve()
        if resolved_package not in resolved_target.parents:
            continue
        return relative.as_posix()
    return None


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _open_owned_regular_file(
    lexical_root: Path,
    resolved_root: Path,
    relative: Path,
) -> tuple[int, os.stat_result]:
    """Open one inode through no-follow directory descriptors."""
    if (
        not relative.parts
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise UnsafeArtifactPathError("Invalid owned file path")
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
    ):
        raise UnsafeArtifactPathError(
            "Secure artifact downloads require POSIX no-follow file descriptors"
        )

    try:
        expected_root_stat = resolved_root.stat(follow_symlinks=False)
    except OSError as error:
        raise ArtifactUnavailableError("Artifact package directory not found") from error

    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_descriptors: list[int] = []
    file_descriptor: int | None = None

    try:
        root_descriptor = os.open(lexical_root, directory_flags)
        directory_descriptors.append(root_descriptor)
        opened_root_stat = os.fstat(root_descriptor)
        if (
            not stat_module.S_ISDIR(opened_root_stat.st_mode)
            or (opened_root_stat.st_dev, opened_root_stat.st_ino)
            != (expected_root_stat.st_dev, expected_root_stat.st_ino)
        ):
            raise UnsafeArtifactPathError("Artifact package changed during authorization")

        for part in relative.parts[:-1]:
            child_descriptor = os.open(
                part,
                directory_flags,
                dir_fd=directory_descriptors[-1],
            )
            directory_descriptors.append(child_descriptor)

        file_descriptor = os.open(
            relative.parts[-1],
            file_flags,
            dir_fd=directory_descriptors[-1],
        )
        target_stat = os.fstat(file_descriptor)
        if not stat_module.S_ISREG(target_stat.st_mode):
            raise ArtifactUnavailableError("Artifact output is not a regular file")
        if target_stat.st_nlink != 1:
            raise UnsafeArtifactPathError("Artifact output must have exactly one hard link")
        return file_descriptor, target_stat
    except (ArtifactUnavailableError, UnsafeArtifactPathError):
        if file_descriptor is not None:
            os.close(file_descriptor)
        raise
    except OSError as error:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if error.errno in {errno.ELOOP, errno.EXDEV}:
            raise UnsafeArtifactPathError(
                "Artifact path must not contain symbolic links"
            ) from error
        raise ArtifactUnavailableError("Artifact file not found") from error
    finally:
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)


def _contains_symlink(base: Path, relative: Path) -> bool:
    current = base
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False
