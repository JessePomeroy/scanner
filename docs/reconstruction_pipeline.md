# Reconstruction Pipeline

The backend currently supports the command sequence needed for local
COLMAP/OpenMVS reconstruction:

1. `colmap feature_extractor`
2. `colmap exhaustive_matcher` or `colmap sequential_matcher`
3. `colmap mapper`
4. `colmap image_undistorter`
5. `colmap patch_match_stereo`
6. `colmap stereo_fusion`
7. Optional OpenMVS mesh and texturing commands

Install COLMAP/OpenMVS locally before using reconstruction mode. Validation mode
does not require those native tools.

The Homebrew COLMAP build on macOS can run sparse reconstruction, but dense
stereo currently requires CUDA. For local Mac testing, run sparse reconstruction
first and export `sparse/sparse_points.ply`. Dense reconstruction should run on a
CUDA-capable Linux workstation or cloud worker.

For phone scans, `sequential_matcher` is the default local smoke-test matcher
because frames are captured in temporal order. Keep `exhaustive_matcher`
available for slower quality checks and difficult scans.

## Backend Planning

Use `scripts/plan_reconstruction_backend.py` to validate a scan package and
write a JSON command plan without executing a reconstruction backend:

```bash
python3 scripts/plan_reconstruction_backend.py scan.zip --backend colmap_openmvs
python3 scripts/plan_reconstruction_backend.py scan.zip --backend meshroom
python3 scripts/plan_reconstruction_backend.py scan.zip --backend alicevision
```

By default, reports and extracted scan files are written to
`ScannerPlans/<scan_id>/<backend>/`, which is ignored by Git. Use `--work-dir`
or `--report` when you want a specific location.

Supported planners:

- `colmap_openmvs`: the primary production path for Blender-ready OBJ output.
- `meshroom`: an alternate photogrammetry path through `meshroom_batch`.
- `alicevision`: an experimental direct AliceVision command chain for future
  workstation tuning.

The Meshroom planner is the safer AliceVision route because Meshroom owns the
pipeline graph and command wiring. The direct AliceVision plan is useful for
inspection and later optimization, but command names and options may need
adjustment for the exact installed AliceVision release.
