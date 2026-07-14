# Neural Backend Experiments

These backends are research paths for the personal art workflow. They should
not block the production COLMAP/OpenMVS path until one real scan has produced a
textured OBJ that opens cleanly in Blender.

Use the planner to inspect inputs and suggested commands without installing
large model dependencies:

```bash
python3 scripts/plan_neural_backend.py scan.zip --backend mast3r_slam
python3 scripts/plan_neural_backend.py scan.zip --backend depth_anything
python3 scripts/plan_neural_backend.py scan.zip --backend lingbot
python3 scripts/plan_neural_backend.py scan.zip --backend gaussian_splatting
```

By default, reports and extracted scan files are written to
`NeuralPlans/<scan_id>/<backend>/`, which is ignored by Git.

## MASt3R-SLAM

Use this as the first neural reconstruction experiment on the native Linux RTX
3070 workstation. It can consume a video or an image folder.

The planner prefers `metadata/video.json` / `video/` input when available, then
falls back to `images/`:

```bash
python3 scripts/plan_neural_backend.py scan.zip --backend mast3r_slam
```

Start with reduced frame counts or lower resolution before attempting full scan
videos. Treat results as an experimental dense SLAM/geometry path, not the
default Blender asset pipeline.

## Depth Anything / DA3

Use Depth Anything-style tools for depth estimation, scan diagnostics, object
isolation, preview depth, and possible future DA3 multi-view experiments. The
planner defaults to the small encoder (`vits`) because larger Depth Anything V2
checkpoints carry non-commercial terms.

```bash
python3 scripts/plan_neural_backend.py scan.zip --backend depth_anything
```

Depth outputs are support data. They are not a replacement for textured mesh
generation.

## Lingbot-Style Viewer

Lingbot-style workflows are useful references for local video-to-point-cloud UX.
They currently expect video input and should be treated as viewer/point-cloud
experiments.

```bash
python3 scripts/plan_neural_backend.py scan.zip --backend lingbot
```

If the scan has no video, the planner will still report available images but
the notes will indicate that video capture is needed.

## Gaussian Splatting / Nerfstudio

Use this as the first viewer-focused Gaussian splatting experiment once the
native Linux RTX workstation is available. The planner targets Nerfstudio
Splatfacto:

```bash
python3 scripts/plan_neural_backend.py scan.zip --backend gaussian_splatting
```

The generated command plan:

1. Converts image or video input into a Nerfstudio dataset with
   `ns-process-data`.
2. Trains a splat with `ns-train splatfacto`.
3. Exports the editable master `splat.ply` with `ns-export gaussian-splat`.
4. Converts that master to compact delivery files with PlayCanvas
   `splat-transform`.

The planner prefers the full-resolution image keyframes already exported by the
iPhone app, even when a video is present. The support video is capped at 30
seconds, while keyframe capture continues for the full session, so choosing the
video could silently omit most of a scene. Video is only a fallback when no
images are available. The export command contains a placeholder `config.yml`
path because Nerfstudio creates the final training output folder at runtime.

The default delivery pair is:

- `scene.sog`: compact R2/browser delivery format.
- `scene.html`: self-contained local viewer for a quick visual check.

Always preserve `exports/splat/splat.ply` as the editable master. Repeat
`--splat-delivery-format` to request other outputs:

```bash
python3 scripts/plan_neural_backend.py scan.zip \
  --backend gaussian_splatting \
  --splat-delivery-format sog \
  --splat-delivery-format html \
  --splat-delivery-format spz
```

A `gaussian-glb` uses the emerging `KHR_gaussian_splatting` extension. It is
not a conventional textured mesh GLB and should not be treated as the Blender
mesh deliverable.

Use the standard `splatfacto` profile first on the RTX 3070. Nerfstudio
documents it around 6 GB VRAM; `splatfacto-big` is documented around 12 GB and
is not the default for an 8 GB card.

The upstream projects have distinct roles:

- [Nerfstudio gsplat](https://github.com/nerfstudio-project/gsplat) supplies the
  CUDA-accelerated Gaussian rasterization foundation.
- [PlayCanvas SuperSplat](https://github.com/playcanvas/supersplat) is the
  browser editor for inspecting, cleaning, cropping, and publishing splats.
- [GraphDECO Gaussian Splatting](https://github.com/graphdeco-inria/gaussian-splatting)
  is the reference implementation, not the default production runtime: its
  paper-quality configuration expects substantially more VRAM and its license
  is limited to non-commercial research/evaluation use.
- [Awesome 3D Gaussian Splatting](https://github.com/MrNeRF/awesome-3D-gaussian-splatting)
  is a discovery index, not a pinned runtime dependency.

Treat this as a separate visual-art/viewer output. It is not a replacement for
the editable OBJ/GLB mesh path used by Blender.

## License Notes

- MASt3R/DUSt3R-family projects may carry non-commercial research licenses.
- Depth Anything V2 Small is Apache-2.0; Base/Large/Giant checkpoints are
  non-commercial.
- Lingbot wrapper code and model weights can have different terms; check both.
- Nerfstudio and gsplat are Apache-2.0, but verify licenses for any extra
  plugins, datasets, viewers, or model checkpoints added later.
- Keep neural tools optional and isolated in their own environments.
