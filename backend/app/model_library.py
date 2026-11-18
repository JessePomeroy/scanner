"""Copy a finished model's everyday files to one folder a person can find.

Job workspaces are named by UUID and hold gigabytes of intermediates. The
library holds only the deliverables, one folder per scan, named by capture time.
Copies never replace an existing folder; the job's own files stay where they are.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import re
import shutil
import tempfile


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
    # Copy under a hidden name first so a visible folder is always complete.
    partial = Path(tempfile.mkdtemp(prefix=f".{name}.partial-", dir=library))
    try:
        for source, filename in sources:
            shutil.copyfile(source, partial / filename)
        partial.chmod(0o755)
        for attempt in range(1, 1000):
            folder = library / (name if attempt == 1 else f"{name}-{attempt}")
            if folder.exists() or folder.is_symlink():
                continue
            try:
                os.rename(partial, folder)
            except OSError:
                continue
            return folder
        raise FileExistsError(f"too many library folders named {name}")
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
