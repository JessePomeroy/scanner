from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop.monitor import (LIMIT, build_snapshot, collect, duration, effective_status,
                             log_tail, parse_progress, read_record, service_properties)


class MonitorTests(unittest.TestCase):
    def test_substep_completion_does_not_claim_overall_completion(self):
        text = 'Initialized views 196 (100%, 1m56s)\nAssigning the best view completed: 12513003 faces'
        self.assertIsNone(parse_progress(text, 3).percent)
        self.assertIsNone(parse_progress('Initialized views 196 (100%, 1m56s)', 3).percent)

    def test_live_percentage_and_eta_are_operation_scoped(self):
        value = parse_progress('Points weighted 100 (25.50%, 1m, ETA 3m)...', 2)
        self.assertEqual(value.percent, 25.5)
        self.assertEqual(value.operation, 'Points weighted 100')
        self.assertEqual(value.eta, '3m for this operation only')

    def test_stale_percentage_and_eta_are_not_live(self):
        value = parse_progress('Points weighted 100 (25%, 1m, ETA 3m)...', 300)
        self.assertIsNone(value.percent)
        self.assertIn('Unavailable', value.eta)

    def test_fusion_counts_finished_not_started_images(self):
        value = parse_progress('I2026 fusion.cc:283] Fusing image [7/196] with index 52', 2)
        self.assertAlmostEqual(value.percent, 6 / 196 * 100)
        self.assertIn('Image 7 of 196', value.operation)

    def test_missing_or_unknown_service_cannot_confirm_success(self):
        self.assertEqual(effective_status('succeeded', {}), 'unknown')
        self.assertEqual(effective_status('running', {'ActiveState': 'inactive'}), 'interrupted')
        self.assertEqual(effective_status('running', {'ActiveState': 'failed'}), 'failed')
        self.assertEqual(effective_status('succeeded', {'ActiveState': 'inactive'}), 'succeeded')
        self.assertEqual(effective_status('succeeded', {'ActiveState': 'failed'}), 'failed')

    def test_service_is_read_only_and_unit_is_validated(self):
        with patch('desktop.monitor.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'ActiveState=active\nMemoryCurrent=50\n'
            self.assertEqual(service_properties('scanner-test.service')['ActiveState'], 'active')
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ['systemctl', '--user', 'show'])
            with self.assertRaises(ValueError):
                service_properties('--all')
            self.assertEqual(run.call_count, 1)

    def test_bounded_json_and_log_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input'
            path.write_text('[]')
            with self.assertRaises(ValueError):
                read_record(path)
            path.write_text('x' * (LIMIT + 1))
            with self.assertRaises(ValueError):
                read_record(path)
            path.write_text('old\n' * LIMIT + 'step 1\rstep 2\n')
            tail = log_tail(path)
            self.assertLessEqual(len(tail), LIMIT)
            self.assertTrue(tail.endswith('step 1\nstep 2'))

    def test_elapsed_stops_at_recorded_finish(self):
        state = {'status': 'succeeded', 'started_at': '2026-09-24T12:00:00+00:00',
                 'finished_at': '2026-09-24T13:00:00+00:00', 'current_stage': 'texture_mesh',
                 'stages': [{'name': 'texture_mesh', 'status': 'succeeded',
                             'started_at': '2026-09-24T12:30:00+00:00'}]}
        now = datetime(2026, 9, 24, 15, tzinfo=timezone.utc).timestamp()
        value = build_snapshot(Path('/tmp/run'), state, {'stages': [{}]},
                               {'ActiveState': 'inactive'}, '', None, 'Unavailable', now)
        self.assertEqual(value.elapsed, '1h 00m')
        self.assertEqual(value.stage_elapsed, '30m 00s')
        self.assertEqual(value.completed, '1 of 1 stages completed in this attempt')

    def test_corrupt_state_does_not_crash_or_report_running(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'state.json').write_text('{')
            (path / 'plan.json').write_text('{}')
            with patch('desktop.monitor.service_properties', return_value={'ActiveState': 'active'}), \
                 patch('desktop.monitor.gpu_memory', return_value='Unavailable'):
                value = collect(path, 'scanner-test.service')
            self.assertEqual(value.status, 'unknown')
            self.assertIn('Cannot read state.json', value.error)

    def test_stage_path_cannot_escape_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'state.json').write_text(json.dumps({'status': 'running', 'current_stage': '../../elsewhere'}))
            (path / 'plan.json').write_text('{}')
            (path / 'service.log').write_text('safe fallback')
            with patch('desktop.monitor.service_properties', return_value={'ActiveState': 'active'}), \
                 patch('desktop.monitor.gpu_memory', return_value='Unavailable'):
                value = collect(path, 'scanner-test.service')
            self.assertEqual(value.log, 'safe fallback')

    def test_missing_memory_does_not_show_uint64_sentinel(self):
        value = build_snapshot(Path('/tmp/run'), {}, {},
                               {'ActiveState': 'failed', 'MemoryCurrent': str(2**64-1), 'MemoryMax': 'infinity'},
                               '', None, 'Unavailable', 0)
        self.assertEqual(value.memory, 'Unavailable')
        self.assertIn('Service failed', value.error)

    def test_duration(self):
        self.assertEqual(duration(3661), '1h 01m')
        self.assertEqual(duration(None), 'Unavailable')


if __name__ == '__main__':
    unittest.main()
