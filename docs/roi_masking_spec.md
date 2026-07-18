# Reconstruction Scope Control Specification

Status: Phase 1, density budgets, OpenMVS mask validation, and capture-mask package validation implemented

## Problem

Phone captures can remain centered on a small subject while COLMAP/OpenMVS
reconstructs distant, repeatedly visible background geometry. The July ATV and
raised-bed scan reconstructed the house more than 40 feet away and produced a
9,343,817-point dense cloud. Manual cleanup reduced the useful cloud, but only
after dense reconstruction had already spent GPU time and created an oversized
meshing workload.

This is a processing-scope problem, not necessarily a capture-quality problem.
Stable background features are useful for camera registration, but they should
not automatically become dense geometry.

## Goals

- Preserve unmasked background features for robust COLMAP camera alignment.
- Exclude unwanted image regions during dense reconstruction.
- Bound dense geometry before meshing, refinement, and texturing.
- Make object/scene scope explicit, observable, and configurable per job.
- Retain a safe unscoped fallback when masks or coordinate alignment are not
  trustworthy.
- Target 1-2 million cleaned points for an ATV/garden-scale Blender asset by
  default, without making point count the sole quality criterion.

## Non-goals

- Do not replace COLMAP camera registration with semantic segmentation.
- Do not destructively crop the uploaded source images.
- Do not promise automatic semantic understanding of multi-part subjects such
  as an ATV, garden bed, and the ground between them.
- Do not discard the original sparse model or the uncropped diagnostic cloud.
- Do not require LiDAR; ARKit depth and scene mesh remain optional evidence.

## Current Repository Gaps

1. Phase 1 invokes `DensifyPointCloud` with explicit ROI, visibility-filter,
   and fusion controls. Jobs can require a complete OpenMVS-ready mask set from
   `dense/masks`; capture-mask production and undistortion are not implemented.
2. COLMAP feature extraction has no mask input. This is acceptable for the
   recommended first pass because background features should remain available
   for alignment.
3. Object scan packages can contain `object_center_world` and
   `object_radius_meters`, but the backend does not know the ARKit-to-COLMAP
   transform. Existing scripts therefore support only a manually supplied
   COLMAP-space center and post-hoc spherical crop.
4. Job records expose broad stages only. They do not report scope source,
   mask coverage, ROI bounds, point counts, or reduction ratios.
5. Dense PLY headers are now checked against warning and hard point-count
   limits before meshing. Cleanup still does not set a voxel size, so clouds
   below the hard limit are not automatically downsampled.

## Proposed Pipeline

```text
validate upload
  -> unmasked COLMAP feature extraction / matching / sparse mapping
  -> sparse preview and camera/subject diagnostics
  -> choose reconstruction scope
  -> COLMAP image undistortion
  -> create/warp masks for undistorted images
  -> InterfaceCOLMAP
  -> OpenMVS DensifyPointCloud with masks + ROI + fusion controls
  -> scope validation and bounded downsampling
  -> ReconstructMesh
  -> optional RefineMesh
  -> TextureMesh using original RGB images and scoped mesh
  -> publish scoped artifacts and metrics
```

The sparse alignment is intentionally unmasked. Dense-stage masks suppress
background geometry without sacrificing useful camera-registration features.

## Scope Modes

Add a per-job `scope_mode` with these values:

- `auto_roi` (default for scene scans): OpenMVS estimates an ROI from the
  imported scene and crops dense output to it.
- `object_radius` (default for object scans when alignment is available): use
  the tapped ARKit center/radius transformed into reconstruction coordinates.
- `image_masks`: use supplied or generated per-image masks during OpenMVS
  densification.
- `hybrid`: apply image masks and a 3D ROI; recommended final production mode.
- `unbounded`: current behavior, retained as an explicit diagnostic fallback.

Scope selection must record both the requested and effective mode. A missing or
invalid optional input may fall back only according to a declared policy, and
the fallback must be visible in the job report.

## Phase 1: Activate Native OpenMVS Scope Controls

This phase requires no AI model and should ship first.

Change `build_openmvs_commands` to run:

1. `InterfaceCOLMAP`
2. `DensifyPointCloud scene.mvs -o scene_dense.mvs`
3. `ReconstructMesh scene_dense.mvs -o scene_mesh.mvs`
4. Optional `RefineMesh`
5. `TextureMesh`

Expose and pin these settings instead of relying on installed-version defaults:

