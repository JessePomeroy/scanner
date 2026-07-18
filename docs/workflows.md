# Workflows

## Local Mac Workflow

Use the MacBook for capture iteration and package validation:

1. Build and run the iPhone app from Xcode.
2. Choose `Object` or `Scene` mode.
3. For object scans, start scanning and tap the subject once tracking is stable.
4. Export the scan zip from the iPhone.
5. Validate and run sparse COLMAP locally:

```bash
python3 scripts/reconstruct_local.py scan.zip --work-dir /tmp/scanner-test --run-colmap
```

This produces `sparse/sparse_points.ply`. The Homebrew COLMAP build on macOS is
useful for sparse smoke tests, but not for dense CUDA reconstruction.
The script also writes `metadata/scan_report.json` with capture-quality warnings.
The default local matcher is `sequential_matcher`; use
`--matcher exhaustive_matcher` only when you want a slower quality check.

To inspect a backend command plan without running native reconstruction tools:

```bash
python3 scripts/plan_reconstruction_backend.py scan.zip \
  --backend colmap_openmvs
```

When `--work-dir` is omitted, the planner writes a persistent workspace under
`ScannerPlans/<scan_id>/<backend>/`. This folder is ignored by Git because it
contains extracted scan files and generated reports.

Alternate dry-run planners are available for Meshroom/AliceVision research:

```bash
python3 scripts/plan_reconstruction_backend.py scan.zip \
  --backend meshroom

python3 scripts/plan_reconstruction_backend.py scan.zip \
  --backend alicevision
```

The Meshroom planner uses `meshroom_batch`, which is the preferred AliceVision
entry point until the exact native Linux install is verified. The direct
AliceVision planner is experimental and may need command option tuning for the
installed AliceVision release.

## Video Package Export

Scan packages include a `video/` folder and optional `metadata/video.json`
file. The iPhone app records `video/scan.mov` from the live ARFrame camera
stream while a scan is active, then writes one metadata entry when the recording
finishes successfully. The initial recorder caps video at 30 seconds to keep
on-device ZIP export memory use manageable; keyframe image capture continues
after the video cap is reached.

Video metadata entries look like:

```json
[
  {
    "path": "video/scan.mov",
    "captured_at": "2026-07-07T00:00:00Z",
    "duration_seconds": 12.5,
    "frame_rate": 30,
    "resolution": [1920, 1080],
    "codec": "h264",
    "includes_audio": false
  }
]
```

The video path is optional for COLMAP/OpenMVS, but it will be useful for
MASt3R-SLAM, Lingbot-style point-cloud workflows, Gaussian splatting, and other
video-oriented neural reconstruction experiments. Treat this video as a
neural/viewer support artifact. The photogrammetry mesh path should still prefer
high-quality keyframe images and, later, high-resolution still capture.

## Package Integrity Validation

Desktop validation parses `frames.json`, `session.json`, and optional
`video.json` into typed records before reconstruction planning. Each frame must
have a unique non-negative ID, a finite timestamp, a positive two-dimensional
resolution, and one unique direct file reference inside the flat `images/`
directory. Frame timestamps must increase in metadata order, and every
supported image file must have a matching frame entry.

When `video.json` is present, each entry must reference one unique supported
direct file inside the flat `video/` directory. Capture time must include a UTC
offset; optional duration, frame rate, resolution, and codec fields are
validated when present. The audio flag is required. If `session.json` declares
`image_count` or `video_count`, the declared value must match the package files.

The package root owns `images/`, `metadata/`, and optional `video/`, `depth/`,
`arkit/`, and `preview/` directories. Those directories and every entry inside
them must be regular, package-local files rather than symbolic links. Metadata
and capture directories are flat and cannot contain nested directories. This
single inventory rule also covers dynamically named planner reports, keeping
validation, manifests, reports, and downstream planners on the same files
inside the scan package.

