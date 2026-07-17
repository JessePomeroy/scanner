# Paired Mesh and Gaussian Benchmark Runbook

Status: Mac-side preparation complete; CachyOS/RTX execution pending.

Prepare the workstation with [`cachyos_setup.md`](cachyos_setup.md). The base
package script is not the entry gate: the pinned COLMAP/OpenMVS and isolated
Nerfstudio/gsplat toolchains must also be installed before this run begins.

This runbook uses one frozen iPhone package to produce two intentionally
different canonical results:

- a conventional textured mesh GLB through COLMAP, OpenMVS, and Blender;
- a Gaussian SOG through Nerfstudio Splatfacto/gsplat and PlayCanvas
  SplatTransform.

The run is evidence, not a tuning contest. Do not silently replace the input,
change quality profiles, omit failed stages, or repair one result beyond the
agreed 15-minute preparation allowance.

## Frozen Inputs

- Scan ID: `scan_2026_07_15_00_41_09`
- SHA-256: `ef9a6e0aefa564facf17357252e7fa2bd2cec55882a107461abad5c6459cb779`
- Size: 429,671,685 bytes
- Images: 254 JPEG keyframes at 1440 × 1920
- Capture span: 114.59 seconds
- Scanner reconstruction/configuration baseline: `d5f19d9160e94479fd97c6d40d7bc703bc269c8e`
- Original Mac path:
  `/Users/jessepomeroy/Downloads/scan_2026_07_15_00_41_09.zip`
- Verified backup path:
  `/Users/jessepomeroy/Documents/scanner-benchmarks/official/scan_2026_07_15_00_41_09.zip`

The evidence-tool commit is recorded separately from the scanner baseline. The
tool may improve how measurements are collected, but it must not change the
frozen reconstruction commands or profiles without a new explicit decision.

## Mac Sparse Sanity Evidence

A CPU-only COLMAP 4.1 preflight completed on the exact frozen ZIP before Linux
setup. It detected 3,405,974 features, verified 1,777 image pairs, and
registered all 254 cameras into one model with 80,047 sparse points, 417,365
observations, a 5.214 mean track length, and 0.686064 px mean reprojection
error. Feature extraction took 28.254 minutes, sequential matching took 15.034
minutes, sparse mapping took 3.540 minutes, and the recorded COLMAP total was
2,816.411 seconds.

This is a strong package/overlap pass, not benchmark performance evidence: the
Mac COLMAP build has no CUDA, so its timing must not be used to estimate the
RTX 3070 run. Preserved evidence lives beside the durable ZIP under
`preflight/mac-sparse/`.

## Required Linux Layout

Keep active files on a Linux-native filesystem:

```text
~/scanner/                         current evidence-tool checkout
~/scanner-baseline/                detached worktree at d5f19d9
~/ScannerBenchmarks/input/         untouched ZIP copy
~/ScannerBenchmarks/run-001/
  evidence.json
  logs/
  plans/
  mesh/
  gaussian/
  blender/
  comparison/
```

Create the baseline worktree only after the repository and commit are present:

```bash
git -C ~/scanner worktree add --detach ~/scanner-baseline d5f19d9
```

## Entry Gate

Do not begin the paired run until all of these are true:

1. `nvidia-smi` sees the RTX 3070 and reports 8 GB VRAM.
2. `nvcc` and a real PyTorch CUDA check pass inside the activated neural
   environment.
3. CUDA-enabled COLMAP, OpenMVS, Blender, Nerfstudio/gsplat, Node.js, Codex,
   and SplatTransform pass the strict environment record.
4. The input ZIP hash matches exactly.
5. The baseline worktree resolves to `d5f19d9` and is clean.
6. The evidence-tool checkout is clean and its separate commit is recorded.
7. There is sufficient Linux-native free space for both paths and logs.

## Initialize Evidence

```bash
SCAN=~/ScannerBenchmarks/input/scan_2026_07_15_00_41_09.zip
RUN=~/ScannerBenchmarks/run-001
mkdir -p "$RUN"/{logs,plans,mesh,gaussian,blender,comparison}

python3 ~/scanner/scripts/benchmark_evidence.py init \
  --scan "$SCAN" \
  --expected-sha256 ef9a6e0aefa564facf17357252e7fa2bd2cec55882a107461abad5c6459cb779 \
  --scanner-baseline-commit d5f19d9 \
  --report "$RUN/evidence.json"
```

