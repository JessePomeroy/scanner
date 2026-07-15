"""Reproducible evidence recording for paired reconstruction benchmarks."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = "0.1.0"
DAYTIME_MAX_SECONDS = 3 * 60 * 60
PRACTICAL_LIMIT_SECONDS = 12 * 60 * 60

DEFAULT_TOOL_PROBES: dict[str, list[str]] = {
    "nvidia": [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total",
        "--format=csv,noheader",
    ],
    "colmap": ["colmap", "-h"],
    "openmvs": ["InterfaceCOLMAP", "--help"],
    "blender": ["blender", "--version"],
    "nerfstudio": ["ns-train", "--help"],
    "node": ["node", "--version"],
    "splat_transform": ["splat-transform", "--help"],
}


class BenchmarkEvidenceError(ValueError):
    """Raised when benchmark evidence would be ambiguous or non-reproducible."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def verified_input(path: Path, expected_sha256: str) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise BenchmarkEvidenceError(f"Benchmark input is not a regular file: {path}")
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise BenchmarkEvidenceError("Expected input SHA-256 must be 64 hexadecimal characters")
    actual = sha256_file(path)
    if not hmac.compare_digest(actual, expected):
        raise BenchmarkEvidenceError(
            f"Benchmark input SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return {
        "path": str(path),
        "sha256": actual,
        "size_bytes": path.stat().st_size,
    }


def runtime_guidance(
    estimated_seconds: float | None,
    *,
    confidence: str = "low",
) -> dict[str, Any]:
    if confidence not in {"low", "medium", "high"}:
        raise BenchmarkEvidenceError("Runtime confidence must be low, medium, or high")
    if estimated_seconds is None:
        return {
            "estimated_seconds": None,
            "confidence": confidence,
            "classification": "uncalibrated",
            "overnight_recommended": False,
            "practical_limit_warning": False,
            "message": "No calibrated RTX 3070 estimate is available yet.",
        }
    if estimated_seconds < 0:
        raise BenchmarkEvidenceError("Estimated runtime cannot be negative")
    if estimated_seconds <= DAYTIME_MAX_SECONDS:
        classification = "daytime"
        message = "Normal daytime candidate (three hours or less)."
        overnight = False
        limit_warning = False
    elif estimated_seconds < PRACTICAL_LIMIT_SECONDS:
        classification = "overnight_candidate"
        message = "Schedule overnight unless an attended run is preferred."
        overnight = True
        limit_warning = False
    else:
        classification = "practical_limit_warning"
        message = "Estimate reaches or exceeds the 12-hour practical limit."
        overnight = True
        limit_warning = True
    return {
        "estimated_seconds": estimated_seconds,
        "confidence": confidence,
        "classification": classification,
        "overnight_recommended": overnight,
        "practical_limit_warning": limit_warning,
        "message": message,
    }


def git_revision(repo_root: Path, revision: str = "HEAD") -> str:
    completed = subprocess.run(
        ["git", "rev-parse", f"{revision}^{{commit}}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or revision
        raise BenchmarkEvidenceError(f"Cannot resolve Git revision: {detail}")
    return completed.stdout.strip()


def git_is_dirty(repo_root: Path) -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise BenchmarkEvidenceError("Cannot inspect Git working-tree state")
    return bool(completed.stdout.strip())


def probe_command(command: Sequence[str], *, timeout: float = 20) -> dict[str, Any]:
    if not command:
        raise ValueError("Tool probe command cannot be empty")
    executable = shutil.which(command[0])
    if executable is None:
        return {"status": "missing", "command": list(command), "detail": None}
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": "error", "command": list(command), "detail": str(error)}
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return {
        "status": "ok" if completed.returncode == 0 else "error",
        "command": list(command),
        "return_code": completed.returncode,
        "detail": lines[0] if lines else executable,
    }


def collect_tool_versions(
    probes: Mapping[str, Sequence[str]] = DEFAULT_TOOL_PROBES,
) -> dict[str, dict[str, Any]]:
    return {name: probe_command(command) for name, command in probes.items()}


def initialize_report(
    *,
    scan_path: Path,
    expected_sha256: str,
    scanner_baseline_revision: str,
    repo_root: Path,
    tool_probes: Mapping[str, Sequence[str]] = DEFAULT_TOOL_PROBES,
) -> dict[str, Any]:
    created_at = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "updated_at": created_at,
        "input": verified_input(scan_path, expected_sha256),
        "provenance": {
            "scanner_baseline_commit": git_revision(repo_root, scanner_baseline_revision),
            "evidence_tool_commit": git_revision(repo_root),
            "evidence_tool_worktree_dirty": git_is_dirty(repo_root),
            "host": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
            },
            "tools": collect_tool_versions(tool_probes),
        },
        "schedule": runtime_guidance(None),
        "stages": [],
        "artifacts": {},
        "summary": {
            "status": "initialized",
            "stage_elapsed_seconds": 0.0,
            "peak_vram_mib": None,
        },
    }


