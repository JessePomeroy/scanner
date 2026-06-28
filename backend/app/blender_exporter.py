"""Blender command-line export helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class BlenderConfig:
    executable: str = "blender"
    script_path: Path | None = None


def export_blender_formats(scan_dir: Path, config: BlenderConfig | None = None) -> None:
    """Run a Blender export script if one has been configured."""
    config = config or BlenderConfig()
    if config.script_path is None:
        return

    subprocess.run(
        [
            config.executable,
            "--background",
            "--python",
            str(config.script_path),
            "--",
            str(scan_dir),
        ],
        check=True,
    )
