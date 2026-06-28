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
and the planned Windows GPU reconstruction workflow.

## iOS App

Open the app in Xcode:

```bash
open ios/ScannerApp.xcodeproj
```

The current capture path writes accepted `ARFrame.capturedImage` frames to JPEG
files and records matching AR camera metadata. This is enough to produce real
scan zip packages for backend validation. True high-resolution still capture via
`AVCapturePhotoOutput` is the next refinement after the package format is proven
on a physical iPhone.

The app requires a physical ARKit-capable device for scanning. The simulator
build is useful for compile checks, but world tracking is unavailable there.

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

## Local Scripts

Inspect an extracted scan:

```bash
python3 scripts/inspect_scan.py path/to/scan_dir
```

Validate and optionally run reconstruction:

```bash
python3 scripts/reconstruct_local.py scan.zip --work-dir /tmp/scan-work --run-colmap
```

On macOS/Homebrew, COLMAP can run sparse reconstruction without CUDA. Dense
stereo may require a CUDA-capable build and GPU. Use `--dense` only when that
toolchain is available:

```bash
python3 scripts/reconstruct_local.py scan.zip --work-dir /tmp/scan-work --run-colmap --dense
```

## Tests

```bash
python3 -m unittest discover -s tests
```
