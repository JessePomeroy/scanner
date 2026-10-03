"""Private SAM2 subprocess entry point; no request-selected model or network downloads."""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image

from app.mask_authoring import load_mask_authoring_plan
from app.sam2_generator import (
    SAM2_CHECKPOINT_SHA256, SAM2_MODEL_CONFIG, SAM2_SOURCE_COMMIT,
    validate_sam2_inputs, verify_checkpoint,
)
from app.scan_metadata import load_scan_metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-root", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--mask-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()
    verify_checkpoint(args.checkpoint)
    metadata = load_scan_metadata(args.scan_root / "metadata")
    plan = load_mask_authoring_plan(args.scan_root / "metadata", metadata.frames)
    if plan is None:
        raise ValueError("SAM2 requires an explicit one-box selection")
    seed, box = validate_sam2_inputs(args.scan_root, plan, metadata.frames)
    if any(args.mask_dir.iterdir()):
        raise ValueError("SAM2 mask output must be an empty private directory")
    started = time.perf_counter()
    proxy_dir = args.work_dir / "frames"
    proxy_dir.mkdir(exist_ok=False)
    first_width, first_height = metadata.frames[0].resolution
    scale = 1024 / max(first_width, first_height)
    canvas = max(1, round(first_width*scale)), max(1, round(first_height*scale))
    transforms: list[tuple[int, int, int, int]] = []
    source_hashes = []
    for index, frame in enumerate(metadata.frames):
        source = args.scan_root / frame.image
        with source.open("rb") as file:
            source_hashes.append(hashlib.file_digest(file, "sha256").hexdigest())
        with Image.open(source) as image:
            if image.size != frame.resolution:
                raise ValueError("Capture image does not match its declared dimensions")
            width, height = image.size
            factor = min(canvas[0]/width, canvas[1]/height)
            resized = max(1, round(width*factor)), max(1, round(height*factor))
            left, top = (canvas[0]-resized[0])//2, (canvas[1]-resized[1])//2
            proxy = Image.new("RGB", canvas)
            proxy.paste(image.convert("RGB").resize(resized, Image.Resampling.LANCZOS), (left,top))
        proxy.save(proxy_dir / f"{index:06d}.jpg", quality=95, subsampling=0)
        transforms.append((left,top,*resized))
    # Heavy dependencies live only in this short-lived process and are unloaded before COLMAP/OpenMVS.
    import torch
    import torch.nn.functional as functional
    from sam2.build_sam import build_sam2_video_predictor
    install = json.loads(distribution("SAM-2").read_text("direct_url.json") or "{}")
    if install.get("vcs_info", {}).get("commit_id") != SAM2_SOURCE_COMMIT:
        raise RuntimeError("SAM2 installation is not the pinned source revision")
    if torch.__version__ != "2.7.1+cu126":
        raise RuntimeError("SAM2 worker requires the pinned PyTorch 2.7.1+cu126 runtime")
    if not torch.cuda.is_available():
        raise RuntimeError("SAM2 object processing requires an available NVIDIA CUDA GPU")
    torch.set_num_threads(4)
    precision = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    outputs: set[int] = set()
    with torch.inference_mode(), torch.autocast("cuda", dtype=precision):
        predictor = build_sam2_video_predictor(SAM2_MODEL_CONFIG, str(args.checkpoint), device="cuda")
        predictor.fill_hole_area = 0
        state = predictor.init_state(str(proxy_dir), offload_video_to_cpu=True, offload_state_to_cpu=True)
        if (state["video_width"], state["video_height"]) != canvas or state["num_frames"] != len(metadata.frames):
            raise RuntimeError("SAM2 loaded inconsistent proxy geometry")
        left, top, width, height = transforms[seed]
        coordinates = np.array([left+box[0]*width, top+box[1]*height,
                                left+box[2]*width, top+box[3]*height], dtype=np.float32)
        predictor.add_new_points_or_box(state, frame_idx=seed, obj_id=1, box=coordinates)
        torch.cuda.reset_peak_memory_stats()
        for reverse in ((False,True) if seed else (False,)):
            for index, object_ids, logits in predictor.propagate_in_video(state, start_frame_idx=seed, reverse=reverse):
                if index in outputs:
                    continue
                if object_ids != [1] or not 0 <= index < len(metadata.frames):
                    raise RuntimeError("SAM2 returned an unexpected object or frame")
                if tuple(logits.shape) != (1,1,canvas[1],canvas[0]) or not torch.isfinite(logits).all():
                    raise RuntimeError("SAM2 returned invalid mask logits")
                frame = metadata.frames[index]
                left, top, width, height = transforms[index]
                cropped = logits[:, :, top:top+height, left:left+width].float()
                mask = functional.interpolate(cropped, size=(frame.resolution[1],frame.resolution[0]),
                                              mode="bilinear", align_corners=False)[0,0] > 0
                output = args.mask_dir / (Path(frame.image).name + ".png")
                with output.open("xb") as file:
                    Image.fromarray(mask.cpu().numpy().astype(np.uint8)*255).save(file, format="PNG")
                outputs.add(index)
                if len(outputs) % 25 == 0 or len(outputs) == len(metadata.frames):
                    print(f"SAM2 generated {len(outputs)}/{len(metadata.frames)} masks", flush=True)
        torch.cuda.synchronize()
    if outputs != set(range(len(metadata.frames))):
        raise RuntimeError("SAM2 did not cover the full capture in both directions")
    result = {
        "model":"SAM 2.1 Small", "source_commit":SAM2_SOURCE_COMMIT,
        "checkpoint_sha256":SAM2_CHECKPOINT_SHA256, "torch_version":torch.__version__,
        "frame_ids":[frame.id for frame in metadata.frames], "seed_frame_id":metadata.frames[seed].id,
        "proxy_canvas":canvas, "proxy_transforms":transforms, "source_image_sha256":source_hashes,
        "mask_export":"Remove proxy letterbox, resize logits to original dimensions, then threshold at zero. No EXIF rotation or source-image edits.",
        "elapsed_seconds":time.perf_counter()-started,
        "peak_torch_allocated_bytes":torch.cuda.max_memory_allocated(),
        "optional_cuda_cleanup":False,
    }
    with (args.work_dir / "result.json").open("x", encoding="utf-8") as file:
        json.dump(result, file, indent=2, allow_nan=False)


if __name__ == "__main__":
    main()
