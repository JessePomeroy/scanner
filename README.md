# Scanner

Scan an object with an iPhone, draw one box around it, and get a model that
opens in Blender. The phone captures; a Linux workstation with an NVIDIA GPU
does the reconstruction.

## Use it

1. **Scan.** In the Scanner app, tap **Start**, walk slowly around the object
   keeping it in view, and tap **Stop**.
2. **Send.** In the **Scans** tab, tap the upload button on the scan, then
   **Draw the Box**. This opens the workstation's page in the browser.
3. **Box.** Drag a box around the whole object and tap **Make 3D model**.
4. **Open.** About 8 minutes later the page shows the model with download
   buttons. On the workstation a notification offers **Open in Blender**, and
   the files are in `~/ScannerModels/<capture time>/`.

A phone build from before this flow has no **Draw the Box** button: upload,
then open the page yourself and pick the scan marked **Draw a box**.

You can close the page while it works. If another model is being built, the
new one waits and starts by itself.

The page is at `http://<workstation address>:8000`. It also accepts a scan ZIP
directly: choose it or drop it on the page from any computer on the network.

On the box screen you can choose **Detailed** (about 12 minutes, roughly two and
a half times the triangles) and **Also make a retro low-poly version** (a separate
500, 1000 or 2000-triangle copy with a pixelated 256-pixel texture).

## What you get

| File | Use |
| --- | --- |
| `<name>.blend` | Open in Blender; textures are embedded |
| `<name>.glb` | Other 3D apps and game engines |
| `<name>-obj.zip` | OBJ, MTL and texture images |
| `<name>-retro.glb`, `<name>-retro.blend` | The low-poly copy, when requested |
| `preview.png` | A picture of the model from the photo you drew the box on |

## Limits to know about

- Only **Object** scans use this flow. A capture saved in Scene mode is refused
  with a message saying so.
- The whole flow has been proven on one subject: a teddy bear, 138 photos.
  Shiny, dark, thin or very small objects have not been tried.
- The model is not upright or true to size in Blender. Its orientation and
  scale come from the reconstruction; rotate and scale it by hand.
- Meshes are automatic output. Expect coarse fine detail and flat bridging
  where the camera could not see, such as the underside.
- The backend has no login. Run it only on a network you trust.

## One-time setup on the workstation

1. Install the GPU tools (CUDA-enabled COLMAP, OpenMVS, Blender 5.2) by
   following [docs/cachyos_setup.md](docs/cachyos_setup.md).
2. Create the backend environment:

   ```bash
   cd backend
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cd ..
   ```

3. Create the SAM2 environment, which finds the object in every photo. It is a
   separate Python 3.11 environment so the backend never loads PyTorch:

   ```bash
   uv venv --seed --python 3.11 backend/.venv-sam2
   backend/.venv-sam2/bin/python -m pip install -r backend/requirements-sam2-runtime.txt
   SAM2_BUILD_CUDA=0 backend/.venv-sam2/bin/python -m pip install --no-build-isolation -r backend/requirements-sam2.txt
   backend/.venv-sam2/bin/python scripts/install_sam2_model.py
   ```

4. Install and start the services:

   ```bash
   scripts/install_services.sh
   ```

   It prints the address to open. Run it again after pulling new code; it will
   not restart the backend while a model is being built.

5. On the phone, open the **Jobs** tab and enter that address as the backend
   URL. This is needed once.

## Settings

The service unit in `deploy/systemd/` sets these. Change them there and run
`scripts/install_services.sh` again.

| Variable | Meaning | Default in the unit |
| --- | --- | --- |
| `SCANNER_SCANS_DIR` | Uploads, working files and job records | `backend/scans` |
| `SCANNER_LIBRARY_DIR` | Where finished models are copied; unset means no copy | `~/ScannerModels` |
| `SCANNER_OBJECT_UPLOADS` | `one_box` sends every Object upload through the box flow, including uploads from older phone builds | `one_box` |
| `SCANNER_SAM2_PYTHON`, `SCANNER_SAM2_CHECKPOINT` | SAM2 interpreter and model file | `backend/.venv-sam2/bin/python`, `backend/models/sam2.1_hiera_small.pt` |

The unit caps the backend at 32 GiB of RAM with no swap. The reference scan
peaked near 6 GiB (Fast) and 20 GiB (Detailed).

## When something goes wrong

- **"This scan did not finish"** shows the reason on the page. "Too few photos
  aligned" means the capture needs more overlap: move slower and keep the
  object in every photo. The scan stays on the phone, so you can upload again.
- **The box screen never appears.** The upload is still being checked, or the
  capture was saved in Scene mode.
- **Nothing happens after the box.** Another job is running; this one starts
  when it finishes. The page says so.
- **Logs.** `journalctl --user -u scanner-backend` for the service. Each job
  keeps its own logs under `backend/scans/completed/<id>/` or
  `backend/scans/failed/<id>/`, in `object_logs/` and `exports/`.
- **Disk.** A finished job keeps about 3 GB of working files. **Delete scan** on
  the page removes them and keeps the copy in `~/ScannerModels`.

## What is in this repository

- `ios/ScannerApp`: the capture app. Build it in Xcode on a Mac; scanning needs
  a physical ARKit device.
- `backend/app`: the upload API, the page (`web/index.html`) and the
  reconstruction pipeline.
- `scripts`: the notifier, service installer, SAM2 model installer, Blender
  scripts and command-line runners.
- `deploy/systemd`: the two user service units.
- `desktop`: a Qt monitor for hand-launched recovery runs; see
  [desktop/README.md](desktop/README.md).
- `experiments`: engines, planners and research that are not part of this
  flow; see [experiments/README.md](experiments/README.md).
- `docs`: [backend_reference.md](docs/backend_reference.md) covers the API, the
  older review flow with masks and a 3D region, the command-line runners and
  storage rules. [capture_guidelines.md](docs/capture_guidelines.md) covers
  how to shoot.

## Tests

```bash
backend/.venv/bin/python -m unittest discover -s tests
```

Two cases run real Blender and are skipped unless asked for. Run them while no
model is being built:

```bash
SCANNER_TEST_NATIVE_MODEL=1 SCANNER_TEST_NATIVE_RETRO=1 \
  backend/.venv/bin/python -m unittest discover -s tests
```

The iPhone code has portable Swift checks listed in
[scripts/README.md](scripts/README.md); they need a Mac.
