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

Each gallery row also has an upload button. It sends the existing ZIP to the
backend URL configured in the `Jobs` tab for validation-only processing. The
client streams the ZIP into a temporary multipart body off the main UI thread,
then URLSession uploads that file without loading the archive into one `Data`
value. The temporary body is removed after success, failure, or cancellation;
an abandoned body from a terminated process is removed when the upload client
next starts. The original gallery ZIP is never modified. Upload results link
the user back to the `Jobs` tab for lifecycle details.

The `Jobs` tab reads recent reconstruction jobs from a configurable backend URL
and shows status, lifecycle stage, message, capture counts, and update time. The
URL is saved on device. `http://localhost:8000` works when the backend is local
to the simulator; on iPhone, enter the Mac or PC LAN URL instead.

Terminal jobs with published outputs open a typed result list. The app downloads
one result at a time directly to an app-owned temporary file, verifies the exact
byte count declared by the backend, and presents the file through the iOS share
sheet. Closing the sheet, leaving the result screen, or cancelling removes that
temporary copy. A later launch also removes result files abandoned by a
terminated process.

PLY result rows also offer an in-app point-cloud preview. The loader memory-maps
the owned temporary file, validates ASCII or little/big-endian binary vertex
records, and deterministically samples at most 120,000 points for SceneKit.
Vertex colors are preserved when present. The preview supports orbit, zoom, pan,
and adjustable point size without changing the downloaded reconstruction.

## Backend

Create an environment and install dependencies:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

To view job status from an iPhone on the same trusted LAN, bind the backend to
the workstation network interface:

```bash
uvicorn app.main:app --reload --host 0.0.0.0
```

Then enter `http://<workstation-lan-ip>:8000` in the app's `Jobs` tab. The
backend currently has no authentication, so do not expose this listener to the
public internet or an untrusted network.

Upload a scan package in validation-only mode:

```bash
curl -F "file=@scan.zip" "http://localhost:8000/scans"
```

Incoming uploads are copied from FastAPI's spooled upload in bounded 1 MiB
chunks. Blocking writes and syncs run off the event loop. The backend fsyncs a
temporary sibling file, atomically publishes the final incoming ZIP without
clobbering late or concurrent paths, and syncs the containing directory on
macOS/Linux/WSL. Read failures and request
cancellation remove partial or newly published files and mark the job failed
instead of leaving a truncated package that looks complete. Job-state failure
recording is best-effort and never replaces the original storage error or
cancellation.

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
failed and partial workspaces are preserved under `scans/failed/` rather than
silently appearing active or attempting an unsafe automatic resume. If a valid
workspace had already reached `scans/completed/`, its terminal record and
download path are restored. Uploaded ZIP files also remain available for
inspection.

List recent jobs:

```bash
curl "http://localhost:8000/scans?limit=20"
```

List the currently downloadable single-file outputs for a job:

```bash
curl "http://localhost:8000/scans/<scan_id>/artifacts"
```

Each artifact has a stable result name, package-relative path, filename, byte
count, and media type. Download the returned relative path through
`/scans/<scan_id>/files/<relative_path>`. The backend resolves both persisted
output declarations and requested paths inside its completed/failed scan roots,
rejects traversal, symlinks, and multi-link files, serves only
manifest-published outputs, and never exposes raw server paths as download
instructions. File responses stream from the already-authorized no-follow file
descriptor rather than reopening a validated pathname.

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
It also validates the typed frame/session/video metadata contract, exact flat
image/video references, unique frame and video identities, increasing frame
timestamps, optional session file counts, and video metadata values before
reconstruction starts. Package root discovery ignores symbolic links;
package-owned metadata and capture directories are flat, and none of their
entries can redirect through symbolic links. Supported-file counts stay
consistent across validation, manifests, and planners. Older packages that
contain video files but predate
`video.json` remain readable and receive a visible `video_metadata_missing`
integrity warning.
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
