# CachyOS reconstruction workstation setup research

Research date: 2026-07-17

## Question

How should the scanner project's Ubuntu-oriented post-install setup be adapted for native CachyOS on an RTX 3070 workstation?

## Recommendation

Do not run `scripts/wsl/setup_gpu_reconstruction.sh` unchanged on CachyOS. It assumes `apt-get`, Ubuntu package names, and Ubuntu's Python layout. Add a CachyOS/Arch-specific setup path that:

1. fully updates the rolling-release system with `pacman`;
2. verifies the NVIDIA driver chosen by the CachyOS installer before changing it;
3. prefers CachyOS's precompiled open NVIDIA module matching the installed CachyOS kernel;
4. installs the CUDA toolkit and ordinary tools from official repositories;
5. keeps Nerfstudio/gsplat in a separate, version-pinned Python environment instead of the system Python;
6. treats COLMAP and OpenMVS as reviewed, pinned source/AUR builds rather than unattended official-repository installs; and
7. reuses `check_reconstruction_env.py`, which is already largely distribution-independent.

For the default `linux-cachyos` kernel and this desktop RTX 3070, the preferred driver package is `linux-cachyos-nvidia-open`, with its matching `nvidia-utils`. The RTX 3070 is Ampere; NVIDIA recommends the open kernel-module flavor for Turing and newer GPUs, and CachyOS supplies a precompiled open module tied to the exact `linux-cachyos` version. Prefer CachyOS's `chwd` hardware manager to select and configure the package rather than duplicating its hardware/profile logic in the scanner script. Do not mix open and proprietary kernel modules. If a verified GSP-related fault appears, the fallback is the matching proprietary `linux-cachyos-nvidia` package, not an automatic first choice.

Sources:

