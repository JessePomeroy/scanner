---
title: Scanner Project Worklog
project: scanner
tags:
  - scanner
  - photogrammetry
  - iphone
  - blender
  - reconstruction
---

# Scanner Project Worklog

## Goal

Build a personal iPhone 3D scanning pipeline that captures real-world objects
or scenes, reconstructs them locally or on a Windows RTX workstation, and
produces Blender-friendly assets such as PLY, OBJ, GLB, and eventually USDZ.

## Current Architecture

- iOS SwiftUI scanner app captures ARKit-tracked scan packages.
- Scan packages contain images, frame metadata, session metadata, IMU samples,
  a manifest, and optional depth/ARKit folders.
- Mac scripts validate packages, write scan reports, and run sparse COLMAP
  smoke tests.
- Windows/WSL2 is the planned GPU reconstruction environment for dense COLMAP,
  OpenMVS, Meshroom/AliceVision, neural experiments, and Blender work.
- Blender automation scripts prepare reconstructed outputs for inspection and
  downstream art workflows.

## Completed Work

- [x] Created public GitHub repo and pushed the project scaffold.
- [x] Built initial iOS SwiftUI scanner app structure.
- [x] Added scan metadata models and package writer.
- [x] Added ARKit tracking and keyframe selection.
- [x] Added object scan mode with subject tap metadata.
- [x] Added scan validation and package reporting.
- [x] Added local COLMAP sparse reconstruction path.
- [x] Added WSL2/GPU reconstruction setup scripts and dry-run command planner.
- [x] Added object crop planning and point-cloud crop utilities.
- [x] Added IMU capture and `metadata/imu.json`.
- [x] Added package manifest generation.
- [x] Tuned report warnings so normal keyframe throttling is not treated as a
  scan failure.
- [x] Added project roadmap and external research references.

## Active PR Loop

### Backend Runner Planner

- [x] Create feature branch.
- [x] Inspect current backend/script/test structure.
- [x] Add shared reconstruction command-plan primitive.
- [x] Add Meshroom batch command planner.
- [x] Add experimental direct AliceVision command planner.
- [x] Add backend registry for `colmap_openmvs`, `meshroom`, and `alicevision`.
- [x] Add CLI for dry-run backend planning.
- [x] Add tests for new planners.
- [x] Update docs for the planner workflow.
- [x] Run verification.
- [ ] Open PR.
- [ ] Run subagent PR review.
- [ ] Address review findings.
- [ ] Merge PR.

## Upcoming To Do

- [ ] Prove full COLMAP/OpenMVS pipeline on the Windows RTX 3070 machine.
- [ ] Get one textured OBJ from a physical iPhone scan into Blender.
- [ ] Fix any WSL2 path/tooling issues found during GPU reconstruction.
- [ ] Replace ARFrame JPEG capture with `AVCapturePhotoOutput` high-resolution
  still capture.
- [ ] Add optional iOS video capture package export.
- [ ] Add Meshroom/AliceVision as an alternate photogrammetry backend.
- [ ] Evaluate ThreeCrate against Open3D for point-cloud/mesh processing.
- [ ] Add MASt3R-SLAM experiment workflow for video or image-folder input.
- [ ] Add Depth Anything / DA3 experiment notes and optional depth export path.
- [ ] Add Blender automation for import, scale/origin cleanup, decimation,
  material relinking, `.blend` save, and GLB export.
- [ ] Add job status UI.
- [ ] Add scan gallery.
- [ ] Add local workstation upload/processing flow.

## Research References

- COLMAP/OpenMVS: primary production reconstruction path.
- Meshroom/AliceVision: alternate traditional photogrammetry path.
- ThreeCrate: possible Rust/Python point-cloud and mesh processing layer.
- MASt3R-SLAM: experimental neural SLAM path for videos/image folders.
- MASt3R/DUSt3R: experimental learned matching and geometry references.
- Depth Anything / Depth Anything 3: depth estimation and future multi-view
  geometry experiments.
- Lingbot desktop workflow: reference for local video-to-point-cloud UX.

## Notes

- Keep production reconstruction separate from neural experiments until one
  real scan produces a textured OBJ that opens cleanly in Blender.
- High-resolution still capture should be prioritized before video capture for
  photogrammetry texture quality.
- Video export is still important for neural reconstruction, Gaussian
  splatting, NSR, and Lingbot/MASt3R-style workflows.