Archive root discovery also ignores child-directory symbolic links before a
scan root is selected. Regular unsupported files such as Finder metadata or
notes may remain in a flat capture directory, but they are not counted as
capture images by validation, manifests, or neural planners.

Legacy packages with video files but no `video.json` are accepted so old user
data remains inspectable. Their `scan_report.json` includes
`package_integrity.warnings: ["video_metadata_missing"]`, and the same code is
also included in the top-level warnings list. A present but incomplete
`video.json` is rejected because it otherwise makes the reconstruction input
ambiguous.

To plan neural backend experiments without installing model dependencies:

```bash
python3 scripts/plan_neural_backend.py scan.zip --backend mast3r_slam
python3 scripts/plan_neural_backend.py scan.zip --backend depth_anything
python3 scripts/plan_neural_backend.py scan.zip --backend lingbot
python3 scripts/plan_neural_backend.py scan.zip --backend gaussian_splatting
```

For Gaussian scene training, the planner deliberately chooses exported image
keyframes before the optional video. The iPhone video is capped at 30 seconds,
but keyframes cover the full scan. The default Gaussian delivery plan preserves
an editable PLY master and produces SOG plus a standalone HTML viewer.

See `docs/neural_backends.md` for backend-specific notes and license cautions.

## High-Resolution Photo Scaffold

`CameraCaptureManager` contains reusable `AVCapturePhotoOutput` plumbing for
capturing a high-resolution still directly to a scan package path. It writes the
photo file, reports pixel dimensions, and extracts basic exposure/ISO metadata.

The active scanner still uses ARFrame JPEG capture by default. Switching
accepted keyframes over to high-resolution stills needs phone testing because
the ARKit pose timestamp and AVCapture photo timestamp must be synchronized
carefully.

## Export Summary

After a scan is stopped and zipped, the iPhone UI shows a compact export
summary with the scan ID, ZIP file name, mode, accepted/rejected frame counts,
average and minimum blur scores, maximum movement speed, capture duration, and
object subject/radius status when available.

Use this summary as the quick on-device sanity check before sharing the ZIP to
the Mac or Linux RTX workstation. The same values are also written into
`metadata/session.json` so desktop validation can compare the exported package
against what the phone showed.

## Scan Gallery

The iPhone app has a `Scans` tab that lists exported `.zip` packages from the
local `Scans/` documents folder. Pull to refresh or use the refresh button after
exporting, then tap the share icon on a package row to open the share sheet.

Swipe a scan row or use `Edit` to delete a package. Deleting from the gallery
removes both the exported `.zip` and the matching extracted scan folder from the
device.

Tap the cloud-upload button on a row to send that existing ZIP to the backend
URL configured in the `Jobs` tab. The upload requests CUDA COLMAP dense
reconstruction and OpenMVS meshing, then decodes the queued job returned by the
backend. A queued or failed result is shown immediately, and the `Jobs` tab is
the ongoing status/history surface. Only one gallery upload runs at a time, and
the uploading ZIP cannot be deleted until the request finishes.

The iOS client constructs multipart form data in a temporary file by copying
the ZIP in 1 MiB chunks on a background task. URLSession uploads from that file;
the multipart copy is removed after success, HTTP/decoding failure, or
cancellation. Any body left by a crash or system termination is removed when
the upload client next starts. This requires temporary free space roughly equal
to the ZIP plus the small multipart envelope, but avoids holding a video-heavy
archive in memory. The original exported ZIP remains untouched.

Scan package ZIP creation streams file contents to disk. This keeps export
memory lower for packages that include `video/scan.mov` or many keyframes.

## Backend Upload Persistence

The `/scans` endpoint does not call an unbounded `read()` on the uploaded ZIP.
It requests 1 MiB chunks from FastAPI's spooled `UploadFile` and writes them to
a uniquely named `.part` sibling under `scans/incoming/`. After the final chunk,
the backend flushes and fsyncs the temporary file, atomically creates the job's
unique incoming `.zip` as a hard link without clobbering an existing path, and
fsyncs the containing directory. It then removes the temporary name and syncs
the directory again before reporting success. Standard macOS and Linux
filesystems support the required hard-link and directory-sync path. Native
Windows lacks a portable directory-fsync API in Python; the documented RTX
workflow runs on native Linux, where the POSIX durability path is active.

