from __future__ import annotations

import json
from pathlib import Path
import zipfile


class ScanFixtureMixin:
    """Build small scan packages on disk for tests that need a realistic layout."""

    def _write_scan(self, root: Path) -> Path:
        scan_dir = root / "scan_test"
        images = scan_dir / "images"
        metadata = scan_dir / "metadata"
        images.mkdir(parents=True)
        metadata.mkdir(parents=True)

        (images / "frame_000001.jpg").write_bytes(b"not a real jpeg")
        (metadata / "frames.json").write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "image": "images/frame_000001.jpg",
                        "timestamp": 0.0,
                        "tracking_state": "normal",
                        "blur_score": 0.42,
                        "movement_delta_meters": 0.12,
                        "rotation_delta_degrees": 8.0,
                        "movement_speed_meters_per_second": 0.25,
                        "resolution": [1920, 1080],
                    }
                ]
            )
        )
        (metadata / "session.json").write_text(
            json.dumps({"scan_id": "scan_test", "scan_mode": "scene_scan"})
        )
        return scan_dir

    def _video_metadata(self, *, path: str = "video/scan.mov") -> dict[str, object]:
        return {
            "path": path,
            "captured_at": "2026-07-07T00:00:00Z",
            "duration_seconds": 12.5,
            "frame_rate": 30,
            "resolution": [1920, 1080],
            "codec": "h264",
            "includes_audio": False,
        }

    def _zip_scan(self, scan_dir: Path, archive: Path, root: Path) -> None:
        if archive.exists():
            archive.unlink()
        with zipfile.ZipFile(archive, "w") as zip_file:
            for path in scan_dir.rglob("*"):
                if path.is_file():
                    zip_file.write(path, path.relative_to(root))
