from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop.features import delivery_checks, diagnostic_summary, validate_selection
from desktop.monitor import build_snapshot


class FeatureTests(unittest.TestCase):
    def test_selects_matching_service_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            for name in ['state.json', 'plan.json']:
                (run / name).write_text('{}')
            response = SimpleNamespace(returncode=0, stdout=f'LoadState=loaded\nExecStart=argv[]=/usr/bin/python {run}/runner.py ;\n')
            with patch('desktop.features.subprocess.run', return_value=response) as command:
                self.assertEqual(validate_selection(run, 'scanner-reconstruct-test.service'), (run, 'scanner-reconstruct-test.service'))
                self.assertEqual(command.call_args.args[0][:3], ['systemctl', '--user', 'show'])
                response.stdout = 'LoadState=loaded\nExecStart=/wrong/run.py\n'
                with self.assertRaisesRegex(ValueError, 'does not belong'):
                    validate_selection(run, 'scanner-reconstruct-test.service')

    def test_processing_does_not_imply_delivery_or_visual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            checks = delivery_checks(output, 'succeeded')
            self.assertIn('missing OBJ', checks[1])
            self.assertIn('not found', checks[2])
            for name in ['scene_textured.obj', 'a.blend', 'a.glb', 'texture.jpg']:
                (output / name).write_bytes(b'fixture')
            (output / 'scene_textured.mtl').write_text('map_Kd texture.jpg\n')
            checks = delivery_checks(output, 'succeeded')
            self.assertIn('pixels not validated', checks[1])
            self.assertIn('not validated', checks[2])
            self.assertIn('manual review required', checks[3])
            (output / 'scene_textured.mtl').write_text('map_Kd ../outside.jpg\n')
            self.assertIn('unsafe', delivery_checks(output, 'succeeded')[1])

    def test_running_job_never_checks_partial_output_as_complete(self):
        checks = delivery_checks(Path('/not/available'), 'running')
        self.assertIn('not checked', checks[1])

    def test_summary_excludes_raw_logs_and_redacts_common_credentials(self):
        value = build_snapshot(Path('/tmp/run'), {'status': 'failed', 'error': 'token=secret1 Bearer secret2 https://user:pass@host/'},
                               {}, {'ActiveState': 'failed'}, 'private raw log', None, 'Unavailable', 0)
        summary = diagnostic_summary(value, Path('/tmp/run'), 'scanner-reconstruct-test.service', ('Visual quality: manual review required',))
        for private in ['secret1', 'secret2', 'user:pass', 'private raw log']:
            self.assertNotIn(private, summary)
        self.assertIn('/tmp/run/logs', summary)
        self.assertIn('review before sharing', summary)


if __name__ == '__main__':
    unittest.main()
