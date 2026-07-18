# Temporal Mask Generation Contract

Status: replaceable generator boundary and deterministic polygon-keyframe
interpolation implemented. Generated sets remain proposals and cannot resume
reconstruction until sampled review is approved.

The backend detects `metadata/mask_authoring.json` during validation and asks a
`MaskGenerator` implementation to create one mask per ordered capture frame.
The first implementation, `polygon_keyframe_interpolation_v1`, does not claim
to understand image contents. It provides a deterministic baseline while an
optical-flow or video-segmentation generator is evaluated behind the same
interface.

The baseline generator:

- resamples each polygon to a stable bounded point count;
- aligns polygon winding and cyclic starting points;
- linearly interpolates compatible ordered Keep/Erase regions between authored
  frames, covering both temporal directions;
- holds the nearest authored boundary outside the authored interval;
- uses an explicit low-confidence nearest-frame fallback when region topology
  differs; and
- rasterizes a complete 8-bit grayscale PNG set under `masks/proposed`.

It never writes `masks/capture`, never changes `reconstruction_scope`, and never
modifies source JPEGs. Existing proposal directories are replaced under a file
lock through an owned staging directory.

`metadata/mask_generation.json` records:

- schema version and `awaiting_review` state;
- generator identifier and source authoring revision;
- exact output frame count and the five standard review indices; and
- for every frame: frame/image/mask association, confidence, propagation
  method, and contributing authored frame IDs.

Confidence is `1.0` for authored frames, `0.8` for compatible interpolation,
low and distance-sensitive for boundary holds, and `0.25` for topology
fallback. These are conservative provenance signals, not calibrated model
probabilities. The next slice measures mask area and centroid continuity,
applies safety dilation, publishes sampled previews in Jobs, and provides the
approval/promotion endpoint. A draft-bearing job is blocked from resume until
that approval exists.
