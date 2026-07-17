from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "scripts" / "wsl" / "setup_gpu_reconstruction.sh"


class LinuxSetupScriptTests(unittest.TestCase):
    def run_dry_setup(self, os_release: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            release_file = Path(temporary_directory) / "os-release"
            release_file.write_text(os_release, encoding="utf-8")
            environment = os.environ.copy()
            environment["SCANNER_OS_RELEASE_FILE"] = str(release_file)
            environment["SCANNER_NODE_MAJOR"] = "0"
            environment["HOME"] = temporary_directory
            return subprocess.run(
                ["bash", str(SETUP_SCRIPT), "--dry-run"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=20,
                env=environment,
            )

    def test_cachyos_uses_complete_pacman_upgrade_and_cuda(self) -> None:
        completed = self.run_dry_setup(
            'ID=cachyos\nID_LIKE=arch\nPRETTY_NAME="CachyOS"\n'
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("sudo pacman -Syu --needed", completed.stdout)
        self.assertIn("cuda", completed.stdout)
        self.assertIn("nodejs-lts-jod", completed.stdout)
        self.assertNotIn("apt-get", completed.stdout)
        self.assertIn("@playcanvas/splat-transform", completed.stdout)
        self.assertIn("@openai/codex", completed.stdout)

    def test_ubuntu_keeps_the_debian_package_path(self) -> None:
        completed = self.run_dry_setup(
            'ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu Linux"\n'
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("sudo apt-get update", completed.stdout)
        self.assertIn("sudo apt-get install -y", completed.stdout)
        self.assertNotIn("sudo pacman", completed.stdout)

    def test_unknown_distribution_is_rejected(self) -> None:
        completed = self.run_dry_setup(
            'ID=futureos\nPRETTY_NAME="Future OS"\n'
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("Unsupported Linux distribution: Future OS", completed.stdout)


if __name__ == "__main__":
    unittest.main()
