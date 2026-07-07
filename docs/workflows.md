# Workflows

## Local Mac Workflow

Use the MacBook for capture iteration and package validation:

1. Build and run the iPhone app from Xcode.
2. Choose `Object` or `Scene` mode.
3. For object scans, start scanning and tap the subject once tracking is stable.
4. Export the scan zip from the iPhone.
5. Validate and run sparse COLMAP locally:

```bash
python3 scripts/reconstruct_local.py scan.zip --work-dir /tmp/scanner-test --run-colmap
```

This produces `sparse/sparse_points.ply`. The Homebrew COLMAP build on macOS is
useful for sparse smoke tests, but not for dense CUDA reconstruction.
The script also writes `metadata/scan_report.json` with capture-quality warnings.
The default local matcher is `sequential_matcher`; use
`--matcher exhaustive_matcher` only when you want a slower quality check.

To inspect a backend command plan without running native reconstruction tools:

```bash
python3 scripts/plan_reconstruction_backend.py scan.zip \
  --backend colmap_openmvs
```

When `--work-dir` is omitted, the planner writes a persistent workspace under
`ScannerPlans/<scan_id>/<backend>/`. This folder is ignored by Git because it
contains extracted scan files and generated reports.

Alternate dry-run planners are available for Meshroom/AliceVision research:

```bash
python3 scripts/plan_reconstruction_backend.py scan.zip \
  --backend meshroom

python3 scripts/plan_reconstruction_backend.py scan.zip \
  --backend alicevision
```

The Meshroom planner uses `meshroom_batch`, which is the preferred AliceVision
entry point until the exact Windows/WSL2 install is verified. The direct
AliceVision planner is experimental and may need command option tuning for the
installed AliceVision release.

## Video Package Scaffold

Scan packages now reserve a `video/` folder and optional
`metadata/video.json` file. Current iPhone exports write an empty
`metadata/video.json` and set video counts to zero; actual `.mov` recording is
planned as a later capture feature.

Future video metadata entries should look like:

```json
[
  {
    "path": "video/scan.mov",
    "captured_at": "2026-07-07T00:00:00Z",
    "duration_seconds": 12.5,
    "frame_rate": 30,
    "resolution": [1920, 1080],
    "codec": "h264",
    "includes_audio": false
  }
]
```

The video path is optional for COLMAP/OpenMVS, but it will be useful for
MASt3R-SLAM, Lingbot-style point-cloud workflows, Gaussian splatting, and other
video-oriented neural reconstruction experiments.

To plan neural backend experiments without installing model dependencies:

```bash
python3 scripts/plan_neural_backend.py scan.zip --backend mast3r_slam
python3 scripts/plan_neural_backend.py scan.zip --backend depth_anything
python3 scripts/plan_neural_backend.py scan.zip --backend lingbot
```

See `docs/neural_backends.md` for backend-specific notes and license cautions.

## Windows GPU Workflow

Use the Windows RTX 3070 machine for final reconstruction and Blender work:

1. Set up WSL2 Ubuntu with NVIDIA GPU support.
2. Clone or copy this repo into WSL2.
3. Run the environment checker:

```bash
scripts/wsl/setup_gpu_reconstruction.sh
python3 scripts/wsl/check_reconstruction_env.py --strict
```

4. Dry-run the command plan:

```bash
python3 scripts/reconstruct_gpu.py scan.zip \
  --output-root /mnt/c/Users/YOU/ScannerOutputs \
  --dry-run
```

5. Run COLMAP dense reconstruction and OpenMVS from WSL2:

```bash
python3 scripts/reconstruct_gpu.py scan.zip --output-root /mnt/c/Users/YOU/ScannerOutputs
```

6. Open OBJ/PLY outputs directly in Blender on Windows.

To compare backend command plans on the workstation before a long run:

```bash
python3 scripts/plan_reconstruction_backend.py scan.zip \
  --backend colmap_openmvs \
  --work-dir /mnt/c/Users/YOU/ScannerPlans/colmap

python3 scripts/plan_reconstruction_backend.py scan.zip \
  --backend meshroom \
  --work-dir /mnt/c/Users/YOU/ScannerPlans/meshroom
```

The expected output layout is:

```text
ScannerOutputs/
  scan_id/
    source/
    logs/
      commands.log
    report.json
```

COLMAP/OpenMVS write their native outputs under `source/scan_id/`.

To create a `.blend` file from an output asset:

```bash
blender --background --python scripts/blender/prepare_scan_asset.py -- \
  /mnt/c/Users/YOU/ScannerOutputs/scan_id/source/scan_id/dense/scene_textured.obj \
  /mnt/c/Users/YOU/ScannerOutputs/scan_id/blender/scan_id.blend
```

The Blender helper accepts OBJ, PLY, GLB, and GLTF. It can also apply a scale,
set origins, relink textures, decimate meshes, and export a GLB:

```bash
blender --background --python scripts/blender/prepare_scan_asset.py -- \
  /mnt/c/Users/YOU/ScannerOutputs/scan_id/source/scan_id/dense/scene_textured.obj \
  /mnt/c/Users/YOU/ScannerOutputs/scan_id/blender/scan_id.blend \
  --texture-dir /mnt/c/Users/YOU/ScannerOutputs/scan_id/source/scan_id/dense \
  --scale 1.0 \
  --origin geometry \
  --decimate-ratio 0.5 \
  --export-glb /mnt/c/Users/YOU/ScannerOutputs/scan_id/blender/scan_id.glb
```

The helper supports Blender 4.x native OBJ/PLY import operators and falls back
to the Blender 3.x legacy OBJ/PLY import operators when needed.

To manually crop a point cloud in COLMAP/OpenMVS coordinates:

```bash
python3 scripts/plan_object_crop.py scan.zip
python3 scripts/crop_point_cloud.py input.ply cropped.ply --center 0 0 0 --radius 2.0
```

To dry-run point-cloud cleanup or downsampling after any backend produces a PLY:

```bash
python3 scripts/process_point_cloud.py input.ply output.ply \
  --processor open3d \
  --voxel-size 0.03 \
  --dry-run
```

Open3D is the default processing backend. ThreeCrate is available as an
experimental optional processor and is not installed by default:

```bash
python3 scripts/process_point_cloud.py input.ply output.ply \
  --processor threecrate \
  --voxel-size 0.03 \
  --dry-run
```

Remove `--dry-run` only after installing the selected processor in the active
Python environment. ThreeCrate should be compared against Open3D on real scan
outputs before replacing the default cleanup path.
ThreeCrate normal estimation is currently used only as a processing step; the
script converts the result back to a plain point cloud before writing, so
ThreeCrate output PLY files should not be treated as normal-preserving exports.

## Object Scan Metadata

Object scans store:

- `scan_mode`: `object_scan`
- `object_center_world`: ARKit world-space subject center from the iPhone tap
- `object_radius_meters`: selected radius preset

This metadata is ready for object-focused processing, but exact COLMAP point
cloud cropping needs an ARKit-to-COLMAP coordinate alignment step.
