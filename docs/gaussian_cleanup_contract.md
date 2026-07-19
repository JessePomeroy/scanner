# Gaussian Cleanup Contract

Gaussian cleanup has two distinct states:

1. the immutable Nerfstudio `splat.ply` master and any non-destructive viewer
   hiding/edit state; and
2. a destructively filtered publication PLY from which SOG, HTML, compressed
   PLY, SPZ, or Gaussian GLB delivery artifacts are generated.

Viewer hiding alone is never publication proof. When a cleanup recipe is used,
the neural backend plan inserts `scripts/cleanup_gaussian_ply.py` after
`ns-export gaussian-splat` and before every `splat-transform` conversion.

## Recipe

The version 1.0 JSON recipe accepts `crop`, `selection`, or both. See
[`examples/gaussian_cleanup_recipe.json`](examples/gaussian_cleanup_recipe.json).

`crop` matches the mesh contract:

- axis-aligned `box` with world-space `center` and full XYZ `size`; or
- world-Z `cylinder` with `center`, `radius`, and full `height`;
- `keep` is `inside` or `outside`.

`selection` identifies source primitive indices before filtering:

```json
{
  "mode": "discard",
  "ranges": [[100, 250], [900, 1000]]
}
```

Ranges are sorted, non-overlapping, half-open `[start, end)` intervals. `keep`
retains only listed indices; `discard` removes listed indices. Crop and
selection combine: a primitive must pass both.

## PLY and evidence guarantees

The filter streams ASCII, binary little-endian, or binary big-endian
vertex-only PLY files with fixed-size scalar properties. It preserves every
retained primitive record byte-for-byte, changing only the vertex count in the
header. List properties, extra non-empty elements, missing/non-finite XYZ,
truncation, trailing payloads, unsafe links, out-of-range selections, empty
results, and unsafe output paths fail closed.

After atomic output publication, the filter rereads the complete cleaned PLY,
verifies the exact retained count and zero crop violations, and writes an
atomic report containing source/retained/removed counts, ratio, normalized
recipe, output SHA-256, and destructive-verification status. If report writing
fails, the new cleaned output is removed. The master `splat.ply` is never
modified.
