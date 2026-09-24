"""Native Qt tests; skipped on backend-only installations without PySide6."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from dataclasses import replace
import unittest
from unittest.mock import patch

try:
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtCore import QPoint, QRect
    from desktop.scanner_panel import Panel
    from desktop.theme import BLUE, GREEN, INK, RUST, SAGE, SURFACE, TEAL, scanner_palette
except ImportError:
    QApplication = None

from desktop.monitor import build_snapshot


@unittest.skipIf(QApplication is None, 'Requires optional workstation PySide6 toolkit')
class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.panel = Panel(Path('/tmp/test-run'), 'scanner-test.service', poll=False, tray=False)

    def tearDown(self):
        self.panel.tray_timer.stop()
        self.panel.hide()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_unknown_progress_not_fabricated(self):
        value = build_snapshot(Path('/tmp/test'), {'status': 'running', 'current_stage': 'texture_mesh'},
                               {}, {'ActiveState': 'active'}, 'View assignment completed', 2, 'Unavailable', 0)
        self.panel.render(value)
        self.assertEqual(self.panel.progress.maximum(), 0)
        self.assertIn('Unavailable', self.panel.values['eta'].text())
        self.assertIn('does not report', self.panel.operation.text())
        self.assertFalse(self.panel.resume_button.isEnabled())

    def test_palette_replaces_dark_desktop_roles_without_replacing_style(self):
        base = QPalette()
        base.setColor(QPalette.ColorRole.Window, QColor('#101010'))
        base.setColor(QPalette.ColorRole.WindowText, QColor('#ffffff'))
        themed = scanner_palette(base)
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            for role, expected in ((QPalette.ColorRole.Window, SAGE),
                                   (QPalette.ColorRole.WindowText, INK),
                                   (QPalette.ColorRole.Button, SURFACE),
                                   (QPalette.ColorRole.ButtonText, TEAL),
                                   (QPalette.ColorRole.Highlight, BLUE)):
                self.assertEqual(themed.color(group, role).name(), expected)
        self.assertEqual(base.color(QPalette.ColorRole.Window).name(), '#101010')
        self.assertEqual(self.panel.styleSheet(), '')
        self.assertEqual(self.panel.choose_button.palette().color(QPalette.ColorRole.ButtonText).name(), TEAL)

    def test_visible_palette_text_pairs_meet_normal_text_contrast(self):
        def luminance(hex_color):
            values = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
            return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))
        for foreground, background in ((INK, SAGE), (INK, SURFACE), (TEAL, SURFACE),
                                       (GREEN, SURFACE), (RUST, SURFACE), ('#ffffff', BLUE)):
            light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
            self.assertGreaterEqual((light + .05) / (dark + .05), 4.5)

    def test_failure_uses_readable_error_surface_and_keeps_status_text(self):
        value = build_snapshot(Path('/tmp/test'), {'status': 'failed', 'error': 'Texture failed'}, {},
                               {'ActiveState': 'failed'}, '', None, '', 0)
        self.panel.render(value)
        self.assertIn('failed', self.panel.title.text())
        self.assertEqual(self.panel.error.text(), 'Texture failed')
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            palette = self.panel.error.palette()
            self.assertEqual(palette.color(group, QPalette.ColorRole.Window).name(), SURFACE)
            self.assertEqual(palette.color(group, QPalette.ColorRole.WindowText).name(), RUST)

    def test_resume_is_only_enabled_for_failed_texture(self):
        for stage, status, enabled in [('texture_mesh', 'failed', True),
                                       ('reconstruct_mesh', 'failed', False),
                                       ('texture_mesh', 'succeeded', False)]:
            value = build_snapshot(Path('/tmp/test'), {'status': status, 'current_stage': stage},
                                   {}, {'ActiveState': 'failed' if status == 'failed' else 'inactive'},
                                   '', None, '', 0)
            self.panel.render(value)
            self.assertEqual(self.panel.resume_button.isEnabled(), enabled)

    def test_cancel_confirmation_does_not_launch(self):
        from PySide6.QtWidgets import QMessageBox
        with patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Cancel), \
             patch.object(self.panel, 'begin_resume') as launch:
            self.panel.confirm_resume({'images': 196})
            self.app.processEvents()
            launch.assert_not_called()
            self.assertIn('cancelled', self.panel.recovery_note.text())

    def test_switch_clears_previous_run_and_ignores_old_poller(self):
        value = build_snapshot(Path('/tmp/old'), {'status': 'failed', 'current_stage': 'texture_mesh'},
                               {}, {'ActiveState': 'failed'}, 'old log', None, '', 0)
        self.panel.render(value)
        old = self.panel.poller
        with patch('desktop.scanner_panel.QSettings'):
            self.panel.select_run((Path('/tmp/new-run'), 'scanner-reconstruct-new.service'))
        old.sampled.emit(value)
        self.assertIsNone(self.panel.snapshot)
        self.assertEqual(self.panel.logs.toPlainText(), '')
        self.assertFalse(self.panel.resume_button.isEnabled())
        self.assertFalse(self.panel.copy_button.isEnabled())
        self.assertIn('new-run', self.panel.identity.text())

    def test_copy_summary_and_expand_checklist(self):
        value = build_snapshot(Path('/tmp/test'), {'status': 'running'}, {},
                               {'ActiveState': 'active'}, 'private raw log', None, '', 0)
        self.panel.render(value)
        clipboard = self.app.clipboard()
        original = clipboard.text()
        try:
            self.panel.copy_button.click()
            self.assertIn('Scanner diagnostic summary', clipboard.text())
            self.assertNotIn('private raw log', clipboard.text())
        finally:
            clipboard.setText(original)
        self.panel.check_toggle.click()
        self.assertFalse(self.panel.checklist.isHidden())
        self.assertIn('manual review', self.panel.checklist.text())

    def test_narrow_window_can_scroll_all_details(self):
        value = build_snapshot(Path('/tmp/test'), {'status': 'running', 'current_stage': 'texture_mesh'},
                               {}, {'ActiveState': 'active'}, 'View assignment completed. ' * 10, 300,
                               '2.0 / 8 GiB (whole GPU)', 0)
        self.panel.render(value)
        self.app.processEvents()
        self.panel.resize(380, 400)
        self.panel.show()
        self.app.processEvents()
        scroll = self.panel.centralWidget()
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        scroll.ensureWidgetVisible(self.panel.recovery_note)
        self.assertGreaterEqual(self.panel.recovery_note.height(),
                                self.panel.recovery_note.heightForWidth(self.panel.recovery_note.width()))

    def assert_main_controls_fit(self):
        self.app.processEvents()
        scroll = self.panel.centralWidget()
        viewport = scroll.viewport()
        self.assertEqual(scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        for widget in (self.panel.title, self.panel.identity, self.panel.choose_button,
                       self.panel.stage, *self.panel.values.values(), self.panel.completed,
                       self.panel.check_toggle, self.panel.activity, self.panel.recovery_note,
                       self.panel.resume_button, self.panel.log_toggle, self.panel.folder,
                       self.panel.hide_button, self.panel.copy_button, self.panel.footer):
            rect = QRect(widget.mapTo(viewport, QPoint()), widget.size())
            self.assertTrue(viewport.rect().contains(rect), widget.text())
            if widget.hasHeightForWidth():
                self.assertGreaterEqual(widget.height(), widget.heightForWidth(widget.width()), widget.text())

    def compact_snapshot(self, status='running'):
        value = build_snapshot(Path('/tmp/test'), {'status': status, 'current_stage': 'texture_mesh'},
                               {}, {'ActiveState': 'active' if status == 'running' else
                                    'failed' if status == 'failed' else 'inactive'},
                               '12:16:58 [Scn textr] Assigning the best view to each face completed: '
                               '12513003 faces, 817175 patches (43m37s784ms)',
                               12000, '2.0 / 8 GiB (whole GPU)', 0)
        self.panel.identity.setText('object-scan-20260923-r001 / recovery-r003')
        return replace(value, elapsed='4h 15m', stage_elapsed='4h 03m',
                       memory='34.0 / 48 GiB limit', completed='1 of 2 stages completed in this attempt')

    def test_main_view_fits_without_scrolling_for_running_and_terminal_states(self):
        self.panel.show()
        for status in ('running', 'failed', 'succeeded'):
            with self.subTest(status=status):
                self.panel.resize(520, 600)
                self.panel.render(self.compact_snapshot(status))
                self.assert_main_controls_fit()
                self.assertEqual(self.panel.width(), 520)
        self.panel.poll_failed('Service query timed out')
        self.assert_main_controls_fit()
        self.assertFalse(self.panel.resume_button.isEnabled())

    def test_expanding_details_grows_window_and_keeps_actions_visible(self):
        with patch.object(self.panel, 'screen') as screen:
            screen.return_value.availableGeometry.return_value = QRect(0, 0, 2560, 1440)
            self.panel.render(self.compact_snapshot())
            self.panel.show()
            self.assert_main_controls_fit()
            initial_height = self.panel.height()
            self.panel.log_toggle.click()
            self.panel.check_toggle.click()
            self.assert_main_controls_fit()
            self.assertGreater(self.panel.height(), initial_height)
            self.assertFalse(self.panel.logs.isHidden())
            self.assertFalse(self.panel.checklist.isHidden())
            self.panel.log_toggle.click()
            self.panel.check_toggle.click()
            self.assert_main_controls_fit()
            self.assertEqual(self.panel.height(), initial_height)

    def test_copy_feedback_keeps_all_actions_visible(self):
        self.panel.render(self.compact_snapshot())
        self.panel.show()
        self.assert_main_controls_fit()
        with patch.object(self.app.clipboard(), 'setText'):
            self.panel.copy_button.click()
        self.assertIn('review before sharing', self.panel.footer.text())
        self.assert_main_controls_fit()

    def test_close_with_tray_hides_without_quitting(self):
        self.panel.tray_enabled = True
        self.panel.show()
        with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True), \
             patch.object(self.panel, 'quit_panel') as quit_panel:
            self.panel.close()
            self.assertFalse(self.panel.isVisible())
            quit_panel.assert_not_called()
            self.panel.show_panel()
            self.assertTrue(self.panel.isVisible())

    def test_no_tray_never_strands_hidden_window(self):
        self.panel.hide()
        self.panel.check_tray()
        self.assertTrue(self.panel.isVisible())
        self.assertFalse(self.panel.hide_button.isEnabled())

    def test_logs_and_status_text_do_not_interpret_html(self):
        value = build_snapshot(Path('/tmp/test'), {'status': 'failed', 'error': '<b>failure</b>'}, {},
                               {'ActiveState': 'failed'}, '<script>test</script>', 3, 'Unavailable', 0)
        self.panel.render(value)
        self.assertEqual(self.panel.error.text(), '<b>failure</b>')
        self.assertEqual(self.panel.logs.toPlainText(), '<script>test</script>')
        self.panel.log_toggle.click()
        self.assertFalse(self.panel.logs.isHidden())

    def test_notifications_only_on_transition_not_initial_history(self):
        self.panel.tray_enabled = True
        success = build_snapshot(Path('/tmp/test'), {'status': 'succeeded'}, {},
                                 {'ActiveState': 'inactive'}, '', None, '', 0)
        with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True), \
             patch.object(self.panel.tray, 'showMessage') as notify:
            self.panel.render(success)
            notify.assert_not_called()
            self.panel.previous_status = 'running'
            self.panel.render(success)
            self.panel.render(success)
            self.assertEqual(notify.call_count, 1)


if __name__ == '__main__':
    unittest.main()
