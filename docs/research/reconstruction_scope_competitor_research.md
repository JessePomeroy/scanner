# How established photogrammetry tools limit unwanted reconstruction

Research date: 2026-07-18

## Question

How do established photogrammetry and 3D-scanning applications avoid
reconstructing too much background, and how does the scanner project's new
**Limit Reconstruction Area** feature compare?

## Short answer

The scanner now has a useful first layer: one screen-space polygon is mapped
into every accepted iPhone frame, saved as a white-keep/black-exclude mask,
warped into COLMAP's undistorted image geometry, and used by OpenMVS only while
building the dense point cloud. COLMAP alignment remains unmasked. This is a
deliberate and defensible choice for scene scans because stable background
features can help recover camera poses without becoming dense geometry.

Established products generally use several complementary controls instead of
one:

1. capture guidance and coverage feedback;
2. per-image masks, sometimes selectable by processing stage;
3. a 3D reconstruction region chosen after sparse alignment;
4. component, mesh, or point selection for non-destructive cleanup; and
5. automatic object segmentation where the subject is object-like.

For the project's artistic doorway, building, street-object, and room-scale
goals, the most valuable next addition is a **post-alignment 3D reconstruction
box with sparse-point preview**, followed by mask propagation and review. A
fixed screen-space polygon is helpful when framing is consistent, but it cannot
represent an irregular world-space scene by itself.

## What the current scanner implementation does

The current iPhone control is a capture-time framing window, not semantic
segmentation and not a 3D crop:

- The user traces one normalized polygon over the live portrait preview.
- The same normalized polygon is mapped from the aspect-filled preview into
  every accepted JPEG; it does not track an object or change shape between
  frames.
- The app emits one full-resolution grayscale PNG per accepted frame. White is
  kept and black is excluded.
- The package declares mask scope only when the mask set is complete.
- The backend validates the masks, maps them from the distorted capture images
  into COLMAP's undistorted images, and refuses partial or malformed sets.
- COLMAP feature extraction, matching, and sparse alignment still use the full
  images.
- `DensifyPointCloud` receives `--mask-path` and
  `--ignore-mask-label 0`, so OpenMVS suppresses black pixels during dense
  reconstruction.
- OpenMVS mesh generation therefore starts from the scoped dense cloud. The
  current runner does not pass the masks to sparse alignment or to
  `TextureMesh`; original RGB images remain available for texturing the kept
  geometry.

Relevant implementation:

- [`CaptureMaskEditorView.swift`](../../ios/ScannerApp/CaptureMaskEditorView.swift)
- [`CaptureMaskCoordinateMapper.swift`](../../ios/ScannerApp/CaptureMaskCoordinateMapper.swift)
- [`ScanCaptureManager.swift`](../../ios/ScannerApp/ScanCaptureManager.swift)
- [`mask_undistorter.py`](../../backend/app/mask_undistorter.py)
- [`openmvs_runner.py`](../../backend/app/openmvs_runner.py)
- [`roi_masking_spec.md`](../roi_masking_spec.md)

## What other tools do

### RealityScan / RealityCapture