Writes, flushes, closes, replacement, and directory sync run in worker threads
so slow storage does not block other event-loop work. Cancellation waits for an
active sink operation to finish safely, then removes the temporary or newly
published file before propagating. Late and concurrent destinations are
rejected atomically rather than overwritten, and rollback only removes a final
path that still has the uploaded temporary file's device/inode identity.
Ordinary storage failures move the job from
`received` to `failed` and return a generic HTTP 500 response. Failure-state
recording is best-effort: a job-record write failure cannot mask the original
storage error or cancellation. The source upload remains owned by FastAPI; the
storage helper owns only its temporary and destination paths.

## Backend Job Status

The local FastAPI backend stores job records under `scans/jobs/`. Query a single
job when you know its ID:

```bash
curl "http://localhost:8000/scans/<scan_id>"
```

Or list recent jobs, newest first:

```bash
curl "http://localhost:8000/scans?limit=20"
```

Each job record includes:

- `status`: the coarse lifecycle (`received`, `processing`, `validated`,
  `complete`, or `failed`).
- `stage`: the current processing activity (`queued`, `validating`,
  `reconstructing`, `meshing`, `exporting`, or `finished`).
- `message`: a human-readable description suitable for a local status UI.
- `created_at`, `updated_at`, `started_at`, and `finished_at`: UTC timestamps;
  older records created before lifecycle tracking may return `null` values.
- `image_count`, `frame_count`, and `outputs`: final capture counts and output
  paths when available.

Treat `outputs` as backend-internal diagnostics rather than client download
instructions. Ask for the typed downloadable manifest instead:

```bash
curl "http://localhost:8000/scans/<scan_id>/artifacts"
```

The response contains only declared output files that currently exist inside
the job package. Each entry includes `name`, `relative_path`, `filename`,
`byte_count`, and `media_type`. Download one with:

```bash
curl -O "http://localhost:8000/scans/<scan_id>/files/<relative_path>"
```

The manifest omits directories, missing/stale declarations, duplicate files,
symlinks, and paths outside `scans/completed/` or `scans/failed/`. Download
requests independently repeat containment and manifest-membership validation,
walk every path component through POSIX no-follow directory descriptors, reject
multi-link files, and stream the already-opened inode. This prevents a pathname
swap between authorization and response streaming. Run the reconstruction
backend on native Linux because secure artifact serving depends on POSIX
descriptor semantics.

Reconstruction output paths are rebased when their workspace moves from
processing to completed storage so future terminal jobs do not retain stale
processing paths. If the backend restarts after that move but before the final
job update, recovery rediscovers the known scan report, dense-or-sparse COLMAP
point cloud, and textured OBJ before restoring a `complete` job. An exporting-
stage job with no safe dense or sparse COLMAP result is marked failed with an
explicit message while its package directory remains preserved.

The iOS app's `Jobs` tab consumes this list through a persisted, editable
backend URL. Pull to refresh or use the refresh button. The initial URL is
`http://localhost:8000`, which is useful for simulator development. On a
physical iPhone, run the backend on the same trusted LAN with:

```bash
uvicorn app.main:app --reload --host 0.0.0.0
```

Then enter `http://<workstation-lan-ip>:8000`. The app requests local-network
permission and the client rejects cleartext HTTP unless the host is loopback, a
private/link-local IP address, `.local`, or `.home.arpa`; HTTPS remains supported
for other hosts. The backend has no authentication yet, so never expose this
listener to the public internet or an untrusted network.

