#!/usr/bin/env python3
"""Check whether the native Linux reconstruction workstation is ready."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when a required check fails.")
    args = parser.parse_args()

    results = [
        check_linux_distribution(),
        check_command(
            "nvidia-smi",
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader",
            ],
        ),
        check_command("nvcc", ["nvcc", "--version"]),
        check_colmap(),
        check_command("InterfaceCOLMAP", ["InterfaceCOLMAP", "--help"]),
        check_command("DensifyPointCloud", ["DensifyPointCloud", "--help"]),
        check_command("ReconstructMesh", ["ReconstructMesh", "--help"]),
        check_command("RefineMesh", ["RefineMesh", "--help"]),
        check_command("TextureMesh", ["TextureMesh", "--help"]),
        check_command("blender", ["blender", "--version"]),
        check_command("ns-process-data", ["ns-process-data", "--help"]),
        check_command("ns-train", ["ns-train", "--help"]),
        check_command("ns-export", ["ns-export", "--help"]),
        check_torch_cuda(),
        check_node(),
        check_command("codex", ["codex", "--version"]),
        check_command("splat-transform", ["splat-transform", "--help"]),
        check_python_package("open3d", required=False),
    ]

    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        for result in results:
            status = "ok" if result.ok else "missing"
            print(f"{status:7} {result.name}: {result.detail}")

    if args.strict and any(not result.ok and is_required(result.name) for result in results):
        raise SystemExit(1)


def is_required(name: str) -> bool:
    return name in {
        "linux",
        "nvidia-smi",
        "nvcc",
        "colmap",
        "InterfaceCOLMAP",
        "DensifyPointCloud",
        "ReconstructMesh",
        "RefineMesh",
        "TextureMesh",
        "blender",
        "ns-process-data",
        "ns-train",
        "ns-export",
        "torch-cuda",
        "node",
        "codex",
        "splat-transform",
    }


def check_linux_distribution() -> CheckResult:
    os_release = Path("/etc/os-release")
    if platform.system() != "Linux" or not os_release.is_file():
        return CheckResult(
            "linux",
            False,
            f"{platform.system()} {platform.release()}; Linux required",
        )

    values: dict[str, str] = {}
    for line in os_release.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip().strip('"')
    name = values.get("PRETTY_NAME", values.get("ID", "Linux"))
    return CheckResult("linux", True, f"{name}; kernel {platform.release()}")


def check_command(name: str, command: list[str], *, required: bool = True) -> CheckResult:
    executable = shutil.which(command[0])
    if executable is None:
        suffix = "required" if required else "optional"
        return CheckResult(name, False, f"{command[0]} not found ({suffix})")

    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
    except Exception as error:
        return CheckResult(name, False, str(error))

    first_line = (
        completed.stdout.strip().splitlines()[0]
        if completed.stdout.strip()
        else executable
    )
    return CheckResult(name, completed.returncode == 0, first_line)


def check_colmap() -> CheckResult:
    executable = shutil.which("colmap")
    if executable is None:
        return CheckResult("colmap", False, "colmap not found (required)")

    completed = subprocess.run(
        ["colmap", "-h"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )
    output = completed.stdout.strip()
    first_line = output.splitlines()[0] if output else executable
    cuda_detail = "CUDA status unknown"
    if "without CUDA" in output:
        cuda_detail = "without CUDA"
    elif "with CUDA" in output:
        cuda_detail = "with CUDA"

    return CheckResult(
        "colmap",
        completed.returncode == 0 and "without CUDA" not in output,
        f"{first_line}; {cuda_detail}",
    )


def check_node() -> CheckResult:
    executable = shutil.which("node")
    if executable is None:
        return CheckResult(
            "node",
            False,
            "node not found (required; version 22 or newer)",
        )

    completed = subprocess.run(
        ["node", "--version"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
    )
    version = completed.stdout.strip()
    try:
        major = int(version.removeprefix("v").split(".", 1)[0])
    except (ValueError, IndexError):
        return CheckResult("node", False, version or "unable to parse Node.js version")
    if completed.returncode != 0 or major < 22:
        return CheckResult("node", False, f"{version}; version 22 or newer is required")
    return CheckResult("node", True, version)


def check_torch_cuda() -> CheckResult:
    script = """
import torch
if not torch.cuda.is_available():
    raise SystemExit("PyTorch cannot use CUDA in this environment")
properties = torch.cuda.get_device_properties(0)
probe = torch.ones(1, device="cuda").add_(1)
torch.cuda.synchronize()
if probe.item() != 2:
    raise SystemExit("PyTorch CUDA computation returned the wrong result")
memory_gib = properties.total_memory / 1024**3
print(f"torch {torch.__version__}; {properties.name}; {memory_gib:.1f} GiB; CUDA operation passed")
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
    except Exception as error:
        return CheckResult("torch-cuda", False, str(error))

    detail = (
        completed.stdout.strip().splitlines()[-1]
        if completed.stdout.strip()
        else "no output"
    )
    return CheckResult("torch-cuda", completed.returncode == 0, detail)


def check_python_package(name: str, *, required: bool = True) -> CheckResult:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", f"import {name}; print({name}.__version__)"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
    except Exception as error:
        return CheckResult(name, False, str(error))

    if completed.returncode != 0:
        suffix = "required" if required else "optional"
        return CheckResult(name, False, f"Python package not importable ({suffix})")

    return CheckResult(name, True, completed.stdout.strip())


if __name__ == "__main__":
    main()
