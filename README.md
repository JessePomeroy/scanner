# Polycam-Style Scanner

Foundation for an iOS capture app and Python reconstruction backend.

The first working target is:

1. Capture images and AR metadata on iPhone.
2. Export a structured scan package zip.
3. Upload or inspect that package locally.
4. Run COLMAP/OpenMVS reconstruction when the native tools are installed.

## Layout

- `ios/ScannerApp`: Swift/SwiftUI capture app source.
- `ios/ScannerApp.xcodeproj`: Xcode project for building the iOS app.
- `backend/app`: FastAPI backend, scan validation, job status, and command runners.
- `scripts`: Local inspection and reconstruction experiments.
- `docs`: Architecture and capture notes.
- `tests`: Backend unit tests.

See [docs/workflows.md](docs/workflows.md) for the current Mac capture workflow
and the planned Windows GPU reconstruction workflow. See
[docs/roadmap.md](docs/roadmap.md) for the implementation roadmap.

## iOS App

Open the app in Xcode:

```bash
open ios/ScannerApp.xcodeproj
```

The current capture path writes accepted `ARFrame.capturedImage` frames to JPEG
files and records matching AR camera metadata. It now records blur scores,
motion deltas, movement speed, rejected-frame counts, and an export summary in
`metadata/session.json`. Scans also include an ARFrame-derived `video/scan.mov`
and `metadata/video.json` when the device can encode the live camera stream.
True high-resolution still capture via `AVCapturePhotoOutput` remains a later
refinement after the package format is proven on a physical iPhone.

The app requires a physical ARKit-capable device for scanning. The simulator
build is useful for compile checks, but world tracking is unavailable there.

Exported ZIP packages appear in the app's `Scans` tab. Use that gallery to
refresh local exports and reopen the share sheet for an existing package without
starting a new scan. Delete a gallery item to remove the ZIP and matching
extracted scan folder from the device. ZIP export streams file contents to disk
so packages with video or many keyframes do not require the full archive to sit
in memory.

## Backend

Create an environment and install dependencies:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Upload a scan package in validation-only mode:

```bash
curl -F "file=@scan.zip" "http://localhost:8000/scans"
```

Run reconstruction mode when COLMAP is installed:

```bash
curl -F "file=@scan.zip" "http://localhost:8000/scans?run_reconstruction=true"
```

For the Windows/WSL2 GPU workstation path:

```bash
scripts/wsl/setup_gpu_reconstruction.sh
python3 scripts/wsl/check_reconstruction_env.py --strict
python3 scripts/reconstruct_gpu.py scan.zip --output-root /mnt/c/Users/YOU/ScannerOutputs --dry-run
python3 scripts/reconstruct_gpu.py scan.zip --output-root /mnt/c/Users/YOU/ScannerOutputs
```

Check job status:

```bash
curl "http://localhost:8000/scans/<scan_id>"
```

Job responses include the lifecycle `stage`, a human-readable `message`, and
UTC `created_at`, `updated_at`, `started_at`, and `finished_at` timestamps.
Active reconstruction jobs move through `queued`, `validating`,
`reconstructing`, optional `meshing`, and `exporting` stages before finishing.
Job records are replaced atomically so a failed status update leaves the last
valid JSON record readable.

The local backend uses in-process background tasks and should run as one process
per scans directory. After a backend restart, unfinished records are marked
failed rather than silently appearing active or attempting an unsafe automatic
resume; their uploaded ZIP files remain available for inspection.

List recent jobs:

```bash
curl "http://localhost:8000/scans?limit=20"
```

## Local Scripts

Inspect an extracted scan:

```bash
python3 scripts/inspect_scan.py path/to/scan_dir
```

Validate and optionally run reconstruction:

```bash
python3 scripts/reconstruct_local.py scan.zip --work-dir /tmp/scan-work --run-colmap
```

Validation writes `metadata/scan_report.json` with capture-quality diagnostics.
After COLMAP/OpenMVS stages run, the same report is refreshed with any sparse or
dense output counts that can be detected.
Local COLMAP smoke tests default to `sequential_matcher`, which is much faster
for ordered iPhone scans. Use exhaustive matching only when you want a slower
quality check:

```bash
python3 scripts/reconstruct_local.py scan.zip --work-dir /tmp/scan-work --run-colmap --matcher exhaustive_matcher
```

On macOS/Homebrew, COLMAP can run sparse reconstruction without CUDA. Dense
stereo may require a CUDA-capable build and GPU. Use `--dense` only when that
toolchain is available:

```bash
python3 scripts/reconstruct_local.py scan.zip --work-dir /tmp/scan-work --run-colmap --dense
```

For object scans, inspect the crop metadata and get the manual crop command:

```bash
python3 scripts/plan_object_crop.py scan.zip
python3 scripts/crop_point_cloud.py input.ply object_cropped.ply --center X Y Z --radius 1.5
```

## Tests

```bash
python3 -m unittest discover -s tests
```
