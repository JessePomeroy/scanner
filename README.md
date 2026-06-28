# Polycam-Style Scanner

Foundation for an iOS capture app and Python reconstruction backend.

The first working target is:

1. Capture images and AR metadata on iPhone.
2. Export a structured scan package zip.
3. Upload or inspect that package locally.
4. Run COLMAP/OpenMVS reconstruction when the native tools are installed.

## Layout

- `ios/ScannerApp`: Swift/SwiftUI capture app source.
- `backend/app`: FastAPI backend, scan validation, job status, and command runners.
- `scripts`: Local inspection and reconstruction experiments.
- `docs`: Architecture and capture notes.
- `tests`: Backend unit tests.

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

## Tests

```bash
python3 -m unittest discover -s tests
```
