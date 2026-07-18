# Reconstruction Region Contract

Status: backend contract and durable sparse-review checkpoint implemented;
region persistence API, preview editor, and OpenMVS application remain future
slices.

This contract records a user-reviewed 3D region after COLMAP sparse alignment.
It is deliberately independent of the iPhone UI and OpenMVS file format so the
same validated data can travel through job records, reports, review clients,
and reconstruction runners.

## Version 1.0

```json
{
  "schema_version": "1.0",
  "shape": "oriented_box",
  "coordinate_system": "colmap_reconstruction",
  "center": [1.5, -2.0, 3.25],
  "extents": [8.0, 6.0, 3.0],
  "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
  "source": "user_sparse_preview",
  "revision": 1
}
```

- `center` is the box center in COLMAP reconstruction coordinates.
- `extents` contains the full local-axis edge lengths, not half-extents.
- `orientation_xyzw` is a normalized quaternion rotating the box's local axes
  into COLMAP reconstruction coordinates.
- `source` is one of `user_sparse_preview`, `automatic`, `arkit_alignment`, or
  `imported`.
- `revision` is a positive integer for later optimistic concurrency checks.

The parser rejects missing and unknown fields, unsupported identifiers,
non-finite numbers, non-positive extents, non-unit quaternions, and invalid
revisions. Version 1.0 intentionally supports only an oriented box. Later
shapes require a new schema version or an explicitly compatible extension.

Implementation: [`backend/app/reconstruction_region.py`](../backend/app/reconstruction_region.py)

## Next integration slice

Persist a selected region with optimistic revision checks and add the resume
endpoint. Sparse-review jobs now publish the point-cloud and registered-camera
artifacts, preserve the continuation settings, and remain at the intentional
`awaiting_scope` checkpoint across backend restarts.
