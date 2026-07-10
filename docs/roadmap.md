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

1. Add a fast local COLMAP mode using `sequential_matcher`. Status: implemented.
   - Keep `exhaustive_matcher` for quality checks.
   - Default Mac smoke tests should use sequential matching because iPhone
     frames are naturally ordered.
   - Report matcher type and runtime in `scan_report.json`.

2. Tune capture warnings in `scan_report.json`. Status: implemented initial pass.
   - Separate normal skipped live AR frames from real capture problems.
   - Keep warnings for tracking loss, accepted blurry frames, too few accepted
     frames, and excessive camera speed.
   - Rename or soften `high_rejected_frame_count` so successful keyframe
     throttling does not look like a scan failure.

3. Add optional IMU/motion capture. Status: implemented initial pass.
   - Write `motion/imu.json` or `metadata/imu.json`.
   - Capture CoreMotion rotation rate, user acceleration, gravity, attitude,
     and timestamps.
   - Use this first for diagnostics; later it can help pose sanity checks.

4. Add richer camera exposure metadata. Status: partially implemented.
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

3. Add package manifest. Status: implemented initial pass.
   - Write `metadata/manifest.json` with schema version, app version, capture
     mode, enabled sensors, file counts, and known processing limitations.

4. Stream scan ZIP exports. Status: implemented.
   - Write ZIP headers and file contents directly to disk.
   - Avoid holding full scan archives or large video files in memory during
     export.

5. Add a video capture export mode. Status: implemented initial pass.
   - Record `video/scan.mov` from the live ARFrame camera stream alongside
     keyframes.
   - Keep the initial 30-second video cap until phone testing proves longer
     recordings export reliably.
   - Keep the video mode secondary to high-resolution stills for textured mesh
     quality.
   - Use video exports for neural reconstruction experiments such as
     MASt3R-SLAM, Lingbot-style point-cloud generation, and future splat/NSR
     tests.

6. Replace ARFrame JPEG capture with high-resolution still capture.
   - Use `AVCapturePhotoOutput` for source images intended for photogrammetry.
   - Keep ARFrame image capture available as a fallback/debug path.
   - Preserve ARKit pose, intrinsics, IMU, and lighting metadata for each
     accepted high-resolution photo.

7. Strengthen package metadata integrity. Status: implemented initial pass.
   - Parse frame, session, and video metadata into typed records.
   - Reject duplicate, nested, symlinked, or escaping references; invalid scalar
     values; non-increasing frame timestamps; and declared file-count
     mismatches.
   - Report legacy video files without `metadata/video.json` as a compatibility
     warning instead of silently ignoring the missing metadata.

8. Stream backend upload persistence. Status: implemented.
   - Copy FastAPI's spooled upload to disk in bounded chunks instead of loading
     the full ZIP into process memory.
   - Run sink I/O off-loop and publish through a temporary sibling, atomic
     no-clobber hard link, and POSIX directory sync.
   - Remove partial files and mark the job failed after read, write, or request
     cancellation errors.

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

3. Add scan summary after export. Status: implemented initial pass.
   - Show accepted frames, rejected frames, blur range, movement speed, scan
     mode, object radius, and whether object center was set.

## Reconstruction Pipeline

1. Mac smoke test path.
   - Validate package.
   - Run sparse COLMAP with sequential matching by default. Status: implemented.
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

4. Blender automation.
   - Add import scripts for OBJ/PLY/GLB outputs.
   - Add optional cleanup, origin placement, decimation, scale markers, and
     material relinking.
   - Save a ready-to-open `.blend` file in the Windows output folder.

## Backend Tracks

The production path should stay separate from experimental neural backends until
one physical scan has produced a textured OBJ that opens cleanly in Blender.

### Production Reconstruction

1. COLMAP/OpenMVS. Status: primary path.
   - Traditional image matching, sparse reconstruction, dense stereo, meshing,
     and texturing.
   - First milestone: real iPhone scan to textured OBJ on the Windows RTX 3070
     workstation.

2. Meshroom/AliceVision. Status: candidate alternate path.
   - Evaluate after COLMAP/OpenMVS is working.
   - Useful as a second photogrammetry stack for comparison and fallback.

3. ThreeCrate. Status: candidate processing utility.
   - Use as a possible Rust/Python point-cloud and mesh processing layer, not
     as the first image-to-mesh backend.
   - Candidate uses: fast PLY/OBJ I/O, voxel downsampling, normals, ICP,
     registration, Poisson/BPA reconstruction, simplification, and lightweight
     point-cloud viewing.
   - Compare against Open3D before replacing any existing cleanup scripts.

### Experimental Neural Reconstruction

These tools are useful research paths for a personal art workflow, but they
should not block the production COLMAP/OpenMVS path.

1. MASt3R-SLAM.
   - Strong first neural experiment because it accepts videos or image folders
     and documents WSL usage.
   - Test on the RTX 3070 with reduced frame counts/resolution if needed.

2. MASt3R and DUSt3R.
   - Useful for learned matching, pose estimation, and geometry experiments.
   - Keep optional because their licenses are non-commercial.

3. Depth Anything / Depth Anything 3.
   - Use for monocular depth, segmentation/cropping assistance, preview depth,
     and future multi-view geometry experiments.
   - Treat as support for scan understanding, not a direct textured OBJ
     replacement yet.

4. Lingbot-style local viewer.
   - Useful reference for drag-video-to-point-cloud UX.
   - Treat as point-cloud/viewer experimentation rather than Blender-ready
     textured mesh generation.

5. Gaussian splatting and NSR.
   - Status: implemented initial dry-run planner for Nerfstudio Splatfacto.
   - Add full execution after video export and workstation processing are
     stable.
   - Keep separate from editable mesh export because visual quality and
     Blender-editable geometry are different goals.

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
- `alicevision/meshroom`: alternate open-source photogrammetry workflow.
- `alicevision/alicevision`: alternate photogrammetry and camera-tracking
  framework behind Meshroom.
- `rajgandhi1/threecrate`: Rust/Python point-cloud and mesh processing library
  to evaluate against Open3D for cleanup, simplification, registration, and
  inspection.
- `rmurai0610/MASt3R-SLAM`: experimental video/image-folder neural SLAM path
  to test on WSL2.
- `naver/mast3r` and `naver/dust3r`: experimental learned matching/geometry
  references with non-commercial licenses.
- `LiheYoung/Depth-Anything`: monocular depth reference for future depth
  estimation, object isolation, and scan diagnostics.
- `donalleniii/lingbot-desktop-mac`: local video-to-point-cloud viewer workflow
  reference.

## Later Product Features

- Scan gallery. Status: implemented initial local ZIP gallery.
- Backend job lifecycle/status API. Status: implemented initial pass.
  - Persist lifecycle stages and UTC timestamps for processing history clients.
  - Replace job records atomically and prevent terminal jobs from restarting.
- Job status UI. Status: implemented initial read-only history.
  - Persist a configurable local backend URL on device.
  - Show recent job status, stage, message, capture counts, and update time.
- Upload to local workstation or cloud worker. Status: implemented initial
  validation-only local-workstation upload from the iOS scan gallery.
- Result download/share UI. Status: implemented initial typed single-file
  download and share flow for published job artifacts.
- Basic point cloud preview.
- Blender helper scripts for import, cleanup, decimation, and export variants.
