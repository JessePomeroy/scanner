# Local Reconstruction Experiments

This folder is reserved for prototype scripts used while developing the local
COLMAP/OpenMVS reconstruction workflow.

Keep scripts here focused and disposable until the behavior is stable enough to
move into `backend/app`.

Current entry points:

- `inspect_scan.py`: validate a scan package and print capture metadata.
- `reconstruct_local.py`: validate and optionally run local COLMAP/OpenMVS.
- `reconstruct_gpu.py`: WSL2/RTX workstation command runner.
- `plan_neural_backend.py`: dry-run command planner for MASt3R-SLAM, Depth
  Anything, Lingbot-style viewer experiments, and Nerfstudio Gaussian
  splatting.
- `plan_object_crop.py`: inspect object-scan tap/radius metadata and print the
  next manual crop command.
- `crop_point_cloud.py`: crop a PLY point cloud by center and radius.
- `verify_scan_zip_writer.swift`: compile with the iOS `ScanPackageWriter`
  source to round-trip the custom ZIP writer through Python `zipfile`.
- `verify_reconstruction_job_client.swift`: exercise the iOS job client through
  mock HTTP and in-memory adapters.

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
