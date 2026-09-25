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
- `desktop`: Native KDE/Qt reconstruction monitor with guarded texture recovery;
  see [desktop/README.md](desktop/README.md) for local usage.
- `scripts`: Local inspection and reconstruction experiments.
- `docs`: Architecture and capture notes.
- `tests`: Backend unit tests.

See [docs/workflows.md](docs/workflows.md) for the current Mac capture workflow
and native Linux RTX 3070 reconstruction/recovery status. See
[docs/roadmap.md](docs/roadmap.md) for the implementation roadmap. The frozen
paired output experiment is specified in
[docs/benchmark_runbook.md](docs/benchmark_runbook.md).

## iOS App

Open the app in Xcode:

```bash
open ios/ScannerApp.xcodeproj
```

The active capture path qualifies useful live AR frames, then requests a
pose-synchronized JPEG with `ARSession.captureHighResolutionFrame`. If that
request is unavailable or fails its qualification checks, the app packages the
already-qualified live frame and records an explicit fallback reason. It also
records source/resolution provenance, blur scores, motion deltas, movement
speed, rejected-frame counts, and an export summary in
`metadata/session.json`. Scans include an ARFrame-derived `video/scan.mov` and
`metadata/video.json` when the device can encode the live camera stream. The
software path and package audit are implemented; physical-iPhone resolution,
cadence, thermal, pose, and texture-quality validation remain.

The app requires a physical ARKit-capable device for scanning. The simulator
build is useful for compile checks, but world tracking is unavailable there.

Exported ZIP packages appear in the app's `Scans` tab. Use that gallery to
refresh local exports and reopen the share sheet for an existing package without
starting a new scan. Delete a gallery item to remove the ZIP and matching
extracted scan folder from the device. ZIP export streams file contents to disk
so packages with video or many keyframes do not require the full archive to sit
in memory.

Each gallery row also has an upload button. It sends the existing ZIP to the
backend URL configured in the `Jobs` tab for CUDA reconstruction. The
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

Updated API, reconstruction CLI, and desktop workers share one fail-fast heavy-job
admission lock per OS user. A busy initial reconstruction fails with a clear
message; a busy reviewed continuation stays at its checkpoint for manual retry.
There is no automatic queue. Native workers inherit the lock so a surviving
child still excludes competing jobs if its Python supervisor exits. Existing
desktop memory limits remain separate: this guard cannot limit another app's RAM.

The persistent lock is `~/.local/state/scanner/heavy-work.lock`; do not delete it
to clear a busy status. `SCANNER_HEAVY_LOCK` is for tests or explicitly coordinated
deployment. All cooperating launchers must share its inode and user; raw native
commands, other users, and unconfigured containers are not automatically covered.
Docker/host coordination requires an explicitly shared lock and matching UID.

Upload a scan package in validation-only mode:

```bash
curl -F "file=@scan.zip" "http://localhost:8000/scans"
```

Incoming uploads are copied from FastAPI's spooled upload in bounded 1 MiB
chunks. Blocking writes and syncs run off the event loop. The backend fsyncs a
temporary sibling file, atomically publishes the final incoming ZIP without
clobbering late or concurrent paths, and syncs the containing directory on
macOS/Linux. Read failures and request
cancellation remove partial or newly published files and mark the job failed
instead of leaving a truncated package that looks complete. Job-state failure
recording is best-effort and never replaces the original storage error or
cancellation.

Validation-only extraction, package checks, and final placement also run off the
event loop. Unexpected validation failures mark the job failed and preserve its
partial workspace; a failed preservation move does not hide the original error.
Existing processing, completed, or failed workspaces are never replaced. After
the ZIP is stored, validation can finish even if its HTTP caller disconnects.

Run reconstruction mode when COLMAP is installed:

```bash
curl -F "file=@scan.zip" "http://localhost:8000/scans?run_reconstruction=true"
```

To stop after sparse alignment for a future 3D-region review, request the
durable checkpoint explicitly:

```bash
curl -F "file=@scan.zip" \
  "http://localhost:8000/scans?run_reconstruction=true&run_dense=true&run_openmvs=true&review_scope=true"
```

The job remains in the `awaiting_scope` stage and publishes its sparse PLY,
registered-camera JSON, and continuation checkpoint as downloadable artifacts.
The iPhone uploader enables this flow by default. In Jobs, open the sparse PLY,
adjust the cyan box, then tap **Save & Continue**.

