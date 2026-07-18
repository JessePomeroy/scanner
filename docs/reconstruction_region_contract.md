# Reconstruction Region Contract

Status: implemented end to end, including the durable sparse-review checkpoint,
revision-safe API, iPhone editor, native OpenMVS application, output
verification, and resume without repeating sparse alignment.

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

## Reconstruction application

`POST /scans/{scan_id}/resume` claims an `awaiting_scope` checkpoint exactly
once, reuses its sparse model, and continues with dense reconstruction. The
backend converts the local-to-world quaternion above into OpenMVS's
world-to-local OBB matrix, preserves `scene_dense_unscoped.ply`, creates the
scoped dense cloud before meshing, and asks OpenMVS to integrate and crop only
the reviewed ROI. It then streams every scoped dense and mesh vertex through
the same oriented-box test. Any outside vertex fails the job rather than
publishing a silently unscoped result.

The application sidecar at
`metadata/reconstruction_region_application.json` records the exact region
revision, unscoped/scoped point counts, removed count, retained ratio, mesh
vertex count, and zero-outside verification result.
