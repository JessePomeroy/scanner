import json
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zlib
from unittest.mock import patch

from desktop import blender
from desktop.monitor import build_snapshot
from desktop.resume import sha256


def setUpModule():
    from heavy_work_fixture import isolated_heavy_work
    unittest.enterModuleContext(isolated_heavy_work())


class BlenderFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='scanner-blender-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'recovery'
        self.run.mkdir()
        self.source = self.root / 'dense'
        self.source.mkdir()
        self.state = {'status': 'succeeded', 'stages': [{'name': 'texture_mesh', 'status': 'succeeded'}]}
        (self.run / 'state.json').write_text(json.dumps(self.state))
        (self.run / 'plan.json').write_text(json.dumps({'workspace': str(self.source)}))
        (self.source / 'scene_textured.obj').write_text(
            'mtllib scene_textured.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\n'
            'vt 0 0\nvt 1 0\nvt 0 1\nusemtl surface\nf 1/1 2/2 3/3\n')
        (self.source / 'scene_textured.mtl').write_text('newmtl surface\nKd 1 1 1\nmap_Kd texture.png\n')
        def chunk(kind, data):
            return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
        (self.source / 'texture.png').write_bytes(b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('!IIBBBBB', 1, 1, 8, 6, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(b'\x00\x00\xff\x00\xff')) + chunk(b'IEND', b''))
        tool = shutil.which('blender') or sys.executable
        for guard in (patch.object(blender, 'LOCK_ROOT', self.root / 'locks'),
                      patch.object(blender.shutil, 'which', return_value=tool)):
            guard.start()
            self.addCleanup(guard.stop)
        self.unit = 'scanner-reconstruct-fixture.service'
        self.service = patch.object(blender, 'service_properties', return_value={'ActiveState': 'inactive'})
        self.service.start()
        self.addCleanup(self.service.stop)
        for name in ('check_other_workers', 'check_memory'):
            guard = patch.object(blender, name)
            guard.start()
            self.addCleanup(guard.stop)

    def launch(self, retry=False):
        with patch.object(blender.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stderr='')) as call:
            attempt = blender.launch(self.run, self.unit, retry=retry)
        args = call.call_args.args[0]
        self.assertIn('--property=MemoryMax=48G', args)
        self.assertIn('--property=MemorySwapMax=0', args)
        return attempt

    def test_auto_preference_is_per_run_and_preserves_attempt_record(self):
        blender.set_automatic(self.run, True)
        attempt = self.launch()
        blender.set_automatic(self.run, False)
        self.assertEqual(blender.configuration(self.run), {'enabled': False, 'attempt': attempt.name})
        other = self.root / 'other'
        other.mkdir()
        self.assertEqual(blender.configuration(other), {})

    def test_duplicate_launch_is_blocked_and_retry_keeps_previous_outputs(self):
        first = self.launch()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.launch()
        (first / 'state.json').write_text(json.dumps({'status': 'failed'}))
        partial = first / 'output/partial.blend'
        partial.write_bytes(b'preserved')
        second = self.launch(retry=True)
        self.assertNotEqual(first, second)
        self.assertEqual(partial.read_bytes(), b'preserved')

    def test_unfinished_source_and_unsafe_texture_reference_are_rejected(self):
        (self.run / 'state.json').write_text(json.dumps({'status': 'running'}))
        with self.assertRaisesRegex(ValueError, 'successful texture stage'):
            self.launch()
        (self.run / 'state.json').write_text(json.dumps(self.state))
        (self.source / 'scene_textured.mtl').write_text('map_Kd ../outside.png\n')
        with self.assertRaisesRegex(ValueError, 'stay in the reconstruction'):
            self.launch()
        self.assertEqual(list(self.run.glob('blender-*')), [])

    def test_worker_refuses_changed_source_before_running_blender(self):
        attempt = self.launch()
        (self.source / 'scene_textured.obj').write_text('changed')
        with patch.object(blender.subprocess, 'run') as call:
            with self.assertRaisesRegex(ValueError, 'changed before'):
                blender.execute(attempt)
            call.assert_not_called()
        self.assertEqual(json.loads((attempt / 'state.json').read_text())['status'], 'failed')

    def test_worker_refuses_overwrite(self):
        attempt = self.launch()
        old = attempt / 'output/scan.blend'
        old.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'not empty'):
            blender.execute(attempt)
        self.assertEqual(old.read_bytes(), b'keep')

    def test_launch_error_is_persistent_and_does_not_claim_reconstruction_failure(self):
        blender.set_automatic(self.run, True)
        blender.record_launch_error(self.run, 'Not enough available memory')
        source = build_snapshot(self.run, self.state, {}, {'ActiveState': 'inactive'}, '', None, '', 0)
        value = blender.follow_snapshot(self.run, source)
        self.assertEqual(value.phase, 'blender')
        self.assertEqual(value.status, 'failed')
        self.assertIn('memory', value.error)
        self.assertEqual(json.loads((self.run / 'state.json').read_text()), self.state)

    def test_automatic_preparation_waits_for_worker_exit_after_success_record(self):
        blender.set_automatic(self.run, True)
        for active, expected in [('active', False), ('deactivating', False), ('inactive', True)]:
            source = build_snapshot(self.run, self.state, {}, {'ActiveState': active}, '', None, '', 0)
            value = blender.follow_snapshot(self.run, source)
            self.assertEqual(value.can_prepare_blend, expected)
            self.assertNotIn('error', blender.configuration(self.run))

    @unittest.skipUnless(shutil.which('blender'), 'Requires native Blender')
    def test_native_conversion_reopens_with_packed_texture_and_keeps_inputs(self):
        before = {p.name: sha256(p) for p in self.source.iterdir()}
        attempt = self.launch()
        blender.execute(attempt)
        state = json.loads((attempt / 'state.json').read_text())
        report = json.loads((attempt / 'verification.json').read_text())
        self.assertEqual(state['status'], 'succeeded')
        self.assertTrue(report['verified'])
        self.assertEqual(report['faces'], 1)
        self.assertEqual(report['textures'], {'texture.png': [1, 1]})
        self.assertTrue(report['material_texture_links_verified'])
        self.assertTrue(report['active_uvs_verified'])
        self.assertEqual(before, {p.name: sha256(p) for p in self.source.iterdir()})
        self.assertTrue((attempt / 'output/scan.blend').is_file())

    @unittest.skipUnless(shutil.which('blender'), 'Requires native Blender')
    def test_corrupt_texture_cannot_produce_a_ready_result(self):
        (self.source / 'texture.png').write_bytes(b'not an image')
        attempt = self.launch()
        with self.assertRaisesRegex(ValueError, 'Texture decoding failed'):
            blender.execute(attempt)
        state = json.loads((attempt / 'state.json').read_text())
        self.assertEqual(state['status'], 'failed')
        self.assertNotIn('blend', state)

    @unittest.skipUnless(shutil.which('blender'), 'Requires native Blender')
    def test_disconnected_packed_texture_fails_reopen_verification(self):
        attempt = self.launch()
        blender.execute(attempt)
        original = attempt / 'output/scan.blend'
        damaged = self.root / 'disconnected.blend'
        script = self.root / 'disconnect.py'
        script.write_text(
            'import bpy\n'
            f'bpy.ops.wm.open_mainfile(filepath={str(original)!r})\n'
            'for material in bpy.data.materials:\n'
            '    if material.node_tree:\n'
            '        for link in list(material.node_tree.links):\n'
            '            if link.from_node.type == "TEX_IMAGE":\n'
            '                material.node_tree.links.remove(link)\n'
            f'bpy.ops.wm.save_as_mainfile(filepath={str(damaged)!r})\n')
        executable = shutil.which('blender')
        common = [executable, '--background', '--factory-startup', '--disable-autoexec', '--threads', '1', '--python-exit-code', '1', '--python']
        subprocess.run(common + [str(script)], check=True, capture_output=True, timeout=30)
        result = subprocess.run(common + [str(blender.VERIFY), '--', str(damaged),
                                         str(self.root / 'invalid-verification.json'), 'texture.png'],
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not connected', result.stdout + result.stderr)
        self.assertFalse((self.root / 'invalid-verification.json').exists())


if __name__ == '__main__':
    unittest.main()
