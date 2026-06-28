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
2. Run COLMAP dense reconstruction and OpenMVS from WSL2.
3. Write outputs into a Windows-accessible folder.
4. Open OBJ/PLY/GLB outputs directly in Blender on Windows.

## Object Scan Metadata

Object scans store:

- `scan_mode`: `object_scan`
- `object_center_world`: ARKit world-space subject center from the iPhone tap
- `object_radius_meters`: selected radius preset

This metadata is ready for object-focused processing, but exact COLMAP point
cloud cropping needs an ARKit-to-COLMAP coordinate alignment step.
