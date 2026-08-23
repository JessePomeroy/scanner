from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "nerfstudio_colmap_compat.py"
CHECKER = ROOT / "scripts" / "wsl" / "check_reconstruction_env.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_reconstruction_env", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class NerfstudioColmapCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.fake_colmap = self.root / "colmap"
        self.fake_colmap.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

mode = os.environ.get("FAKE_COLMAP_MODE", "modern")
if len(sys.argv) == 3 and sys.argv[2] == "-h":
    command = sys.argv[1]
    if mode in {"modern", "nonzero-modern"}:
        if command == "feature_extractor":
            print("--FeatureExtraction.use_gpu arg (=1)")
        elif command.endswith("_matcher"):
            print("--FeatureMatching.use_gpu arg (=1)")
    elif mode == "legacy":
        if command == "feature_extractor":
            print("--SiftExtraction.use_gpu arg (=1)")
        elif command.endswith("_matcher"):
            print("--SiftMatching.use_gpu arg (=1)")
    else:
        print("help without a compatible GPU option")
    raise SystemExit(7 if mode == "nonzero-modern" else 0)
print(json.dumps(sys.argv[1:]))
""",
            encoding="utf-8",
        )
        self.fake_colmap.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_wrapper(
        self,
        *arguments: str,
        mode: str = "modern",
        executable: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["SCANNER_REAL_COLMAP"] = str(executable or self.fake_colmap)
        environment["FAKE_COLMAP_MODE"] = mode
        return subprocess.run(
            [str(WRAPPER), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            env=environment,
        )

    def test_modern_colmap_translates_only_the_two_obsolete_flags(self) -> None:
        completed = self.run_wrapper(
            "feature_extractor",
            "--SiftExtraction.use_gpu=1",
            "--SiftMatching.use_gpu",
            "0",
            "--SiftExtraction.max_num_features=42",
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(
            json.loads(completed.stdout),
            [
                "feature_extractor",
                "--FeatureExtraction.use_gpu=1",
                "--FeatureMatching.use_gpu",
                "0",
                "--SiftExtraction.max_num_features=42",
            ],
        )

    def test_legacy_colmap_preserves_obsolete_flags_and_equals_forms(self) -> None:
        completed = self.run_wrapper(
            "feature_extractor",
            "--SiftExtraction.use_gpu=1",
            "--SiftMatching.use_gpu=0",
            mode="legacy",
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(
            json.loads(completed.stdout),
            [
                "feature_extractor",
                "--SiftExtraction.use_gpu=1",
                "--SiftMatching.use_gpu=0",
            ],
        )

    def test_wrapper_rejects_ambiguous_legacy_and_modern_flags(self) -> None:
        completed = self.run_wrapper(
            "feature_extractor",
            "--SiftExtraction.use_gpu",
            "1",
            "--FeatureExtraction.use_gpu=1",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("both --SiftExtraction.use_gpu", completed.stdout)

    def test_wrapper_fails_when_colmap_advertises_neither_option(self) -> None:
        completed = self.run_wrapper(
            "feature_extractor",
            "--SiftExtraction.use_gpu",
            "1",
            mode="unsupported",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("advertises neither", completed.stdout)

    def test_wrapper_rejects_nonzero_help_even_when_option_is_advertised(self) -> None:
        completed = self.run_wrapper(
            "feature_extractor",
            "--SiftExtraction.use_gpu",
            "1",
            mode="nonzero-modern",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("help exited with status 7", completed.stdout)

    def test_wrapper_probes_the_requested_matcher_subcommand(self) -> None:
        completed = self.run_wrapper(
            "sequential_matcher",
            "--SiftMatching.use_gpu",
            "1",
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(
            json.loads(completed.stdout),
            ["sequential_matcher", "--FeatureMatching.use_gpu", "1"],
        )

    def test_wrapper_rejects_recursive_real_colmap_resolution(self) -> None:
        completed = self.run_wrapper("-h", executable=WRAPPER)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("refusing recursive execution", completed.stdout)

    def test_probe_is_non_executing_and_required_by_strict_gate(self) -> None:
        completed = self.run_wrapper("--scanner-compat-probe")
        checker = _load_checker()

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("SiftExtraction.use_gpu=translate", completed.stdout)
        self.assertIn("SiftMatching.use_gpu=translate", completed.stdout)
        self.assertTrue(os.access(WRAPPER, os.X_OK))
        self.assertTrue(checker.is_required("nerfstudio-colmap-compat"))
        self.assertEqual(
            checker.COLMAP_COMPAT_WRAPPER.resolve(),
            WRAPPER.resolve(),
        )

    def test_openmvs_help_probe_uses_and_removes_temporary_cwd(self) -> None:
        checker = _load_checker()
        fake_openmvs = self.root / "InterfaceCOLMAP"
        cwd_record = self.root / "probe-cwd.txt"
        fake_openmvs.write_text(
            """#!/usr/bin/env python3
import os
from pathlib import Path

Path(os.environ["SCANNER_TEST_CWD_RECORD"]).write_text(os.getcwd())
Path("InterfaceCOLMAP-probe.log").write_text("probe-only log")
print("OpenMVS fake help")
raise SystemExit(1)
""",
            encoding="utf-8",
        )
        fake_openmvs.chmod(0o755)

        with patch.dict(
            os.environ,
            {
                "PATH": f"{self.root}{os.pathsep}{os.environ.get('PATH', '')}",
                "SCANNER_TEST_CWD_RECORD": str(cwd_record),
            },
        ):
            result = checker.check_openmvs_command("InterfaceCOLMAP")

        probe_cwd = Path(cwd_record.read_text())
        self.assertTrue(result.ok, result.detail)
        self.assertNotEqual(probe_cwd, ROOT)
        self.assertFalse(probe_cwd.exists())
        self.assertFalse((ROOT / "InterfaceCOLMAP-probe.log").exists())


if __name__ == "__main__":
    unittest.main()