- `resolution_level`: default `1`
- `max_resolution`: default `2560`
- `number_views`: default `8`
- `number_views_fuse`: default `3` for production, configurable down to `2`
- `filter_point_cloud`: default `1`
- `estimate_roi`: default `1.1` for `auto_roi`, otherwise `0`
- `crop_to_roi`: true except in `unbounded` mode
- `roi_border`: default `10` percent and configurable
- `mask_path`: omitted unless validated masks exist
- `mask_ignore_label`: `0` when masks exist, matching black-exclude semantics

The exact defaults require benchmark confirmation. The runner must include them
in the reconstruction report so upgrades cannot silently change behavior.

Keep COLMAP `fused.ply` optional for diagnostics. Production meshing should use
OpenMVS `scene_dense.ply`, because that is the cloud affected by OpenMVS masks
and ROI controls.

## Phase 2: User-Defined Image Scope

### Phone UX

Add an optional "Reconstruction Area" step after capture and before upload:

- Show a representative frame selected near the middle of the capture.
- Let the user draw one or more keep polygons, not only a single-object tap.
- Offer "propagate through frames" and show several sampled mask previews.
- Let the user expand/erase the propagated region.
- Make the ground between selected components retainable.
- Store masks separately; never modify source JPEGs.

The original object-center/radius UI remains useful for distance guidance and
future 3D ROI alignment. It is not sufficient by itself for irregular scenes.

### Package Contract

The typed `reconstruction_scope` manifest object below is implemented and
preserved when the backend regenerates its downstream manifest. Capture masks
under `masks/capture` are checked for safe layout, exact frame association,
declared count, grayscale PNG format, and frame dimensions. The geometric core
for conversion parses COLMAP camera text records and implements nearest-neighbor
`SIMPLE_RADIAL` to `PINHOLE` mapping, matching the current iPhone pipeline.
Lossless PNG decoding, binary normalization, atomic publication, and no-clobber
behavior are implemented. Original and dense COLMAP image records are matched
by exact filename and their camera IDs are paired automatically; this was also
verified against all 254 registered images in the ATV scan. Batch conversion
now exports both COLMAP models to temporary text form, stages every converted
mask, validates the complete set against dense images, and publishes
`dense/masks` under an advisory lock. Backend and workstation execution detect
validated capture-scope metadata, run or resume conversion after COLMAP dense
preparation, pass the generated directory to OpenMVS, and record conversion and
validation evidence.

Extend the manifest schema with an optional reconstruction-scope object:

```json
{
  "reconstruction_scope": {
    "schema_version": "1.0",
    "mode": "image_masks",
    "mask_space": "capture_image",
    "mask_convention": "white_keep_black_exclude",
    "mask_count": 98,
    "representative_frame_id": 49,
    "propagation_method": "manual_or_model_identifier",
    "minimum_keep_fraction": 0.05,
    "maximum_keep_fraction": 0.85
  }
}
```

Store masks under `masks/capture/<image-filename>.png`. Masks must be
single-channel or lossless RGB PNG, match the source image dimensions, use 255
for keep and 0 for exclude, and contain no partially transparent semantics.

The iOS package writer lazily creates `masks/capture` when the first mask is
saved, writes lossless mask data as
`<original-image-filename>.png` (for example `frame.jpg.png`), refuses invalid
image references and overwrites, and includes those files in the streamed ZIP.
The iOS target includes a deterministic polygon rasterizer that validates
normalized keep polygons and emits full-resolution, 8-bit grayscale PNG masks.
The capture UI can draw, clear, cancel, and confirm a normalized keep polygon
over the live camera preview. The portrait-only capture path maps the preview's
centered aspect-fill crop into each portrait JPEG, rasterizes a complete mask
set as frames are accepted, and declares `image_masks` scope only when every
captured frame has a mask.

### Backend Validation

Reject or ignore masks safely when any of these checks fail:

- path/reference containment and uniqueness
- PNG decoding and exact source dimensions
- frame-to-mask association
- binary value convention after normalization
- minimum/maximum keep-area thresholds
- declared and actual mask counts

Report missing masks separately from invalid masks. A partial mask set must not
silently turn unmasked frames into full-keep images; the configured fallback
must be explicit (`fail`, `exclude_frame`, or `unmasked_frame`). Production
default: `fail` for an explicitly requested mask job.

### Coordinate Handling

