from __future__ import annotations

from io import BytesIO
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"backend"))
from app.jobs import JobStore
from app.mask_generator import generate_mask_proposals
from app.object_pipeline import OBJECT_PRESETS, reconstruct_object
from app.openmvs_runner import OpenMVSConfig, build_openmvs_commands
from app.sam2_generator import Sam2Runtime
from app.scan_metadata import load_scan_metadata
from app.scan_package import validate_and_report_scan
from heavy_work_fixture import isolated_heavy_work
from test_sam2_generator import fake_worker, single_box_scan


class ObjectPipelineInputTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_heavy_work())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        single_box_scan(self.root)

    def test_rejects_scene_mode_before_model_or_native_work(self):
        path = self.root/"metadata/session.json"
        session = json.loads(path.read_text()); session["scan_mode"] = "scene_scan"
        path.write_text(json.dumps(session))
        package = validate_and_report_scan(self.root)
        with patch("app.object_pipeline.generate_mask_proposals") as generate, self.assertRaisesRegex(ValueError,"Object scan"):
            reconstruct_object(package,"preview")
        generate.assert_not_called()

    def test_rejects_existing_native_workspace_without_overwriting_it(self):
        (self.root/"dense").mkdir()
        evidence = self.root/"dense/keep.txt"
        evidence.write_text("user-owned result")
        with self.assertRaisesRegex(ValueError,"fresh"):
            reconstruct_object(validate_and_report_scan(self.root),"preview")
        self.assertEqual(evidence.read_text(),"user-owned result")

    def test_legacy_polygon_plan_is_not_silently_reinterpreted_as_a_box(self):
        path = self.root/"metadata/mask_authoring.json"
        plan = json.loads(path.read_text()); plan.update(schema_version="1.0",authoring_mode="representative_frames")
        path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError,"single_box"):
            reconstruct_object(validate_and_report_scan(self.root),"preview")

    def test_presets_keep_the_measured_resolution_and_point_budgets(self):
        self.assertEqual((OBJECT_PRESETS["preview"].prepared_image_max_size,OBJECT_PRESETS["preview"].dense_max_resolution),(1600,960))
        self.assertEqual((OBJECT_PRESETS["detail"].prepared_image_max_size,OBJECT_PRESETS["detail"].dense_max_resolution),(3200,1920))
        self.assertEqual(OBJECT_PRESETS["preview"].point_hard_limit,1_000_000)
        self.assertEqual(OBJECT_PRESETS["detail"].point_hard_limit,3_000_000)

    def test_native_mesh_and_refine_handoffs_do_not_require_optional_mesh_archives(self):
        for refine in (False,True):
            with self.subTest(refine=refine):
                commands = build_openmvs_commands(self.root,OpenMVSConfig(include_refine=refine))
                texture = commands[-1]
                self.assertEqual(texture[1],str(self.root/"dense/scene_dense.mvs"))
                mesh = "scene_mesh_refined.ply" if refine else "scene_mesh.ply"
                self.assertEqual(texture[texture.index("-m")+1],str(self.root/"dense"/mesh))
                if refine:
                    command = commands[-2]
                    self.assertEqual(command[1],str(self.root/"dense/scene_dense.mvs"))
                    self.assertEqual(command[command.index("--mesh-file")+1],str(self.root/"dense/scene_mesh.ply"))

    def test_pipeline_serializes_density_evidence_and_reaches_texturing(self):
        frames = load_scan_metadata(self.root/"metadata").frames
        with patch.object(Sam2Runtime, "validate"), patch("app.sam2_generator.subprocess.run", side_effect=fake_worker):
            generation = generate_mask_proposals(self.root, frames)

        def native_command(command, **kwargs):
            if command[0] == "DensifyPointCloud":
                (self.root/"dense/scene_dense.ply").write_text(
                    "ply\nformat ascii 1.0\nelement vertex 300000\nend_header\n"
                )
            return SimpleNamespace(returncode=0)

        patches = {
            "generate_mask_proposals": {"return_value":generation},
            "prepare_colmap_sparse_execution": {"return_value":([["colmap","matching"],["colmap","mapping"]],[])},
            "record_colmap_intake": {"return_value":{"registered_image_count":len(frames)}},
            "convert_capture_mask_set": {"return_value":SimpleNamespace(as_dict=lambda: {"mask_count":len(frames)})},
            "validate_openmvs_config_masks": {},
            "stage_openmvs_texture_masks": {},
            "write_texture_report": {"return_value":{"warnings":[]}},
        }
        for name, options in patches.items():
            self.enterContext(patch(f"app.object_pipeline.{name}", **options))
        with patch("app.object_pipeline.subprocess.run", side_effect=native_command):
            outputs = reconstruct_object(validate_and_report_scan(self.root), "preview")

        steps = json.loads((self.root/"metadata/processing.json").read_text())["steps"]
        self.assertEqual(steps["density_budget"]["path"],str(self.root/"dense/scene_dense.ply"))
        self.assertEqual(steps["density_budget"]["point_count"],300000)
        self.assertTrue(steps["density_budget"]["warning"])
        self.assertEqual(steps["automatic_object"]["state"],"succeeded")
        self.assertEqual(steps["automatic_object"]["commands"][-1]["phase"],"TextureMesh")
        self.assertIn("textured_mesh",outputs)


@unittest.skipUnless(importlib.util.find_spec("fastapi"),"FastAPI runtime not installed")
class ObjectUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app import main
        self.main = main
        self.enterContext(isolated_heavy_work())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for setting in ("INCOMING_DIR","PROCESSING_DIR","COMPLETED_DIR","FAILED_DIR"):
            directory = self.root/setting.lower(); directory.mkdir()
            self.enterContext(patch.object(main,setting,directory))
        self.jobs = JobStore(self.root/"jobs")
        self.enterContext(patch.object(main,"jobs",self.jobs))

    async def upload(self, **options):
        from fastapi import BackgroundTasks, UploadFile
        background = BackgroundTasks()
        arguments = dict(run_reconstruction=True,run_dense=False,run_openmvs=False,
                         scope_mode="auto_roi",use_masks=False,mask_profile="scene_geometry",
                         review_scope=False,object_preset="preview")
        arguments.update(options)
        result = await self.main.upload_scan(background,UploadFile(file=BytesIO(b"upload fixture")),**arguments)
        return result,background

    async def test_object_preset_queues_complete_pipeline_without_manual_review_flags(self):
        result, background = await self.upload()
        self.assertEqual(result.stage,"queued")
        self.assertEqual(len(background.tasks),1)
        self.assertEqual(background.tasks[0].args[2:4],(True,True))
        self.assertEqual(background.tasks[0].args[-1],"preview")

    async def test_requires_explicit_reconstruction_and_rejects_conflicting_policies(self):
        from fastapi import HTTPException
        for options in ({"run_reconstruction":False},{"review_scope":True},{"use_masks":True},
                        {"mask_profile":"object_foreground"},{"scope_mode":"unbounded"}):
            with self.subTest(options=options), self.assertRaises(HTTPException) as caught:
                await self.upload(**options)
            self.assertEqual(caught.exception.status_code,400)
        self.assertEqual(self.jobs.list(),[])

    def input_archive(self):
        source = self.root/"capture"; source.mkdir()
        single_box_scan(source)
        archive = self.main.INCOMING_DIR/"object-api.zip"
        with zipfile.ZipFile(archive,"w") as output:
            for path in source.rglob("*"):
                if path.is_file():
                    output.write(path,path.relative_to(self.root))
        self.jobs.create("object-api")
        return archive

    async def test_object_failure_preserves_archive_and_partial_package(self):
        archive = self.input_archive()
        original = archive.read_bytes()
        with patch.object(self.main,"reconstruct_object",side_effect=RuntimeError("SAM2 failed safely")):
            self.main.process_scan("object-api",archive,True,True,object_preset="preview")
        job = self.jobs.read("object-api")
        self.assertEqual(job.status,"failed")
        self.assertIn("SAM2 failed safely",job.message)
        self.assertTrue((Path(job.outputs["package_dir"])/"capture/images/frame_000000.jpg").is_file())
        self.assertEqual(archive.read_bytes(),original)

    async def test_object_success_uses_portable_delivery_and_rebases_artifact_paths(self):
        archive = self.input_archive()
        def reconstructed(package,preset,**kwargs):
            dense = package.scan_root/"dense"; dense.mkdir()
            Image.new("RGB",(8,8),(160,120,80)).save(dense/"texture.jpg")
            (dense/"scene_textured.mtl").write_text("newmtl surface\nmap_Kd texture.jpg\n")
            mesh = dense/"scene_textured.obj"
            mesh.write_text("mtllib scene_textured.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\nusemtl surface\nf 1/1 2/2 3/3\n")
            return {"textured_mesh":str(mesh)}
        with patch.object(self.main,"reconstruct_object",side_effect=reconstructed):
            self.main.process_scan("object-api",archive,True,True,object_preset="preview")
        job = self.jobs.read("object-api")
        self.assertEqual(job.status,"complete",job.message)
        self.assertFalse((self.main.PROCESSING_DIR/"object-api").exists())
        for name,path in job.outputs.items():
            self.assertTrue(Path(path).is_relative_to(self.main.COMPLETED_DIR),name)
        with zipfile.ZipFile(job.outputs["textured_bundle"]) as bundle:
            self.assertTrue(any(name.endswith(".obj") for name in bundle.namelist()))
            self.assertTrue(any(name.endswith(".mtl") for name in bundle.namelist()))
            self.assertTrue(any(name.endswith(".jpg") for name in bundle.namelist()))


if __name__ == "__main__":
    unittest.main()
