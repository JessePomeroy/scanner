#!/usr/bin/env python3
"""Show a desktop notification when a Scanner job finishes.

A finished model's notification offers to open it in Blender or show its
folder. Runs as a user service beside the backend and only reads job records.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time


SCANS_DIR = Path(os.environ.get("SCANNER_SCANS_DIR", "scans"))
JOBS_DIR = SCANS_DIR / "jobs"
STATE_FILE = Path(
    os.environ.get(
        "SCANNER_NOTIFICATION_STATE",
        Path.home() / ".local/state/scanner/notified-jobs.json",
    )
)
TERMINAL_STATUSES = ("complete", "failed")


def read_terminal_jobs() -> dict[str, dict]:
    jobs: dict[str, dict] = {}
    for path in JOBS_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("status") in TERMINAL_STATUSES:
            jobs[str(record.get("scan_id", path.stem))] = record
    return jobs


def load_state() -> set[str]:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {str(item) for item in value}
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def save_state(notified: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(notified), indent=2), encoding="utf-8")
    os.replace(temporary, STATE_FILE)


def result_paths(record: dict) -> tuple[Path | None, Path | None]:
    """Return (.blend to open, folder to show) for a finished model, when they exist."""
    outputs = record.get("outputs")
    if not isinstance(outputs, dict):
        return None, None
    folder = blend = None
    library = outputs.get("library_dir")
    if isinstance(library, str) and Path(library).is_dir():
        folder = Path(library)
        blend = next((path for path in sorted(folder.glob("*.blend"))
                      if not path.name.endswith("-retro.blend")), None)
    job_blend = outputs.get("model_blend")
    if blend is None and isinstance(job_blend, str) and Path(job_blend).is_file():
        blend = Path(job_blend)
    if folder is None and blend is not None:
        folder = blend.parent
    return blend, folder


def notification(record: dict) -> dict:
    """Describe the notification for one finished job without showing it."""
    scan_id = str(record.get("scan_id", "unknown scan"))
    message = str(record.get("message") or "")
    if record.get("status") != "complete":
        return {"title": "Scanner could not finish a model", "urgency": "critical",
                "body": message or f"Scan {scan_id} failed.", "actions": {}}
    blend, folder = result_paths(record)
    actions = {}
    if blend is not None:
        actions["blender"] = ("Open in Blender", blend)
    if folder is not None:
        actions["folder"] = ("Show folder", folder)
    if blend is not None:
        title = "Your 3D model is ready"
        body = folder.name if folder is not None and "library_dir" in record.get("outputs", {}) else message
    else:
        title = "Scanner job finished"
        body = message or f"Scan {scan_id} is ready."
    return {"title": title, "urgency": "normal", "body": body, "actions": actions}


def open_path(action: str, path: Path) -> None:
    blender = shutil.which("blender")
    command = [blender, str(path)] if action == "blender" and blender else ["xdg-open", str(path)]
    # Start the app in its own systemd scope. As a plain child it would belong
    # to this service and be killed, unsaved work included, whenever the
    # notifier restarts.
    launcher = (["systemd-run", "--user", "--scope", "--collect", "--quiet", "--"]
                if shutil.which("systemd-run") else [])
    subprocess.Popen([*launcher, *command], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def show(details: dict) -> bool:
    """Send one notification; False means it was not sent and should be retried."""
    if shutil.which("notify-send") is None:
        return False
    command = ["notify-send", "--app-name=Scanner", f"--urgency={details['urgency']}"]
    actions = details["actions"]
    if not actions:
        return subprocess.run([*command, details["title"], details["body"]], check=False).returncode == 0
    # Jobs finish unattended, so the buttons must still work hours later.
    command.append("--expire-time=0")
    for name, (label, _) in actions.items():
        command.append(f"--action={name}={label}")
    command += [details["title"], details["body"]]

    def wait_for_choice() -> None:
        # With actions, notify-send stays alive until the notification closes.
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError:
            return
        choice = result.stdout.strip()
        if choice in actions:
            open_path(choice, actions[choice][1])

    threading.Thread(target=wait_for_choice, daemon=True).start()
    return True


def notify_new(notified: set[str]) -> None:
    """Notify each newly finished job once; one bad record never blocks the rest."""
    for scan_id, record in read_terminal_jobs().items():
        if scan_id in notified:
            continue
        try:
            sent = show(notification(record))
        except Exception as error:
            print(f"Skipping notification for {scan_id}: {error}", flush=True)
            sent = True
        if sent:
            notified.add(scan_id)
            save_state(notified)


def main() -> None:
    notified = load_state()
    while True:
        notify_new(notified)
        time.sleep(2)


if __name__ == "__main__":
    main()
