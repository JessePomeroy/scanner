"""Persisted, one-shot Blender follow-up for a completed reconstruction."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from desktop.monitor import Progress, Snapshot, read_record, service_properties
from desktop.resume import attempt_lock, check_memory, check_other_workers, now, save, sha256
from backend.app import texture_quality, heavy_work as admission
from backend.app.heavy_work import heavy_work, native_kwargs

REPO = Path(__file__).resolve().parents[1]
PREPARE = REPO / 'scripts/blender/prepare_scan_asset.py'
VERIFY = REPO / 'desktop/verify_blend.py'
QUALITY = Path(texture_quality.__file__)
ADMISSION = Path(admission.__file__)
CONFIG = '.panel-blender.json'
LOCK_ROOT = Path.home() / '.local/state/scanner/blender'


def configuration(run: Path) -> dict:
    path = run / CONFIG
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError('Blender settings must be a regular file.')
    value = read_record(path)
    name = value.get('attempt')
    if name is not None and (not isinstance(name, str) or not re.fullmatch(r'blender-[0-9a-f]{12}', name)):
        raise ValueError('Invalid Blender attempt directory.')
    return value


def set_automatic(run: Path, enabled: bool):
    with attempt_lock(run):
        config = configuration(run)
        config['enabled'] = enabled
        save(run / CONFIG, config)


def record_launch_error(run: Path, error: str):
    with attempt_lock(run):
        config = configuration(run)
        config['error'] = error
        save(run / CONFIG, config)


def inspect_source(run: Path, unit: str) -> tuple[Path, list[Path]]:
    if not re.fullmatch(r'scanner-reconstruct-[A-Za-z0-9_-]+\.service', unit):
        raise ValueError('Select a Scanner reconstruction service first.')
    state, plan = read_record(run / 'state.json'), read_record(run / 'plan.json')
    if state.get('status') != 'succeeded' or not any(
        stage.get('name') == 'texture_mesh' and stage.get('status') == 'succeeded'
        for stage in state.get('stages', []) if isinstance(stage, dict)
    ):
        raise ValueError('A successful texture stage is required before Blender preparation.')
    if service_properties(unit).get('ActiveState') != 'inactive':
        raise ValueError('Wait until the reconstruction service has finished.')
    source = Path(plan.get('workspace', ''))
    if not source.is_absolute() or source.resolve() != source or not source.is_relative_to(run.parent):
        raise ValueError('Reconstruction output must be a real directory inside this run.')
    obj, material = source / 'scene_textured.obj', source / 'scene_textured.mtl'
    with material.open('rb') as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('Material file is too large for Blender preparation.')
    names = [line.split(maxsplit=1)[1] for line in data.decode().splitlines() if line.startswith('map_Kd ')]
    if not names:
        raise ValueError('No texture images are referenced by the material.')
    files = [obj, material]
    for name in sorted(set(names)):
        if Path(name).name != name:
            raise ValueError('Texture references must stay in the reconstruction output folder.')
        files.append(source / name)
    for path in files:
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Missing or unsafe Blender input: {path.name}')
    return source, files


def launch(run: Path, unit: str, *, retry: bool = False) -> Path:
    run = run.resolve()
    with attempt_lock(run):
        config = configuration(run)
        if config.get('attempt'):
            previous = run / config['attempt']
            state = read_record(previous / 'state.json')
            plan = read_record(previous / 'plan.json')
            active = service_properties(plan['service']).get('ActiveState')
            if not retry or state.get('status') == 'succeeded' or active not in {'inactive', 'failed'}:
                raise ValueError('A Blender attempt already exists; inspect it before retrying.')
        source, files = inspect_source(run, unit)
        tool = shutil.which('blender')
        if not tool:
            raise ValueError('Blender is not installed on this workstation.')
        check_other_workers()
        check_memory()
        if shutil.disk_usage(run).free < max(4 * 1024**3, sum(p.stat().st_size for p in files) * 4):
            raise ValueError('Insufficient free disk space for a separate Blender output.')
        identifier = uuid.uuid4().hex[:12]
        attempt = run / f'blender-{identifier}'
        attempt.mkdir(mode=0o700)
        (attempt / 'output').mkdir()
        (attempt / 'logs').mkdir()
        service = f'scanner-blender-panel-{identifier}.service'
        plan = {'service': service, 'source_run': str(run), 'source_unit': unit,
                'source': str(source), 'blender': str(Path(tool).resolve()),
                'blender_sha256': sha256(Path(tool).resolve()),
                'workspace': str(attempt / 'output'), 'textures': [p.name for p in files[2:]],
                'stages': [{'name': 'texture_check'}, {'name': 'blend_prepare'}, {'name': 'blend_verify'}],
                'input_sha256': {str(p): sha256(p) for p in files},
                'code_sha256': {str(p): sha256(p) for p in (PREPARE, VERIFY, QUALITY, ADMISSION, Path(__file__).resolve())}}
        save(attempt / 'plan.json', plan, new=True)
        save(attempt / 'state.json', {'status': 'queued', 'current_stage': 'blend_prepare', 'stages': []}, new=True)
        config.update(attempt=attempt.name)
        config.pop('error', None)
        save(run / CONFIG, config)
        args = ['systemd-run', '--user', f'--unit={service}', '--property=Type=exec',
                '--property=MemoryMax=48G', '--property=MemorySwapMax=0',
                f'--working-directory={REPO}',
                f'--property=StandardOutput=append:{attempt / "service.log"}',
                f'--property=StandardError=append:{attempt / "service.log"}',
                '/usr/bin/systemd-inhibit', '--what=sleep:idle', '--who=Scanner',
                '--why=Preparing Scanner Blender file', '--mode=block',
                sys.executable, '-B', '-m', 'desktop.blender', '--execute', str(attempt)]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f'Launch outcome uncertain; inspect {attempt}: {error}') from error
        if result.returncode:
            save(attempt / 'state.json', {'status': 'failed', 'current_stage': 'blend_prepare',
                                         'stages': [], 'error': result.stderr[-2000:], 'finished_at': now()})
            raise ValueError(f'Blender could not start: {result.stderr[-500:]}')
        return attempt


def execute(attempt: Path):
    plan = read_record(attempt / 'plan.json')
    state = {'status': 'running', 'started_at': now(), 'current_stage': 'blend_prepare', 'stages': []}
    try:
        # One Blender preparation at a time, including workers launched by another panel.
        LOCK_ROOT.mkdir(parents=True, exist_ok=True)
        with heavy_work(f'Blender preparation {attempt.name}', excluding_unit=plan.get('service')), \
                attempt_lock(LOCK_ROOT), attempt_lock(attempt.parent.parent):
            if Path(plan['source_run']) != attempt.parent:
                raise ValueError('Blender attempt does not belong to the recorded source run.')
            source, files = inspect_source(Path(plan['source_run']), plan['source_unit'])
            if {str(p): sha256(p) for p in files} != plan['input_sha256']:
                raise ValueError('Reconstruction files changed before Blender preparation.')
            if {str(p): sha256(p) for p in (PREPARE, VERIFY, QUALITY, ADMISSION, Path(__file__).resolve())} != plan['code_sha256']:
                raise ValueError('Blender preparation code changed; create a new attempt.')
            tool = Path(shutil.which('blender') or '').resolve()
            if str(tool) != plan['blender'] or sha256(tool) != plan['blender_sha256']:
                raise ValueError('The Blender executable changed; create a new attempt.')
            check_other_workers()
            check_memory()
            output = attempt / 'output'
            if output.resolve() != output or any(output.iterdir()):
                raise ValueError('Blender output is not empty; existing files are preserved.')
            screening = {'name': 'texture_check', 'status': 'running', 'started_at': now()}
            state['current_stage'] = 'texture_check'
            state['stages'].append(screening)
            save(attempt / 'state.json', state)
            quality = texture_quality.write_texture_report(source / 'scene_textured.obj', attempt / 'texture_quality.json')
            screening.update(status='succeeded', finished_at=now())
            state['texture_quality'] = quality['status']
            blend = output / 'scan.blend'
            common = [plan['blender'], '--background', '--factory-startup', '--disable-autoexec',
                      '--threads', '4', '--python-exit-code', '1', '--python']
            commands = [
                ('blend_prepare', common + [str(PREPARE), '--', str(source / 'scene_textured.obj'), str(blend),
                                           '--texture-dir', str(source), '--obj-forward-axis', 'NEGATIVE_Z', '--obj-up-axis', 'Y']),
                ('blend_verify', common + [str(VERIFY), '--', str(blend), str(attempt / 'verification.json'),
                                          *plan['textures']]),
            ]
            for name, args in commands:
                stage = {'name': name, 'status': 'running', 'started_at': now()}
                state['current_stage'] = name
                state['stages'].append(stage)
                save(attempt / 'state.json', state)
                with (attempt / 'logs' / f'{name}.log').open('x') as stream:
                    result = subprocess.run(args, cwd=attempt, stdout=stream, stderr=subprocess.STDOUT,
                                            check=False, **native_kwargs())
                stage.update(status='succeeded' if result.returncode == 0 else 'failed',
                             finished_at=now(), return_code=result.returncode)
                if result.returncode:
                    raise ValueError(f'Blender exited {result.returncode} during {name}; see the recent log.')
            report = read_record(attempt / 'verification.json')
            if not report.get('verified') or not blend.is_file() or blend.stat().st_size == 0:
                raise ValueError('The Blender file did not pass reopening and embedded-texture checks.')
            report['texture_screening'] = quality
            save(attempt / 'verification.json', report)
            if {str(p): sha256(p) for p in files} != plan['input_sha256']:
                raise ValueError('Reconstruction inputs changed during preparation.')
            state.update(status='succeeded', finished_at=now(), blend=str(blend), sha256=sha256(blend))
            save(attempt / 'state.json', state)
    except Exception as error:
        state.update(status='failed', finished_at=now(), error=str(error))
        save(attempt / 'state.json', state)
        raise


def follow_snapshot(run: Path, value: Snapshot) -> Snapshot:
    from desktop.monitor import collect
    config = configuration(run)
    automatic = config.get('enabled') is True
    if config.get('attempt'):
        attempt = run / config['attempt']
        if attempt.is_symlink():
            raise ValueError('Blender attempt must be a real directory.')
        plan = read_record(attempt / 'plan.json')
        blend_value = collect(attempt, plan['service'], follow_blend=False)
        blend = attempt / 'output/scan.blend'
        ready = blend_value.status == 'succeeded' and blend.is_file() and blend.stat().st_size > 0
        if blend_value.status == 'succeeded' and not ready:
            blend_value = replace(blend_value, status='failed', error='The prepared .blend file is missing.')
        stage = blend_value.stage
        if blend_value.status == 'running' and stage == 'Preparing Blender file':
            markers = re.findall(r'^SCANNER_STAGE: (.+)$', blend_value.log, re.MULTILINE)
            if markers:
                stage = markers[-1]
        quality_note = 'Texture screening: not recorded'
        if ready:
            verification = read_record(attempt / 'verification.json')
            screening = verification.get('texture_screening', {})
            if screening.get('status') == 'needs_review':
                quality_note = 'Texture warning: mostly near-black; inspect appearance'
            elif screening.get('status') == 'checks_passed':
                quality_note = 'Texture screening: decoded and sampled; appearance not approved'
        checks = ('Reconstruction: finished successfully',
                  f'Blender preparation: {blend_value.status}',
                  'Embedded textures: verified on reopen' if ready else 'Embedded textures: not yet verified',
                  quality_note,
                  'Visual quality: manual review required')
        return replace(blend_value, phase='blender', stage=stage, auto_blend=automatic,
                       blend_path=blend if ready else None, checks=checks,
                       can_prepare_blend=blend_value.status in {'failed', 'interrupted', 'stopped'},
                       progress=Progress(operation=stage))
    if config.get('error'):
        return replace(value, phase='blender', status='failed', auto_blend=automatic,
                       stage='Blender preparation blocked', error=str(config['error']), can_prepare_blend=True)
    # A success record can precede process exit by one polling interval.
    inactive = value.worker_inactive
    return replace(value, auto_blend=automatic,
                   can_prepare_blend=value.status == 'succeeded' and inactive)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', required=True, type=Path)
    execute(parser.parse_args().execute.resolve())
