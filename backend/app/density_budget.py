"""Bounded PLY point-count inspection for dense reconstruction guardrails."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MAX_PLY_HEADER_BYTES = 64 * 1024


class PointCloudBudgetError(RuntimeError):
    """Raised when a dense point cloud is malformed or exceeds its hard budget."""


@dataclass(frozen=True)
class PointCloudBudgetResult:
    path: Path
    point_count: int
    warning_limit: int
    hard_limit: int

    @property
    def warning(self) -> bool:
        return self.point_count > self.warning_limit

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "point_count": self.point_count,
            "warning_limit": self.warning_limit,
            "hard_limit": self.hard_limit,
            "warning": self.warning,
        }


def inspect_ply_point_budget(
    path: Path,
    *,
    warning_limit: int = 2_000_000,
    hard_limit: int = 10_000_000,
) -> PointCloudBudgetResult:
    """Read only a PLY header and reject clouds above the configured hard limit."""
    if warning_limit < 1:
        raise ValueError("Point-cloud warning limit must be positive")
    if hard_limit < warning_limit:
        raise ValueError("Point-cloud hard limit must be at least the warning limit")
    if not path.is_file():
        raise PointCloudBudgetError(f"Missing dense point cloud: {path}")

    point_count = _read_ply_vertex_count(path)
    if point_count > hard_limit:
        raise PointCloudBudgetError(
            f"Dense point cloud has {point_count} points, exceeding the hard limit of {hard_limit}"
        )
    return PointCloudBudgetResult(path, point_count, warning_limit, hard_limit)


def _read_ply_vertex_count(path: Path) -> int:
    consumed = 0
    vertex_count: int | None = None
    with path.open("rb") as stream:
        first = stream.readline(MAX_PLY_HEADER_BYTES + 1)
        consumed += len(first)
        if first.rstrip(b"\r\n") != b"ply":
            raise PointCloudBudgetError(f"Not a PLY file: {path}")

        while consumed < MAX_PLY_HEADER_BYTES:
            remaining = MAX_PLY_HEADER_BYTES - consumed
            line = stream.readline(remaining + 1)
            consumed += len(line)
            if consumed > MAX_PLY_HEADER_BYTES:
                break
            if not line:
                break
            stripped = line.rstrip(b"\r\n")
            if stripped.startswith(b"element vertex "):
                try:
                    vertex_count = int(stripped.split()[2])
                except (IndexError, ValueError) as error:
                    raise PointCloudBudgetError(f"Invalid PLY vertex count: {path}") from error
            if stripped == b"end_header":
                if vertex_count is None or vertex_count < 0:
                    raise PointCloudBudgetError(f"PLY header has no valid vertex count: {path}")
                return vertex_count

    raise PointCloudBudgetError(f"PLY header exceeds {MAX_PLY_HEADER_BYTES} bytes or is incomplete: {path}")