A terminal job with published outputs opens a result screen backed by the typed
artifact manifest. The app treats the job record's `outputs` only as an
availability signal; it never turns those internal paths into download URLs.
Manifest identifiers and nested relative paths are validated and percent-
encoded before the app downloads one file at a time through URLSession's file
API. The downloaded regular file must match the declared filename and exact
byte count before it is moved into an app-owned temporary directory and offered
to the iOS share sheet. Dismissing the sheet, navigating away, cancelling, or a
later app launch removes the owned temporary copy.

PLY manifest entries also show a `Preview` action. The same safe artifact client
downloads and verifies the file before the preview loader opens it. The loader
supports PLY 1.0 ASCII, binary little-endian, and binary big-endian scalar vertex
records with the standard 8/16/32-bit integer and 32/64-bit floating-point
types. Vertex data must be the first non-empty element, contain scalar `x`, `y`,
and `z` properties, and may include complete RGB plus optional alpha channels;
list-valued vertex properties are rejected. Binary files over 2 GiB or
declarations over 100 million vertices are rejected; ASCII files have a tighter
512 MiB and 5-million-vertex work budget because their lines must be scanned.
Up to 120,000 evenly distributed points, including both endpoints, are decoded
for SceneKit and sampled bounds. Binary input seeks directly to those records;
ASCII input scans with cancellation checks every 64 KiB and decodes only the
retained records. The UI
preserves vertex color when available and provides orbit, zoom, pan, and point-
temporary download; it never edits the reconstruction result.

The backend rejects attempts to restart terminal jobs and writes each job JSON
record through a temporary sibling file followed by an atomic replacement.
Concurrent status reads therefore see either the previous complete record or
the new complete record, not partially written JSON.

Reconstruction currently runs as an in-process FastAPI background task. Run a
single backend process for each `SCANNER_SCANS_DIR`; the job store serializes
threads within that process but is not a multi-process queue. On startup, any
job left in `received` or `processing` is marked `failed` with an interruption
message. Partial processing directories move to `scans/failed/` without
overwriting an existing failed directory. If processing had already moved a
valid package to `scans/completed/`, startup restores the matching `validated`
or `complete` record and its download path. Processing does not resume
automatically, and the uploaded ZIP remains in `scans/incoming/` so it can be
inspected before the scan is submitted again.

## Native Linux RTX 3070 Workflow

Use the dual-boot RTX 3070 PC while it is booted into native Linux for final
reconstruction and Blender work:

1. Install CachyOS and let its `chwd` hardware manager configure the native
   NVIDIA driver. Follow [`cachyos_setup.md`](cachyos_setup.md); do not use
   NVIDIA's standalone `.run` installer.
2. Verify `nvidia-smi` sees the RTX 3070 after rebooting into Linux.
3. Clone this repo onto a Linux-native filesystem. Do not place active COLMAP
   databases or reconstruction workspaces on NTFS.
4. Preview and install the CachyOS base packages, then inspect the environment:

```bash
scripts/wsl/setup_gpu_reconstruction.sh --dry-run
scripts/wsl/setup_gpu_reconstruction.sh
python3 scripts/wsl/check_reconstruction_env.py
```

The helper directory retains its historical `scripts/wsl/` name so existing
commands keep working. The setup script detects CachyOS/Arch and Ubuntu/Debian.
It does not install neural packages into the rolling system Python or execute
unreviewed AUR recipes.

5. Build the pinned CUDA-enabled COLMAP/OpenMVS tools, activate the isolated
   Nerfstudio/gsplat environment, and pass the full gate:

```bash
source /etc/profile.d/cuda.sh
export PATH="$HOME/.local/bin:$PATH"
python3 scripts/wsl/check_reconstruction_env.py --strict
```

6. Create Linux-native workspace folders and dry-run the command plan:

```bash
mkdir -p ~/ScannerOutputs ~/ScannerPlans
python3 scripts/reconstruct_gpu.py scan.zip \
  --output-root ~/ScannerOutputs \
  --dry-run
```

7. Run COLMAP dense reconstruction and OpenMVS:

