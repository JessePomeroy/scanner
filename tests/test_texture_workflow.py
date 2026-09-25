from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.openmvs_runner import OpenMVSConfig, build_openmvs_commands
from desktop import resume


def setUpModule():
    from heavy_work_fixture import isolated_heavy_work
    unittest.enterModuleContext(isolated_heavy_work())


class TexturePolicyTests(unittest.TestCase):
    def assert_safe_texture(self, command):
        for option in ('--global-seam-leveling', '--local-seam-leveling'):
            self.assertIn(option, command)
            self.assertEqual(command[command.index(option) + 1], '0')
        self.assertEqual(command[command.index('--resolution-level') + 1], '0')

    def test_default_and_fused_plans_disable_both_seam_paths(self):
        for config in (OpenMVSConfig(), OpenMVSConfig(point_cloud_source='colmap_fused', scope_mode='unbounded')):
            command = build_openmvs_commands(Path('/tmp/fixture'), config)[-1]
            self.assert_safe_texture(command)

    def test_desktop_retry_uses_the_same_texture_policy(self):
        self.assert_safe_texture(resume.command(Path('/tmp/source'), Path('/tmp/output')))

    def test_effective_texture_settings_are_recorded(self):
        settings = OpenMVSConfig().report_settings()['texture']
        self.assertFalse(settings['global_seam_leveling'])
        self.assertFalse(settings['local_seam_leveling'])
        self.assertEqual(settings['resolution_level'], 0)


if __name__ == '__main__':
    unittest.main()
