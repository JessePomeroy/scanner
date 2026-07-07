"""Shared command-plan primitives for reconstruction backends."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any


@dataclass(frozen=True)
class CommandPlan:
    """A backend-agnostic reconstruction command plan."""

    backend: str
    scan_root: Path
    commands: list[list[str]]
    outputs: dict[str, Path] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def command_count(self) -> int:
        return len(self.commands)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "scan_root": str(self.scan_root),
            "command_count": self.command_count,
            "commands": self.commands,
            "command_lines": [shell_join(command) for command in self.commands],
            "outputs": {name: str(path) for name, path in self.outputs.items()},
            "notes": self.notes,
        }


def run_command_plan(plan: CommandPlan, *, dry_run: bool = False, command_log: Path | None = None) -> None:
    """Run or print every command in a command plan."""
    for command in plan.commands:
        line = shell_join(command)
        print(line)
        if command_log is not None:
            command_log.parent.mkdir(parents=True, exist_ok=True)
            with command_log.open("a") as log:
                log.write(line + "\n")
        if not dry_run:
            subprocess.run(command, check=True)


def write_command_plan_report(plan: CommandPlan, path: Path, *, extra: dict[str, Any] | None = None) -> Path:
    """Write a JSON report for a planned reconstruction backend run."""
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        **plan.to_dict(),
    }
    if extra:
        payload.update(extra)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def shell_join(command: list[str]) -> str:
    return " ".join(quote(part) for part in command)


def quote(value: str) -> str:
    if value and all(character.isalnum() or character in "-_./:=+" for character in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"