OpenMVS operates on COLMAP's undistorted images. Capture-space masks therefore
must be warped with the same camera model and output geometry used by
`image_undistorter`. Implement a dedicated mask-undistortion step and verify the
result against `dense/images` before passing its directory to
`DensifyPointCloud --mask-path`.

The installed OpenMVS expects mask filenames ending in `.mask.png`. Add an
integration fixture that proves the exact name mapping for this pinned build.

## Phase 3: Automatic Mask Proposal

Automatic masks are proposals, not authoritative scan semantics.

Recommended approach:

1. User marks keep regions on one representative frame.
2. A video/image-sequence segmentation model propagates those regions forward
   and backward through temporally ordered frames.
3. Dilate masks by a configurable safety margin (initial proposal: 2-5% of the
   shorter image dimension) to avoid cutting subject boundaries.
4. Flag abrupt area/centroid changes and low-confidence frames for review.
5. Preview at least the first, quartile, middle, three-quarter, and last masks.

Do not start with a fixed semantic class model. "ATV plus garden bed plus the
ground between them" is a user-defined region, not one reliable object class.

The model must be optional and isolated behind a `MaskGenerator` interface so
manual masks and future on-device/backend implementations share the same
validated output contract.

## Phase 4: ARKit-to-Reconstruction 3D ROI

Estimate a similarity transform between ARKit world coordinates and the COLMAP
reconstruction using corresponding camera centers/rotations from accepted
frames. The transform must account for rotation, translation, and scale.

Requirements:

- Match records by image/frame identifier, never array position alone.
- Estimate robustly with RANSAC or an equivalent outlier-resistant method.
- Record inlier count, residual distribution, scale, and handedness checks.
- Reject the transform when thresholds are not met.
- Transform `object_center_world` and `object_radius_meters` into reconstruction
  coordinates only after validation.
- Emit a visible ROI preview artifact before using it for destructive scope
  reduction.

For irregular scene scans, support an oriented box or polygonal prism rather
than forcing a sphere. Suggested scope metadata:

```json
{
  "shape": "oriented_box",
  "center_world": [0.0, 0.0, 0.0],
  "extents_meters": [8.0, 6.0, 3.0],
  "orientation_world": [0.0, 0.0, 0.0, 1.0]
}
```

Until this transform passes real-scan benchmarks, 3D ROI remains opt-in and
must not replace the mask/automatic-ROI path.

## Density Budget and Cleanup

Add a bounded post-dense stage before meshing:

- Record raw and scoped point counts.
- Warn above 2 million points for the default ATV/garden asset profile.
- Fail or require an explicit large-scene profile above a configurable hard
  budget, initially 10 million points.
- Add a voxel-size profile rather than a single universal value:
  - `object_detail`: 0.003-0.005 reconstruction meters after scale is known
  - `yard_asset`: 0.010 meters
  - `site_context`: 0.020-0.030 meters
- Never interpret metric voxel sizes until reconstruction scale is validated.
  Before scale alignment, use a target-count or bounding-diagonal-relative
  spacing.
- Preserve colors and normals, and orient/recompute normals only when needed.

Both pre- and post-downsample counts belong in the report. Point count alone is
not a success metric; retained coverage and texture output must also pass.

## API and Job Model Changes

Add upload/job options:

- `scope_mode`
- `scope_fallback`
- `density_profile`
- `retain_unscoped_cloud`
- optional OpenMVS tuning overrides for benchmark/admin use

Add lifecycle stages or structured substages for:

- `aligning`
- `preparing_scope`
- `densifying`
- `scoping`
- `meshing`
- `texturing`
- `exporting`

Do not encode progress only in a human-readable message. Add structured progress
fields: current command, percent when available, elapsed seconds, and the latest
point count.

Add a `reconstruction_scope_report.json` artifact containing:

- requested/effective scope mode and fallback reason
- mask source, count, validation results, and keep-area statistics
- ROI source, bounds, and coordinate system
- ARKit/COLMAP alignment quality when applicable
- all pinned OpenMVS scope/density arguments
- point counts and reduction ratios at every stage
- timings and peak memory when available
- warnings and rejected/fallback decisions

## Artifact Changes

Publish distinct, unambiguous outputs:

- `sparse/sparse_points.ply`
- `dense/scene_dense_unscoped.ply` (optional diagnostics)
- `dense/scene_dense_scoped.ply`
- `dense/scene_dense_scoped_clean.ply`
- `dense/scene_mesh_scoped.ply`
- `dense/scene_textured_scoped.obj`
- `dense/scene_textured_scoped.mtl`
- texture images
- `metadata/reconstruction_scope_report.json`

