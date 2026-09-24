"""Read-only adapter for an explicitly selected recovery attempt and user service."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time

LIMIT = 64 * 1024
STAGES = {
    "recover_depth": "Recovering depth maps",
    "stereo_fusion": "Fusing depth maps",
    "interface_colmap": "Preparing point cloud",
    "reconstruct_mesh": "Building mesh",
    "texture_mesh": "Texturing mesh",
}


def read_record(path: Path) -> dict:
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError(f"Evidence file exceeds {LIMIT} bytes: {path.name}")
    record = json.loads(data)
    if not isinstance(record, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return record


def log_tail(path: Path) -> str:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - LIMIT))
        data = stream.read(LIMIT)
    text = data.decode("utf-8", errors="replace").replace("\r", "\n")
    if size > LIMIT:
        text = text.partition("\n")[2]
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def seconds_since(value: object, now: float) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return None
        return max(0, now - stamp.timestamp())
    except ValueError:
        return None


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "Unavailable"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds:02d}s"


@dataclass(frozen=True)
class Progress:
    percent: float | None = None
    operation: str = "This operation does not report a percentage."
    eta: str = "Unavailable — no reliable estimate reported"


def parse_progress(text: str, age: float | None) -> Progress:
    """Never carry a completed sub-operation's percent into the next operation."""
    lines = text.splitlines()
    if not lines:
        return Progress()
    last = lines[-1]
    percent = re.search(r"^(.*?)\s+\((\d+(?:\.\d+)?)%[,)]", last)
    if percent:
        value = float(percent[2])
        if value >= 100 or age is None or age > 30:
            return Progress(operation=f"Last reported: {percent[1]} ({value:g}%)")
        eta = re.search(r"\bETA ([0-9hms. ]+)\)", last)
        return Progress(value, percent[1], f"{eta[1].strip()} for this operation only" if eta else Progress().eta)
    fusion = re.search(r"Fusing image \[(\d+)/(\d+)\]", last)
    if fusion and int(fusion[2]) > 0 and age is not None and age <= 30:
        index, total = map(int, fusion.groups())
        if 1 <= index <= total:
            return Progress(100 * (index - 1) / total, f"Image {index} of {total}; percentage counts completed images")
    return Progress()


def effective_status(saved: object, service: dict[str, str]) -> str:
    active = service.get("ActiveState")
    if not active:
        return "unknown"
    if active == "failed":
        return "failed"
    if saved == "failed":
        return "failed"
    if saved == "succeeded":
        return "succeeded"
    if active in {"active", "activating", "reloading"}:
        return "running"
    if saved == "stopped":
        return "stopped"
    return "interrupted"


@dataclass(frozen=True)
class Snapshot:
    status: str
    stage: str
    attempt: str
    elapsed: str
    stage_elapsed: str
    completed: str
    progress: Progress
    last_activity: str
    log_age: float | None
    memory: str
    gpu: str
    error: str
    log: str
    output: Path | None
    sampled_at: str
    checks: tuple[str, ...] = ()


def build_snapshot(run: Path, state: dict, plan: dict, service: dict[str, str],
                   text: str, age: float | None, gpu: str, now: float,
                   evidence_error: str = "") -> Snapshot:
    status = effective_status(state.get("status"), service)
    if evidence_error:
        status = "unknown" if status != "failed" else status
    stage = state.get("current_stage", "")
    stages = state.get("stages", [])
    if not isinstance(stages, list):
        stages = []
    stages = [item for item in stages if isinstance(item, dict)]
    completed = sum(item.get("status") == "succeeded" for item in stages)
    planned = plan.get("stages", [])
    count = len(planned) if isinstance(planned, list) else 0
    end = now
    finished_age = seconds_since(state.get("finished_at"), now)
    if status != "running" and finished_age is not None:
        end = now - finished_age
    start = state.get("started_at")
    stage_start = next((item.get("started_at") for item in stages if item.get("name") == stage), None)
    if stage_start is None:
        stage_start = stages[-1].get("finished_at") if stages else start
    memory = "Unavailable"
    try:
        used = int(service.get("MemoryCurrent", ""))
        ceiling = int(service.get("MemoryMax", ""))
        if 0 <= used < 2**60 and 0 < ceiling < 2**60:
            memory = f"{used / 2**30:.1f} / {ceiling / 2**30:g} GiB limit"
    except ValueError:
        pass
    output = plan.get("workspace")
    output_path = Path(output) if isinstance(output, str) and Path(output).is_absolute() else None
    error = str(state.get("error") or "")
    if status in {"failed", "interrupted"} and not error:
        result = service.get("Result", "unknown")
        error = f"Service {service.get('ActiveState', 'unavailable')} ({result}). Check the logs before resuming."
    if evidence_error:
        error = evidence_error + (f"\n{error}" if error else "")
    if status == "unknown" and not error:
        error = "Service status unavailable. Saved evidence alone cannot confirm whether the job is running."
    progress = parse_progress(text, age) if status == "running" else Progress()
    last = text.splitlines()[-1] if text else "No log messages yet."
    # Long absolute paths and terminal dumps belong in the expandable log view.
    last = last[-600:]
    return Snapshot(status, STAGES.get(str(stage), str(stage) or "Waiting for evidence"), run.name,
                    duration(seconds_since(start, end)), duration(seconds_since(stage_start, end)),
                    f"{completed} of {count} stages completed in this attempt" if count else f"{completed} stages completed in this attempt",
                    progress, last, age, memory, gpu, error, text[-16000:], output_path,
                    datetime.fromtimestamp(now, timezone.utc).astimezone().strftime("%H:%M:%S"))


def service_properties(unit: str) -> dict[str, str]:
    if not re.fullmatch(r"scanner-[A-Za-z0-9_-]+\.service", unit):
        raise ValueError("Expected an explicit scanner-*.service user unit")
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "--no-pager",
         "--property=ActiveState,SubState,Result,MemoryCurrent,MemoryMax"],
        capture_output=True, text=True, timeout=2, check=False)
    if result.returncode:
        return {}
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def gpu_memory() -> str:
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                                 "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=2, check=False)
        if result.returncode == 0:
            used, total = map(int, result.stdout.splitlines()[0].split(","))
            return f"{used / 1024:.1f} / {total / 1024:g} GiB (whole GPU)"
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    return "Unavailable"


def collect(run: Path, unit: str) -> Snapshot:
    errors = []
    records = []
    for name in ("state.json", "plan.json"):
        try:
            records.append(read_record(run / name))
        except (OSError, ValueError) as error:
            records.append({})
            errors.append(f"Cannot read {name}: {error}")
    state, plan = records
    stage = state.get("current_stage", "")
    logfile = run / "service.log"
    if isinstance(stage, str) and re.fullmatch(r"[a-z_]+", stage):
        candidate = run / "logs" / f"{stage}.log"
        if candidate.is_file():
            logfile = candidate
    text, age = "", None
    try:
        text = log_tail(logfile)
        age = max(0, time.time() - logfile.stat().st_mtime)
    except OSError:
        pass
    try:
        service = service_properties(unit)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        service = {}
        errors.append(f"Cannot query service: {error}")
    value = build_snapshot(run, state, plan, service, text, age, gpu_memory(), time.time(), "\n".join(errors))
    from desktop.features import delivery_checks
    return replace(value, checks=delivery_checks(value.output, value.status))
