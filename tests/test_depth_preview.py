from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.depth_preview import (  # noqa: E402
    DepthPreviewError,
    PreviewFrame,
    build_depth_preview_command,
    run_depth_previews,
    select_representative_frames,
)


class DepthPreviewTests(unittest.TestCase):
    def test_select_representative_frames_keeps_endpoints(self) -> None:
        frames = [PreviewFrame(index, Path(f"frame-{index}.jpg")) for index in range(10)]

        selected = select_representative_frames(frames, maximum=4)

        self.assertEqual([frame.frame_id for frame in selected], [0, 3, 6, 9])

    def test_select_one_frame_uses_middle(self) -> None:
        frames = [PreviewFrame(index, Path(f"frame-{index}.jpg")) for index in range(5)]

        selected = select_representative_frames(frames, maximum=1)

        self.assertEqual([frame.frame_id for frame in selected], [2])

    def test_build_command_uses_argv_and_expected_outputs(self) -> None:
        command, outputs = build_depth_preview_command(
            Path("/opt/da3-cli"),
            Path("/opt/model.gguf"),
            PreviewFrame(7, Path("/scan/images/frame.jpg")),
            Path("/work/previews"),
            threads=6,
        )

        self.assertEqual(command[:5], ["/opt/da3-cli", "depth", "--model", "/opt/model.gguf", "--input"])
        self.assertIn("/scan/images/frame.jpg", command)
        self.assertEqual(command[-2:], ["--threads", "6"])
        self.assertEqual(outputs["depth_png"], Path("/work/previews/frame_000007_depth.png"))

    def test_run_writes_hashed_advisory_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "da3-cli"
            model = root / "model.gguf"
            image = root / "frame.jpg"
            runtime.write_bytes(b"runtime")
            model.write_bytes(b"model")
            image.write_bytes(b"image")
            output_dir = root / "previews"
            report_path = root / "report.json"

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                for flag, payload in (("--pfm", b"pfm"), ("--png", b"png"), ("--pose", b"{}")):
                    output = Path(command[command.index(flag) + 1])
                    output.write_bytes(payload)
                return subprocess.CompletedProcess(command, 0, stdout="depth ok", stderr="")

            with patch("app.depth_preview.subprocess.run", side_effect=fake_run):
                report = run_depth_previews(
                    runtime=runtime,
                    model=model,
                    frames=[PreviewFrame(4, image)],
                    output_dir=output_dir,
                    report_path=report_path,
                )

            persisted = json.loads(report_path.read_text())
            self.assertEqual(report["selected_frame_count"], 1)
            self.assertEqual(persisted["purpose"], "representative_depth_previews")
            self.assertIn("advisory visual previews", persisted["limitations"][0])
            self.assertEqual(persisted["frames"][0]["frame_id"], 4)
            self.assertEqual(len(persisted["frames"][0]["artifacts"]["depth_png"]["sha256"]), 64)

    def test_run_rejects_missing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "da3-cli"
            model = root / "model.gguf"
            image = root / "frame.jpg"
            for path in (runtime, model, image):
                path.write_bytes(b"x")

            with patch(
                "app.depth_preview.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ):
                with self.assertRaisesRegex(DepthPreviewError, "omitted expected outputs"):
                    run_depth_previews(
                        runtime=runtime,
                        model=model,
                        frames=[PreviewFrame(1, image)],
                        output_dir=root / "previews",
                        report_path=root / "report.json",
                    )


if __name__ == "__main__":
    unittest.main()
