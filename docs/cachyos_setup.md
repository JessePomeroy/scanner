# CachyOS RTX 3070 Setup

This is the workstation setup path for the scanner benchmark. CachyOS is the
primary Linux target; the Ubuntu path remains available for compatibility.

Status on 2026-08-23: the pinned workstation gate passes and CUDA COLMAP has
completed the frozen scan. OpenMVS CUDA densification still has a runtime
failure documented below, and the formal mesh/Gaussian benchmark remains
incomplete.

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
mkdir -p ~/Documents/work
git clone https://github.com/JessePomeroy/scanner.git \
  ~/Documents/work/scanner
cd ~/Documents/work/scanner
git switch main
git pull --ff-only
mkdir -p ~/ScannerBenchmarks/input ~/ScannerOutputs ~/ScannerPlans
```

On this workstation, that canonical checkout resolves to
`/home/strayblackdog/Documents/work/scanner`. `~/scanner` is retained only as
a compatibility symlink because the preserved virtualenv and historical job
records contain the old absolute path. Do not create a second active checkout
there.

The published `main` branch contains the CachyOS setup and paired benchmark
tooling. At the 2026-08-23 recovery checkpoint, `main` and `origin/main` were at
`c5283b3`; record the actual commit used for every later run rather than
assuming that checkpoint remains the branch tip. The formal scanner baseline
for the paired comparison remains `d5f19d9`.

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

Run `codex` from `~/Documents/work/scanner` and choose **Sign in with
ChatGPT**. That gives us the supported terminal-first Linux workspace so we
can continue development on the CachyOS machine. OpenAI does not currently
publish its ChatGPT desktop app for Linux; use
[chatgpt.com](https://chatgpt.com) in a normal browser for chat. The setup
script therefore installs Codex, not an unofficial ChatGPT wrapper. See the
[official Codex quickstart](https://github.com/openai/codex#quickstart) and
[OpenAI's desktop-app guidance](https://help.openai.com/en/articles/20001276-moving-to-the-new-chatgpt-desktop-app).

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

For a fresh workstation, install and record the pinned reconstruction
toolchains after the base CachyOS environment is known:

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

Run the final check with the pinned neural Python. On the recovered workstation
the interpreter is
`~/ScannerToolchains/envs/nerfstudio-1.1.5-py310/bin/python`:

```bash
NERFSTUDIO_PY=~/ScannerToolchains/envs/nerfstudio-1.1.5-py310/bin/python
"$NERFSTUDIO_PY" scripts/wsl/check_reconstruction_env.py --strict
"$NERFSTUDIO_PY" scripts/wsl/check_reconstruction_env.py --json \
  > ~/ScannerBenchmarks/full-environment.json
```

Strict mode requires the GPU and CUDA toolkit, CUDA-capable PyTorch, COLMAP,
the OpenMVS command suite, Blender, Nerfstudio, Node.js 22 or newer, Codex, and
SplatTransform. It also requires the repository-local Nerfstudio/COLMAP wrapper
to resolve the real COLMAP binary and recognize both renamed GPU options. That
probe reads command help only; it does not create reconstruction data. Open3D
remains optional. OpenMVS writes logs even while displaying help, so the
checker runs each OpenMVS help probe in a disposable working directory rather
than polluting the repository or launch directory.

## 6. Recorded Workstation State — 2026-08-23

The strict gate passes from the pinned interpreter above with this observed
stack:

- CachyOS kernel 7.1.6, RTX 3070 (8 GB), NVIDIA driver 610.57.04;
- CUDA toolkit 13.3 and CUDA-enabled COLMAP 4.0.4;
- OpenMVS 2.4, Blender 5.2 LTS, and Open3D 0.19;
- Nerfstudio 1.1.5, PyTorch `2.4.1+cu124`, and gsplat
  `1.4.0+pt24cu124`;
- Node.js 24, Codex 0.147, and SplatTransform 3.1.2.

Nerfstudio 1.1.5 still emits `SiftExtraction.use_gpu` and
`SiftMatching.use_gpu`; the strict compatibility probe confirms that the
repository wrapper translates exactly those options for COLMAP 4.0.4. Gaussian
plans pass the wrapper to `ns-process-data` by absolute path.

This result establishes tool visibility and exercises PyTorch CUDA. It does
not run an OpenMVS CUDA reconstruction kernel and therefore is not proof that
`DensifyPointCloud` works on this build/GPU combination.

The exact frozen package is present at
`/home/strayblackdog/Documents/work/scanner/backend/scans/incoming/966ec5a6-c763-4ce9-8e75-bf9e660368a8.zip`.
It is 429,671,685 bytes and its SHA-256 is
`ef9a6e0aefa564facf17357252e7fa2bd2cec55882a107461abad5c6459cb779`.

The preserved July job completed CUDA COLMAP in 10,854.525 seconds. Its first
automatic `DensifyPointCloud` launch failed because the old runner resolved
image paths from the backend directory; the runner's dense-workspace `cwd`
fix is now present. A later retry from the correct dense directory got through
image preparation and neighbor selection, then failed with OpenMVS CUDA
`invalid argument (code 1)`.

Manual recovery meshed the 9,343,817-point `InterfaceCOLMAP` `scene.ply`
directly, yielding 3,652,543 vertices and 7,297,100 faces. Texturing took
1:23:16.841 and produced an OBJ/MTL set with two 8192-pixel atlases. No
reviewed `.blend`, GLB, Gaussian artifact, finalized benchmark evidence, or
comparison record exists yet.

## 7. Continue the Paired Benchmark

Only after strict mode passes, continue with
[`benchmark_runbook.md`](benchmark_runbook.md). The current development runner
also exposes the explicit recovery strategy
`--openmvs-point-cloud-source colmap_fused --scope-mode unbounded`; it meshes
the view-aware `InterfaceCOLMAP` cloud and skips `DensifyPointCloud`. That path
is new and has not yet completed a fresh end-to-end run on the frozen package,
so keep it separate from the untouched `d5f19d9` baseline and preserve the
original failure evidence.

Preserve all resolved package versions, build revisions, build flags,
environment output, elapsed times, and peak VRAM measurements with the
benchmark evidence.

The detailed source review and unresolved compatibility questions are recorded
in [`research/cachyos_reconstruction_setup.md`](research/cachyos_reconstruction_setup.md).
