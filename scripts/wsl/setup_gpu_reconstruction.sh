#!/usr/bin/env bash
set -euo pipefail

echo "Scanner GPU reconstruction setup for Ubuntu Linux"
echo

if grep -qi microsoft /proc/version 2>/dev/null; then
  echo "WSL2 compatibility environment detected; native Linux is the primary scanner target."
else
  echo "Native Linux environment detected."
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "NVIDIA GPU visible:"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
else
  echo "nvidia-smi was not found. Install a supported native NVIDIA Linux driver and reboot."
fi

sudo apt-get update
sudo apt-get install -y \
  build-essential \
  cmake \
  git \
  ninja-build \
  pkg-config \
  python3 \
  python3-pip \
  python3-venv \
  unzip \
  zip \
  ffmpeg \
  libgl1 \
  libglib2.0-0

python3 -m pip install --user --upgrade pip
python3 -m pip install --user open3d numpy scipy

echo
echo "Base packages installed."
echo "Next checks:"
echo "  python3 scripts/wsl/check_reconstruction_env.py --strict"
echo
echo "COLMAP/OpenMVS CUDA builds are environment-specific. If apt packages are not CUDA-enabled,"
echo "build them from source on Linux or use a CUDA-enabled Docker image."
