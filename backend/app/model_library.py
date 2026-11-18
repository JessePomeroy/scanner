"""Copy a finished model's everyday files to one folder a person can find.

Job workspaces are named by UUID and hold gigabytes of intermediates. The
library holds only the deliverables, one folder per scan, named by capture time.
Copies never replace an existing folder; the job's own files stay where they are.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
import shutil


LIBRARY_FILES = (
    ("model_blend", "{name}.blend"),
    ("model_glb", "{name}.glb"),
    ("model_preview", "preview.png"),
    ("textured_bundle", "{name}-obj.zip"),
    ("retro_blend", "{name}-retro.blend"),
    ("retro_glb", "{name}-retro.glb"),
)
_CAPTURE_NAME = re.compile(r"^scan_\d{4}(?:_\d{2}){5}")


def library_name(scan_name: str) -> str:
    """Prefer the phone's capture timestamp; otherwise a filesystem-safe name."""
    match = _CAPTURE_NAME.match(scan_name)
    if match:
        return match.group(0)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", scan_name).strip("-.")[:80]
    return safe or "scan"


def publish_to_library(library: Path, scan_name: str, outputs: Mapping[str, str]) -> Path:
    """Copy available deliverables into a new folder and return it."""
    name = library_name(scan_name)
    sources = []
    for key, pattern in LIBRARY_FILES:
        raw = outputs.get(key)
        if raw is None:
            continue
        source = Path(raw)
        if source.is_symlink() or not source.is_file():
            continue
        sources.append((source, pattern.format(name=name)))
    if not sources:
        raise ValueError("no finished model files to copy")
    library.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 1000):
        folder = library / (name if attempt == 1 else f"{name}-{attempt}")
        try:
            folder.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"too many library folders named {name}")
    try:
        for source, filename in sources:
            shutil.copyfile(source, folder / filename)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder
