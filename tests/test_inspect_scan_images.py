from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


class InspectScanImageTests(unittest.TestCase):
    def test_verify_images_rejects_jpeg_with_readable_header_but_truncated_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scan = Path(temporary)
            (scan / "images").mkdir()
            (scan / "metadata").mkdir()
            image = scan / "images" / "frame.jpg"
            Image.new("RGB", (32, 32), (100, 120, 80)).save(image)
            image.write_bytes(image.read_bytes()[:-8])
            with Image.open(image) as header_only:
                header_only.verify()
            with Image.open(image) as pixels, self.assertRaises(OSError):
                pixels.load()
            (scan / "metadata" / "session.json").write_text("{}")
            (scan / "metadata" / "frames.json").write_text(json.dumps([
                {"id": 1, "image": "images/frame.jpg", "timestamp": 0, "resolution": [32, 32]},
            ]))
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "inspect_scan.py"), str(scan), "--verify-images"],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Image decode failed for images/frame.jpg", result.stderr)
            self.assertNotIn("image_decode_and_dimension_check=passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