Mask use is explicit through `mask_profile`. The iPhone requests
`scene_geometry`: COLMAP feature extraction sees each complete photo for stable
alignment, while reviewed masks constrain COLMAP fusion, OpenMVS
densification, and texture selection. For a pre-masked object or turntable
package, request `mask_profile=object_foreground` to constrain COLMAP features
as well. New authored masks pause the job at `awaiting_masks`; approval starts
foreground-only alignment and the job then pauses at `awaiting_scope` for the
3D region. Every processing report records the effective profile and each
stage that consumed masks.

The Scans gallery labels each package by its recorded capture mode: **Object**
or **Scene**, with **Unknown mode** when the local metadata is unavailable or
invalid. This label is independent of the reconstruction mask profile. Upload
uses foreground-only alignment for Object scans with a saved mask draft;
Scene scans and unmasked Object scans use full-image alignment. If the local
metadata is missing or unsafe, upload retains the alignment-safe scene profile.

The paintbrush beside a ZIP in the iPhone Scans tab opens the post-capture scene
mask editor. It supports multiple green Keep and red Erase areas on five
representative photos and safely rebuilds the ZIP. The backend propagates the
regions through the ordered frames, adds a conservative edge margin, checks
abrupt area/center changes, and publishes five red-exclusion/cyan-boundary
photo overlays. Inspect the evidence in the Jobs tab or with
`GET /scans/SCAN_ID/mask-review`, then record the decision with
`POST /scans/SCAN_ID/mask-review/approve` or
`POST /scans/SCAN_ID/mask-review/reject`. Only approval promotes the complete
proposal set into active reconstruction masks; the backend never silently
treats a draft as applied.

In the iPhone Jobs tab, open the paused job and choose **Review Masks**. Swipe
through all five samples, then approve the full set or reject it for correction.
After approval, use **Set Region** on the sparse point cloud and **Save &
Continue** as usual.

Store the first reviewed region as revision `1`; later edits must advance by
exactly one revision:

```bash
curl -X PUT "http://localhost:8000/scans/SCAN_ID/scope" \
  -H "Content-Type: application/json" \
  --data '{
    "schema_version":"1.0",
    "shape":"oriented_box",
    "coordinate_system":"colmap_reconstruction",
    "center":[0,0,0],
    "extents":[3,2,1],
    "orientation_xyzw":[0,0,0,1],
    "source":"user_sparse_preview",
    "revision":1
  }'
```

Read the current selection with `GET /scans/SCAN_ID/scope`. Conflicting or
skipped revisions return HTTP 409. Resume the saved checkpoint with
`POST /scans/SCAN_ID/resume`. The backend preserves the unscoped dense cloud,
uses the reviewed oriented box for OpenMVS cropping and meshing, and verifies
that both the scoped cloud and mesh lie inside the selected region.

For the dual-boot RTX 3070 PC, boot into CachyOS before reconstruction.
Keep active workspaces on the Linux filesystem rather than an NTFS/shared
Windows partition:

```bash
scripts/wsl/setup_gpu_reconstruction.sh --dry-run
scripts/wsl/setup_gpu_reconstruction.sh
python3 scripts/wsl/check_reconstruction_env.py
# After the pinned COLMAP/OpenMVS and neural environments are installed:
python3 scripts/wsl/check_reconstruction_env.py --strict
python3 scripts/reconstruct_gpu.py scan.zip --output-root ~/ScannerPlans/gpu-preview-001 --dry-run
python3 scripts/reconstruct_gpu.py scan.zip --output-root ~/ScannerOutputs/gpu-run-001
```

Dry runs also prepare a workspace. Keep their output separate from real runs;
the runner refuses to replace an existing scan directory. Use a new output-root
name for each repeat attempt, and keep prior results until you choose to remove
them yourself.

The native CachyOS/RTX 3070 toolchain has passed this install and visibility
gate and completed CUDA COLMAP on the frozen iPhone benchmark. Strict mode does
not execute a representative OpenMVS CUDA workload, so a passing check does not
prove that `DensifyPointCloud` will complete on a real scan.

The default workstation plan remains OpenMVS densification with automatic ROI.
For the explicit recovery path that meshes COLMAP's fused cloud instead, use:

