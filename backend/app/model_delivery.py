"""Turn a textured OBJ into the files a person opens: .blend, GLB and a preview.

The reconstruction is never modified. Blender sees a private, validated copy,
and a second Blender process reopens both deliveries before they are published.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from time import perf_counter
from typing import Callable
import zipfile

from app.artifacts import bundle_textured_mesh
from app.heavy_work import guarded, native_kwargs
from app.storage import safe_extract_zip


REPO = Path(__file__).resolve().parents[2]
PREPARE = REPO / "scripts/blender/prepare_scan_asset.py"
PREVIEW = REPO / "scripts/blender/model_preview.py"
MAX_IMAGES_TXT_BYTES = 512 * 1024 * 1024


def read_camera_view(images_txt: Path, preferred_name: str) -> dict[str, object] | None:
    """Return where one capture photo was taken from, in reconstruction coordinates.

    Reads a COLMAP text model's ``images.txt``. The preferred photo is used when
    it aligned; otherwise the first aligned photo. The preview is then rendered
    from a direction the person actually photographed.
    """
    if images_txt.is_symlink() or not images_txt.is_file() or images_txt.stat().st_size > MAX_IMAGES_TXT_BYTES:
        return None
    chosen: tuple[str, list[float]] | None = None
    expect_pose = True
    with images_txt.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("#"):
                continue
            if not expect_pose:
                # Every pose line is followed by one line of 2D points, possibly empty.
                expect_pose = True
                continue
            if not line.strip():
                continue
            parts = line.split(maxsplit=9)
            if len(parts) != 10:
                return None
            try:
                values = [float(value) for value in parts[1:8]]
            except ValueError:
                return None
            if not all(math.isfinite(value) for value in values):
                return None
            expect_pose = False
            name = parts[9].strip()
            if chosen is None or Path(name).name == preferred_name:
                chosen = (name, values)
            if Path(name).name == preferred_name:
                break
    if chosen is None:
        return None
    name, (qw, qx, qy, qz, tx, ty, tz) = chosen
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if norm == 0:
        return None
    qw, qx, qy, qz = (value / norm for value in (qw, qx, qy, qz))
    # World-to-camera rotation rows; its transpose maps camera axes to the world.
    rotation = (
        (1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)),
        (2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)),
        (2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)),
    )
    location = [-(rotation[0][axis] * tx + rotation[1][axis] * ty + rotation[2][axis] * tz) for axis in range(3)]
    up = [-rotation[1][axis] for axis in range(3)]  # image "up" is camera -Y
    return {"image": name, "location": location, "up": up}


def export_camera_view(
    colmap: str, sparse_model: Path, work_dir: Path, preferred_name: str,
    *, timeout_seconds: float = 120,
) -> dict[str, object] | None:
    """Best effort: any problem returns None and the preview uses a default view."""
    try:
        work_dir.mkdir(parents=True, exist_ok=False)
        with (work_dir / "export.log").open("x") as log:
            result = subprocess.run(
                [colmap, "model_converter", "--input_path", str(sparse_model),
                 "--output_path", str(work_dir), "--output_type", "TXT"],
                cwd=work_dir, stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout_seconds, **native_kwargs(),
            )
        if result.returncode:
            return None
        return read_camera_view(work_dir / "images.txt", preferred_name)
    except (OSError, subprocess.SubprocessError):
        return None


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@guarded("Blender model delivery")
def export_blender_model(
    source: Path,
    destination: Path,
    *,
    view: dict[str, object] | None = None,
    blender: str = "blender",
    timeout_seconds: float = 600,
    progress: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Write model.blend, model.glb and preview.png to a new directory.

    Only outputs that passed reopening are returned. A failed attempt keeps its
    logs and partial files in ``destination``; the source is hash-checked before
    and after. RAM caps belong to the enclosing service.
    """
    source = source.absolute()
    destination = destination.absolute()
    if source.suffix.lower() != ".obj" or not source.is_file() or source.resolve() != source:
        raise ValueError("Model source must be an existing, non-linked textured OBJ")
    if destination.resolve() != destination or destination == source.parent or destination in source.parents:
        raise ValueError("Model output must be a new directory separate from its source")
    if not 0 < timeout_seconds <= 900:
        raise ValueError("Model delivery timeout must be positive and no more than 900 seconds")
    executable = shutil.which(blender)
    if executable is None:
        raise ValueError("Blender is required to make the .blend and GLB files")
    destination.mkdir(parents=True, exist_ok=False)
    notify = progress or (lambda message: None)
    started = perf_counter()
    deadline = started + timeout_seconds
    commands: list[dict[str, object]] = []
    state: dict[str, object] = {"state": "running", "commands": commands}
    state_path = destination / "export.json"

    def record() -> None:
        state_path.write_text(json.dumps(state, indent=2, allow_nan=False))

    def execute(name: str, arguments: list[str]) -> None:
        notify(f"Blender files: {name}.")
        remaining = deadline - perf_counter()
        if remaining <= 0:
            raise TimeoutError("Making the Blender files exceeded its time limit")
        command = [executable, "--background", "--factory-startup", "--disable-autoexec",
                   "--threads", "4", "--python-exit-code", "1", *arguments]
        entry: dict[str, object] = {"stage": name, "argv": command, "state": "running"}
        commands.append(entry)
        record()
        phase_start = perf_counter()
        with (destination / f"{name}.log").open("x") as log:
            try:
                result = subprocess.run(command, cwd=destination, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=remaining, **native_kwargs())
            except subprocess.TimeoutExpired as error:
                entry.update(state="timed_out", elapsed_seconds=perf_counter() - phase_start)
                raise TimeoutError("Making the Blender files exceeded its time limit") from error
        entry.update(state="succeeded" if result.returncode == 0 else "failed",
                     elapsed_seconds=perf_counter() - phase_start, returncode=result.returncode)
        if result.returncode:
            raise RuntimeError(f"Blender {name} failed; inspect {name}.log in the preserved output")

    try:
        record()
        blend, glb = destination / "model.blend", destination / "model.glb"
        preview, report = destination / "preview.png", destination / "verification.json"
        with tempfile.TemporaryDirectory(prefix=".source-", dir=destination) as temporary:
            work = Path(temporary)
            # The bundler validates every OBJ/MTL/texture dependency with no-follow
            # descriptors before Blender sees a private copy.
            archive = bundle_textured_mesh(source, work / "source.zip")
            with zipfile.ZipFile(archive) as bundle:
                hashes = {}
                for name in bundle.namelist():
                    with bundle.open(name) as stream:
                        hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
            if any(sha256(source.parent / name) != digest for name, digest in hashes.items()):
                raise ValueError("Model source changed while preparing its private copy")
            state["source_sha256"] = hashes
            private = work / "input"
            private.mkdir()
            safe_extract_zip(archive, private)
            # Forward Y / up Z keeps reconstruction coordinates, so capture
            # camera positions stay valid for the preview.
            execute("prepare", ["--python", str(PREPARE), "--", str(private / source.name), str(blend),
                                "--export-glb", str(glb), "--obj-forward-axis", "Y", "--obj-up-axis", "Z",
                                "--origin", "none", "--set-units", "NONE"])
        arguments = ["--python", str(PREVIEW), "--", str(blend), str(glb), str(preview), str(report)]
        if view is not None:
            view_path = destination / "view.json"
            view_path.write_text(json.dumps(view, allow_nan=False))
            arguments += ["--view", str(view_path)]
        execute("verify", arguments)
        if json.loads(report.read_text()).get("status") != "passed":
            raise ValueError("Blender files did not pass reopening checks")
        if any(sha256(source.parent / name) != digest for name, digest in hashes.items()):
            raise ValueError("Source reconstruction changed while making the Blender files")
        outputs = {"model_blend": str(blend), "model_glb": str(glb),
                   "model_preview": str(preview), "model_verification": str(report)}
        for name, path in outputs.items():
            if Path(path).is_symlink() or not Path(path).is_file() or Path(path).stat().st_size == 0:
                raise ValueError(f"Blender delivery did not produce {name}")
        state.update(state="succeeded", elapsed_seconds=perf_counter() - started, originals_unchanged=True,
                     output_sha256={name: sha256(Path(path)) for name, path in outputs.items()})
        record()
        return {**outputs, "model_export_report": str(state_path)}
    except Exception as error:
        state.update(state="failed", elapsed_seconds=perf_counter() - started, error=str(error))
        record()
        raise