- [NVIDIA driver guide: open modules are suggested for Turing and newer](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/kernel-modules.html)
- [NVIDIA: transition guidance for Turing, Ampere, Ada, and Hopper](https://developer.nvidia.com/blog/nvidia-transitions-fully-towards-open-source-gpu-kernel-modules)
- [CachyOS kernel documentation: precompiled open and closed NVIDIA modules](https://wiki.cachyos.org/features/kernel/)
- [CachyOS `linux-cachyos-nvidia-open` package](https://packages.cachyos.org/package/cachyos/x86_64/linux-cachyos-nvidia-open)
- [CachyOS hardware management with `chwd`](https://wiki.cachyos.org/features/chwd/chwd/)

## Package-manager conversion

CachyOS is Arch-based and uses `pacman`. Arch does not support partial upgrades, so the script must not use `pacman -Sy` followed by selective installation. Refresh and upgrade together:

```bash
sudo pacman -Syu
```

Then install ordinary dependencies without needlessly reinstalling present packages:

```bash
sudo pacman -S --needed \
  base-devel \
  git \
  cmake \
  ninja \
  pkgconf \
  python \
  python-pip \
  unzip \
  zip \
  ffmpeg \
  blender \
  cuda \
  nodejs-lts-jod \
  npm
```

`nodejs-lts-jod` is the official Node 22 LTS package and satisfies the scanner checker's Node 22-or-newer requirement. It conflicts with the rolling `nodejs` package, so the setup should detect an existing Node installation rather than forcing a replacement. The rolling `nodejs` package also satisfies the current minimum when its major version is at least 22.

Primary package references:

- [`base-devel`](https://archlinux.org/packages/core/any/base-devel/) — compiler and Arch package-build tool set
- [`python`](https://archlinux.org/packages/core/x86_64/python/)
- [`python-pip`](https://archlinux.org/packages/extra/any/python-pip/)
- [`cmake`](https://archlinux.org/packages/extra/x86_64/cmake/)
- [`ninja`](https://archlinux.org/packages/extra/x86_64/ninja/)
- [`git`](https://archlinux.org/packages/extra/x86_64/git/)
- [`ffmpeg`](https://archlinux.org/packages/extra/x86_64/ffmpeg/)
- [`blender`](https://archlinux.org/packages/extra/x86_64/blender/)
- [`cuda`](https://archlinux.org/packages/extra/x86_64/cuda/)
- [`nodejs-lts-jod`](https://archlinux.org/packages/extra/x86_64/nodejs-lts-jod/)
- [`npm`](https://archlinux.org/packages/extra/any/npm/)
- [Arch system-maintenance guidance](https://wiki.archlinux.org/title/System_maintenance)
- [CachyOS mirror/update troubleshooting](https://wiki.cachyos.org/cachyos_basic/faq/)

Package versions above must not be hard-coded. CachyOS is rolling release; record resolved versions in the benchmark evidence after installation.

## NVIDIA driver and kernel matching

First inspect what the installer selected:

```bash
uname -r
lspci -k -d ::03xx
pacman -Q | grep -E '^(linux|nvidia)'
```

`chwd` normally runs during CachyOS installation. If no suitable driver profile was installed or the GPU was added later, use CachyOS's supported post-install auto-configuration and then inspect the result:

```bash
sudo chwd -a
chwd -d --list
```

For reference, the concrete package transaction for the default kernel is the precompiled matching module plus user-space utilities:

```bash
sudo pacman -S --needed linux-cachyos-nvidia-open nvidia-utils
sudo reboot
```

The scanner setup should not blindly run both approaches. It should inspect the installed `chwd` profile and packages first, use `chwd` when configuration is missing, and treat the direct package command as a documented recovery/manual path.

The kernel variant and NVIDIA module package must match. Examples visible in CachyOS's repository include:

| Installed kernel | Matching open module |
| --- | --- |
| `linux-cachyos` | `linux-cachyos-nvidia-open` |
| `linux-cachyos-lts` | `linux-cachyos-lts-nvidia-open` |
| `linux-cachyos-server` | `linux-cachyos-server-nvidia-open` |
| another CachyOS variant | the same variant name plus `-nvidia-open` |

CachyOS explains that its precompiled modules avoid rebuilding after every kernel update and effectively supersede DKMS for the supported kernel variants. Use `nvidia-open-dkms` only when a custom/non-precompiled kernel or another concrete requirement makes DKMS necessary; in that case install the matching kernel headers and verify the DKMS build before rebooting. The [CachyOS package search](https://packages.cachyos.org/?repo=cachyos&search=nvidia-) lists the available variant packages.

After the reboot, verify the running driver rather than merely checking installed packages:

```bash
uname -r
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
lsmod | grep '^nvidia'
modinfo nvidia | sed -n '1,12p'
cat /sys/module/nvidia_drm/parameters/modeset
```

The last command should report `Y`. Arch's NVIDIA guidance states that DRM kernel mode setting is enabled by default by current `nvidia-utils`. Wayland does not need to be disabled for reconstruction compute. CachyOS uses a Wayland-based KWin SDDM configuration by default; its documented caveats concern items such as overclocking controls and older GPUs, not CUDA batch work.

Sources:

- [Arch NVIDIA guide](https://wiki.archlinux.org/title/NVIDIA)
- [CachyOS general system tweaks: SDDM/Wayland and GSP notes](https://wiki.cachyos.org/configuration/general_system_tweaks/)

### GSP fallback

The open NVIDIA modules depend on GSP firmware, so `NVreg_EnableGpuFirmware=0` has no effect with them. Only if the workstation demonstrates a reproducible GSP problem should the operator test the matching proprietary package and the CachyOS-documented GSP option. This should be a manual troubleshooting branch because changing the active graphics driver can leave the desktop unbootable if the module/initramfs state is inconsistent.

## CUDA toolkit

Install the official Arch/CachyOS `cuda` package with `pacman`; do not use NVIDIA's standalone `.run` installer on top of distribution-managed drivers:

```bash
sudo pacman -S --needed cuda
```

As of the research date, the repository package is CUDA 13.3.1, installs under `/opt/cuda`, supplies `/etc/profile.d/cuda.sh`, and includes `/opt/cuda/bin/nvcc`. Start a new login shell after installation or source the profile file before the same-shell checks:

```bash
source /etc/profile.d/cuda.sh
nvcc --version
nvidia-smi
```

Sources:

- [CachyOS/Arch CUDA package metadata](https://packages.cachyos.org/package/extra/x86_64/cuda)
- [Arch CUDA package file list](https://archlinux.org/packages/extra/x86_64/cuda/files/)
- [NVIDIA CUDA installation guide](https://docs.nvidia.com/cuda/cuda-installation-guide-linux/)

Important uncertainty: NVIDIA's current distribution-support table does not list Arch Linux or CachyOS. The Arch-maintained `cuda` package is therefore the practical distribution integration, but Arch/CachyOS is not an NVIDIA-qualified host distribution. Also, the newest system CUDA may be newer than the CUDA/PyTorch combinations tested by Nerfstudio or gsplat. Do not assume that installing `cuda` alone selects a compatible Python stack.

## Python, PyTorch, Nerfstudio, and gsplat

Do not repeat the Ubuntu script's global command:

```text
python3 -m pip install --user ...
```

Arch marks the system interpreter as externally managed and rolls Python forward quickly. As of the research date, official Arch `python` is 3.14. The neural reconstruction environment should instead use a dedicated environment with the Python, PyTorch, and CUDA combination frozen by the benchmark runbook. The environment may be created with a project-selected environment manager, but the setup must not silently choose unverified latest versions.

gsplat's upstream installation requires PyTorch first, compiles CUDA code on first use when installed from PyPI, and publishes wheels only for specific Python/PyTorch/CUDA combinations. Its CUDA JIT path also requires a working `nvcc`. Verification must exercise CUDA, not merely import the packages:

```bash
python -c 'import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_name(0))'
ns-process-data --help
ns-train --help
ns-export --help
```

Sources:

- [gsplat upstream installation](https://github.com/nerfstudio-project/gsplat#installation)
- [Nerfstudio Splatfacto documentation](https://github.com/nerfstudio-project/nerfstudio/blob/main/docs/nerfology/methods/splat.md)

## COLMAP

COLMAP is not currently an official Arch repository binary. A user-maintained [`colmap` AUR recipe](https://aur.archlinux.org/packages/colmap) exists, but AUR content is unsupported and must be reviewed. As of the research date it reports version 4.1.0-3 and CUDA enabled by default, with `CUDA_ARCH=native` available for a machine-local build.

For a benchmark whose toolchain must be reproducible, prefer a pinned upstream COLMAP tag/commit and an explicit source build, or pin and archive the reviewed AUR `PKGBUILD` revision. COLMAP's own installation guide warns that distribution binaries commonly omit CUDA and recommends a manual build for CUDA support. For this one-machine build, configure `CMAKE_CUDA_ARCHITECTURES=native`; the RTX 3070 is then detected at build time.

Required verification:

```bash
colmap -h
```

The project checker additionally rejects help output containing `without CUDA`. Before the official benchmark, run a small GPU feature-extraction command or the project preflight so a nominally installed executable does not count as a working CUDA build.

Sources:

- [COLMAP official installation guide](https://github.com/colmap/colmap/blob/dev/doc/install.rst)
- [COLMAP upstream repository](https://github.com/colmap/colmap)
- [Arch AUR security and build guidance](https://wiki.archlinux.org/title/Arch_User_Repository)

## OpenMVS

OpenMVS is also not currently an official Arch repository binary. A user-maintained [`openmvs` AUR recipe](https://aur.archlinux.org/packages/openmvs) exists. As of the research date it reports version 2.4.0-3 and explicitly disables CUDA by default; its maintainer documents enabling it with the make flag `USE_CUDA=ON`.

This means an unattended `paru -S openmvs` would be wrong for the GPU benchmark even if it succeeds. Either:

1. review and pin the AUR recipe and deliberately pass its CUDA build flag; or
2. build a pinned upstream OpenMVS release/commit with the upstream CUDA feature enabled.

The AUR page also shows AUR-only dependencies, so a plain `makepkg -s` may not resolve the entire chain. Arch explicitly says AUR helpers and AUR packages are unsupported; automation should not download and execute changing AUR recipes without review.

Verify every required binary:

```bash
InterfaceCOLMAP --help
DensifyPointCloud --help
ReconstructMesh --help
RefineMesh --help
TextureMesh --help
```

Sources:

- [OpenMVS upstream repository](https://github.com/cdcseacave/openMVS)
- [OpenMVS AUR recipe](https://aur.archlinux.org/packages/openmvs)
- [Arch AUR guidance](https://wiki.archlinux.org/title/Arch_User_Repository)

## Blender, FFmpeg, and Node

These are ordinary supported repository packages:

```bash
sudo pacman -S --needed ffmpeg blender nodejs-lts-jod npm
```

Verify:

```bash
ffmpeg -version | head -n 1
blender --version | head -n 1
node --version
npm --version
```

The Blender package lists CUDA as an optional dependency for Cycles rendering. This is separate from using the NVIDIA GPU for COLMAP/OpenMVS/Nerfstudio, but installing `cuda` satisfies both uses.

## Secure Boot and reboot caveats

CachyOS's installation guide says Secure Boot and CSM must be disabled for its UEFI installation. If Secure Boot remains disabled, the reconstruction setup should not try to configure it. If the user later chooses to enable it, stop and follow CachyOS's dedicated `sbctl` enrollment and signing guide; do not improvise this inside an application setup script.

Sources:

- [CachyOS installation guide](https://wiki.cachyos.org/installation/installation_on_root/)
- [CachyOS Secure Boot setup](https://wiki.cachyos.org/configuration/secure_boot_setup/)

Reboot after changing the NVIDIA kernel module. A package being installed does not prove that the module for the currently running kernel loaded successfully. After any kernel/driver update, `uname -r`, `nvidia-smi`, and the strict project checker are the acceptance gate.

## Proposed setup-script behavior

The CachyOS script should be conservative and idempotent:

1. Require CachyOS/Arch by checking `/etc/os-release` and the presence of `pacman`.
2. Print the active kernel, detected GPU, installed NVIDIA packages, and Secure Boot state.
3. Run a full `pacman -Syu`; never create a partial-upgrade state.
4. Install only supported base packages using `--needed`.
5. Inspect the installed `chwd` profile; if driver configuration is missing, use `sudo chwd -a`, then verify that the selected kernel module matches the kernel. Do not silently switch a working driver flavor.
6. Stop with a reboot-required message if the running NVIDIA module is absent or does not match the updated kernel.
7. Install `cuda`, then make `/opt/cuda/bin` visible in the current setup process.
8. Create the pinned neural environment separately from system Python.
9. Build COLMAP and OpenMVS from pinned, recorded inputs with CUDA explicitly enabled; record commits, flags, compiler, and toolkit versions.
10. Install SplatTransform under Node 22+.
11. Run `python3 scripts/wsl/check_reconstruction_env.py --strict` and save its JSON output with the benchmark evidence.

## Full verification checklist

```bash
cat /etc/os-release
uname -r
lspci -k -d ::03xx
pacman -Q linux-cachyos linux-cachyos-nvidia-open nvidia-utils cuda
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
cat /sys/module/nvidia_drm/parameters/modeset
source /etc/profile.d/cuda.sh
nvcc --version
colmap -h
InterfaceCOLMAP --help
DensifyPointCloud --help
ReconstructMesh --help
RefineMesh --help
TextureMesh --help
blender --version
ffmpeg -version
node --version
npm --version
ns-process-data --help
ns-train --help
ns-export --help
python3 scripts/wsl/check_reconstruction_env.py --strict
```

The `pacman -Q` line assumes the default CachyOS kernel. Substitute the actual kernel/module package names when another variant is installed.

## Uncertainties to resolve on the installed workstation

- Which CachyOS kernel variant the installer selected. This determines the exact NVIDIA module package.
- Whether the installer already configured `linux-cachyos-nvidia-open`; avoid reinstalling or switching a working driver unnecessarily.
- Whether current CUDA 13.3 is compatible with the frozen PyTorch/Nerfstudio/gsplat versions. Resolve this by selecting the neural environment from its upstream compatibility matrix and running a real CUDA operation.
- Whether the current AUR COLMAP recipe builds cleanly after its recently reported dependency changes. A reproducible pinned upstream build may be safer.
- Whether the current OpenMVS AUR dependency chain and `USE_CUDA=ON` build are healthy. Do not count a CPU-only OpenMVS package as benchmark-ready.
- Whether Secure Boot will remain disabled. Enabling it is a separate boot-trust project, not an application dependency step.