```bash
python3 scripts/reconstruct_gpu.py scan.zip \
  --output-root ~/ScannerOutputs/fused-run-001 \
  --matcher sequential_matcher \
  --openmvs-point-cloud-source colmap_fused \
  --scope-mode unbounded
```

The backend, local runner, and GPU runner automatically handle mixed capture
resolutions: they decode images, extract features in one batch per resolution,
and share a camera within each batch. Image names, mask paths, and sequential
matching order remain unchanged. The GPU option retains the name
`--camera-sharing single`, but mixed inputs report the effective policy as
`per_resolution`. A dry run records replayable image lists without running
COLMAP. Real runs verify that every input reached the database before matching,
then record registration counts in `metadata/colmap_intake.json`; missing
registered views are flagged for review, not silently treated as full coverage.

Explicit `--camera-sharing per-folder` remains a legacy alternative that moves
copied images into dimension subfolders. Only this path rejects capture masks
and `--use-masks`, because mask-path remapping is not implemented. Its derived
workspace retains original paths in capture metadata and is not an exportable
scan package. Neither mode modifies the original ZIP.

`InterfaceCOLMAP` imports COLMAP's `dense/fused.ply` as the view-aware
`dense/scene.ply`; this option skips `DensifyPointCloud` and passes `scene.ply`
to `ReconstructMesh`. It intentionally fails closed if automatic ROI, masks, or
a reviewed reconstruction region would be required, because those paths are
not yet supported with this source.

The helper directory retains its historical `scripts/wsl/` name for
compatibility, but native CachyOS is now the primary target. The setup script
auto-detects CachyOS/Arch versus Ubuntu/Debian; see
[`docs/cachyos_setup.md`](docs/cachyos_setup.md) before running it. When the PC
is booted into Windows, the future cloud worker is offline and jobs remain
safely queued until Linux starts again.

Strict mode checks the complete paired benchmark tool inventory: RTX visibility,
the CUDA toolkit and CUDA-capable PyTorch, CUDA-enabled COLMAP, the OpenMVS
command suite, Blender, Nerfstudio, Node.js 22 or newer, Codex, and
SplatTransform. It also requires the repository-local Nerfstudio/COLMAP
compatibility probe for the two renamed GPU flags. Open3D remains optional. It
is an installation/visibility gate, not an end-to-end reconstruction or
OpenMVS CUDA runtime test.

A July benchmark recovery manually reused the `InterfaceCOLMAP` cloud after an
OpenMVS densification failure and produced a textured OBJ with about 3.65
million vertices, 7.30 million faces, and two 8192-pixel texture atlases. That
proves the recovered mesh/texturing route, but an automated rerun, a reviewed
GLB, and the paired Gaussian output are still outstanding.

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

For a textured backend result, download `textured_bundle` (`textured_mesh.zip`)
instead of sharing the OBJ alone. The ZIP contains the OBJ, its referenced MTL
and texture images, and available `texture_quality.json` evidence. It preserves
relative material paths and excludes unrelated files. The manifest also exposes
available texture-quality and COLMAP-intake reports separately. Packaging is not
a Blender conversion or visual approval; the desktop `.blend` step is separate.

## Local Scripts

Inspect an extracted scan:

```bash
python3 scripts/inspect_scan.py path/to/scan_dir
```

Validate and optionally run reconstruction:

```bash
python3 scripts/reconstruct_local.py scan.zip --work-dir ~/ScannerOutputs/local-sparse-001 --run-colmap
```

Reconstruction requires `--work-dir` so results survive script exit. Choose a
new directory and do not create it beforehand: preparation claims it
exclusively and will not replace existing work. Validation without reconstruction
can omit `--work-dir` and use temporary storage.

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
python3 scripts/reconstruct_local.py scan.zip --work-dir ~/ScannerOutputs/local-exhaustive-001 --run-colmap --matcher exhaustive_matcher
```

On macOS/Homebrew, COLMAP can run sparse reconstruction without CUDA. Dense
stereo may require a CUDA-capable build and GPU. Use `--dense` only when that
toolchain is available:

```bash
python3 scripts/reconstruct_local.py scan.zip --work-dir ~/ScannerOutputs/local-dense-001 --run-colmap --dense
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
