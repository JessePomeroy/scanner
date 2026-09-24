#!/usr/bin/env python3
"""Run COLMAP for Nerfstudio while bridging its two renamed GPU flags.

Nerfstudio 1.1.5 still emits ``SiftExtraction.use_gpu`` and
``SiftMatching.use_gpu``. COLMAP 4 renamed only those options to
``FeatureExtraction.use_gpu`` and ``FeatureMatching.use_gpu``. This wrapper
probes the selected COLMAP executable and translates a legacy option only when
that executable advertises the modern spelling.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


REAL_COLMAP_ENV = "SCANNER_REAL_COLMAP"
PROBE_ARGUMENT = "--scanner-compat-probe"
PROBE_TIMEOUT_SECONDS = 30

_FLAG_COMPATIBILITY = {
    "--SiftExtraction.use_gpu": (
        "--FeatureExtraction.use_gpu",
        "feature_extractor",
    ),
    "--SiftMatching.use_gpu": (
        "--FeatureMatching.use_gpu",
        "sequential_matcher",
    ),
}


class ColmapCompatibilityError(RuntimeError):
    """Raised when the real COLMAP executable cannot be used safely."""


def resolve_real_colmap() -> Path:
    """Resolve the real COLMAP binary and reject accidental wrapper recursion."""
    configured = os.environ.get(REAL_COLMAP_ENV, "colmap")
    if os.sep in configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ColmapCompatibilityError(
                f"{REAL_COLMAP_ENV} does not name an executable file: {configured}"
            )
        executable = candidate.resolve()
    else:
        located = shutil.which(configured)
        if located is None:
            raise ColmapCompatibilityError(
                f"COLMAP executable '{configured}' was not found on PATH"
            )
        executable = Path(located).resolve()

    if executable == Path(__file__).resolve():
        raise ColmapCompatibilityError(
            f"refusing recursive execution; {REAL_COLMAP_ENV} resolves to this wrapper"
        )
    return executable


def _option_name(argument: str) -> str:
    return argument.split("=", 1)[0]


def _probe_option_spelling(
    executable: Path,
    legacy_option: str,
    modern_option: str,
    probe_command: str,
) -> str:
    try:
        completed = subprocess.run(
            [str(executable), probe_command, "-h"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ColmapCompatibilityError(
            f"unable to probe {probe_command}: {error}"
        ) from error

    output = completed.stdout
    if completed.returncode != 0:
        raise ColmapCompatibilityError(
            f"{probe_command} help exited with status {completed.returncode}"
        )
    if legacy_option in output:
        return legacy_option
    if modern_option in output:
        return modern_option

    status = f"exit {completed.returncode}"
    raise ColmapCompatibilityError(
        f"{probe_command} help ({status}) advertises neither "
        f"{legacy_option} nor {modern_option}"
    )


def translated_arguments(executable: Path, arguments: list[str]) -> list[str]:
    """Translate only unsupported legacy GPU flags, preserving all other args."""
    requested_names = {_option_name(argument) for argument in arguments}
    translated = list(arguments)

    for legacy_option, (modern_option, probe_command) in _FLAG_COMPATIBILITY.items():
        if legacy_option not in requested_names:
            continue
        if modern_option in requested_names:
            raise ColmapCompatibilityError(
                f"both {legacy_option} and {modern_option} were supplied"
            )

        actual_command = arguments[0] if arguments else ""
        if legacy_option == "--SiftMatching.use_gpu" and actual_command.endswith(
            "_matcher"
        ):
            probe_command = actual_command
        elif legacy_option == "--SiftExtraction.use_gpu" and actual_command == (
            "feature_extractor"
        ):
            probe_command = actual_command

        supported_option = _probe_option_spelling(
            executable,
            legacy_option,
            modern_option,
            probe_command,
        )
        if supported_option == legacy_option:
            continue

        for index, argument in enumerate(translated):
            if argument == legacy_option:
                translated[index] = modern_option
            elif argument.startswith(f"{legacy_option}="):
                translated[index] = f"{modern_option}={argument.split('=', 1)[1]}"

    return translated


def compatibility_summary(executable: Path) -> str:
    """Probe both renamed options without starting reconstruction work."""
    decisions: list[str] = []
    for legacy_option, (modern_option, probe_command) in _FLAG_COMPATIBILITY.items():
        supported = _probe_option_spelling(
            executable,
            legacy_option,
            modern_option,
            probe_command,
        )
        action = "preserve" if supported == legacy_option else "translate"
        decisions.append(f"{legacy_option}={action}:{supported}")
    return f"{executable}; " + "; ".join(decisions)


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    try:
        executable = resolve_real_colmap()
        if arguments == [PROBE_ARGUMENT]:
            print(compatibility_summary(executable))
            return 0
        translated = translated_arguments(executable, arguments)
        os.execv(str(executable), [str(executable), *translated])
    except ColmapCompatibilityError as error:
        print(f"nerfstudio-colmap-compat: error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"nerfstudio-colmap-compat: error: unable to execute COLMAP: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
