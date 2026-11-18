# Experiments

Code and notes that are not part of the everyday flow (scan an object, draw one
box, get a Blender-ready model). Nothing here is imported by the backend the
phone talks to. It is kept, with its tests, because the work was real and may be
picked up again.

| What | Where | State |
| --- | --- | --- |
| Command planners for COLMAP/OpenMVS, Meshroom and AliceVision | `scanner_lab/reconstruction_backends.py`, `scripts/plan_reconstruction_backend.py` | Dry-run planning only for Meshroom and AliceVision |
| Neural back-end planners (Gaussian splatting, MASt3R-SLAM, Depth Anything, Lingbot) | `scanner_lab/neural_backend_planner.py`, `scripts/plan_neural_backend.py`, `docs/neural_backends.md` | Plans only; the August Gaussian benchmark was never completed |
| Gaussian PLY cleanup | `scanner_lab/gaussian_cleanup.py`, `scripts/cleanup_gaussian_ply.py`, `docs/gaussian_cleanup_contract.md` | Implemented and tested, not calibrated on a real splat |
| Benchmark evidence records | `scanner_lab/benchmark_evidence.py`, `scripts/benchmark_evidence.py`, `docs/benchmark_runbook.md` | Used for the August paired benchmark |
| Point-cloud downsampling and cleanup | `scanner_lab/point_cloud_processor.py`, `scripts/process_point_cloud.py` | Needs Open3D or ThreeCrate installed |
| Research notes | `docs/research/` | Reference |

Run a script from the repository root, for example:

```bash
python3 experiments/scripts/plan_neural_backend.py scan.zip --backend gaussian_splatting
```

The scripts add `backend/` and `experiments/` to the import path themselves.
Their tests live in `tests/test_experiments.py` and run with the main suite.