RealityScan Mobile's review flow is the strongest direct model for this
project. After capture, it analyzes the images and shows a sparse point cloud in
color or coverage-quality mode. The user can return to capture to fill weak
areas, then adjust a **3D cropping box** that defines the reconstruction area
before processing. This makes scope a world-space decision after camera
alignment, rather than a fixed 2D decision made before walking around the
subject. [RealityScan Mobile: Review Scan](https://dev.epicgames.com/documentation/en-us/realityscan-mobile/RealityScan-Review-Scan)

Desktop RealityScan provides several additional layers:

- A reconstruction region can be set automatically, by sparse-point density,
  on a grid or reconstruction, from control points, or from a clipping box. The
  documentation explicitly says restricting the region speeds model
  computation by not considering unimportant parts of the scene.
  [Reconstruction Region](https://rshelp.capturingreality.com/en-US/tools/reconbox.htm)
- Masks are independent image layers, and each input can enable them for
  alignment, meshing, and texturing/coloring separately.
  [Selected Input](https://rshelp.capturingreality.com/en-US/appbasics/selectedinputs.htm)
- The CLI exposes AI mask generation, largest-component selection, selection of
  triangles inside or outside the reconstruction region, filtering selected
  triangles, and cutting a model by the box.
  [All Commands](https://dev.epicgames.com/documentation/en-us/realityscan/all-commands)
- Mask layers can be configured for alignment only, meshing only, both, or
  neither. [CLI Settings for Selections](https://rshelp.capturingreality.com/en-US/tutorials/editselectioncommand.htm)

This is a layered workflow: alignment can use the background when useful,
meshing can obey masks and a 3D region, and unwanted connected components or
triangles can still be removed afterward.

### Agisoft Metashape

Metashape likewise separates the jobs masks can perform:

- With **Apply masks to Key points**, each photo's masked pixels are excluded
  from feature detection. With **Apply masks to Tie points**, a region masked
  in one image can suppress corresponding matched 3D points across other
  images. Agisoft presents the latter as useful for turntable/background
  suppression when only a few masks exist.
  [Agisoft background-suppression tutorial](https://agisoft.freshdesk.com/support/solutions/articles/31000158967-aligning-photos-with-background-suppression-from-single-mask)
- Before model building, the user can move, rotate, and resize a 3D bounding
  region; only the scene inside it is reconstructed.
  [Metashape Professional 2.3 manual](https://www.agisoft.com/pdf/metashape-pro_2_3_en.pdf)
- For depth-map mesh generation, **strict volumetric masking** suppresses any
  volume covered by a mask from at least one view. Agisoft warns that each such
  mask is strict, can accidentally remove real surfaces, and should be used
  sparingly.
  [Metashape 2.3 manual](https://www.agisoft.com/pdf/metashape_2_3_en.pdf)

Metashape therefore distinguishes a 2D feature-detection mask, a cross-view
tie-point rule, a strict 3D mesh constraint, and a 3D reconstruction volume.
That distinction matters: the same imperfect mask may be safe for one stage
and destructive in another.

### Polycam

Polycam's official guidance combines capture discipline, an object-oriented
mask option, and post-processing crop:

- Object Mode recommends a contrasting background, 70-75% overlap, whole-object
  outline passes followed by closer rings, photo review before processing, and
  **Object Masking** when the object moves relative to the background.
  [Polycam Object Mode](https://learn.poly.cam/hc/en-us/articles/27425185907348-How-to-Use-Object-Mode)
- Its crop tool uses an adjustable 3D box that can be cubic or cylindrical and
  can remove either the inside or outside. The crop is fully supported for
  photogrammetry meshes. Polycam warns that a cropped Gaussian splat export
  still contains the original uncropped data, which shows that viewer cropping
  and destructive asset cropping are not necessarily the same operation.
  [Polycam crop guide](https://learn.poly.cam/hc/en-us/articles/29647360522516-How-to-Crop-a-Capture-in-Polycam)
- For interiors, Polycam recommends outer, center, and detail passes with
  bridging frames, and explains that the scene must remain static. This solves
  "too little connected coverage" at capture time rather than trying to repair
  it with cropping afterward.
  [Polycam interior capture guide](https://learn.poly.cam/hc/en-us/articles/48339214285844-How-to-Capture-High-Fidelity-Interior-Scans-in-Object-Mode)

Polycam's object mask is most applicable to isolated objects or people. Its
3D crop and capture-path guidance are more applicable to the scanner project's
scene-scale artistic goal.

### COLMAP and OpenMVS

COLMAP and OpenMVS expose why mask timing changes the result:

- COLMAP's `mask_path` and `camera_mask_path` affect **feature extraction**:
  black pixels produce no keypoints. That changes the evidence available to
  matching and sparse camera alignment.
  [COLMAP FAQ: Mask image regions](https://github.com/colmap/colmap/blob/main/doc/faq.rst#mask-image-regions)
- OpenMVS consumes camera poses and a sparse cloud, then performs dense cloud,
  mesh, refinement, and texture stages. Its current densifier exposes a mask
  path and ignored mask label.
  [OpenMVS `DensifyPointCloud.cpp`](https://github.com/cdcseacave/openMVS/blob/develop/apps/DensifyPointCloud/DensifyPointCloud.cpp)
- In an official OpenMVS discussion, the maintainer warned that masks with many
  false negatives removed real tower structure and suggested using such masks
  only for neighbor-view selection while estimating depth over the full image,
  which would require customization. This is direct evidence that a tight
  dense-stage mask can improve scope but also create missing geometry.
  [OpenMVS discussion #1173](https://github.com/cdcseacave/openMVS/discussions/1173#discussioncomment-10749603)

The scanner's decision to leave COLMAP unmasked while masking OpenMVS is thus a
reasonable scene-oriented default. It retains broad alignment evidence but
does not prevent accidental removal when the fixed polygon cuts through the
desired scene during densification.

### Apple RealityKit Object Capture

Apple explicitly separates isolated-object capture from **Area mode**, which is
designed for uneven terrain, surfaces, and objects that cannot be circled. Area
mode skips the object-bounding-box detection step, uses the onscreen reticle
like a brush to paint coverage across surfaces, and provides camera-pose
visualization so the user can check coverage while still on location. Apple
calls the results suitable for 3D environments and artistic projects. This is
the closest official precedent for the scanner project's doorway, building,
and found-scene goal.
[Apple WWDC24: Discover area mode for Object Capture](https://developer.apple.com/videos/play/wwdc2024/10107/)

Apple's Object Capture API accepts a per-image `objectMask`. Black pixels are
ignored, and Apple says masks reduce the number of landmarks it attempts to
match, speed object creation, and improve accuracy when the frame contains
extraneous plants, buildings, or people. This is an object-centric design in
which masking can affect landmark matching, unlike the scanner's current
dense-only mask.
[Apple `PhotogrammetrySample.objectMask`](https://developer.apple.com/documentation/realitykit/photogrammetrysample/objectmask)

Apple's capture samples may also include depth, an oriented world-space
bounding box, camera metadata, gravity, and scan-pass identity. The bounding
box is represented as a transformed unit cube in the capture session's world
coordinate system.
[Apple `PhotogrammetrySample`](https://developer.apple.com/documentation/realitykit/photogrammetrysample)

Apple still emphasizes conventional capture fundamentals: static objects,
many viewpoints, consistent camera settings, soft lighting, and background
masking when needed.
[Capturing photographs for RealityKit Object Capture](https://developer.apple.com/documentation/realitykit/capturing-photographs-for-realitykit-object-capture/)

## Comparison with **Limit Reconstruction Area**

| Capability | Current scanner | Established pattern | Consequence |
| --- | --- | --- | --- |
| Capture guidance | Existing AR tracking, quality filtering, and one keep polygon | Coverage visualization, overlap guidance, capture rings, return-to-capture | The scanner can reject poor frames but cannot yet show where world-space coverage is weak. |
| Mask definition | One polygon drawn before capture and repeated in screen space | Per-frame editing, automatic object masks, or propagated masks | Simple and predictable, but not object tracking. The intended scene must stay inside the same part of the screen. |
| Alignment | Full images; masks unused | Configurable: unmasked, keypoint-masked, or tie-point-masked | Current behavior preserves useful background registration and is well suited to scene scans. It does not help when a moving or dominant background corrupts alignment. |
| Dense reconstruction | OpenMVS ignores black label 0 | Dense/mesh masks plus 3D reconstruction regions | Current behavior can save dense processing and suppress visible background, but anything inside the polygon is still eligible and desired surfaces outside it can disappear. |
| 3D scope | Existing OpenMVS automatic/manual ROI controls are backend settings, not an iPhone sparse-preview editor | User-adjustable post-alignment 3D box | The user cannot yet verify the world-space volume before the expensive run. |
| Texture stage | Original images texture the already-scoped mesh; mask is not passed to `TextureMesh` | Some tools allow masks per alignment, meshing, and texture stage | Usually beneficial for texture detail, but a separate texture-stage exclusion control may be needed for occluders or bad pixels. |
| Cleanup | Downstream Blender workflow | Crop boxes, connected-component selection, triangle/point selection, reversible edits | Blender can finish cleanup, but unwanted geometry may already have consumed reconstruction time. |
| Gaussian output | Current mask path targets OpenMVS mesh reconstruction | Splat viewers often crop or delete primitives separately | The feature does not currently constrain Gaussian training or guarantee a cropped Gaussian export. |

## Strengths of the current addition

- It solves the immediate failure mode at the right expensive stage: background
  can assist alignment without automatically becoming millions of dense points.
- Its white-keep/black-exclude convention matches COLMAP, OpenMVS, and Apple's
  documented black-ignore convention.
- It is deterministic, works without an AI service, preserves source JPEGs,
  and travels inside the scan package.
- Complete-set, dimension, decode, path, and no-overwrite checks avoid silent
  partial masking or corrupted reconstruction inputs.
- The separate capture-to-undistorted mapping is necessary and technically
  stronger than simply reusing capture-space PNGs against different image
  geometry.

## Shortcomings for real-world scene capture

- The polygon is anchored to the screen, not to the doorway, chairs, building,
  or ground in 3D. Reframing or rotating the phone changes what lies inside it.
- One polygon cannot naturally express disconnected subjects plus the ground
  between them, holes, or different scope from different viewpoints.
- There is no representative-frame or sampled-mask review after capture, so a
  boundary mistake may not be noticed until the reconstruction has holes.
- There is no dilation/safety margin. A tight trace risks cutting thin edges,
  especially after viewpoint change and image undistortion.
- A 2D keep window cannot reject distant background visible through a doorway
  or between barriers if that background remains inside the polygon.
- It does not yet provide a user-facing 3D reconstruction region, world-space
  coverage visualization, or a return-to-capture prompt.
- It does not constrain the Gaussian path.

## Recommended next improvements

### 1. Add a post-alignment 3D reconstruction-region review

Adopt the RealityScan Mobile pattern first:

1. run sparse alignment before the expensive dense job;
2. show the registered sparse cloud and cameras on the iPhone or web review
   screen;
3. color coverage/quality where practical;
4. let the user move, rotate, and resize a 3D box or oriented volume;
5. allow returning to capture when the sparse preview reveals a gap; and
6. store that region with the job, apply it before/during dense reconstruction,
   and report its bounds.

This directly handles a doorway, building facade, chairs, portable toilet plus
barriers, or a chosen slice of a room even when the subject moves around the
phone's screen.

For capture guidance, also adopt Apple's Area-mode distinction: Scene mode
should emphasize a reticle/brush, regular capture paths, and live camera-pose or
surface-coverage feedback instead of presenting a centered-object polygon as
the primary scope control.

### 2. Keep dense-only masking as the default scene profile

Preserve the current unmasked COLMAP alignment for outdoor and interior scenes.
Add an explicit advanced profile for object/turntable scans that can also apply
accurate masks to COLMAP feature extraction. Do not reuse the current fixed
screen polygon for sparse masking without per-frame review: removing too much
alignment evidence can split the reconstruction or lose cameras.

### 3. Propagate and review image masks

Upgrade the single fixed polygon into a proposal workflow:

- select the intended region on one or more representative frames;
- propagate it temporally with video segmentation or optical tracking;
- support multiple keep regions and erase regions;
- dilate the keep boundary by a configurable safety margin;
- flag abrupt area or centroid changes; and
- require previews of at least the first, quartile, middle, three-quarter, and
  last frames before submission.

This matches the direction already described in
[`roi_masking_spec.md`](../roi_masking_spec.md) and addresses the OpenMVS
maintainer's false-negative warning.

### 4. Layer 2D masks and 3D scope rather than choosing one

The production scene profile should become **hybrid**:

- full images for robust camera alignment;
- reviewed per-frame masks for foreground/background ambiguity;
- a post-alignment 3D reconstruction region for distance and volume bounds;
- bounded point/triangle budgets before meshing; and
- non-destructive connected-component and crop cleanup before export.

The 3D region removes distant geometry that remains inside a 2D window. Image
masks remove foreground clutter or background that projects into the same 3D
box. Their failure modes are different, so they work better together.

### 5. Give mesh and Gaussian results separate cleanup contracts

For meshes, ensure the final GLB is generated from the scoped mesh and provide
an optional Blender cleanup step for loose components and boundaries. For
Gaussian output, implement explicit primitive selection/cropping and verify
that export removes excluded splats rather than merely hiding them in a viewer.
Record the effective scope separately for mesh and Gaussian outputs.

## Practical guidance until those improvements exist

Use **Limit Reconstruction Area** when the desired scene can remain in a stable
part of the camera view throughout the scan. Draw generously around it rather
than tracing the edge tightly, keep the subject inside the yellow region on
every pass, and maintain normal overlap outside the region because the full
image still supports alignment. For a large facade or room where the desired
area must move around the screen, leave the feature off for the benchmark or
use a broad window and rely on the existing OpenMVS ROI plus Blender cleanup;
the current fixed polygon is not yet a substitute for a world-space crop.

## Primary sources

- [RealityScan Mobile review and reconstruction area](https://dev.epicgames.com/documentation/en-us/realityscan-mobile/RealityScan-Review-Scan)
- [RealityScan reconstruction regions](https://rshelp.capturingreality.com/en-US/tools/reconbox.htm)
- [RealityScan image-layer stage controls](https://rshelp.capturingreality.com/en-US/appbasics/selectedinputs.htm)
- [RealityScan CLI model and mask tools](https://dev.epicgames.com/documentation/en-us/realityscan/all-commands)
- [Agisoft mask effects during alignment](https://agisoft.freshdesk.com/support/solutions/articles/31000158967-aligning-photos-with-background-suppression-from-single-mask)
- [Agisoft Metashape Professional 2.3 manual](https://www.agisoft.com/pdf/metashape-pro_2_3_en.pdf)
- [Agisoft Metashape Standard 2.3 manual](https://www.agisoft.com/pdf/metashape_2_3_en.pdf)
- [Polycam Object Mode](https://learn.poly.cam/hc/en-us/articles/27425185907348-How-to-Use-Object-Mode)
- [Polycam crop guide](https://learn.poly.cam/hc/en-us/articles/29647360522516-How-to-Crop-a-Capture-in-Polycam)
- [Polycam interior capture guide](https://learn.poly.cam/hc/en-us/articles/48339214285844-How-to-Capture-High-Fidelity-Interior-Scans-in-Object-Mode)
- [COLMAP masking documentation](https://github.com/colmap/colmap/blob/main/doc/faq.rst#mask-image-regions)
- [OpenMVS densifier source](https://github.com/cdcseacave/openMVS/blob/develop/apps/DensifyPointCloud/DensifyPointCloud.cpp)
- [OpenMVS maintainer discussion of inaccurate dense masks](https://github.com/cdcseacave/openMVS/discussions/1173#discussioncomment-10749603)
- [Apple RealityKit object masks](https://developer.apple.com/documentation/realitykit/photogrammetrysample/objectmask)
- [Apple Area mode for scene and surface capture](https://developer.apple.com/videos/play/wwdc2024/10107/)
- [Apple Object Capture sample metadata](https://developer.apple.com/documentation/realitykit/photogrammetrysample)
- [Apple Object Capture photography guidance](https://developer.apple.com/documentation/realitykit/capturing-photographs-for-realitykit-object-capture/)
