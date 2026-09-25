"""Bounded, deterministic checks of decoded textures where an OBJ uses them.

This is artifact validation and anomaly screening, not visual approval. A dark
subject can legitimately trigger the warning and still be a useful scan.
"""
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import tempfile


class TextureQualityError(ValueError):
    pass


def _local_file(root: Path, name: str) -> Path:
    if not name or Path(name).name != name or name in {'.', '..'} or '\\' in name:
        raise TextureQualityError(f'Texture references must be local filenames: {name!r}')
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise TextureQualityError(f'Missing, empty, or linked texture input: {name}')
    return path


def _materials(root: Path, names: set[str]) -> dict[str, str]:
    materials = {}
    for name in sorted(names):
        path = _local_file(root, name)
        if path.stat().st_size > 1024 * 1024:
            raise TextureQualityError('Material library exceeds the 1 MiB inspection limit')
        current = None
        for line in path.read_text().splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            if parts[0] == 'newmtl':
                current = parts[1].strip()
            elif parts[0] == 'map_Kd':
                if current is None or current in materials:
                    raise TextureQualityError('Ambiguous diffuse texture declaration')
                texture = parts[1].strip()
                _local_file(root, texture)
                materials[current] = texture
    return materials


def inspect_textured_obj(obj: Path, *, max_samples: int = 12000) -> dict:
    """Read an OBJ twice, retaining only sampled UV indices, not its whole mesh."""
    from PIL import Image

    if type(max_samples) is not int or not 1 <= max_samples <= 12000:
        raise ValueError('Texture samples must be between 1 and 12000')
    obj = Path(obj).absolute()
    if obj.resolve() != obj:
        raise TextureQualityError('OBJ path must not traverse symbolic links')
    _local_file(obj.parent, obj.name)
    materials, libraries = set(), set()
    samples = []
    face_count = uv_count = 0
    current = None
    rng = random.Random(0)
    with obj.open() as stream:
        for line in iter(lambda: stream.readline(65537), ''):
            if len(line) > 65536:
                raise TextureQualityError('OBJ record exceeds the inspection limit')
            if line.startswith('mtllib '):
                libraries.update(line.split()[1:])
                if len(libraries) > 64:
                    raise TextureQualityError('Too many material libraries for texture inspection')
            elif line.startswith('usemtl '):
                current = line.split(maxsplit=1)[1].strip()
            elif line.startswith('vt '):
                uv_count += 1
            elif line.startswith('f '):
                materials.add(current)
                if len(materials) > 4096:
                    raise TextureQualityError('Too many materials for texture inspection')
                face_count += 1
                slot = face_count - 1 if face_count <= max_samples else rng.randrange(face_count)
                if slot >= max_samples:
                    continue
                corners = line.split()[1:]
                if len(corners) > 16:
                    raise TextureQualityError('Sampled OBJ polygons exceed the supported 16-corner limit')
                indices = []
                try:
                    for corner in corners:
                        index = int(corner.split('/')[1])
                        if index == 0:
                            raise ValueError('zero index')
                        indices.append(index - 1 if index > 0 else uv_count + index)
                except (ValueError, IndexError) as error:
                    raise TextureQualityError('Sampled face is missing valid texture coordinates') from error
                if len(indices) < 3:
                    raise TextureQualityError('Sampled OBJ face has fewer than three corners')
                sample = (current, indices)
                if slot == len(samples):
                    samples.append(sample)
                else:
                    samples[slot] = sample
    if not face_count or not uv_count or not libraries:
        raise TextureQualityError('Textured OBJ requires faces, UV coordinates, and a material library')
    textures = _materials(obj.parent, libraries)
    if any(material not in textures for material in materials):
        raise TextureQualityError('Every used material must have a diffuse texture')
    wanted = {index for _, indices in samples for index in indices}
    if any(index < 0 or index >= uv_count for index in wanted):
        raise TextureQualityError('Texture-coordinate index is outside the OBJ table')
    coordinates = {}
    index = 0
    with obj.open() as stream:
        for line in iter(lambda: stream.readline(65537), ''):
            if len(line) > 65536:
                raise TextureQualityError('OBJ record exceeds the inspection limit')
            if line.startswith('vt '):
                if index in wanted:
                    try:
                        u, v = map(float, line.split()[1:3])
                    except ValueError as error:
                        raise TextureQualityError('Invalid texture coordinate') from error
                    if not all(math.isfinite(value) and 0 <= value <= 1 for value in (u, v)):
                        raise TextureQualityError('Atlas UVs must be finite and inside [0, 1]')
                    coordinates[index] = (u, v)
                index += 1
    if coordinates.keys() != wanted:
        raise TextureQualityError('OBJ texture table changed or is incomplete')
    by_texture = defaultdict(list)
    for material, indices in samples:
        by_texture[textures[material]].append((
            sum(coordinates[i][0] for i in indices) / len(indices),
            sum(coordinates[i][1] for i in indices) / len(indices)))
    images, luminance = {}, []
    # Decode every referenced texture, including those not hit by the sample.
    for name in sorted(set(textures[material] for material in materials)):
        try:
            with Image.open(_local_file(obj.parent, name)) as source:
                source.load()
                rgb = source.convert('RGB')
                width, height = rgb.size
                pixels = rgb.load()
                values = []
                for u, v in by_texture[name]:
                    x = min(width - 1, int(u * width))
                    y = min(height - 1, int((1 - v) * height))
                    red, green, blue = pixels[x, y]
                    values.append(.2126 * red + .7152 * green + .0722 * blue)
                images[name] = {'size': [width, height], 'sample_count': len(values)}
                luminance.extend(values)
                rgb.close()
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            raise TextureQualityError(f'Texture decoding failed for {name}: {error}') from error
    near_black = sum(value < 10 for value in luminance) / len(luminance)
    warnings = []
    if near_black >= .8:
        warnings.append('At least 80% of sampled face-used texture pixels are near-black; inspect before accepting this asset.')
    luminance.sort()
    return {'schema_version': '1.0', 'created_at': datetime.now(timezone.utc).isoformat(),
            'status': 'needs_review' if warnings else 'checks_passed',
            'obj': obj.name, 'faces': face_count, 'sample_count': len(samples),
            'sampling': 'deterministic reservoir of face UV centroids; face-weighted, not area-weighted',
            'textures': images, 'near_black_fraction': near_black,
            'near_black_luminance_threshold': 10, 'median_luminance': luminance[len(luminance) // 2],
            'warnings': warnings, 'visual_quality': 'manual review required'}


def write_texture_report(obj: Path, report_path: Path) -> dict:
    """Atomically publish a completed inspection; never a partial success report."""
    report = inspect_textured_obj(obj)
    report_path = Path(report_path)
    if report_path.is_symlink():
        raise TextureQualityError('Report destination must not be a symbolic link')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=report_path.parent, prefix='.texture-quality-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, indent=2)
        temporary.replace(report_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return report