The first RTX 3070 run is intentionally `uncalibrated`; do not invent a precise
duration. After real stage measurements exist, three hours or less is normal
daytime work, more than three hours is an overnight candidate, and 12 hours or
more triggers the practical-limit warning.

## Generate Frozen Command Plans

Generate plans from the detached baseline worktree, never from an unrecorded
working copy:

```bash
python3 ~/scanner-baseline/scripts/plan_reconstruction_backend.py "$SCAN" \
  --backend colmap_openmvs \
  --matcher sequential_matcher \
  --work-dir "$RUN/mesh/work" \
  --report "$RUN/plans/mesh.json"

python3 ~/scanner-baseline/scripts/plan_neural_backend.py "$SCAN" \
  --backend gaussian_splatting \
  --splat-method splatfacto \
  --splat-matching-method sequential \
  --splat-delivery-format sog \
  --splat-delivery-format html \
  --work-dir "$RUN/gaussian/work" \
  --report "$RUN/plans/gaussian.json"
```

Review both JSON plans before execution. Paths may be rebased into the run
folder, but command options and profiles must remain unchanged.

## Record Every Stage

Wrap each real command from the plans separately so failures and timings remain
visible:

```bash
python3 ~/scanner/scripts/benchmark_evidence.py run \
  --report "$RUN/evidence.json" \
  --stage mesh_colmap_feature_extractor \
  --log "$RUN/logs/mesh_colmap_feature_extractor.log" \
  -- colmap feature_extractor ...
```

Use these stable stage names.

Mesh path:

1. `mesh_colmap_feature_extractor`
2. `mesh_colmap_sequential_matcher`
3. `mesh_colmap_mapper`
4. `mesh_colmap_model_converter`
5. `mesh_colmap_image_undistorter`
6. `mesh_colmap_patch_match_stereo`
7. `mesh_colmap_stereo_fusion`
8. `mesh_openmvs_interface_colmap`
9. `mesh_openmvs_densify_point_cloud`
10. `mesh_openmvs_reconstruct_mesh`
11. `mesh_openmvs_refine_mesh`
12. `mesh_openmvs_texture_mesh`
13. `mesh_blender_prepare_glb`

Gaussian path:

1. `splat_ns_process_data`
2. `splat_ns_train_splatfacto`
3. `splat_ns_export_gaussian`
4. `splat_transform_sog`
5. `splat_transform_html`

For the Gaussian export, replace the planner's placeholder with the exact
`config.yml` emitted by the completed Splatfacto run and record that resolved
path in the command. Preserve `splat.ply` as the editable master.

The VRAM sampler records total used memory reported by `nvidia-smi`; close
unrelated GPU applications so the peak remains attributable to the benchmark.
Each stage also records `vram_sample_errors`; a nonzero value makes that
stage's peak VRAM incomplete and must be called out in the comparison record.

## Canonical Artifacts

Mesh path must end with:

- source textured OBJ/MTL/textures;
- Blender `.blend` preparation file;
- conventional textured `scene.glb`.

Gaussian path must end with:

- editable `splat.ply` master;
- canonical `scene.sog`;
- standalone `scene.html` inspection viewer.

Finalize evidence and hash artifacts:

```bash
python3 ~/scanner/scripts/benchmark_evidence.py finalize \
  --report "$RUN/evidence.json" \
  --artifact mesh_glb="$RUN/blender/scene.glb" \
  --artifact mesh_blend="$RUN/blender/scene.blend" \
  --artifact splat_ply="$RUN/gaussian/splat.ply" \
  --artifact splat_sog="$RUN/gaussian/scene.sog" \
  --artifact splat_html="$RUN/gaussian/scene.html"
```

## Stop and Failure Rules

- Never rerun a failed stage under the same name; start a new `run-###` record.
- Never lower quality, reduce images, change matching, or substitute a format
  without recording a deviation and obtaining an explicit decision.
- Preserve failed logs and partial artifacts.
- Stop for an out-of-memory error instead of silently switching profiles.
- Mark any run reaching 12 hours as failing the practical gate, even if it
  eventually completes.
- Do not count unattended reconstruction time as Blender preparation time.

## Blender Review

Use `docs/benchmark_comparison_template.md`. Each result receives at most 15
minutes of crop/orientation/scale/material or viewer setup. Save comparable
stills or short renders for an orbit near the captured path, a low-to-high
move, and a gentle push or pull. Do not remodel, retopologize, repair holes, or
repaint textures.

The benchmark is complete only when `evidence.json`, all logs, canonical
artifacts, the filled comparison record, and the fixed-camera outputs are
preserved together.
