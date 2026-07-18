# CachyOS RTX 3070 Setup

This is the workstation setup path for the scanner benchmark. CachyOS is the
primary Linux target; the Ubuntu path remains available for compatibility.

The setup is deliberately split into a safe base install and a pinned
reconstruction-toolchain install. The base script can install supported
CachyOS packages. COLMAP, OpenMVS, PyTorch, gsplat, and Nerfstudio must be
pinned and verified separately so a rolling package update cannot silently
change the benchmark.

## 1. Finish CachyOS and Verify the Driver

Let the CachyOS installer configure the GPU. After the first boot, update and
reboot once, then inspect what it selected:

```bash
sudo pacman -Syu
sudo reboot
```

After reboot:

```bash
uname -r
chwd -d --list
lspci -k -d ::03xx
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
cat /sys/module/nvidia_drm/parameters/modeset
```

The expected GPU is an RTX 3070 with approximately 8 GB VRAM, and the last
command should print `Y`. If CachyOS did not configure an NVIDIA profile, first
inspect the available profiles with `chwd --list-all`, then run:

```bash
sudo chwd -a
sudo reboot
```

Use CachyOS `chwd`; do not layer NVIDIA's standalone `.run` installer over the
distribution-managed driver. Do not switch a working open/proprietary driver
flavor merely to match an example package name.

## 2. Clone the Scanner Repository

Keep the repository and active reconstruction workspaces on the native Linux
filesystem, not an NTFS Windows partition:

```bash
cd ~
git clone https://github.com/JessePomeroy/scanner.git
cd scanner
git switch main
git pull --ff-only
mkdir -p ~/ScannerBenchmarks/input ~/ScannerOutputs ~/ScannerPlans
```

The published `main` branch contains the CachyOS setup and paired benchmark
tooling. Confirm commit `39fd92b` is present before running the setup script.

## 3. Preview and Run the Base Setup

The script detects CachyOS/Arch and uses `pacman`. Preview the exact package
transaction first:

```bash
scripts/wsl/setup_gpu_reconstruction.sh --dry-run
scripts/wsl/setup_gpu_reconstruction.sh
```

The historical `scripts/wsl/` directory name is retained for compatibility;
the script is intended for native CachyOS. It performs a complete Arch-family
upgrade (`pacman -Syu`) rather than creating an unsupported partial-upgrade
state. It installs the CUDA toolkit, Blender, FFmpeg, build tools, Python,
Node.js 22 LTS when needed, npm, SplatTransform, and the OpenAI Codex CLI.

Open a new terminal after the install. If the CUDA tools are not visible in
the current shell, load the package profile explicitly:

```bash
source /etc/profile.d/cuda.sh
export PATH="$HOME/.local/bin:$PATH"
nvcc --version
splat-transform --help
codex --version
```

Run `codex` from `~/scanner` and choose **Sign in with ChatGPT**. That gives us
the supported terminal-first Linux workspace so we can continue development on
the CachyOS machine. OpenAI does not currently publish its ChatGPT desktop app
for Linux; use [chatgpt.com](https://chatgpt.com) in a normal browser for chat.
The setup script therefore installs Codex, not an unofficial ChatGPT wrapper.
See the [official Codex quickstart](https://github.com/openai/codex#quickstart)
and [OpenAI's desktop-app guidance](https://help.openai.com/en/articles/20001276-moving-to-the-new-chatgpt-desktop-app).

## 4. Record the Base State

The checker is useful before the full toolchain is ready: without `--strict`,
it lists every missing component but does not fail the shell session.

```bash
python3 scripts/wsl/check_reconstruction_env.py
python3 scripts/wsl/check_reconstruction_env.py --json > ~/ScannerBenchmarks/base-environment.json
```

At this point, missing COLMAP, OpenMVS, Nerfstudio, or `torch-cuda` entries are
expected. The base setup is complete when the RTX 3070, `nvcc`, Blender,
Node.js, Codex, and SplatTransform are present.

## 5. Install the Pinned Reconstruction Toolchains

This is the next project checkpoint after the base CachyOS environment is
known:

1. Build a recorded COLMAP revision with CUDA enabled for the local GPU.
2. Build a recorded OpenMVS revision with `USE_CUDA=ON` and verify all five
   required commands.
3. Create an isolated neural environment with a tested Python/PyTorch/CUDA
   combination; install Nerfstudio and gsplat there.
4. Run a real PyTorch CUDA operation and the complete strict gate.

Do not install neural packages into CachyOS's system Python, and do not
unattendedly install changing AUR recipes. The current Arch CUDA package can be
newer than the combinations tested by PyTorch/Nerfstudio, so the neural
environment must select its own compatible versions.

Activate the neural environment before the final check:

```bash
python3 scripts/wsl/check_reconstruction_env.py --strict
python3 scripts/wsl/check_reconstruction_env.py --json \
  > ~/ScannerBenchmarks/full-environment.json
```

Strict mode requires the GPU and CUDA toolkit, CUDA-capable PyTorch, COLMAP,
the OpenMVS command suite, Blender, Nerfstudio, Node.js 22 or newer, Codex, and
SplatTransform. Open3D remains optional.

## 6. Begin the Paired Benchmark

Only after strict mode passes, continue with
[`benchmark_runbook.md`](benchmark_runbook.md). Preserve all resolved package
versions, build revisions, build flags, environment output, elapsed times, and
peak VRAM measurements with the benchmark evidence.

The detailed source review and unresolved compatibility questions are recorded
in [`research/cachyos_reconstruction_setup.md`](research/cachyos_reconstruction_setup.md).
