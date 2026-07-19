# Local Reconstruction Experiments

This folder is reserved for prototype scripts used while developing the local
COLMAP/OpenMVS reconstruction workflow.

Keep scripts here focused and disposable until the behavior is stable enough to
move into `backend/app`.

Current entry points:

- `inspect_scan.py`: validate typed package metadata and file-reference
  integrity, then print capture counts and compatibility warnings.
- `reconstruct_local.py`: validate and optionally run local COLMAP/OpenMVS.
- `reconstruct_gpu.py`: native Linux/RTX workstation command runner. The
  compatibility setup/check helpers still live under the historical
  `scripts/wsl/` path. The setup helper detects CachyOS/Arch and Ubuntu/Debian;
  its `--dry-run` option previews the package transaction, and it installs the
  Codex CLI alongside the scanner tools so work can continue from Linux. See
  `docs/cachyos_setup.md` for the primary workstation path.
- `plan_neural_backend.py`: dry-run command planner for MASt3R-SLAM, Depth
  Anything, Lingbot-style viewer experiments, and Nerfstudio Gaussian
  splatting. Gaussian plans prefer full-session image keyframes over the
  30-second support video, preserve an editable PLY master, and default to SOG
  plus a standalone HTML viewer.
- `cleanup_gaussian_ply.py`: preserve the master Gaussian PLY while applying a
  strict destructive crop/primitive-index recipe to a publication PLY, then
  stream-verify its retained count and bounds before delivery conversion.
- `benchmark_evidence.py`: verify the frozen input hash, record scanner and
  evidence-tool commits, probe tool versions, wrap named stages with logs,
  elapsed time and peak VRAM sampling, classify daytime/overnight estimates,
  and hash final artifacts for the paired mesh/splat benchmark.
- `plan_object_crop.py`: inspect object-scan tap/radius metadata and print the
  next manual crop command.
- `crop_point_cloud.py`: crop a PLY point cloud by center and radius.
- `blender/prepare_scan_asset.py`: import and prepare mesh assets; optional
  versioned cleanup recipes preserve source objects in the `.blend`, apply
  box/cylinder and loose-component cleanup to copies, verify the retained
  result, and export only those copies to GLB.
- `verify_scan_zip_writer.swift`: compile with the iOS `ScanPackageWriter`
  source to round-trip the custom ZIP writer through Python `zipfile`.
- `verify_capture_mask_mapper.swift`: verify identity, aspect-fill crop, and
  invalid-dimension behavior for preview-to-JPEG mask coordinates.
- `verify_reconstruction_job_client.swift`: exercise the iOS job client through
  mock HTTP and in-memory adapters.
- `verify_scan_upload_client.swift`: verify multipart ZIP construction, shared
  backend URL policy, upload response handling, cancellation cleanup, and UI
  store notices without a live backend.
- `verify_reconstruction_artifact_client.swift`: verify typed manifests,
  encoded download URLs, disk handoff, exact-size checks, unsafe-input
  rejection, cancellation, temporary-file cleanup, and result-store state.
- `verify_ply_point_cloud_loader.swift`: verify bounded ASCII and binary PLY
  parsing, endian/scalar handling, color normalization, sampling, bounds,
  malformed-layout rejection, file mapping, and symlink rejection.
- `verify_reconstruction_scope_client.swift`: verify oriented-box encoding,
  safe scope endpoint construction, missing-scope handling, saves, and stale
  revision errors in the iPhone client.
- `verify_mask_review_client.swift`: verify bounded mask-review evidence,
  quality-count validation, safe endpoints, and approve/reject job responses.

Initialize the official benchmark evidence record before running any Linux
reconstruction command:

```bash
python3 scripts/benchmark_evidence.py init \
  --scan scan_2026_07_15_00_41_09.zip \
  --expected-sha256 ef9a6e0aefa564facf17357252e7fa2bd2cec55882a107461abad5c6459cb779 \
  --scanner-baseline-commit d5f19d9 \
  --report ScannerBenchmarks/run-001/evidence.json
```

See `docs/benchmark_runbook.md` for stage wrapping, artifact finalization, stop
rules, and the Blender comparison record.

Run the ZIP writer verifier from the repo root:

```bash
swiftc ios/ScannerApp/ScanMetadataModels.swift \
  ios/ScannerApp/MetadataWriter.swift \
  ios/ScannerApp/ScanPackageWriter.swift \
  scripts/verify_scan_zip_writer.swift \
  -o /tmp/verify_scan_zip_writer && /tmp/verify_scan_zip_writer
```

Run the capture-mask coordinate verifier on a Mac from the repo root:

```bash
xcrun swiftc \
  ios/ScannerApp/CaptureMaskRasterizer.swift \
  ios/ScannerApp/CaptureMaskCoordinateMapper.swift \
  scripts/verify_capture_mask_mapper.swift \
  -o /tmp/verify_capture_mask_mapper
/tmp/verify_capture_mask_mapper
```

Run the post-capture mask-authoring contract verifier on a Mac from the repo root:

```bash
xcrun swiftc -warnings-as-errors -parse-as-library \
  ios/ScannerApp/CaptureMaskRasterizer.swift \
  scripts/verify_mask_authoring_contract.swift \
  -o /tmp/verify_mask_authoring_contract
/tmp/verify_mask_authoring_contract
```

Run the reconstruction job client verifier from the repo root:

```bash
xcrun swiftc \
  ios/ScannerApp/ReconstructionJobClient.swift \
  ios/ScannerApp/ReconstructionJobStore.swift \
  scripts/verify_reconstruction_job_client.swift \
  -o /tmp/verify_reconstruction_job_client
/tmp/verify_reconstruction_job_client
```

Run the scan upload client verifier from the repo root:

```bash
xcrun swiftc -warnings-as-errors \
  ios/ScannerApp/ReconstructionJobClient.swift \
  ios/ScannerApp/ScanUploadClient.swift \
  ios/ScannerApp/ScanUploadStore.swift \
  scripts/verify_scan_upload_client.swift \
  -o /tmp/verify_scan_upload_client
/tmp/verify_scan_upload_client
```

Run the reconstruction artifact client verifier from the repo root:

```bash
xcrun swiftc -warnings-as-errors \
  ios/ScannerApp/ReconstructionJobClient.swift \
  ios/ScannerApp/ReconstructionArtifactClient.swift \
  ios/ScannerApp/ReconstructionArtifactStore.swift \
  scripts/verify_reconstruction_artifact_client.swift \
  -o /tmp/verify_reconstruction_artifact_client
/tmp/verify_reconstruction_artifact_client
```

Run the PLY point-cloud loader verifier from the repo root:

```bash
xcrun swiftc -warnings-as-errors \
  ios/ScannerApp/PLYPointCloudLoader.swift \
  scripts/verify_ply_point_cloud_loader.swift \
  -o /tmp/verify_ply_point_cloud_loader
/tmp/verify_ply_point_cloud_loader
```

Run the reconstruction scope client verifier from the repo root:

```bash
xcrun swiftc -warnings-as-errors -parse-as-library \
  ios/ScannerApp/ReconstructionJobClient.swift \
  ios/ScannerApp/ReconstructionScopeClient.swift \
  scripts/verify_reconstruction_scope_client.swift \
  -o /tmp/verify_reconstruction_scope_client
/tmp/verify_reconstruction_scope_client
```

Run the mask review client verifier from the repo root:

```bash
xcrun swiftc -warnings-as-errors -parse-as-library \
  ios/ScannerApp/ReconstructionJobClient.swift \
  ios/ScannerApp/MaskReviewClient.swift \
  scripts/verify_mask_review_client.swift \
  -o /tmp/verify_mask_review_client
/tmp/verify_mask_review_client
```

Run the scene coverage tracker verifier from the repo root:

```bash
xcrun swiftc -warnings-as-errors -parse-as-library \
  ios/ScannerApp/SceneCoverageTracker.swift \
  scripts/verify_scene_coverage_tracker.swift \
  -o /tmp/verify_scene_coverage_tracker
/tmp/verify_scene_coverage_tracker
```
