"""Fail-closed, opt-in texture retry. Never execute commands from a saved plan."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from desktop.monitor import read_record
from backend.app.texture_policy import TextureSettings
from backend.app import texture_policy, texture_quality, heavy_work as admission
from backend.app.heavy_work import heavy_work, native_kwargs

REPO = Path(__file__).resolve().parents[1]
TOOL = Path.home() / 'ScannerToolchains/install/openMVS-2.4.0/bin/OpenMVS/TextureMesh'
PROFILE = 'openmvs-texture-v2'


class ResumeBlocked(ValueError):
    pass


def sha256(path: Path) -> str:
    if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file():
        raise ResumeBlocked(f'Expected a regular, non-symlink file: {path}')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(path: Path, value: dict, *, new: bool = False):
    if new:
        with path.open('x') as stream:
            json.dump(value, stream, indent=2)
    else:
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, indent=2))
        temporary.replace(path)


def command(source: Path, output: Path) -> list[str]:
    return [str(TOOL), str(source / 'scene.mvs'), '-m', str(source / 'scene_mesh.ply'),
            '-o', str(output / 'scene_textured.mvs'), '--export-type', 'obj',
            '--working-folder', str(source), '--max-threads', '4',
            *TextureSettings().arguments()]


def check_service(run: Path, unit: str):
    if not re.fullmatch(r'scanner-reconstruct-[a-zA-Z0-9_-]+\.service', unit):
        raise ResumeBlocked('Only an explicitly selected Scanner reconstruction service is supported.')
    result = subprocess.run(['systemctl', '--user', 'show', unit, '--no-pager',
                             '--property=LoadState,ActiveState,ExecStart'],
                            capture_output=True, text=True, timeout=3, check=False)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode or values.get('LoadState') != 'loaded':
        raise ResumeBlocked('Service identity cannot be verified; engineer review required.')
    if values.get('ActiveState') not in {'failed', 'inactive'}:
        raise ResumeBlocked('The selected service is still active or changing state.')
    # Legacy launchers carry a script inside the run; this runner carries --execute RUN.
    invocation = values.get('ExecStart', '')
    if str(run) + '/' not in invocation and f'--execute {run} ' not in invocation:
        raise ResumeBlocked('Selected service does not belong to this attempt.')


def check_other_workers(*, excluding: str | None = None):
    result = subprocess.run(['systemctl', '--user', 'list-units', 'scanner-reconstruct-*.service',
                             '--state=active,activating,reloading,deactivating', '--plain', '--no-legend', '--no-pager'],
                            capture_output=True, text=True, timeout=3, check=False)
    if result.returncode:
        raise ResumeBlocked('Cannot check for competing reconstruction services.')
    units = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    if any(unit != excluding for unit in units):
        raise ResumeBlocked('Another reconstruction service is active. Wait for it to finish.')


def check_memory():
    available = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()
                     if line.startswith('MemAvailable:'))
    if available < 44 * 1024**2:
        raise ResumeBlocked('Texture retry requires at least 44 GiB of available RAM.')


@contextmanager
def attempt_lock(root: Path, *, blocking: bool = False):
    locks = []
    try:
        # Share the original recovery lock where present, in addition to panel serialization.
        paths = [root / '.panel-resume.lock']
        legacy = root / 'recovery-r001/.lock'
        if legacy.exists():
            paths.append(legacy)
        for path in paths:
            if path.is_symlink():
                raise ResumeBlocked('Unsafe lock file.')
            stream = path.open('a')
            locks.append(stream)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as error:
                raise ResumeBlocked('A recovery already holds the workspace lock.') from error
        yield
    finally:
        for stream in reversed(locks):
            stream.close()


def preview(run: Path, unit: str) -> dict:
    run = run.absolute()
    if run.resolve() != run or not run.is_dir():
        raise ResumeBlocked('Attempt must be a real directory, not a symlink.')
    check_service(run, unit)
    check_other_workers()
    state = read_record(run / 'state.json')
    plan = read_record(run / 'plan.json')
    if state.get('status') not in {'failed', 'running', 'stopped'}:
        raise ResumeBlocked('This attempt is not failed or interrupted.')
    if state.get('current_stage') != 'texture_mesh':
        raise ResumeBlocked('Only texturing from a completed mesh is supported. This stage needs engineer review.')
    stages = state.get('stages', [])
    if not isinstance(stages, list) or not all(isinstance(stage, dict) for stage in stages):
        raise ResumeBlocked('Invalid stage evidence.')
    if any(stage.get('name') == 'texture_mesh' and stage.get('status') == 'succeeded' for stage in stages):
        raise ResumeBlocked('Texturing already succeeded; inspect existing output instead of repeating it.')
    is_retry = plan.get('resume_profile') in {PROFILE, 'openmvs-texture-v1'}
    if not is_retry and not any(stage.get('name') == 'reconstruct_mesh' and stage.get('status') == 'succeeded' for stage in stages):
        raise ResumeBlocked('No successful mesh stage is recorded.')
    source = Path(plan.get('input_workspace') if is_retry else plan.get('workspace', ''))
    if not source.is_absolute() or source.resolve() != source or not source.is_relative_to(run.parent):
        raise ResumeBlocked('Input workspace must be inside this reconstruction run.')
    if not source.is_dir():
        raise ResumeBlocked('Input workspace is missing.')
    # Legacy commands must match the known, bounded profile. Saved arbitrary commands never run.
    if not is_retry:
        texture = [stage for stage in plan.get('stages', []) if isinstance(stage, dict) and stage.get('name') == 'texture_mesh']
        current = command(source, source)
        legacy = current[:-len(TextureSettings().arguments())]
        if len(texture) != 1 or texture[0].get('command') not in (current, legacy):
            raise ResumeBlocked('Texture settings are outside the supported four-thread OBJ profile.')
    expected_tool = plan.get('binaries', {}).get(str(TOOL))
    if not expected_tool or sha256(TOOL) != expected_tool:
        raise ResumeBlocked('TextureMesh binary changed or is not pinned in this plan.')
    mesh = source / 'scene_mesh.ply'
    with mesh.open('rb') as stream:
        header = stream.read(65536).split(b'end_header', 1)
    if len(header) != 2 or not re.search(rb'element face [1-9][0-9]*\r?\n', header[0]):
        raise ResumeBlocked('Saved mesh has no valid nonempty face table.')
    images = sorted((source / 'images').rglob('*'))
    if any(path.is_symlink() for path in images):
        raise ResumeBlocked('Image links require engineer review.')
    images = [path for path in images if path.is_file()]
    if not images:
        raise ResumeBlocked('Source images are missing.')
    files = [source / 'scene.mvs', mesh, *images]
    fingerprints = {str(path): sha256(path) for path in files}
    if is_retry:
        if fingerprints != plan.get('input_sha256'):
            raise ResumeBlocked('Inputs changed since the prior retry.')
    else:
        original_scene = plan.get('input_artifacts', {}).get('scene.mvs', {}).get('sha256')
        if fingerprints[str(source / 'scene.mvs')] != original_scene:
            raise ResumeBlocked('Scene calibration differs from the recorded input.')
    check_memory()
    return {'source_run': str(run), 'source_unit': unit, 'input_workspace': str(source),
            'state_sha256': sha256(run / 'state.json'), 'plan_sha256': sha256(run / 'plan.json'),
            'input_sha256': fingerprints, 'tool_sha256': expected_tool,
            'runner_sha256': sha256(Path(__file__)),
            'texture_code_sha256': {str(Path(module.__file__)): sha256(Path(module.__file__))
                                   for module in (texture_policy, texture_quality, admission)},
            'images': len(images)}


def launch(approved: dict) -> tuple[Path, str]:
    run = Path(approved['source_run'])
    with attempt_lock(run.parent):
        if preview(run, approved['source_unit']) != approved:
            raise ResumeBlocked('Evidence changed after confirmation. Review a fresh plan.')
        identifier = uuid.uuid4().hex[:12]
        attempt = run.parent / f'panel-resume-{identifier}'
        attempt.mkdir(mode=0o700)
        output = attempt / 'output'
        output.mkdir()
        unit = f'scanner-reconstruct-panel-{identifier}.service'
        plan = {**approved, 'resume_profile': PROFILE, 'created_at': now(), 'service': unit,
                'texture_settings': TextureSettings().as_dict(),
                'workspace': str(output), 'binaries': {str(TOOL): approved['tool_sha256']},
                'stages': [{'name': 'texture_mesh', 'command': command(Path(approved['input_workspace']), output)}]}
        save(attempt / 'plan.json', plan, new=True)
        save(attempt / 'state.json', {'status': 'queued', 'current_stage': 'texture_mesh', 'stages': []}, new=True)
        args = ['systemd-run', '--user', f'--unit={unit}', '--property=Type=exec',
                '--property=MemoryMax=48G', '--property=MemorySwapMax=0',
                f'--working-directory={REPO}',
                f'--property=StandardOutput=append:{attempt / "service.log"}',
                f'--property=StandardError=append:{attempt / "service.log"}',
                '/usr/bin/systemd-inhibit', '--what=sleep:idle', '--who=Scanner',
                '--why=Manual Scanner texture resume', '--mode=block',
                sys.executable, '-B', '-m', 'desktop.resume', '--execute', str(attempt)]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ResumeBlocked(f'Launch outcome uncertain. Inspect {attempt}; do not retry blindly: {error}') from error
        if result.returncode:
            save(attempt / 'state.json', {'status': 'failed', 'current_stage': 'texture_mesh', 'stages': [],
                                         'error': result.stderr[-2000:], 'finished_at': now()})
            raise ResumeBlocked(f'Could not launch. Evidence saved in {attempt}: {result.stderr[-500:]}')
        save(run.parent / '.panel-latest.json', {'run': str(attempt), 'unit': unit})
        return attempt, unit


def execute(attempt: Path):
    with attempt_lock(attempt.parent, blocking=True):
        plan = read_record(attempt / 'plan.json')
        state = {'status': 'running', 'started_at': now(), 'pid': os.getpid(),
                 'current_stage': 'texture_mesh', 'stages': []}
        try:
            # The workspace lock and global lock have different ownership:
            # workspace protects evidence; admission excludes all heavy jobs.
            with heavy_work(f'Texture retry {attempt.name}', excluding_unit=plan.get('service')):
                _execute(attempt, plan, state)
        except Exception as error:
            for stage in state['stages']:
                if stage.get('status') != 'succeeded':
                    stage.update(status='failed', finished_at=now())
            state.update(status='failed', error=str(error), finished_at=now())
            save(attempt / 'state.json', state)
            raise


def _execute(attempt: Path, plan: dict, state: dict):
    if plan.get('resume_profile') != PROFILE or plan.get('runner_sha256') != sha256(Path(__file__)):
        raise ResumeBlocked('Resume profile or runner changed.')
    if plan.get('texture_code_sha256') != {
        str(Path(module.__file__)): sha256(Path(module.__file__))
        for module in (texture_policy, texture_quality, admission)
    }:
        raise ResumeBlocked('Texture policy or validation code changed.')
    check_other_workers(excluding=plan['service'])
    check_service(Path(plan['source_run']), plan['source_unit'])
    for name, key in [('state.json', 'state_sha256'), ('plan.json', 'plan_sha256')]:
        if sha256(Path(plan['source_run']) / name) != plan[key]:
            raise ResumeBlocked('Source attempt changed before execution.')
    for name, expected in plan['input_sha256'].items():
        if sha256(Path(name)) != expected:
            raise ResumeBlocked(f'Input changed before execution: {name}')
    if sha256(TOOL) != plan['tool_sha256']:
        raise ResumeBlocked('Texture binary changed before execution.')
    check_memory()
    output = attempt / 'output'
    if output.resolve() != output.absolute() or any(output.iterdir()):
        raise ResumeBlocked('Retry output is not empty; refusing to overwrite it.')
    save(attempt / 'state.json', state)
    logs = attempt / 'logs'
    logs.mkdir(exist_ok=True)
    args = command(Path(plan['input_workspace']), output)
    with (logs / 'texture_mesh.log').open('x') as stream:
        result = subprocess.run(args, cwd=plan['input_workspace'], stdout=stream,
                                stderr=subprocess.STDOUT, check=False, **native_kwargs())
    state['stages'] = [{'name': 'texture_mesh', 'status': 'running' if result.returncode == 0 else 'failed',
                        'started_at': state['started_at'], 'process_finished_at': now(), 'return_code': result.returncode}]
    save(attempt / 'state.json', state)
    if result.returncode:
        raise ResumeBlocked(f'TextureMesh exited {result.returncode}; inspect the stage log.')
    for name in ('scene_textured.obj', 'scene_textured.mtl'):
        if not (output / name).is_file() or (output / name).stat().st_size == 0:
            raise ResumeBlocked(f'Texture output missing: {name}')
    with (output / 'scene_textured.mtl').open('rb') as stream:
        material = stream.read(65537)
    if len(material) > 65536:
        raise ResumeBlocked('Material file exceeds the supported validation size.')
    textures = [line.split(maxsplit=1)[1] for line in material.decode().splitlines()
                if line.startswith('map_Kd ')]
    if not textures:
        raise ResumeBlocked('No texture images referenced by the material.')
    for name in textures:
        texture = output / name
        if texture.resolve().parent != output or not texture.is_file() or not texture.stat().st_size:
            raise ResumeBlocked(f'Missing or unsafe texture image: {name}')
    for name, expected in plan['input_sha256'].items():
        if sha256(Path(name)) != expected:
            raise ResumeBlocked(f'Input changed during execution: {name}')
    quality = texture_quality.write_texture_report(output / 'scene_textured.obj', output / 'texture_quality.json')
    state['texture_quality'] = quality['status']
    state['visual_quality'] = 'manual review required'
    state['stages'][-1].update(status='succeeded', finished_at=now())
    state['status'] = 'succeeded'
    state['finished_at'] = now()
    save(attempt / 'state.json', state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', required=True, type=Path)
    execute(parser.parse_args().execute.resolve())
