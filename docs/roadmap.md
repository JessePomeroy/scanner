# Roadmap

This roadmap is ordered around the current goal: produce useful Blender assets
from iPhone scans, with Mac validation and Windows/WSL2 GPU reconstruction.

## Current Baseline

- iOS app captures ARFrame JPEG keyframes, camera transforms, intrinsics, blur
  scores, motion deltas, movement speed, and object-scan tap metadata.
- Scan packages export as ZIP files with `images/`, `metadata/`, `arkit/`,
  `depth/`, and `preview/`.
- Mac validation writes `metadata/scan_report.json`.
- Mac sparse COLMAP reconstruction works.
- Latest object scan test registered `97 / 98` frames and produced a sparse
  point cloud with `58,385` points.
- Dense reconstruction and textured mesh generation are planned for the Windows
  RTX 3070 / WSL2 workstation.

## Immediate Local Improvements

1. Add a fast local COLMAP mode using `sequential_matcher`.
   - Keep `exhaustive_matcher` for quality checks.
   - Default Mac smoke tests should use sequential matching because iPhone
     frames are naturally ordered.
   - Report matcher type and runtime in `scan_report.json`.

2. Tune capture warnings in `scan_report.json`.
   - Separate normal skipped live AR frames from real capture problems.
   - Keep warnings for tracking loss, accepted blurry frames, too few accepted
     frames, and excessive camera speed.
   - Rename or soften `high_rejected_frame_count` so successful keyframe
     throttling does not look like a scan failure.

3. Add optional IMU/motion capture.
   - Write `motion/imu.json` or `metadata/imu.json`.
   - Capture CoreMotion rotation rate, user acceleration, gravity, attitude,
     and timestamps.
   - Use this first for diagnostics; later it can help pose sanity checks.

4. Add richer camera exposure metadata.
   - Capture exposure duration, ISO, exposure target/offset when available.
   - Track focus/exposure/white-balance lock state more accurately.
   - Use these fields for texture-quality scoring and report warnings.

## Capture Package Evolution

1. Add a `raw_polycam_like/` or `pro_package/` export profile.
   - Include corrected images, camera metadata, optional depth/confidence,
     blur scores, pose metadata, and processing reports.
   - Keep the current simple scan package as the default until the pro profile
     proves useful.

2. Add optional depth/confidence slots without requiring LiDAR.
   - Standard iPhone scans should continue to work with no depth frames.
   - LiDAR-capable devices can fill `depth/` and confidence maps later.

3. Add package manifest.
   - Write `metadata/manifest.json` with schema version, app version, capture
     mode, enabled sensors, file counts, and known processing limitations.

## Object Scan Workflow

1. Improve object scan guidance.
   - Show whether the subject tap is set.
   - Warn when the user is likely too close/far for the selected radius.
   - Encourage circling the subject and varying height.

2. Improve object-focused processing.
   - Keep manual crop support with `plan_object_crop.py` and
     `crop_point_cloud.py`.
   - Add ARKit-to-COLMAP coordinate alignment once enough reconstructed scans
     are available to test reliably.
   - Use the tapped object center and radius for automatic crop proposals.

3. Add scan summary after export.
   - Show accepted frames, rejected frames, blur range, movement speed, scan
     mode, object radius, and whether object center was set.

## Reconstruction Pipeline

1. Mac smoke test path.
   - Validate package.
   - Run sparse COLMAP with sequential matching by default.
   - Export `sparse/sparse_points.ply`.
   - Refresh `scan_report.json`.

2. Windows/WSL2 GPU path.
   - Verify CUDA-enabled COLMAP.
   - Run dense COLMAP.
   - Run OpenMVS mesh reconstruction, refinement, and texturing.
   - Save outputs into a Windows-accessible Blender folder.

3. Output formats.
   - Keep OBJ first for Blender.
   - Add PLY point cloud inspection outputs.
   - Add GLB after textured OBJ is stable.
   - Add USDZ only after core reconstruction works.

## Reference Repositories To Learn From

- `xiongyiheng/ARKit-Scanner`: RGB-D package design, transforms, exposure,
  IMU, and desktop handoff.
- `3dugc/Area-Target-Scanner`: native Swift capture to ZIP plus Python
  processing workflow.
- `PolyCam/polyform`: raw Polycam-style export conventions for images, cameras,
  depth/confidence, blur scores, and pose metadata.
- `cedanmisquith/SwiftUI-LiDAR`: SwiftUI LiDAR scan UI and mesh export ideas.
- `kentaroy47/apple-lidar-stream`: future live LiDAR/Open3D streaming ideas.
- `apple/ARKitScenes`: dataset conventions and RGB-D/pose processing reference.

## Later Product Features

- Scan gallery.
- Job status UI.
- Upload to local workstation or cloud worker.
- Result download/share UI.
- Basic point cloud preview.
- Blender helper scripts for import, cleanup, decimation, and export variants.
