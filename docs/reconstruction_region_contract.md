# Reconstruction Region Contract

Status: backend contract implemented; job checkpoint, persistence API, preview
editor, and OpenMVS application remain future slices.

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

After sparse alignment, publish a bounded point-cloud/camera preview and pause
jobs that request region review. The chosen region can then be stored with a
revision and applied without repeating feature extraction, matching, or sparse
mapping.
