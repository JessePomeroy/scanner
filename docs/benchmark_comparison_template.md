# Blender Benchmark Comparison Record

Copy this file into the benchmark run's `comparison/` folder and fill it in
without changing the agreed gates or rating categories.

## Provenance

- Run ID:
- Input SHA-256:
- Scanner baseline commit:
- Evidence-tool commit:
- NVIDIA driver / GPU:
- COLMAP version:
- OpenMVS version:
- Blender version:
- Nerfstudio / gsplat version:
- Node.js / SplatTransform version:

## Required Artifacts

| Result | Canonical artifact | SHA-256 | Bytes | Loads successfully? |
| --- | --- | --- | ---: | --- |
| Textured mesh | `scene.glb` |  |  |  |
| Gaussian | `scene.sog` |  |  |  |

Supporting artifacts:

- Mesh OBJ/MTL/textures:
- Mesh `.blend`:
- Gaussian `splat.ply` master:
- Gaussian standalone HTML:
- Evidence JSON:
- Logs directory:

## Performance

| Measurement | Textured mesh | Gaussian |
| --- | ---: | ---: |
| Estimated total time |  |  |
| Estimate confidence |  |  |
| Actual total time |  |  |
| Peak VRAM MiB |  |  |
| Canonical artifact bytes |  |  |
| Blender preparation time |  |  |
| Blender memory use |  |  |
| Viewport/playback notes |  |  |
| Unattended completion? |  |  |

Attach the per-stage table exported from `evidence.json` rather than manually
retyping timings.

## Preparation Audit

Allowed for each result: import, correct orientation/scale, crop obvious stray
fragments, and configure the intended material or Gaussian viewer.

Not allowed: remodeling, retopology, hole repair, texture repainting, or edits
that conceal reconstruction failures.

- Mesh preparation actions and minutes:
- Gaussian preparation actions and minutes:
- Any disallowed intervention attempted or needed:

## Fixed Camera Evidence

Use the same perceived scene framing and motion envelope for both results. Save
the camera transforms, stills, and any short renders.

1. Orbit near the captured path
   - Mesh output:
   - Gaussian output:
   - Notes:
2. Low-to-high move
   - Mesh output:
   - Gaussian output:
   - Notes:
3. Gentle push toward or pull away from the focal scene
   - Mesh output:
   - Gaussian output:
   - Notes:

## Quality Ratings

Use whole-number ratings from 1 (unusable) to 5 (excellent).

| Category | Mesh rating | Mesh notes | Gaussian rating | Gaussian notes |
| --- | ---: | --- | ---: | --- |
| Photographic realism |  |  |  |  |
| Completeness and view stability |  |  |  |  |
| Blender usability and artistic freedom |  |  |  |  |
| Workflow efficiency |  |  |  |  |

Explicitly note holes, melted geometry, smeared textures, floaters, ghosting,
view-dependent shape changes, foliage/wire failures, and sun-facing failures.
If both outputs fail in the same difficult region, mark it as a possible
capture/subject confound.

## Hard Gates

| Gate | Textured mesh | Gaussian |
| --- | --- | --- |
| Correct canonical GLB/SOG artifact | Pass / Fail | Pass / Fail |
| Loads and renders in intended Blender workflow | Pass / Fail | Pass / Fail |
| Recognizable through fixed cameras | Pass / Fail | Pass / Fail |
| Preparation is 15 minutes or less | Pass / Fail | Pass / Fail |
| Completes within RTX 3070 8 GB VRAM | Pass / Fail | Pass / Fail |
| Completes within 12 hours | Pass / Fail | Pass / Fail |
| Canonical artifact is 2 GB or less | Pass / Fail | Pass / Fail |
| Preferred artifact target of 1 GB or less | Pass / Fail | Pass / Fail |

## Decision Evidence

- Mesh strengths:
- Mesh weaknesses:
- Gaussian strengths:
- Gaussian weaknesses:
- Are the strengths complementary?
- Which candidates pass every hard gate?
- Provisional recommendation: mesh / Gaussian / both / select by scan type
- Rationale:
- Additional evidence needed before the final strategy decision:
