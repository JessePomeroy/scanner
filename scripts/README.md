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
  `scripts/wsl/` path.
- `plan_neural_backend.py`: dry-run command planner for MASt3R-SLAM, Depth
  Anything, Lingbot-style viewer experiments, and Nerfstudio Gaussian
  splatting. Gaussian plans prefer full-session image keyframes over the
  30-second support video, preserve an editable PLY master, and default to SOG
  plus a standalone HTML viewer.
- `plan_object_crop.py`: inspect object-scan tap/radius metadata and print the
  next manual crop command.
- `crop_point_cloud.py`: crop a PLY point cloud by center and radius.
- `verify_scan_zip_writer.swift`: compile with the iOS `ScanPackageWriter`
  source to round-trip the custom ZIP writer through Python `zipfile`.
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

Run the ZIP writer verifier from the repo root:

```bash
swiftc ios/ScannerApp/ScanMetadataModels.swift \
  ios/ScannerApp/MetadataWriter.swift \
  ios/ScannerApp/ScanPackageWriter.swift \
  scripts/verify_scan_zip_writer.swift \
  -o /tmp/verify_scan_zip_writer && /tmp/verify_scan_zip_writer
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
