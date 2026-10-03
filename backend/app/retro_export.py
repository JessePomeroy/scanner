"""Create and verify a separate Retro asset without modifying its reconstruction."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from time import perf_counter
from typing import Callable
import zipfile

from app.artifacts import bundle_textured_mesh
from app.heavy_work import guarded, native_kwargs
from app.retro_style import RetroStyle
from app.storage import safe_extract_zip
from app.texture_quality import write_texture_report


REPO = Path(__file__).resolve().parents[2]
PREPARE = REPO / "scripts/blender/prepare_scan_asset.py"
VERIFY = REPO / "scripts/blender/retro_asset.py"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_retro_glb(path: Path) -> dict[str, object]:
    """Check portable GLB color/filter semantics, not just a successful file write."""
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Retro GLB is missing or exceeds 32 MiB")
    with path.open("rb") as stream:
        header = stream.read(20)
        if len(header) != 20:
            raise ValueError("Retro GLB header is incomplete")
        magic, version, length, json_length, chunk_type = struct.unpack("<4sIIII", header)
        if magic != b"glTF" or version != 2 or length != path.stat().st_size or chunk_type != 0x4E4F534A or json_length > length - 20:
            raise ValueError("Retro GLB header is invalid")
        document = json.loads(stream.read(json_length))
    materials = document.get("materials", [])
    images = document.get("images", [])
    samplers = document.get("samplers", [])
    if len(materials) != 1 or "KHR_materials_unlit" not in materials[0].get("extensions", {}):
        raise ValueError("Retro GLB did not preserve its unlit material")
    if len(images) != 1 or "bufferView" not in images[0] or "uri" in images[0]:
        raise ValueError("Retro GLB must embed exactly one texture")
    if not samplers or any(item.get("magFilter") != 9728 or item.get("minFilter") not in (9728, 9984) for item in samplers):
        raise ValueError("Retro GLB did not preserve nearest-neighbor sampling")
    if any("uri" in buffer for buffer in document.get("buffers", [])):
        raise ValueError("Retro GLB references an external buffer")
    return {"embedded_texture": True, "nearest_filter": True, "unlit_material": True}


@guarded("Retro low-poly asset export")
def export_retro_asset(
    source: Path,
    destination: Path,
    style: RetroStyle | None = None,
    *,
    blender: str = "blender",
    timeout_seconds: float = 600,
    progress: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Validate inputs, bake a new asset, and reopen both deliveries before publishing.

    The fresh destination retains logs/partial results if a worker fails. Only
    verified outputs are returned. RAM caps belong to the enclosing service.
    """
    style = style or RetroStyle()
    source = source.absolute()
    destination = destination.absolute()
    if source.suffix.lower() != ".obj" or not source.is_file() or source.resolve() != source:
        raise ValueError("Retro source must be an existing, non-linked textured OBJ")
    if destination.resolve() != destination or destination == source.parent or destination in source.parents:
        raise ValueError("Retro output must be a new directory separate from its source")
    if not 0 < timeout_seconds <= 900:
        raise ValueError("Retro export timeout must be positive and no more than 900 seconds")
    executable = shutil.which(blender)
    if executable is None:
        raise ValueError("Blender is required for Retro export")
    destination.mkdir(parents=True, exist_ok=False)
    notify = progress or (lambda message: None)
    started = perf_counter()
    deadline = started + timeout_seconds
    commands: list[dict[str, object]] = []
    state: dict[str, object] = {"state": "running", "settings": asdict(style), "commands": commands}
    state_path = destination / "export.json"

    def record() -> None:
        state_path.write_text(json.dumps(state, indent=2, allow_nan=False))

    def execute(name: str, arguments: list[str]) -> None:
        notify(f"Retro export: {name}.")
        remaining = deadline - perf_counter()
        if remaining <= 0:
            raise TimeoutError("Retro export exceeded its time limit")
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
                raise TimeoutError("Retro export exceeded its time limit; source is preserved") from error
        entry.update(state="succeeded" if result.returncode == 0 else "failed",
                     elapsed_seconds=perf_counter() - phase_start, returncode=result.returncode)
        if result.returncode:
            raise RuntimeError(f"Retro {name} failed; inspect {name}.log in the preserved output")

    try:
        record()
        blend, glb = destination / "retro.blend", destination / "retro.glb"
        report, verification = destination / "retro-report.json", destination / "verification.json"
        with tempfile.TemporaryDirectory(prefix=".source-", dir=destination) as temporary:
            work = Path(temporary)
            # The existing bundler validates every OBJ/MTL/texture dependency with
            # no-follow file descriptors before Blender sees a private copy.
            archive = bundle_textured_mesh(source, work / "source.zip")
            with zipfile.ZipFile(archive) as bundle:
                hashes = {}
                for name in bundle.namelist():
                    with bundle.open(name) as stream:
                        hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
            if any(sha256(source.parent / name) != digest for name, digest in hashes.items()):
                raise ValueError("Retro source changed while preparing its private copy")
            state["source_sha256"] = hashes
            private = work / "input"
            private.mkdir()
            safe_extract_zip(archive, private)
            args = ["--python", str(PREPARE), "--", str(private / source.name), str(blend),
                    "--export-glb", str(glb), "--asset-style", "retro", "--retro-report", str(report),
                    "--triangle-budget", str(style.triangle_budget), "--texture-size", str(style.texture_size),
                    "--obj-forward-axis", "Y", "--obj-up-axis", "Z", "--origin", "none", "--set-units", "NONE"]
            if style.dither:
                args.append("--dither")
            execute("prepare", args)
            # Verification runs in a fresh process after the source copy becomes
            # unavailable, so packed textures cannot be masked by local files.
        execute("verify", ["--python", str(VERIFY), "--", str(blend), str(glb), str(verification),
                            "--triangle-budget", str(style.triangle_budget), "--texture-size", str(style.texture_size)])
        if json.loads(verification.read_text()).get("status") != "passed":
            raise ValueError("Retro assets did not pass reopening checks")
        state["glb_contract"] = inspect_retro_glb(glb)
        quality = write_texture_report(destination / "retro.obj", destination / "texture-quality.json")
        state["texture_quality"] = quality
        if any(sha256(source.parent / name) != digest for name, digest in hashes.items()):
            raise ValueError("Source reconstruction changed during Retro export")
        bundle = bundle_textured_mesh(destination / "retro.obj", destination / "retro-obj.zip",
                                     evidence_files=(Path("retro-report.json"), Path("texture-quality.json")))
        outputs = {"retro_blend": str(blend), "retro_glb": str(glb), "retro_bundle": str(bundle),
                   "retro_report": str(report), "retro_verification": str(verification), "retro_export_report": str(state_path)}
        state.update(state="succeeded", elapsed_seconds=perf_counter() - started,
                     originals_unchanged=True, output_sha256={name: sha256(Path(path)) for name, path in outputs.items() if name != "retro_export_report"})
        record()
        return outputs
    except Exception as error:
        state.update(state="failed", elapsed_seconds=perf_counter() - started, error=str(error))
        record()
        raise
