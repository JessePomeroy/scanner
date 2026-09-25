"""Small read-only helpers for selection, handoff, and delivery evidence."""
from pathlib import Path
import re
import subprocess

from desktop.monitor import Snapshot, read_record


def validate_selection(run: Path, unit: str) -> tuple[Path, str]:
    run = run.expanduser().resolve()
    if not run.is_dir():
        raise ValueError('Choose an attempt directory containing state.json and plan.json.')
    state = read_record(run / 'state.json')
    plan = read_record(run / 'plan.json')
    if not re.fullmatch(r'scanner-reconstruct-[A-Za-z0-9_-]+\.service', unit):
        raise ValueError('Enter the exact scanner-reconstruct-*.service user unit.')
    result = subprocess.run(['systemctl', '--user', 'show', unit, '--no-pager',
                             '--property=LoadState,ExecStart'], capture_output=True,
                            text=True, timeout=2, check=False)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if values.get('LoadState') == 'not-found' and state.get('status') in {'succeeded', 'failed', 'stopped'}:
        if plan.get('service') not in (None, unit):
            raise ValueError('Recorded service does not belong to this attempt.')
        # Inspecting a completed result does not authorize execution. Recovery
        # separately requires a live, verified service-to-attempt association.
        return run, unit
    if result.returncode or values.get('LoadState') != 'loaded':
        raise ValueError('Service is unavailable; cannot verify that it belongs to this attempt.')
    invocation = values.get('ExecStart', '')
    if str(run) + '/' not in invocation and f'--execute {run} ' not in invocation:
        raise ValueError('That service does not belong to the chosen attempt.')
    return run, unit


def delivery_checks(output: Path | None, status: str) -> tuple[str, ...]:
    processing = 'Processing: finished successfully' if status == 'succeeded' else f'Processing: {status}'
    pending = (processing, 'Texture package: not checked until processing succeeds',
               'Blender delivery: not checked until processing succeeds', 'Visual quality: manual review required')
    if status != 'succeeded' or output is None:
        return pending
    try:
        obj, mtl = output / 'scene_textured.obj', output / 'scene_textured.mtl'
        present = lambda p: p.is_file() and not p.is_symlink() and p.stat().st_size > 0
        texture_status = 'missing OBJ or material file'
        if present(obj) and present(mtl):
            with mtl.open('rb') as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                texture_status = 'material too large for this check'
            else:
                textures = [output / line.split(maxsplit=1)[1] for line in data.decode().splitlines()
                            if line.startswith('map_Kd ')]
                complete = bool(textures) and all(p.resolve().parent == output.resolve() and present(p) for p in textures)
                texture_status = 'files present; pixels not validated' if complete else 'missing or unsafe texture references'
        blend = any(present(p) for p in output.glob('*.blend'))
        glb = any(present(p) for p in output.glob('*.glb'))
        blender = 'BLEND and GLB present; not validated' if blend and glb else 'BLEND/GLB pair not found in output folder'
        quality = output / 'texture_quality.json'
        screening = 'Texture screening: not recorded'
        if present(quality):
            report = read_record(quality)
            if report.get('status') == 'needs_review':
                screening = 'Texture warning: mostly near-black; inspect appearance'
            elif report.get('status') == 'checks_passed':
                screening = 'Texture screening: decoded and sampled; appearance not approved'
        return processing, f'Texture package: {texture_status}', f'Blender delivery: {blender}', 'Visual quality: manual review required', screening
    except (OSError, UnicodeError, ValueError):
        return processing, 'Texture package: could not inspect output', 'Blender delivery: not verified', 'Visual quality: manual review required'


def redact(value: str) -> str:
    value = re.sub(r'(?i)(bearer\s+)\S+', r'\1<redacted>', value)
    value = re.sub(r'(?i)((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+', r'\1<redacted>', value)
    return re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1<redacted>@', value)


def diagnostic_summary(value: Snapshot, run: Path, unit: str, checks: tuple[str, ...]) -> str:
    return '\n'.join([
        'Scanner diagnostic summary (local paths included; review before sharing)',
        f'Observed: {value.sampled_at}', f'Attempt: {run}', f'Service: {unit}',
        f'Status: {value.status}', f'Stage: {value.stage}',
        f'Elapsed: {value.elapsed} (stage: {value.stage_elapsed})',
        f'Job memory: {value.memory}', f'Whole-GPU memory: {value.gpu}',
        f'Error: {redact(value.error[:1500]) or "None recorded"}',
        f'Logs: {run / "logs"}', f'Service log: {run / "service.log"}',
        f'Output: {value.output or "Unavailable"}', *checks,
        'Full logs and commands are intentionally omitted.',
    ])
