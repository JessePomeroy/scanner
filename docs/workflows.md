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

To manually crop a point cloud in COLMAP/OpenMVS coordinates:

```bash
python3 scripts/crop_point_cloud.py input.ply cropped.ply --center 0 0 0 --radius 2.0
```

## Object Scan Metadata

Object scans store:

- `scan_mode`: `object_scan`
- `object_center_world`: ARKit world-space subject center from the iPhone tap
- `object_radius_meters`: selected radius preset

This metadata is ready for object-focused processing, but exact COLMAP point
cloud cropping needs an ARKit-to-COLMAP coordinate alignment step.
