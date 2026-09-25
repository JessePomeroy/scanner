from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import asyncio
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app import colmap_runner
from app import heavy_work as admission


class WorkflowAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lock = Path(self.temp.name) / 'heavy.lock'
        env = patch.dict(os.environ, {'SCANNER_HEAVY_LOCK': str(self.lock)})
        env.start()
        self.addCleanup(env.stop)
        legacy = patch.object(admission, 'check_legacy_workers')
        legacy.start()
        self.addCleanup(legacy.stop)

    def test_competing_native_workflow_never_enters_native_command(self):
        """A blocked first native command must exclude a second workflow."""
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def native(command):
            calls.append(command)
            entered.set()
            release.wait(3)

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            colmap_runner, 'build_colmap_dense_commands', return_value=[['native-fixture']]
        ), patch.object(colmap_runner, 'run_command', side_effect=native):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(colmap_runner.run_colmap_dense_pipeline, Path(tmp) / 'a')
                self.assertTrue(entered.wait(2))
                second = pool.submit(colmap_runner.run_colmap_dense_pipeline, Path(tmp) / 'b')
                try:
                    with self.assertRaises(admission.HeavyWorkBusy):
                        second.result(timeout=0.2)
                    self.assertEqual(len(calls), 1, 'Two workflows entered native work simultaneously')
                finally:
                    release.set()
                    first.result(timeout=2)

    def test_nested_stages_share_admission_and_owner_is_visible(self):
        with admission.heavy_work('workflow fixture'):
            first = admission.native_kwargs()
            with admission.heavy_work('nested COLMAP stage'):
                self.assertEqual(admission.native_kwargs(), first)
                self.assertEqual(json.loads(self.lock.read_text())['label'], 'workflow fixture')
        with admission.heavy_work('next workflow'):
            self.assertEqual(json.loads(self.lock.read_text())['label'], 'next workflow')

    def test_copied_async_task_context_does_not_bypass_admission(self):
        async def contender():
            with self.assertRaises(admission.HeavyWorkBusy):
                with admission.heavy_work('other task'):
                    self.fail('Another task inherited admission')
            with self.assertRaises(admission.HeavyWorkBusy):
                admission.native_kwargs()

        async def run():
            with admission.heavy_work('parent task'):
                await asyncio.create_task(contender())
                with admission.heavy_work('same task nested stage'):
                    self.assertIn('pass_fds', admission.native_kwargs())
        asyncio.run(run())

    def test_exception_releases_admission(self):
        with self.assertRaisesRegex(ValueError, 'fixture'):
            with admission.heavy_work('failure'):
                raise ValueError('fixture')
        with admission.heavy_work('recovery'):
            pass

    def test_reservation_handoff_has_no_admission_gap_and_is_one_shot(self):
        reservation = admission.reserve_heavy_work('HTTP to worker')
        with self.assertRaises(admission.HeavyWorkBusy):
            with admission.heavy_work('competing launch'):
                self.fail('Reservation gap')
        with ThreadPoolExecutor(max_workers=1) as pool:
            inherited = pool.submit(reservation.run, admission.native_kwargs).result(timeout=2)
        self.assertIn('pass_fds', inherited)
        with self.assertRaises(admission.HeavyWorkBusy):
            reservation.run(lambda: None)
        with admission.heavy_work('next worker'):
            pass

    def test_native_launch_receives_inherited_descriptor(self):
        with admission.heavy_work('native owner'), patch.object(colmap_runner.subprocess, 'run') as run:
            colmap_runner.run_command(['fixture'])
            self.assertEqual(run.call_args.kwargs['pass_fds'], admission.native_kwargs()['pass_fds'])

    def test_symlink_lock_rejected_without_changing_target(self):
        target = self.lock.parent / 'original'
        target.write_text('keep')
        self.lock.symlink_to(target)
        with self.assertRaises(admission.HeavyWorkUnavailable):
            with admission.heavy_work('unsafe'):
                self.fail('symlink accepted')
        self.assertEqual(target.read_text(), 'keep')

    def test_parent_crash_does_not_release_native_child_admission(self):
        child = None
        supervisor = None
        ready_read, ready_write = os.pipe()
        release_read, release_write = os.pipe()
        child_code = 'import os,sys; os.read(int(sys.argv[1]), 1)'
        supervisor_code = '''
import os, subprocess, sys
from app import heavy_work as admission
admission.check_legacy_workers = lambda **kwargs: None
with admission.heavy_work('crash fixture'):
    inherited = admission.native_kwargs()['pass_fds']
    child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],
                             pass_fds=(*inherited, int(sys.argv[2])))
    os.write(int(sys.argv[3]), str(child.pid).encode() + b'\\n')
    os.read(int(sys.argv[2]), 1)
'''
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'backend')}
        try:
            supervisor = subprocess.Popen(
                [sys.executable, '-B', '-c', supervisor_code, child_code,
                 str(release_read), str(ready_write)],
                env=env, pass_fds=(ready_write, release_read),
            )
            os.close(ready_write)
            ready_write = -1
            import select
            self.assertTrue(select.select([ready_read], [], [], 3)[0], 'Fixture did not start')
            child = int(os.read(ready_read, 64))
            supervisor.kill()
            supervisor.wait(timeout=3)
            with self.assertRaisesRegex(admission.HeavyWorkBusy, 'crash fixture'):
                with admission.heavy_work('must stay blocked'):
                    self.fail('Child survived but admission was released')
            os.write(release_write, b'x')
            # This is bounded polling for our child, not a production-worker wait.
            import time
            deadline = time.monotonic() + 3
            while True:
                try:
                    with admission.heavy_work('after child exit'):
                        break
                except admission.HeavyWorkBusy:
                    if time.monotonic() >= deadline:
                        self.fail('Child did not release admission after exiting')
                    time.sleep(0.01)
            child = None
        finally:
            if supervisor is not None and supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=3)
            if child is not None:
                try:
                    os.kill(child, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for fd in (ready_read, ready_write, release_read, release_write):
                if fd != -1:
                    os.close(fd)

    def api_fixture(self, stage):
        if importlib.util.find_spec('fastapi') is None:
            self.skipTest('FastAPI is not installed in the lightweight test environment')
        with patch.dict(os.environ, {'SCANNER_SCANS_DIR': str(self.lock.parent / 'scans')}):
            from app import main
        from app.jobs import JobStore
        jobs = JobStore(self.lock.parent / 'jobs')
        jobs.create('fixture')
        jobs.update('fixture', status='processing', stage='queued' if stage == 'queued' else 'validating')
        if stage not in {'validating', 'queued'}:
            jobs.update('fixture', status='processing', stage=stage)
        patcher = patch.object(main, 'jobs', jobs)
        patcher.start()
        self.addCleanup(patcher.stop)
        return main, jobs

    def test_initial_api_busy_fails_without_extracting_or_moving_workspace(self):
        main, jobs = self.api_fixture('validating')
        with admission.heavy_work('other workflow'), \
                patch.object(main, 'prepare_processing_dir') as prepare, \
                patch.object(main, 'fail_processing') as move, ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(main.process_scan, 'fixture', Path('incoming.zip'), True, True).result(timeout=2)
        prepare.assert_not_called()
        move.assert_called_once_with('fixture', None)
        self.assertEqual(jobs.read('fixture').status, 'failed')
        self.assertIn('Scanner is busy', jobs.read('fixture').message)

    def test_unavailable_admission_terminalizes_initial_job_without_extracting(self):
        main, jobs = self.api_fixture('queued')
        self.lock.mkdir()
        with patch.object(main, 'prepare_processing_dir') as prepare:
            main.process_scan('fixture', Path('incoming.zip'), True, True)
        prepare.assert_not_called()
        self.assertEqual(jobs.read('fixture').status, 'failed')
        self.assertIn('admission is unavailable', jobs.read('fixture').message)

    def test_unavailable_admission_preserves_reviewed_resume(self):
        main, jobs = self.api_fixture('reconstructing')
        self.lock.mkdir()
        with patch.object(main, '_resume_scoped_scan') as native, patch.object(main, 'fail_processing') as move:
            main.resume_scoped_scan('fixture')
        native.assert_not_called()
        move.assert_not_called()
        self.assertEqual(jobs.read('fixture').stage, 'awaiting_scope')
        self.assertIn('admission is unavailable', jobs.read('fixture').message)

    def test_scoped_resume_busy_preserves_checkpoint_and_explicit_retry(self):
        main, jobs = self.api_fixture('reconstructing')
        with admission.heavy_work('other workflow'), \
                patch.object(main, '_resume_scoped_scan') as native, \
                patch.object(main, 'fail_processing') as move, ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(main.resume_scoped_scan, 'fixture').result(timeout=2)
        native.assert_not_called()
        move.assert_not_called()
        self.assertEqual(jobs.read('fixture').stage, 'awaiting_scope')

    def test_mask_approval_busy_does_not_promote_masks_or_schedule_alignment(self):
        main, jobs = self.api_fixture('awaiting_masks')
        tasks = main.BackgroundTasks()
        with admission.heavy_work('other workflow'), patch.object(main, 'approve_mask_review') as approve:
            with self.assertRaises(main.HTTPException) as error:
                main.approve_scan_mask_review('fixture', tasks)
        self.assertEqual(error.exception.status_code, 409)
        approve.assert_not_called()
        self.assertEqual(tasks.tasks, [])
        self.assertEqual(jobs.read('fixture').stage, 'awaiting_masks')

    def test_mask_approval_reserves_until_background_alignment_finishes(self):
        main, jobs = self.api_fixture('awaiting_masks')
        tasks = main.BackgroundTasks()
        with patch.object(main, '_active_scan_root', return_value=self.lock.parent), \
                patch.object(main, 'approve_mask_review'), patch.object(main, 'validate_and_report_scan'):
            main.approve_scan_mask_review('fixture', tasks)
        with self.assertRaises(admission.HeavyWorkBusy):
            admission.reserve_heavy_work('cannot pass HTTP-to-worker gap')
        with patch.object(main, '_resume_masked_alignment', side_effect=lambda _: self.assertIn(
                'pass_fds', admission.native_kwargs())) as native:
            asyncio.run(tasks())
        native.assert_called_once_with('fixture')
        with admission.heavy_work('next job'):
            pass


class LegacyWorkerTests(unittest.TestCase):
    def test_blender_and_reconstruction_workers_checked_read_only(self):
        result = subprocess.CompletedProcess([], 0, 'scanner-blender-panel-other.service loaded active running\n')
        with patch.object(admission.Path, 'exists', return_value=True), \
                patch.object(admission, '_current_worker_unit', return_value='scanner-reconstruct-self.service') as own, \
                patch.object(admission.shutil, 'which', return_value='/usr/bin/systemctl'), \
                patch.object(admission.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(admission.HeavyWorkBusy, 'scanner-blender-panel-other'):
                admission.check_legacy_workers(excluding='scanner-reconstruct-self.service')
            command = run.call_args.args[0]
            self.assertIn('scanner-reconstruct-*.service', command)
            self.assertIn('scanner-blender-*.service', command)
            self.assertEqual(command[2], 'list-units')
            own.return_value = 'scanner-blender-panel-other.service'
            admission.check_legacy_workers(excluding='scanner-blender-panel-other.service')

    def test_cli_own_unit_comes_from_cgroup_without_caller_bypass(self):
        unit = 'scanner-reconstruct-cli-fixture.service'
        result = subprocess.CompletedProcess([], 0, f'{unit} loaded active running\n')
        with patch.object(admission.Path, 'exists', return_value=True), \
                patch.object(admission.Path, 'read_text', return_value=f'0::/user.slice/app.slice/{unit}\n'), \
                patch.object(admission.shutil, 'which', return_value='/usr/bin/systemctl'), \
                patch.object(admission.subprocess, 'run', return_value=result):
            self.assertEqual(admission._current_worker_unit(), unit)
            admission.check_legacy_workers()
            with self.assertRaisesRegex(admission.HeavyWorkBusy, 'identity'):
                admission.check_legacy_workers(excluding='scanner-blender-someone-else.service')

    def test_unreadable_manager_is_not_treated_as_idle(self):
        with patch.object(admission.Path, 'exists', return_value=True), \
                patch.object(admission.shutil, 'which', return_value='/usr/bin/systemctl'), \
                patch.object(admission.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '')):
            with self.assertRaisesRegex(admission.HeavyWorkBusy, 'Cannot inspect'):
                admission.check_legacy_workers()


if __name__ == '__main__':
    unittest.main()
