from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.mask_authoring import MaskAuthoringError, load_mask_authoring_plan, single_box_selection
from app.mask_generator import MaskGenerationError, generate_mask_proposals
from app.mask_review import MaskReviewBlockedError, MaskReviewError, accept_automatic_masks, load_mask_review
from app.sam2_generator import Sam2MaskGenerator, Sam2Runtime, validate_sam2_inputs, verify_checkpoint
from app.scan_metadata import load_scan_metadata
from app.scan_validator import validate_scan_package
from heavy_work_fixture import isolated_heavy_work


def single_box_scan(root: Path, count: int = 5) -> Path:
    (root / "images").mkdir()
    (root / "metadata").mkdir()
    frames = []
    for index in range(count):
        size = (40,60) if index % 2 == 0 else (60,40)
        name = f"frame_{index:06d}.jpg"
        Image.new("RGB", size, (140,90,30)).save(root / "images" / name)
        frames.append({"id":index+1, "image":f"images/{name}", "timestamp":float(index), "resolution":size})
    (root/"metadata/frames.json").write_text(json.dumps(frames))
    (root/"metadata/session.json").write_text(json.dumps({"scan_id":"sam2-test", "scan_mode":"object_scan", "image_count":count, "video_count":0}))
    (root/"metadata/manifest.json").write_text(json.dumps({"scan_id":"sam2-test"}))
    (root/"metadata/mask_authoring.json").write_text(json.dumps({
        "schema_version":"1.1", "authoring_mode":"single_box", "revision":1,
        "coordinate_space":"normalized_capture_image", "mask_convention":"white_keep_black_exclude",
        "representative_frames":[{"frame_id":1,"image":frames[0]["image"],"regions":[{
            "operation":"keep", "points":[{"x":.2,"y":.2},{"x":.8,"y":.2},{"x":.8,"y":.8},{"x":.2,"y":.8}],
        }]}],
    }))
    return root


def fake_worker(command, **kwargs):
    root = Path(command[command.index("--scan-root")+1])
    mask_dir = Path(command[command.index("--mask-dir")+1])
    work = Path(command[command.index("--work-dir")+1])
    frames = load_scan_metadata(root / "metadata").frames
    for frame in frames:
        image = Image.new("L", frame.resolution)
        ImageDraw.Draw(image).rectangle((5,5,20,25), fill=255)
        image.save(mask_dir / (Path(frame.image).name+".png"))
    (work/"result.json").write_text(json.dumps({"frame_ids":[frame.id for frame in frames]}))
    return SimpleNamespace(returncode=0)