Avoid a generic `scene.ply`, which contributed to ambiguity between COLMAP and
OpenMVS outputs during recovery.

## Safety and Failure Behavior

- Never overwrite source images, uploaded masks, sparse models, or the last
  successful artifact.
- Treat empty scope output as a failed scope stage, not a successful empty mesh.
- Detect implausible reduction (for example, less than 1% retained or no
  reduction when masks exclude most pixels) and stop before meshing.
- If fallback is allowed, retain the failed scope report and mark the final job
  as completed-with-warning rather than silently changing behavior.
- Keep enough intermediate state to rerun densification/scoping without
  repeating sparse alignment.

## Repository Change Map

Backend:

- `backend/app/openmvs_runner.py`: add densification, scope config, explicit
  flags, filenames, and optional refinement.
- `backend/app/colmap_runner.py`: split/retain undistortion as a resumable stage;
  add optional diagnostic COLMAP fusion rather than requiring it for OpenMVS.
- `backend/app/reconstruction_backends.py`: include scope configuration and
  scoped artifacts in command plans.
- `backend/app/main.py`: orchestrate scope preparation and granular job stages.
- `backend/app/schemas.py` and `backend/app/jobs.py`: typed scope/progress data.
- New `backend/app/reconstruction_scope.py`: mode selection, validation,
  fallback policy, reports, and point-budget decisions.
- New `backend/app/mask_processor.py`: validate and undistort/rename masks.
- Later `backend/app/arkit_colmap_alignment.py`: robust similarity transform.
- `backend/app/point_cloud_processor.py`: density profiles, count reporting,
  color/normal preservation checks, and bounded processing.

iOS:

- `ScanMetadataModels.swift`: reconstruction-scope manifest types.
- `ScanCaptureManager.swift` / `MetadataWriter.swift`: persist scope metadata.
- New mask editor/preview and propagation boundary.
- `ScanPackageWriter.swift`: include masks with deterministic paths.
- Processing history: display effective scope, fallback warning, and reduction
  statistics.

Tests:

- Unit tests for config validation, commands, naming, fallback policy, and
  report serialization.
- Package validation tests for missing, malformed, escaping, mismatched, and
  partial masks.
- A synthetic integration fixture with foreground/background geometry proving
  that sparse alignment remains stable while dense background points decrease.
- A pinned OpenMVS integration test proving `.mask.png` discovery.
- ARKit/COLMAP transform tests with known scale/rotation/translation and
  injected outliers.
- Resume tests proving sparse alignment is reused after mask/ROI changes.

## Acceptance Criteria

For the ATV/garden regression scan or an equivalent fixture:

- At least 95% of images remain registered compared with the unmasked sparse
  baseline.
- The house/background is absent from the scoped dense preview.
- Dense point count decreases by at least 60% without visibly removing the ATV,
  raised bed, or selected connecting ground.
- Default output is at or below 2 million cleaned points unless the report
  explains why the density budget was intentionally overridden.
- Meshing starts from the scoped cloud, not the unscoped COLMAP fusion.
- Texturing completes with an OBJ, MTL, and all referenced texture files.
- The report makes every scope input, fallback, point-count change, and command
  setting auditable.
- A user can revise scope and rerun from the post-alignment checkpoint without
  repeating feature extraction, matching, or mapping.

## Rollout

1. Correct the OpenMVS runner and benchmark native automatic ROI on the existing
   scan.
2. Add reports, density budgets, and resumable post-alignment execution.
3. Add manual mask ingestion and backend mask undistortion.
4. Add phone polygon editing and mask packaging.
5. Add optional mask propagation with review.
6. Implement and benchmark ARKit-to-COLMAP alignment and 3D ROI.
7. Make `hybrid` the object/scene default only after regression scans meet the
   acceptance criteria.

## Research Basis

- [COLMAP FAQ: mask image regions](https://github.com/colmap/colmap/blob/master/doc/faq.rst)
- [OpenMVS discussion: masks during dense reconstruction](https://github.com/cdcseacave/openMVS/discussions/1173)
- [OpenMVS issue: small desired ROI producing very large dense clouds](https://github.com/cdcseacave/openMVS/issues/1079)
- [SlicerMorph photogrammetry workflow: propagated specimen masking](https://pmc.ncbi.nlm.nih.gov/articles/PMC12421799/)
