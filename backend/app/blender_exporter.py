"""Blender command-line export helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from app.heavy_work import heavy_work, native_kwargs


@dataclass(frozen=True)
class BlenderConfig:
    executable: str = "blender"
    script_path: Path | None = None


def export_blender_formats(scan_dir: Path, config: BlenderConfig | None = None) -> None:
    """Run a Blender export script if one has been configured."""
    config = config or BlenderConfig()
    if config.script_path is None:
        return

    with heavy_work(f'Blender export {scan_dir.name}'):
        _export(scan_dir, config)


def _export(scan_dir: Path, config: BlenderConfig) -> None:
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
        **native_kwargs(),
    )
