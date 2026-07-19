# Mesh Cleanup Contract

The Blender preparation helper supports a versioned, reversible cleanup step
between textured mesh reconstruction and GLB publication. The imported source
objects remain hidden and unchanged in the saved `.blend`; crop, component
filtering, and optional decimation operate on retained copies. A cleanup GLB is
exported from the retained selection only.

## Recipe

Pass a JSON recipe with `schema_version: "1.0"` and a positive integer
`revision`. Increment the revision whenever crop or component intent changes.
Unknown fields and no-op recipes fail instead of being ignored. See
[`examples/mesh_cleanup_recipe.json`](examples/mesh_cleanup_recipe.json).

`crop` is optional and supports:

- `shape`: `box` or `cylinder`;
- `center`: three world-coordinate numbers;
- `keep`: `inside` or `outside`;
- box `size`: full X/Y/Z dimensions; or
- cylinder `radius` and full `height`. Cylinders are aligned to world Z in
  schema 1.0.

`loose_components` is optional and supports:

- `minimum_vertices`: discard connected components below this vertex count;
- `keep_largest`: after the minimum, retain at most this many components.

At least one of `crop` or `loose_components` is required. Crop comparisons are
inclusive at the boundary.

## Publication proof

`--cleanup-report` is required whenever `--cleanup-recipe` is used. It records
the normalized recipe, source and final retained vertex counts, removed count,
retained ratio, per-object component counts, preservation of source objects in
the `.blend`, whether a GLB was exported selection-only, and final verification
status. It also exposes `artifact_type`, `cleanup_revision`, and normalized
`effective_bounds` independently of Gaussian cleanup evidence. The report is
written only after Blender successfully saves the `.blend` and completes any
requested GLB export.

After optional decimation, the helper rechecks every retained vertex against
the crop and verifies the component limits. An empty result or any excluded
vertex fails before saving or publishing. The GLB exporter receives only the
retained object selection, so hidden source geometry is not merely viewer-hidden
inside the published asset.

The original reconstruction files remain outside Blender as the immutable
pipeline source. The saved `.blend` is the reversible working artifact; the
GLB is the destructive retained publication artifact.
