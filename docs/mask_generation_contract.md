# Temporal Mask Generation Contract

Status: replaceable generator boundary, deterministic polygon-keyframe
interpolation, conservative safety processing, quality gates, and backend
approval/promotion implemented. Generated sets remain proposals until a person
reviews the samples and approves them.

The backend detects `metadata/mask_authoring.json` during validation and asks a
`MaskGenerator` implementation to create one mask per ordered capture frame.
The first implementation, `polygon_keyframe_interpolation_v1`, does not claim
to understand image contents. It provides deterministic polygon interpolation,
not semantic subject tracking.

The baseline generator:

- resamples each polygon to a stable bounded point count;
- aligns polygon winding and cyclic starting points;
- linearly interpolates compatible ordered Keep/Erase regions between authored
  frames, covering both temporal directions;
- holds the nearest authored boundary outside the authored interval;
- uses an explicit low-confidence nearest-frame fallback when region topology
  differs; and
- rasterizes a complete 8-bit grayscale PNG set under `masks/proposed`; and
- dilates the final Keep result by 1% of the short image edge, bounded to 2-32
  pixels, so small tracking errors retain extra scene context instead of
  prematurely cutting it away.

It never writes `masks/capture`, never changes `reconstruction_scope`, and never
modifies source JPEGs. Existing proposal directories are replaced under a file
lock through an owned staging directory.

`metadata/mask_generation.json` records:

- schema version and `awaiting_review`, `needs_correction`, `approved`, or
  `rejected` state;
- generator identifier and source authoring revision;
- exact output frame count and up to five selected review indices; and
- for every frame: frame/image/mask association, confidence, propagation
  method, contributing authored frame IDs, retained-area fraction, normalized
  centroid, and safety-dilation radius.

The backend measures every proposed mask. An empty mask, a greater-than-75%
adjacent area jump, or a greater-than-0.18 normalized centroid jump makes the
set `needs_correction` and blocks approval. Very small regions and low generator
confidence remain visible warnings that a person can judge from the samples.

Review selection happens after all proposed masks have been measured. Captures
with five frames or fewer show every frame. Longer captures show five unique
frames, sorted in capture order:

- Reserve one slot for the lowest-confidence generated frame when confidence
  is below `0.5` (prefer farther from authored frames on a tie).
- Reserve up to one further slot for an empty mask or the greatest adjacent
  area/centroid change. Changes are scaled by the existing quality thresholds.
  A blocking change may select an authored endpoint; otherwise prefer a
  generated frame.
- Fill remaining slots with generated frames farthest in frame-index distance
  from authored/already selected frames. If too few generated frames exist,
  fill from authored frames spread across the remaining interval. Ties choose
  the earlier frame, making the result deterministic.

This checks propagation between authored keyframes rather than repeating the
authoring quartiles. The bounded sample cannot show every risk or guarantee
subject alignment; the full-frame quality checks and explicit approval still
apply. Risk slots can reduce temporal coverage when several problems cluster.

Only selected frames are rendered under `masks/review`: retained pixels show
the original photo, excluded pixels are dark red, and a cyan line marks the
boundary. Ordered `review_indices` and `review_masks` refer to the same frames;
all proposed masks remain present regardless of the five-preview limit.

Confidence is `1.0` for authored frames, `0.8` for compatible interpolation,
low and distance-sensitive for boundary holds, and `0.25` for topology
fallback. These are conservative provenance signals, not calibrated model
probabilities. `GET /scans/{scan_id}/mask-review` returns the evidence.
`POST /scans/{scan_id}/mask-review/approve` revalidates the exact full set and
atomically promotes it to `masks/capture` while activating
`reconstruction_scope`. Reject records the decision without deleting evidence.
A draft-bearing job is blocked from resume until approval succeeds. Jobs now
shows one required **Review Masks** step with the selected photo overlays, quality
messages, confirmation before approving the full set, and a reject-and-correct
path. Approval returns the user to the existing 3D-region step.
