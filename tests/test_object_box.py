from __future__ import annotations

from io import BytesIO
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.heavy_work import HeavyWorkBusy, HeavyWorkUnavailable
from app.jobs import JobStore
from app.mask_authoring import load_mask_authoring_plan, single_box_selection
from app.object_box import (
    ObjectBoxError,
    ObjectBoxOptions,
    apply_object_box,
    check_object_box_workspace,
    load_object_box_checkpoint,
    needs_object_box,
    parse_box,
    parse_object_box_request,
    publish_object_box_checkpoint,
)
from app.scan_metadata import load_scan_metadata
from app.scan_package import validate_and_report_scan
from heavy_work_fixture import isolated_heavy_work
from test_sam2_generator import single_box_scan


def older_draft(root: Path) -> dict:
    """The five-photo keep/erase draft current phone builds save."""
    frames = load_scan_metadata(root / "metadata").frames
    return {
        "schema_version": "1.0", "authoring_mode": "representative_frames", "revision": 2,
        "coordinate_space": "normalized_capture_image", "mask_convention": "white_keep_black_exclude",
        "representative_frames": [{"frame_id": frames[0].id, "image": frames[0].image, "regions": [{
            "operation": "keep",
            "points": [{"x": .1, "y": .1}, {"x": .9, "y": .1}, {"x": .5, "y": .9}],
        }]}],
    }


def phone_scan(
    root: Path, *, capture_outline: bool = False, mode: str = "object_scan",
    draft: bool = False, box: bool = False,
) -> Path:
    """A package as the phone exports it: no box, optionally a capture-time outline."""
    single_box_scan(root)
    if draft:
        (root / "metadata/mask_authoring.json").write_text(json.dumps(older_draft(root)))
    elif not box:
        (root / "metadata/mask_authoring.json").unlink()
    session_path = root / "metadata/session.json"
    session = json.loads(session_path.read_text())
    session["scan_mode"] = mode
    session_path.write_text(json.dumps(session))
    if capture_outline:
        frames = load_scan_metadata(root / "metadata").frames
        capture = root / "masks/capture"
        capture.mkdir(parents=True)
        for frame in frames:
            Image.new("L", frame.resolution, 255).save(capture / f"{Path(frame.image).name}.png")
        manifest_path = root / "metadata/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["reconstruction_scope"] = {
            "schema_version": "1.0", "mode": "image_masks", "mask_space": "capture_image",
            "mask_convention": "white_keep_black_exclude", "mask_count": len(frames),
        }
        manifest_path.write_text(json.dumps(manifest))
    return root


class ObjectBoxRequestTests(unittest.TestCase):
    def test_box_must_be_four_ordered_unit_numbers_with_real_area(self):
        self.assertEqual(parse_box([0.1, 0.2, 0.6, 0.9]), (0.1, 0.2, 0.6, 0.9))
        for value in ([0.1, 0.2, 0.6], "0.1,0.2,0.6,0.9", [0.6, 0.2, 0.1, 0.9], [0.1, 0.2, 0.11, 0.9],
                      [0.1, 0.2, 1.2, 0.9], [0.1, 0.2, float("nan"), 0.9], [True, 0.2, 0.6, 0.9],
                      [0.1, 0.2, float("inf"), 0.9], [0, 0, 10 ** 400, 1], [-10 ** 400, 0, 1, 1], None):
            with self.subTest(value=value), self.assertRaises(ObjectBoxError):
                parse_box(value)

    def test_request_uses_upload_choices_unless_the_client_overrides_them(self):
        defaults = ObjectBoxOptions("detail", "retro", 1000)
        request = parse_object_box_request({"frame_id": 3, "box": [0.1, 0.1, 0.9, 0.9]}, defaults)
        self.assertEqual((request.frame_id, request.options), (3, defaults))
        request = parse_object_box_request(
            {"frame_id": 3, "box": [0.1, 0.1, 0.9, 0.9], "preset": "preview", "export_style": "standard"},
            defaults,
        )
        self.assertEqual(request.options, ObjectBoxOptions("preview", "standard", 500))

    def test_request_rejects_unknown_fields_and_inconsistent_choices(self):
        box = [0.1, 0.1, 0.9, 0.9]
        for payload in (
            [], {"box": box}, {"frame_id": -1, "box": box}, {"frame_id": 1.5, "box": box},
            {"frame_id": 1, "box": box, "matcher": "exhaustive"},
            {"frame_id": 1, "box": box, "preset": "ultra"},
            {"frame_id": 1, "box": box, "export_style": "retro", "retro_triangles": 750},
            {"frame_id": 1, "box": box, "retro_triangles": 2000},
        ):
            with self.subTest(payload=payload), self.assertRaises(ObjectBoxError):
                parse_object_box_request(payload, ObjectBoxOptions())


class ObjectBoxWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_only_an_explicit_single_box_package_can_skip_the_box_step(self):
        single_box_scan(self.root)
        self.assertFalse(needs_object_box(self.root))
        path = self.root / "metadata/mask_authoring.json"
        plan = json.loads(path.read_text())
        plan.update(schema_version="1.0", authoring_mode="representative_frames")
        path.write_text(json.dumps(plan))
        self.assertTrue(needs_object_box(self.root))
        path.unlink()
        self.assertTrue(needs_object_box(self.root))

    def test_box_becomes_the_single_box_plan_bound_to_the_chosen_photo(self):
        phone_scan(self.root)
        applied = apply_object_box(self.root, 3, (0.25, 0.3, 0.75, 0.8))
        self.assertEqual((applied["frame_id"], applied["superseded"]), (3, []))
        frames = load_scan_metadata(self.root / "metadata").frames
        selected, box = single_box_selection(load_mask_authoring_plan(self.root / "metadata", frames))
        self.assertEqual((selected.frame_id, selected.image, box),
                         (3, "images/frame_000002.jpg", (0.25, 0.3, 0.75, 0.8)))
        self.assertFalse((self.root / "superseded").exists())
        self.assertIsNone(validate_and_report_scan(self.root).validation.reconstruction_scope)

    def test_capture_outline_and_older_draft_are_set_aside_not_deleted(self):
        phone_scan(self.root, capture_outline=True, draft=True)
        draft = older_draft(self.root)
        validate_and_report_scan(self.root)
        mask = (self.root / "masks/capture/frame_000000.jpg.png").read_bytes()
        applied = apply_object_box(self.root, 1, (0.2, 0.2, 0.8, 0.8))
        self.assertEqual(len(applied["superseded"]), 3)
        self.assertFalse((self.root / "masks").exists())
        self.assertEqual((self.root / "superseded/masks/capture/frame_000000.jpg.png").read_bytes(), mask)
        self.assertEqual(json.loads((self.root / "superseded/mask_authoring.json").read_text()), draft)
        saved = json.loads((self.root / "superseded/reconstruction_scope.json").read_text())
        self.assertEqual(saved["reconstruction_scope"]["mask_count"], 5)
        report = validate_and_report_scan(self.root).validation
        self.assertIsNone(report.reconstruction_scope)
        self.assertEqual(report.capture_mask_count, 0)
        self.assertFalse(needs_object_box(self.root))

    def test_unknown_photo_and_second_box_change_nothing(self):
        phone_scan(self.root, capture_outline=True)
        with self.assertRaisesRegex(ObjectBoxError, "not a photo"):
            apply_object_box(self.root, 99, (0.2, 0.2, 0.8, 0.8))
        self.assertTrue((self.root / "masks/capture").is_dir())
        apply_object_box(self.root, 1, (0.2, 0.2, 0.8, 0.8))
        first = (self.root / "metadata/mask_authoring.json").read_bytes()
        with self.assertRaisesRegex(ObjectBoxError, "already"):
            apply_object_box(self.root, 2, (0.1, 0.1, 0.9, 0.9))
        self.assertEqual((self.root / "metadata/mask_authoring.json").read_bytes(), first)

    def test_failure_while_saving_leaves_the_workspace_exactly_as_it_was(self):
        phone_scan(self.root, capture_outline=True, draft=True)

        def snapshot():
            return {str(path.relative_to(self.root)): path.read_bytes()
                    for path in sorted(self.root.rglob("*")) if path.is_file()}

        before = snapshot()
        from app import object_box
        real = object_box._write_json_atomic

        def no_space_for_manifest(path, payload):
            if path.name == ".manifest.json.box":
                raise OSError(28, "No space left on device")
            real(path, payload)

        with patch.object(object_box, "_write_json_atomic", side_effect=no_space_for_manifest), \
                self.assertRaisesRegex(ObjectBoxError, "unchanged"):
            apply_object_box(self.root, 1, (0.2, 0.2, 0.8, 0.8))
        self.assertEqual(snapshot(), before)
        self.assertFalse((self.root / "superseded").exists())
        validate_and_report_scan(self.root)
        apply_object_box(self.root, 1, (0.2, 0.2, 0.8, 0.8))
        self.assertIsNone(validate_and_report_scan(self.root).validation.reconstruction_scope)

    def test_workspace_check_refuses_an_existing_superseded_folder_and_links(self):
        phone_scan(self.root, capture_outline=True)
        check_object_box_workspace(self.root)
        (self.root / "metadata/manifest.json").rename(self.root / "manifest-real.json")
        (self.root / "metadata/manifest.json").symlink_to(self.root / "manifest-real.json")
        with self.assertRaisesRegex(ObjectBoxError, "unsafe"):
            check_object_box_workspace(self.root)
        (self.root / "metadata/manifest.json").unlink()
        (self.root / "manifest-real.json").rename(self.root / "metadata/manifest.json")
        (self.root / "superseded").mkdir()
        with self.assertRaisesRegex(ObjectBoxError, "already"):
            check_object_box_workspace(self.root)

    def test_checkpoint_round_trips_and_rejects_tampering(self):
        phone_scan(self.root)
        options = ObjectBoxOptions("detail", "retro", 2000)
        path = publish_object_box_checkpoint(self.root, options)
        self.assertEqual(load_object_box_checkpoint(self.root), options)
        with self.assertRaises(ObjectBoxError):
            load_object_box_checkpoint(self.root, "queued")
        publish_object_box_checkpoint(self.root, options, "queued")
        self.assertEqual(load_object_box_checkpoint(self.root, "queued"), options)
        with self.assertRaises(ObjectBoxError):
            load_object_box_checkpoint(self.root)
        path = publish_object_box_checkpoint(self.root, options)
        validate_and_report_scan(self.root)
        payload = json.loads(path.read_text())
        payload["options"]["preset"] = "ultra"
        path.write_text(json.dumps(payload))
        with self.assertRaises(ObjectBoxError):
            load_object_box_checkpoint(self.root)
        path.unlink()
        with self.assertRaises(ObjectBoxError):
            load_object_box_checkpoint(self.root)


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI runtime not installed")
class ObjectBoxAPITests(unittest.TestCase):
    def setUp(self):
        from app import main
        self.main = main
        self.enterContext(isolated_heavy_work())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for setting in ("INCOMING_DIR", "PROCESSING_DIR", "COMPLETED_DIR", "FAILED_DIR"):
            directory = self.root / setting.lower()
            directory.mkdir()
            self.enterContext(patch.object(main, setting, directory))
        self.jobs = JobStore(self.root / "jobs")
        self.enterContext(patch.object(main, "jobs", self.jobs))
        self.enterContext(patch.object(main, "ADMISSION_POLL_SECONDS", 0))

    def archive(self, scan_id="phone-job", **options):
        source = self.root / f"source-{scan_id}/capture"
        source.mkdir(parents=True)
        phone_scan(source, **options)
        archive = self.main.INCOMING_DIR / f"{scan_id}.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    output.write(path, path.relative_to(source.parent))
        self.jobs.create(scan_id)
        return archive

    def paused(self, scan_id="phone-job", **options):
        archive = self.archive(scan_id, **options)
        self.main.process_scan(scan_id, archive, True, True, object_preset="preview")
        return self.jobs.read(scan_id)

    def start(self, scan_id="phone-job", **payload):
        from fastapi import BackgroundTasks
        background = BackgroundTasks()
        body = {"frame_id": 1, "box": [0.2, 0.2, 0.8, 0.8], **payload}
        return self.main.start_object_from_box(scan_id, background, body), background

    def test_upload_without_a_box_pauses_without_taking_the_heavy_work_admission(self):
        with patch.object(self.main, "reserve_heavy_work", side_effect=AssertionError("admission")):
            job = self.paused(capture_outline=True)
        self.assertEqual((job.status, job.stage, job.image_count), ("processing", "awaiting_box", 5))
        self.assertIn("box", job.message)
        workspace = Path(job.outputs["package_dir"])
        self.assertEqual(workspace, self.main.PROCESSING_DIR / "phone-job")
        self.assertEqual(load_object_box_checkpoint(workspace / "capture"), ObjectBoxOptions())

    def test_photos_are_listed_and_served_reduced_for_drawing_the_box(self):
        self.paused()
        photos = self.main.list_scan_photos("phone-job")["photos"]
        self.assertEqual([photo["frame_id"] for photo in photos], [1, 2, 3, 4, 5])
        self.assertEqual((photos[0]["width"], photos[0]["height"]), (40, 60))
        response = self.main.get_scan_photo("phone-job", 1, max_size=64)
        self.assertEqual(response.media_type, "image/jpeg")
        with Image.open(BytesIO(response.body)) as image:
            self.assertEqual(image.size, (40, 60))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            self.main.get_scan_photo("phone-job", 77, max_size=64)
        self.assertEqual(caught.exception.status_code, 404)

    def test_box_starts_the_paused_job_and_the_job_completes(self):
        self.paused(capture_outline=True)
        claimed, background = self.start(preset="detail", export_style="retro", retro_triangles=1000)
        self.assertEqual((claimed.status, claimed.stage), ("processing", "reconstructing"))
        self.assertEqual(len(background.tasks), 1)
        seen = {}

        def reconstructed(package, preset, **kwargs):
            seen.update(preset=preset, retro=kwargs["retro_style"], scope=package.validation.reconstruction_scope)
            return {}

        with patch.object(self.main, "reconstruct_object", side_effect=reconstructed):
            background.tasks[0].func(*background.tasks[0].args)
        job = self.jobs.read("phone-job")
        self.assertEqual(job.status, "complete", job.message)
        self.assertEqual((seen["preset"], seen["retro"].triangle_budget, seen["scope"]), ("detail", 1000, None))
        scan = self.main.COMPLETED_DIR / "phone-job/capture"
        self.assertFalse(needs_object_box(scan))
        self.assertTrue((scan / "superseded/masks/capture").is_dir())
        steps = json.loads((scan / "metadata/processing.json").read_text())["steps"]
        self.assertEqual(steps["object_box"]["box"], [0.2, 0.2, 0.8, 0.8])

    def test_bad_box_keeps_the_job_paused_and_a_second_start_is_refused(self):
        from fastapi import HTTPException
        self.paused()
        for payload in ({"box": [0.5, 0.5, 0.4, 0.9]}, {"frame_id": 42}, {"preset": "ultra"}):
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as caught:
                self.start(**payload)
            self.assertEqual(caught.exception.status_code, 422)
            self.assertEqual(self.jobs.read("phone-job").stage, "awaiting_box")
        self.start()
        with self.assertRaises(HTTPException) as caught:
            self.start()
        self.assertEqual(caught.exception.status_code, 409)

    def test_losing_a_start_race_is_refused_without_touching_the_workspace(self):
        from fastapi import HTTPException
        from app.jobs import JobClaimError
        self.paused(capture_outline=True)
        with patch.object(self.jobs, "claim", side_effect=JobClaimError("claimed elsewhere")), \
                self.assertRaises(HTTPException) as caught:
            self.start()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue((self.main.PROCESSING_DIR / "phone-job/capture/masks/capture").is_dir())

    def test_box_that_cannot_be_saved_returns_the_job_to_its_pause_for_a_retry(self):
        from fastapi import HTTPException
        self.paused(capture_outline=True)
        with patch.object(self.main, "apply_object_box", side_effect=ObjectBoxError("unchanged")), \
                self.assertRaises(HTTPException) as caught:
            self.start()
        self.assertEqual(caught.exception.status_code, 500)
        job = self.jobs.read("phone-job")
        self.assertEqual((job.status, job.stage), ("processing", "awaiting_box"))
        claimed, background = self.start()
        self.assertEqual((claimed.stage, len(background.tasks)), ("reconstructing", 1))

    def test_unexpected_apply_failure_fails_the_job_and_preserves_the_workspace(self):
        from fastapi import HTTPException
        self.paused()
        with patch.object(self.main, "apply_object_box", side_effect=OSError("rename failed")), \
                self.assertRaises(HTTPException) as caught:
            self.start()
        self.assertEqual(caught.exception.status_code, 500)
        job = self.jobs.read("phone-job")
        self.assertEqual(job.status, "failed")
        self.assertTrue((self.main.FAILED_DIR / "phone-job/capture/images").is_dir())

    def test_package_that_already_has_a_superseded_folder_fails_before_asking_for_a_box(self):
        source = self.root / "source-phone-job/capture"
        source.mkdir(parents=True)
        phone_scan(source)
        (source / "superseded").mkdir()
        (source / "superseded/note.txt").write_text("from an earlier job copy")
        archive = self.main.INCOMING_DIR / "phone-job.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    output.write(path, path.relative_to(source.parent))
        self.jobs.create("phone-job")
        self.main.process_scan("phone-job", archive, True, True, object_preset="preview")
        job = self.jobs.read("phone-job")
        self.assertEqual(job.status, "failed")
        self.assertIn("already has an object box", job.message)

    def test_package_with_its_own_box_and_a_capture_outline_starts_without_a_pause(self):
        archive = self.archive(capture_outline=True, box=True)
        seen = {}

        def reconstructed(package, preset, **kwargs):
            seen["scope"] = package.validation.reconstruction_scope
            return {}

        with patch.object(self.main, "reconstruct_object", side_effect=reconstructed):
            self.main.process_scan("phone-job", archive, True, True, object_preset="preview")
        self.assertEqual(self.jobs.read("phone-job").status, "complete", self.jobs.read("phone-job").message)
        self.assertEqual(seen, {"scope": None})
        scan = self.main.COMPLETED_DIR / "phone-job/capture"
        frames = load_scan_metadata(scan / "metadata").frames
        _, box = single_box_selection(load_mask_authoring_plan(scan / "metadata", frames))
        self.assertEqual(box, (0.2, 0.2, 0.8, 0.8))
        self.assertTrue((scan / "superseded/masks/capture").is_dir())

    def test_scene_capture_fails_early_with_a_plain_reason(self):
        archive = self.archive(mode="scene_scan")
        self.main.process_scan("phone-job", archive, True, True, object_preset="preview")
        job = self.jobs.read("phone-job")
        self.assertEqual(job.status, "failed")
        self.assertIn("Scene mode", job.message)
        self.assertTrue((self.main.FAILED_DIR / "phone-job/capture/images").is_dir())

    def test_policy_routes_older_phone_object_uploads_to_the_box_step(self):
        archive = self.archive(capture_outline=True, draft=True)
        with patch.object(self.main, "OBJECT_UPLOAD_POLICY", "one_box"):
            self.main.process_scan("phone-job", archive, True, True, "auto_roi", False, True, "object_foreground")
        self.assertEqual(self.jobs.read("phone-job").stage, "awaiting_box")
        _, background = self.start()
        with patch.object(self.main, "reconstruct_object", return_value={}):
            background.tasks[0].func(*background.tasks[0].args)
        self.assertEqual(self.jobs.read("phone-job").status, "complete", self.jobs.read("phone-job").message)
        scan = self.main.COMPLETED_DIR / "phone-job/capture"
        self.assertEqual(json.loads((scan / "superseded/mask_authoring.json").read_text()), older_draft(scan))

    def test_policy_leaves_scene_uploads_on_their_requested_path(self):
        archive = self.archive(mode="scene_scan")
        with patch.object(self.main, "OBJECT_UPLOAD_POLICY", "one_box"), \
                patch.object(self.main, "_process_scan") as legacy:
            self.main.process_scan("phone-job", archive, True, True, "auto_roi", False, True, "scene_geometry")
        legacy.assert_called_once()
        prepared = legacy.call_args.kwargs["prepared"]
        self.assertEqual(prepared.processing_dir, self.main.PROCESSING_DIR / "phone-job")
        self.assertEqual(prepared.package.validation.scan_mode, "scene_scan")

    def test_without_the_policy_older_uploads_keep_their_requested_path(self):
        archive = self.archive()
        with patch.object(self.main, "_process_scan") as legacy:
            self.main.process_scan("phone-job", archive, True, True, "auto_roi", False, True, "scene_geometry")
        legacy.assert_called_once()
        self.assertIsNone(legacy.call_args.kwargs["prepared"])
        self.assertEqual(self.jobs.read("phone-job").stage, "received")

    def test_job_waits_for_a_busy_computer_then_starts_by_itself(self):
        self.paused()
        _, background = self.start()
        real = self.main.reserve_heavy_work
        attempts = []

        def busy_twice(label):
            attempts.append(label)
            if len(attempts) < 3:
                raise HeavyWorkBusy("Scanner is busy: another model. Wait for it to finish, then retry manually.")
            return real(label)

        messages = []
        update = self.jobs.update

        def record(scan_id, **values):
            messages.append(values.get("message"))
            return update(scan_id, **values)

        with patch.object(self.main, "reserve_heavy_work", side_effect=busy_twice), \
                patch.object(self.jobs, "update", side_effect=record), \
                patch.object(self.main, "reconstruct_object", return_value={}):
            background.tasks[0].func(*background.tasks[0].args)
        self.assertEqual(len(attempts), 3)
        waiting = [message for message in messages if message and message.startswith("Waiting")]
        self.assertEqual(waiting, ["Waiting to start automatically. Scanner is busy: another model."])
        self.assertEqual(self.jobs.read("phone-job").status, "complete")

    def test_wait_gives_up_with_a_recorded_failure_and_preserved_workspace(self):
        self.paused()
        _, background = self.start()
        with patch.object(self.main, "ADMISSION_WAIT_SECONDS", -1), \
                patch.object(self.main, "reserve_heavy_work", side_effect=HeavyWorkBusy("Scanner is busy.")):
            background.tasks[0].func(*background.tasks[0].args)
        job = self.jobs.read("phone-job")
        self.assertEqual((job.status, job.message), ("failed", "Scanner is busy."))
        self.assertTrue((self.main.FAILED_DIR / "phone-job/capture/metadata/mask_authoring.json").is_file())

    def test_permanent_admission_problem_fails_at_once_instead_of_waiting(self):
        self.paused()
        _, background = self.start()
        attempts = []

        def unavailable(label):
            attempts.append(label)
            raise HeavyWorkUnavailable("Cannot inspect legacy Scanner workers: systemctl is unavailable.")

        with patch.object(self.main, "reserve_heavy_work", side_effect=unavailable):
            background.tasks[0].func(*background.tasks[0].args)
        job = self.jobs.read("phone-job")
        self.assertEqual((job.status, len(attempts)), ("failed", 1))
        self.assertIn("Cannot inspect", job.message)

    def test_job_queued_behind_another_resumes_after_a_backend_restart(self):
        from app.job_recovery import reconcile_interrupted_jobs
        self.paused(capture_outline=True)
        self.start(preset="detail")
        self.jobs.update("phone-job", status="processing", stage="reconstructing",
                         message="Waiting to start automatically.")
        queued = self.main.queued_object_jobs()
        self.assertEqual(queued, {"phone-job": ObjectBoxOptions("detail")})
        recovered, = reconcile_interrupted_jobs(
            self.jobs, processing_dir=self.main.PROCESSING_DIR,
            completed_dir=self.main.COMPLETED_DIR, failed_dir=self.main.FAILED_DIR, queued=set(queued),
        )
        self.assertEqual((recovered.status, recovered.stage), ("processing", "reconstructing"))
        seen = {}

        def reconstructed(package, preset, **kwargs):
            # Once admitted, the job must no longer look restartable.
            seen.update(preset=preset, queued=self.main.queued_object_jobs())
            return {}

        with patch.object(self.main, "reconstruct_object", side_effect=reconstructed):
            self.main.run_object_job("phone-job", queued["phone-job"])
        self.assertEqual(seen, {"preset": "detail", "queued": {}})
        self.assertEqual(self.jobs.read("phone-job").status, "complete")

    def test_interrupted_running_job_is_not_mistaken_for_a_queued_one(self):
        from app.job_recovery import reconcile_interrupted_jobs
        self.paused()
        _, background = self.start()

        def interrupted(package, preset, **kwargs):
            raise KeyboardInterrupt

        with patch.object(self.main, "reconstruct_object", side_effect=interrupted), \
                self.assertRaises(KeyboardInterrupt):
            background.tasks[0].func(*background.tasks[0].args)
        self.assertEqual(self.main.queued_object_jobs(), {})
        recovered, = reconcile_interrupted_jobs(
            self.jobs, processing_dir=self.main.PROCESSING_DIR,
            completed_dir=self.main.COMPLETED_DIR, failed_dir=self.main.FAILED_DIR,
        )
        self.assertEqual(recovered.status, "failed")
        self.assertIn("restarted", recovered.message)

    def test_paused_job_survives_a_backend_restart(self):
        from app.job_recovery import reconcile_interrupted_jobs
        self.paused()
        recovered, = reconcile_interrupted_jobs(
            self.jobs, processing_dir=self.main.PROCESSING_DIR,
            completed_dir=self.main.COMPLETED_DIR, failed_dir=self.main.FAILED_DIR,
        )
        self.assertEqual((recovered.status, recovered.stage), ("processing", "awaiting_box"))
        claimed, _ = self.start()
        self.assertEqual(claimed.stage, "reconstructing")


if __name__ == "__main__":
    unittest.main()