class Sam2GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_heavy_work())
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = single_box_scan(Path(self.temp))
        self.frames = load_scan_metadata(self.root/"metadata").frames
        self.plan = load_mask_authoring_plan(self.root/"metadata", self.frames)
        self.runtime = Sam2Runtime(Path("/test/python"),Path("/test/model.pt"))

    def generate(self, worker=fake_worker):
        with patch.object(Sam2Runtime, "validate"), patch("app.sam2_generator.subprocess.run", side_effect=worker):
            return generate_mask_proposals(self.root, self.frames, generator=Sam2MaskGenerator(self.runtime))

    def test_single_box_contract_round_trips_without_reinterpreting_polygons(self):
        selected, box = single_box_selection(self.plan)
        self.assertEqual((selected.frame_id,box), (1,(.2,.2,.8,.8)))
        payload = json.loads((self.root/"metadata/mask_authoring.json").read_text())
        self.assertEqual(self.plan.as_dict(),payload)
        payload["schema_version"] = "1.0"
        (self.root/"metadata/mask_authoring.json").write_text(json.dumps(payload))
        with self.assertRaises(MaskAuthoringError):
            load_mask_authoring_plan(self.root/"metadata",self.frames)

    def test_rejects_nonrectangle_multiple_selections_and_unhashable_modes(self):
        original = self.plan.as_dict()
        variants = []
        payload = json.loads(json.dumps(original)); payload["representative_frames"] *= 2; variants.append(payload)
        payload = json.loads(json.dumps(original)); payload["representative_frames"][0]["regions"][0]["points"][1]["y"] = .3; variants.append(payload)
        payload = json.loads(json.dumps(original)); payload["authoring_mode"] = []; variants.append(payload)
        payload = json.loads(json.dumps(original)); payload["schema_version"] = {}; variants.append(payload)
        for payload in variants:
            with self.subTest(payload=payload):
                (self.root/"metadata/mask_authoring.json").write_text(json.dumps(payload))
                with self.assertRaises(MaskAuthoringError):
                    load_mask_authoring_plan(self.root/"metadata",self.frames)

    def test_generation_preserves_sources_and_validates_every_mixed_size_mask(self):
        originals = {frame.image:(self.root/frame.image).read_bytes() for frame in self.frames}
        result = self.generate()
        self.assertEqual(result.generator,"sam2_1_small_temporal_v1")
        self.assertEqual(result.state,"awaiting_review")
        self.assertTrue(all(frame.confidence is None for frame in result.frames))
        self.assertFalse((self.root/"masks/capture").exists())
        for frame in self.frames:
            self.assertEqual((self.root/frame.image).read_bytes(), originals[frame.image])
            with Image.open(self.root/"masks/proposed"/(Path(frame.image).name+".png")) as image:
                self.assertEqual(image.size,frame.resolution)
        self.assertEqual(validate_scan_package(self.root).capture_mask_count,0)

    def test_auto_acceptance_is_not_reported_as_human_approval(self):
        self.generate()
        decision = accept_automatic_masks(self.root)
        self.assertEqual(decision["state"],"auto_accepted")
        self.assertFalse(decision["decision"]["human_reviewed"])
        self.assertEqual(validate_scan_package(self.root).capture_mask_count,len(self.frames))
        self.assertEqual(load_mask_review(self.root)["state"],"auto_accepted")
        with self.assertRaises(MaskReviewError):
            accept_automatic_masks(self.root)

    def test_automatic_policy_rejects_empty_masks_but_does_not_block_motion_hints(self):
        def abrupt(command, **kwargs):
            result = fake_worker(command,**kwargs)
            directory = Path(command[command.index("--mask-dir")+1])
            Image.new("L", self.frames[2].resolution,255).save(directory/(Path(self.frames[2].image).name+".png"))
            return result
        result = self.generate(abrupt)
        self.assertEqual(result.state,"awaiting_review")
        self.assertTrue(any(item["code"] == "abrupt_area_change" for item in result.warnings))
        def empty(command, **kwargs):
            result = fake_worker(command,**kwargs)
            directory = Path(command[command.index("--mask-dir")+1])
            Image.new("L", self.frames[2].resolution).save(directory/(Path(self.frames[2].image).name+".png"))
            return result
        result = self.generate(empty)
        self.assertEqual(result.state,"needs_correction")
        with self.assertRaises(MaskReviewBlockedError):
            accept_automatic_masks(self.root)
        self.assertFalse((self.root/"masks/capture").exists())

    def test_failed_worker_preserves_previous_proposals_and_never_promotes(self):
        self.generate()
        before = (self.root/"metadata/mask_generation.json").read_bytes()
        for failure in (SimpleNamespace(returncode=1), subprocess.TimeoutExpired("sam2",600)):
            def failed(command, **kwargs):
                if isinstance(failure,Exception):
                    raise failure
                return failure
            with self.subTest(failure=failure), self.assertRaises(MaskGenerationError):
                self.generate(failed)
            self.assertEqual((self.root/"metadata/mask_generation.json").read_bytes(),before)
            self.assertFalse((self.root/"masks/capture").exists())

    def test_missing_or_misaligned_masks_cannot_be_published(self):
        def invalid(command, **kwargs):
            result = fake_worker(command,**kwargs)
            directory = Path(command[command.index("--mask-dir")+1])
            Image.new("L",(12,12),255).save(directory/(Path(self.frames[3].image).name+".png"))
            return result
        with self.assertRaises(ValueError):
            self.generate(invalid)
        self.assertFalse((self.root/"masks/proposed").exists())

    def test_input_boundary_rejects_wrong_order_duplicate_names_paths_and_symlinks(self):
        for frames in (
            (self.frames[0],replace(self.frames[1],timestamp=self.frames[0].timestamp),*self.frames[2:]),
            (self.frames[0],replace(self.frames[1],image=self.frames[0].image),*self.frames[2:]),
            (self.frames[0],replace(self.frames[1],image="../outside.jpg"),*self.frames[2:]),
        ):
            with self.subTest(frames=frames), self.assertRaises(MaskGenerationError):
                validate_sam2_inputs(self.root,self.plan,frames)
        path = self.root/self.frames[1].image
        path.unlink(); path.symlink_to(self.root/self.frames[0].image)
        with self.assertRaises(MaskGenerationError):
            validate_sam2_inputs(self.root,self.plan,self.frames)

    def test_untrusted_checkpoint_and_uninstalled_runtime_fail_before_model_loading(self):
        path = self.root/"untrusted.pt"
        path.write_bytes(b"not the approved model")
        with self.assertRaisesRegex(MaskGenerationError,"SHA256"):
            verify_checkpoint(path)
        with self.assertRaisesRegex(MaskGenerationError,"not installed"):
            self.runtime.validate()

    def test_single_box_dispatch_selects_sam2_without_loading_torch_in_api(self):
        with patch("app.sam2_generator.Sam2MaskGenerator.generate", return_value="sentinel") as generate:
            self.assertEqual(generate_mask_proposals(self.root,self.frames),"sentinel")
        generate.assert_called_once()
        self.assertNotIn("torch",sys.modules)


if __name__ == "__main__":
    unittest.main()