def write_report(path: Path, report: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as target:
            json.dump(report, target, indent=2, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def load_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise BenchmarkEvidenceError(f"Cannot read benchmark report: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise BenchmarkEvidenceError("Unsupported benchmark evidence report")
    return payload


def ensure_report_open(report: Mapping[str, Any]) -> None:
    status = report.get("summary", {}).get("status")
    if status in {"complete", "failed"}:
        raise BenchmarkEvidenceError(
            f"Benchmark report is terminal ({status}); start a new run record"
        )


def sample_nvidia_vram_mib() -> int | None:
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    values: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            values.append(int(line.strip()))
        except ValueError:
            continue
    return max(values) if values else None


def run_stage(
    *,
    name: str,
    command: Sequence[str],
    log_path: Path,
    poll_interval_seconds: float = 1.0,
    gpu_sampler: Callable[[], int | None] = sample_nvidia_vram_mib,
) -> dict[str, Any]:
    if not name.strip():
        raise BenchmarkEvidenceError("Stage name cannot be empty")
    if not command:
        raise BenchmarkEvidenceError("Stage command cannot be empty")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    started = time.perf_counter()
    peak_vram_mib: int | None = None
    vram_sample_errors = 0
    return_code: int | None = None
    error_detail: str | None = None

    def sample_vram() -> None:
        nonlocal peak_vram_mib, vram_sample_errors
        try:
            current_vram = gpu_sampler()
        except Exception:
            vram_sample_errors += 1
            return
        if current_vram is not None:
            peak_vram_mib = max(peak_vram_mib or current_vram, current_vram)

    with log_path.open("w", encoding="utf-8") as log:
        log.write("Command: " + shell_join(command) + "\n\n")
        log.flush()
        try:
            process = subprocess.Popen(
                list(command),
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as error:
            error_detail = str(error)
            log.write(error_detail + "\n")
        else:
            while True:
                sample_vram()
                try:
                    return_code = process.wait(timeout=poll_interval_seconds)
                except subprocess.TimeoutExpired:
                    continue
                break
            sample_vram()

    elapsed_seconds = time.perf_counter() - started
    status = "succeeded" if return_code == 0 else "failed"
    return {
        "name": name,
        "status": status,
        "command": list(command),
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": elapsed_seconds,
        "return_code": return_code,
        "peak_vram_mib": peak_vram_mib,
        "vram_sample_errors": vram_sample_errors,
        "log_path": str(log_path.resolve()),
        "error": error_detail,
    }


def planned_stage(*, name: str, command: Sequence[str]) -> dict[str, Any]:
    return {
        "name": name,
        "status": "planned",
        "command": list(command),
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": 0.0,
        "return_code": None,
        "peak_vram_mib": None,
        "vram_sample_errors": 0,
        "log_path": None,
        "error": None,
    }


def artifact_fact(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if path.is_file():
        return {
            "path": str(path),
            "status": "present",
            "kind": "file",
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    if path.is_dir():
        files = [candidate for candidate in path.rglob("*") if candidate.is_file()]
        return {
            "path": str(path),
            "status": "present",
            "kind": "directory",
            "file_count": len(files),
            "size_bytes": sum(candidate.stat().st_size for candidate in files),
            "sha256": None,
        }
    return {
        "path": str(path),
        "status": "missing",
        "kind": None,
        "size_bytes": None,
        "sha256": None,
    }


def append_stage(report: dict[str, Any], stage: Mapping[str, Any]) -> None:
    name = stage.get("name")
    if any(existing.get("name") == name for existing in report["stages"]):
        raise BenchmarkEvidenceError(f"Stage already exists in report: {name}")
    report["stages"].append(dict(stage))
    refresh_summary(report)


def record_artifacts(report: dict[str, Any], artifacts: Mapping[str, Path]) -> None:
    for name, path in artifacts.items():
        report["artifacts"][name] = artifact_fact(path)
    report["updated_at"] = utc_now()


def refresh_summary(report: dict[str, Any], *, finalized: bool = False) -> None:
    stages = report["stages"]
    elapsed = sum(float(stage.get("elapsed_seconds") or 0) for stage in stages)
    vram_values = [
        int(stage["peak_vram_mib"])
        for stage in stages
        if stage.get("peak_vram_mib") is not None
    ]
    if any(stage.get("status") == "failed" for stage in stages):
        status = "failed"
    elif finalized:
        status = "complete"
    elif stages:
        status = "in_progress"
    else:
        status = "initialized"
    report["summary"] = {
        "status": status,
        "stage_elapsed_seconds": elapsed,
        "peak_vram_mib": max(vram_values) if vram_values else None,
    }
    report["updated_at"] = utc_now()


def shell_join(command: Sequence[str]) -> str:
    return " ".join(_quote(part) for part in command)


def _quote(value: str) -> str:
    if value and all(character.isalnum() or character in "-_./:=+" for character in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"