```bash
python3 scripts/reconstruct_gpu.py scan.zip --output-root ~/ScannerOutputs
```

OpenMVS densification uses its estimated region of interest by default. Use
`--scope-mode unbounded` only for diagnostics when the automatic ROI removes
required geometry. The runner warns above 2 million dense points and stops
before meshing above 10 million points; both thresholds are recorded in the
reconstruction report.

8. Open OBJ/PLY outputs directly in Blender for Linux. Copy only finished OBJ,
   GLB, `.blend`, reports, or logs to a shared partition if they are also needed
   from Windows.

Dual boot changes worker availability: while the PC is booted into Windows, the
Linux reconstruction agent is offline. Local manual work waits until the next
Linux boot; future Convex jobs remain queued and show **Waiting for
reconstruction computer**. Starting Linux should launch the agent through
`systemd` once that automation is implemented.

To compare backend command plans on the workstation before a long run:

```bash
python3 scripts/plan_reconstruction_backend.py scan.zip \
  --backend colmap_openmvs \
  --work-dir ~/ScannerPlans/colmap

python3 scripts/plan_reconstruction_backend.py scan.zip \
  --backend meshroom \
  --work-dir ~/ScannerPlans/meshroom
```

The expected output layout is:

```text
ScannerOutputs/
  scan_id/
    source/
    logs/
      commands.log
    report.json
```

COLMAP/OpenMVS write their native outputs under `source/scan_id/`.

To create a `.blend` file from an output asset:

```bash
blender --background --python scripts/blender/prepare_scan_asset.py -- \
  ~/ScannerOutputs/scan_id/source/scan_id/dense/scene_textured.obj \
  ~/ScannerOutputs/scan_id/blender/scan_id.blend
```

The Blender helper accepts OBJ, PLY, GLB, and GLTF. It can also apply a scale,
set origins, relink textures, decimate meshes, and export a GLB:

```bash
blender --background --python scripts/blender/prepare_scan_asset.py -- \
  ~/ScannerOutputs/scan_id/source/scan_id/dense/scene_textured.obj \
  ~/ScannerOutputs/scan_id/blender/scan_id.blend \
  --texture-dir ~/ScannerOutputs/scan_id/source/scan_id/dense \
  --scale 1.0 \
  --origin geometry \
  --decimate-ratio 0.5 \
  --export-glb ~/ScannerOutputs/scan_id/blender/scan_id.glb
```

The helper supports Blender 4.x native OBJ/PLY import operators and falls back
to the Blender 3.x legacy OBJ/PLY import operators when needed.

To manually crop a point cloud in COLMAP/OpenMVS coordinates:

```bash
python3 scripts/plan_object_crop.py scan.zip
python3 scripts/crop_point_cloud.py input.ply cropped.ply --center 0 0 0 --radius 2.0
```

To dry-run point-cloud cleanup or downsampling after any backend produces a PLY:

```bash
python3 scripts/process_point_cloud.py input.ply output.ply \
  --processor open3d \
  --voxel-size 0.03 \
  --dry-run
```

Open3D is the default processing backend. ThreeCrate is available as an
experimental optional processor and is not installed by default:

```bash
python3 scripts/process_point_cloud.py input.ply output.ply \
  --processor threecrate \
  --voxel-size 0.03 \
  --dry-run
```

Remove `--dry-run` only after installing the selected processor in the active
Python environment. ThreeCrate should be compared against Open3D on real scan
outputs before replacing the default cleanup path.
ThreeCrate normal estimation is currently used only as a processing step; the
script converts the result back to a plain point cloud before writing, so
ThreeCrate output PLY files should not be treated as normal-preserving exports.

## Object Scan Metadata

Object scans store:

- `scan_mode`: `object_scan`
- `object_center_world`: ARKit world-space subject center from the iPhone tap
- `object_radius_meters`: selected radius preset

This metadata is ready for object-focused processing, but exact COLMAP point
cloud cropping needs an ARKit-to-COLMAP coordinate alignment step.
