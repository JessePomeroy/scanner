#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=0
INSTALL_SPLAT_TRANSFORM=1
INSTALL_CODEX=1

usage() {
  cat <<'EOF'
Usage: setup_gpu_reconstruction.sh [--dry-run] [--skip-splat-transform] [--skip-codex]

Install the scanner workstation's base packages on CachyOS/Arch or
Ubuntu/Debian. NVIDIA driver selection remains a deliberate host-level step.

  --dry-run                 Print commands without changing the computer.
  --skip-splat-transform    Do not install the PlayCanvas CLI.
  --skip-codex              Do not install the OpenAI Codex CLI.
EOF
}

while (($#)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --skip-splat-transform)
      INSTALL_SPLAT_TRANSFORM=0
      ;;
    --skip-codex)
      INSTALL_CODEX=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if ((EUID == 0)); then
  echo "Run this script as your normal user; it invokes sudo only for system packages." >&2
  exit 1
fi

run() {
  if ((DRY_RUN)); then
    printf '  '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

OS_RELEASE_FILE="${SCANNER_OS_RELEASE_FILE:-/etc/os-release}"
if [[ ! -r "$OS_RELEASE_FILE" ]]; then
  echo "Cannot read $OS_RELEASE_FILE; this setup script requires Linux." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$OS_RELEASE_FILE"
OS_ID="${ID:-unknown}"
OS_ID_LIKE="${ID_LIKE:-}"
OS_NAME="${PRETTY_NAME:-$OS_ID}"

case " $OS_ID $OS_ID_LIKE " in
  *" cachyos "*|*" arch "*)
    DISTRO_FAMILY=arch
    ;;
  *" ubuntu "*|*" debian "*)
    DISTRO_FAMILY=debian
    ;;
  *)
    echo "Unsupported Linux distribution: $OS_NAME" >&2
    echo "Supported families: CachyOS/Arch and Ubuntu/Debian." >&2
    exit 1
    ;;
esac

if ((DRY_RUN == 0)); then
  if [[ "$DISTRO_FAMILY" == arch ]] && ! command -v pacman >/dev/null 2>&1; then
    echo "CachyOS/Arch was detected, but pacman is not available." >&2
    exit 1
  fi
  if [[ "$DISTRO_FAMILY" == debian ]] && ! command -v apt-get >/dev/null 2>&1; then
    echo "Ubuntu/Debian was detected, but apt-get is not available." >&2
    exit 1
  fi
fi

echo "Scanner GPU reconstruction base setup for $OS_NAME"
if grep -qi microsoft /proc/version 2>/dev/null; then
  echo "WSL2 compatibility environment detected; native Linux is the primary scanner target."
else
  echo "Native Linux environment detected."
fi
echo

if [[ "$OS_ID" == cachyos ]] && command -v chwd >/dev/null 2>&1; then
  echo "CachyOS hardware profile:"
  chwd -d --list || echo "  chwd could not report the installed profile."
  echo
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "NVIDIA GPU visible:"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
else
  echo "NVIDIA GPU is not visible yet."
  if [[ "$DISTRO_FAMILY" == arch && "$OS_ID" == cachyos ]]; then
    echo "After this base setup, use CachyOS Hardware Detection to select the"
    echo "appropriate NVIDIA profile (start by inspecting 'chwd --list-all'), then reboot."
  else
    echo "Install a supported native NVIDIA Linux driver, then reboot."
  fi
fi
echo

if [[ "$DISTRO_FAMILY" == arch ]]; then
  arch_packages=(
    base-devel
    blender
    cmake
    cuda
    ffmpeg
    git
    libglvnd
    ninja
    npm
    pciutils
    pkgconf
    python
    python-pip
    unzip
    zip
  )
  node_major="${SCANNER_NODE_MAJOR:-}"
  if [[ -z "$node_major" ]] && command -v node >/dev/null 2>&1; then
    node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
  fi
  if [[ ! "$node_major" =~ ^[0-9]+$ ]] || ((node_major < 22)); then
    arch_packages+=(nodejs-lts-jod)
  else
    echo "Keeping the installed Node.js major version $node_major (minimum is 22)."
  fi

  # Arch-family systems do not support partial upgrades, so refresh and upgrade
  # the system together before adding the workstation packages.
  run sudo pacman -Syu --needed "${arch_packages[@]}"
else
  run sudo apt-get update
  run sudo apt-get install -y \
    blender \
    build-essential \
    cmake \
    ffmpeg \
    git \
    libgl1 \
    libglib2.0-0 \
    ninja-build \
    nodejs \
    npm \
    pkg-config \
    python3 \
    python3-pip \
    python3-venv \
    unzip \
    zip
fi

if ((INSTALL_SPLAT_TRANSFORM || INSTALL_CODEX)); then
  run mkdir -p "$HOME/.local"
  run npm config set prefix "$HOME/.local"
  npm_packages=()
  if ((INSTALL_SPLAT_TRANSFORM)); then
    npm_packages+=(@playcanvas/splat-transform)
  fi
  if ((INSTALL_CODEX)); then
    npm_packages+=(@openai/codex)
  fi
  run npm install --global "${npm_packages[@]}"
fi

echo
echo "Base packages installed."
echo "Add this to the current shell if ~/.local/bin is not already on PATH:"
echo '  export PATH="$HOME/.local/bin:$PATH"'
if ((INSTALL_CODEX)); then
  echo "Start Codex with 'codex', choose 'Sign in with ChatGPT', and continue from the scanner repo."
  echo "OpenAI does not provide a native ChatGPT desktop app for Linux; use https://chatgpt.com in a browser."
fi
echo
echo "Keep Nerfstudio, PyTorch, gsplat, and optional Open3D packages in isolated"
echo "environments; do not install them into CachyOS/Arch's system Python."
echo "COLMAP and OpenMVS still require the documented CUDA-capable build/install step."
echo
echo "After activating the Nerfstudio environment, run:"
echo "  python3 scripts/wsl/check_reconstruction_env.py --strict"
