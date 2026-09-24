import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop import resume


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'recovery-r003'
        self.run.mkdir()
        self.source = self.root / 'dense'
        self.source.mkdir()
        (self.source / 'images').mkdir()
        (self.source / 'images/a.jpg').write_bytes(b'fixture-image')
        (self.source / 'scene.mvs').write_bytes(b'fixture-calibration')
        (self.source / 'scene_mesh.ply').write_bytes(b'ply\nformat ascii 1.0\nelement vertex 3\nelement face 1\nend_header\n0 0 0\n')
        self.tool = self.root / 'TextureMesh'
        self.tool.write_text('#!/usr/bin/python\nimport pathlib,sys\np=pathlib.Path(sys.argv[sys.argv.index("-o")+1])\np.with_suffix(".obj").write_text("mtllib scene_textured.mtl\\n")\np.with_suffix(".mtl").write_text("newmtl fixture\\nmap_Kd texture.jpg\\n")\n(p.parent/"texture.jpg").write_bytes(b"fixture pixels")\n')
        self.tool.chmod(0o700)
        self.addCleanup(patch.stopall)
        patch.object(resume, 'TOOL', self.tool).start()
        self.service = patch.object(resume, 'check_service').start()
        self.others = patch.object(resume, 'check_other_workers').start()
        patch.object(resume, 'check_memory').start()
        self.state = {'status': 'failed', 'current_stage': 'texture_mesh',
                      'stages': [{'name': 'reconstruct_mesh', 'status': 'succeeded'}]}
        self.plan = {'workspace': str(self.source), 'stages': [{'name': 'texture_mesh', 'command': resume.command(self.source, self.source)}],
                     'binaries': {str(self.tool): resume.sha256(self.tool)},
                     'input_artifacts': {'scene.mvs': {'sha256': resume.sha256(self.source / 'scene.mvs')}}}
        self.write_evidence()

    def write_evidence(self):
        (self.run / 'state.json').write_text(json.dumps(self.state))
        (self.run / 'plan.json').write_text(json.dumps(self.plan))

    def preview(self):
        return resume.preview(self.run, 'scanner-reconstruct-fixture.service')

    def launch(self, approved):
        with patch.object(resume.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stderr='')) as run:
            result = resume.launch(approved)
            args = run.call_args.args[0]
            self.assertIn('--property=MemoryMax=48G', args)
            self.assertIn('--property=MemorySwapMax=0', args)
            self.assertNotIn('restart', args)
            return result

    def test_preview_is_read_only_and_pins_all_inputs(self):
        before = set(self.root.rglob('*'))
        value = self.preview()
        self.assertEqual(value['images'], 1)
        self.assertEqual(len(value['input_sha256']), 3)
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_live_or_competing_service_blocks_before_reading_files(self):
        self.service.side_effect = resume.ResumeBlocked('active')
        with self.assertRaisesRegex(resume.ResumeBlocked, 'active'):
            self.preview()
        self.service.side_effect = None
        self.others.side_effect = resume.ResumeBlocked('competing')
        with self.assertRaisesRegex(resume.ResumeBlocked, 'competing'):
            self.preview()

    def test_unsupported_stage_and_completed_texture_are_rejected(self):
        self.state['current_stage'] = 'stereo_fusion'
        self.write_evidence()
        with self.assertRaisesRegex(resume.ResumeBlocked, 'Only texturing'):
            self.preview()
        self.state['current_stage'] = 'texture_mesh'
        self.state['stages'].append({'name': 'texture_mesh', 'status': 'succeeded'})
        self.write_evidence()
        with self.assertRaisesRegex(resume.ResumeBlocked, 'already succeeded'):
            self.preview()

    def test_missing_mesh_and_changed_binary_are_rejected(self):
        self.tool.write_text('changed')
        with self.assertRaisesRegex(resume.ResumeBlocked, 'binary changed'):
            self.preview()

    def test_arbitrary_saved_command_is_never_executed(self):
        self.plan['stages'][0]['command'] = ['/bin/sh', '-c', 'false']
        self.write_evidence()
        with self.assertRaisesRegex(resume.ResumeBlocked, 'outside the supported'):
            self.preview()

    def test_confirmation_becomes_invalid_when_an_image_changes(self):
        approved = self.preview()
        (self.source / 'images/a.jpg').write_bytes(b'changed-image')
        with self.assertRaisesRegex(resume.ResumeBlocked, 'changed after confirmation'):
            self.launch(approved)
        self.assertEqual(list(self.root.glob('panel-resume-*')), [])

    def test_duplicate_workspace_lock_blocks_launch(self):
        approved = self.preview()
        with resume.attempt_lock(self.root):
            with self.assertRaisesRegex(resume.ResumeBlocked, 'holds the workspace lock'):
                self.launch(approved)

    def test_fresh_attempt_executes_only_texture_and_preserves_originals(self):
        approved = self.preview()
        (self.source / 'scene_textured.obj').write_text('previous partial output')
        attempt, unit = self.launch(approved)
        self.assertNotEqual(attempt, self.run)
        self.assertEqual(json.loads((attempt / 'state.json').read_text())['status'], 'queued')
        resume.execute(attempt)
        state = json.loads((attempt / 'state.json').read_text())
        self.assertEqual(state['status'], 'succeeded')
        self.assertEqual([stage['name'] for stage in state['stages']], ['texture_mesh'])
        self.assertEqual((self.source / 'scene_textured.obj').read_text(), 'previous partial output')
        self.assertEqual((attempt / 'output/scene_textured.obj').read_text(), 'mtllib scene_textured.mtl\n')
        for name, digest in approved['input_sha256'].items():
            self.assertEqual(resume.sha256(Path(name)), digest)
        self.assertEqual(json.loads((self.root / '.panel-latest.json').read_text())['unit'], unit)

    def test_worker_rechecks_inputs_and_never_overwrites_retry_output(self):
        attempt, _ = self.launch(self.preview())
        (attempt / 'output/keep.txt').write_text('preserve')
        with self.assertRaisesRegex(resume.ResumeBlocked, 'refusing to overwrite'):
            resume.execute(attempt)
        self.assertEqual((attempt / 'output/keep.txt').read_text(), 'preserve')
        self.assertEqual(json.loads((attempt / 'state.json').read_text())['status'], 'failed')

    def test_worker_failure_is_recorded_and_log_preserved(self):
        self.tool.write_text('#!/usr/bin/python\nimport sys\nprint("fixture failure")\nsys.exit(9)\n')
        self.plan['binaries'][str(self.tool)] = resume.sha256(self.tool)
        self.write_evidence()
        attempt, _ = self.launch(self.preview())
        with self.assertRaisesRegex(resume.ResumeBlocked, 'exited 9'):
            resume.execute(attempt)
        self.assertIn('fixture failure', (attempt / 'logs/texture_mesh.log').read_text())
        self.assertEqual(json.loads((attempt / 'state.json').read_text())['stages'][0]['return_code'], 9)

    def test_symlink_images_are_rejected(self):
        (self.source / 'images/link.jpg').symlink_to(self.source / 'images/a.jpg')
        with self.assertRaisesRegex(resume.ResumeBlocked, 'Image links'):
            self.preview()

    def test_service_query_fails_closed_and_checks_identity(self):
        # Test the real adapter outside the setUp patch.
        patch.stopall()
        for stdout in ['LoadState=loaded\nActiveState=active\n',
                       'LoadState=not-found\nActiveState=inactive\n',
                       'LoadState=loaded\nActiveState=failed\nExecStart=/wrong/run.py\n']:
            with patch.object(resume.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=stdout)):
                with self.assertRaises(resume.ResumeBlocked):
                    resume.check_service(self.run, 'scanner-reconstruct-fixture.service')


if __name__ == '__main__':
    unittest.main()
