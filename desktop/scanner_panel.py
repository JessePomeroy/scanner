"""Native Scanner monitor with guarded, opt-in texture recovery."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PySide6.QtCore import QLockFile, QSettings, QStandardPaths, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QStyle,
    QScrollArea, QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from desktop.monitor import Snapshot, collect, duration, read_record
from desktop.resume import launch, preview
from desktop.features import delivery_checks, diagnostic_summary, validate_selection
from desktop.theme import GREEN, RUST, message_palette, scanner_palette


class ResumeTask(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, action):
        super().__init__()
        self.action = action

    def run(self):
        try:
            self.result.emit(self.action())
        except Exception as error:
            self.failed.emit(str(error))


class Poller(QThread):
    sampled = Signal(object)
    failed = Signal(str)

    def __init__(self, run: Path, unit: str):
        super().__init__()
        self.run_dir, self.unit = run, unit

    def run(self):
        while not self.isInterruptionRequested():
            try:
                self.sampled.emit(collect(self.run_dir, self.unit))
            except Exception as error:
                self.failed.emit(str(error))
            for _ in range(30):
                if self.isInterruptionRequested():
                    return
                self.msleep(100)


def label(text: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


class Panel(QMainWindow):
    def __init__(self, run: Path, unit: str, *, poll: bool = True, tray: bool = True):
        super().__init__()
        self.setPalette(scanner_palette(self.palette()))
        self.run_dir, self.unit = run, unit
        self.snapshot: Snapshot | None = None
        self.previous_status: str | None = None
        self.quitting = False
        self.resume_task = None
        self.polling_enabled = poll
        self.retired_pollers = []
        self.checks = ()
        self.tray_enabled = tray
        self.setWindowTitle("Scanner — Reconstruction")
        self.resize(520, 600)
        self.setMinimumWidth(380)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        self.title = label("Checking reconstruction…")
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.45)
        font.setBold(True)
        self.title.setFont(font)
        layout.addWidget(self.title)
        self.identity = label(f"{run.parent.name} / {run.name}")
        self.identity.setToolTip(str(run))
        selection = QHBoxLayout()
        selection.setSpacing(12)
        selection.addWidget(self.identity, 1)
        self.choose_button = QPushButton('Choose run…')
        self.choose_button.clicked.connect(self.choose_run)
        selection.addWidget(self.choose_button)
        layout.addLayout(selection)
        stage_layout = QVBoxLayout()
        stage_layout.setSpacing(4)
        self.stage = label("Waiting for evidence")
        font = self.stage.font()
        font.setBold(True)
        self.stage.setFont(font)
        stage_layout.addWidget(self.stage)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Current operation progress")
        self.progress.setRange(0, 0)
        stage_layout.addWidget(self.progress)
        self.operation = label("Reading service state and logs…")
        stage_layout.addWidget(self.operation)
        layout.addLayout(stage_layout)
        metrics = QHBoxLayout()
        metrics.setSpacing(16)
        self.values = {}
        for fields in ((("elapsed", "Attempt elapsed"), ("memory", "Job RAM")),
                       (("stage_elapsed", "Stage elapsed"), ("gpu", "GPU (system)"))):
            form = QFormLayout()
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setHorizontalSpacing(8)
            form.setVerticalSpacing(4)
            for key, title in fields:
                self.values[key] = label("Checking…")
                form.addRow(title, self.values[key])
            metrics.addLayout(form, 1)
        layout.addLayout(metrics)
        eta = QHBoxLayout()
        eta.setSpacing(8)
        eta.addWidget(label("Estimated remaining"))
        self.values["eta"] = label("Checking…")
        eta.addWidget(self.values["eta"], 1)
        layout.addLayout(eta)
        completion = QHBoxLayout()
        completion.setSpacing(8)
        self.completed = label()
        self.completed.setPalette(message_palette(self.palette(), GREEN))
        self.completed.setAutoFillBackground(True)
        self.completed.setMargin(6)
        completion.addWidget(self.completed, 1)
        self.check_toggle = QPushButton('Checklist')
        self.check_toggle.setAccessibleName('Show completion checklist')
        self.check_toggle.setCheckable(True)
        self.checklist = label()
        self.checklist.hide()
        self.check_toggle.toggled.connect(self.toggle_checklist)
        completion.addWidget(self.check_toggle)
        layout.addLayout(completion)
        layout.addWidget(self.checklist)
        self.activity = label()
        layout.addWidget(self.activity)
        self.error = label()
        self.error.setPalette(message_palette(self.palette(), RUST))
        self.error.setAutoFillBackground(True)
        self.error.setMargin(6)
        self.error.hide()
        layout.addWidget(self.error)
        self.recovery_note = label("Recovery is disabled while reconstruction is running.")
        layout.addWidget(self.recovery_note)
        self.resume_button = QPushButton("Review texture resume…")
        self.resume_button.setEnabled(False)
        self.resume_button.clicked.connect(self.review_resume)
        layout.addWidget(self.resume_button)
        self.log_toggle = QPushButton("Show recent log")
        self.log_toggle.setCheckable(True)
        self.log_toggle.toggled.connect(self.toggle_log)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setAccessibleName("Recent reconstruction log")
        self.logs.setMaximumBlockCount(200)
        self.logs.setFixedHeight(150)
        self.logs.hide()
        layout.addWidget(self.logs)
        buttons = QGridLayout()
        buttons.addWidget(self.log_toggle, 0, 0)
        self.folder = QPushButton("Output folder")
        self.folder.setEnabled(False)
        self.folder.clicked.connect(self.open_output)
        buttons.addWidget(self.folder, 0, 1)
        self.hide_button = QPushButton("Hide to tray")
        self.hide_button.clicked.connect(self.hide_to_tray)
        buttons.addWidget(self.hide_button, 1, 0)
        layout.addLayout(buttons)
        self.copy_button = QPushButton('Copy diagnostic summary')
        self.copy_button.setToolTip('Includes local paths. Review before sharing; full logs and commands are excluded.')
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_diagnostics)
        buttons.addWidget(self.copy_button, 1, 1)
        self.footer = label("Closing leaves reconstruction running.")
        layout.addWidget(self.footer)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)
        self.tray = QSystemTrayIcon(self)
        self.menu = QMenu(self)
        self.show_action = self.menu.addAction("Show Scanner")
        self.show_action.triggered.connect(self.show_panel)
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("Quit panel (leave reconstruction running)")
        self.quit_action.triggered.connect(self.quit_panel)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.tray_activated)
        self.set_status_icon("unknown")
        if tray:
            self.tray.show()
        file_menu = self.menuBar().addMenu("Panel")
        file_menu.addAction(self.show_action)
        file_menu.addAction(self.quit_action)
        self.check_tray()
        self.tray_timer = QTimer(self)
        self.tray_timer.timeout.connect(self.check_tray)
        self.tray_timer.start(3000)
        self.poller = Poller(run, unit)
        self.connect_poller(self.poller)
        if poll:
            self.poller.start()

    def set_status_icon(self, status: str):
        theme, fallback = {
            "running": ("view-refresh", QStyle.StandardPixmap.SP_BrowserReload),
            "succeeded": ("dialog-ok", QStyle.StandardPixmap.SP_DialogApplyButton),
            "failed": ("dialog-error", QStyle.StandardPixmap.SP_MessageBoxCritical),
        }.get(status, ("dialog-warning", QStyle.StandardPixmap.SP_MessageBoxWarning))
        icon = QIcon.fromTheme(theme, self.style().standardIcon(fallback))
        self.setWindowIcon(icon)
        self.tray.setIcon(icon)

    def render(self, value: Snapshot):
        self.snapshot = value
        self.copy_button.setEnabled(True)
        self.checks = value.checks or delivery_checks(None, value.status)
        self.checklist.setText('\n\n'.join(self.checks))
        titles = {"running": "Reconstruction running", "succeeded": "Reconstruction finished",
                  "failed": "Reconstruction failed", "interrupted": "Reconstruction interrupted",
                  "stopped": "Reconstruction stopped", "unknown": "Status unavailable"}
        self.title.setText(titles[value.status])
        self.stage.setText(value.stage if value.status == "running" else f"Last stage: {value.stage}")
        self.set_status_icon(value.status)
        self.tray.setToolTip(f"Scanner: {titles[value.status]}\n{value.stage}")
        if value.progress.percent is None:
            self.progress.setRange(0, 0 if value.status == "running" else 100)
            self.progress.setValue(0)
            self.progress.setTextVisible(False)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(round(value.progress.percent * 10))
            self.progress.setFormat(f"{value.progress.percent:.1f}% of current operation")
            self.progress.setTextVisible(True)
        self.progress.setVisible(value.status == "running")
        self.operation.setText(value.progress.operation if value.status == "running" else
                               "Processing finished; visual quality still needs checking." if value.status == "succeeded" else
                               "Saved outputs are retained. Review the error and logs before recovery.")
        for key in ("elapsed", "stage_elapsed", "memory"):
            self.values[key].setText(getattr(value, key))
        self.values["gpu"].setText(value.gpu.removesuffix(" (whole GPU)"))
        self.values["eta"].setText(value.progress.eta if value.status == "running" else "Not applicable")
        self.completed.setText(value.completed)
        age = f"{duration(value.log_age)} ago" if value.log_age is not None else "unavailable"
        quiet = " (quiet logs do not prove a stall)" if value.status == "running" and value.log_age is not None and value.log_age > 60 else ""
        self.activity.setText(f"Last log message · {age}{quiet}\n{value.last_activity}")
        self.error.setVisible(bool(value.error))
        self.error.setText(value.error)
        self.folder.setEnabled(value.output is not None and value.output.is_dir())
        self.logs.setPlainText(value.log)
        self.footer.setText(f"Checked {value.sampled_at} · Closing leaves reconstruction running.")
        if self.previous_status is not None and value.status != self.previous_status and value.status in {"succeeded", "failed", "interrupted"}:
            if self.tray_enabled and QSystemTrayIcon.isSystemTrayAvailable():
                kind = QSystemTrayIcon.MessageIcon.Information if value.status == "succeeded" else QSystemTrayIcon.MessageIcon.Warning
                self.tray.showMessage("Scanner", titles[value.status] + ". Open the panel for details.", kind)
        if value.status != self.previous_status:
            QTimer.singleShot(0, self.fit_content_height)
        self.previous_status = value.status
        if not self.resume_task:
            allowed = value.status in {'failed', 'interrupted', 'stopped'} and value.stage == 'Texturing mesh'
            self.resume_button.setEnabled(allowed)
            self.recovery_note.setText(
                'Review a texture-only retry. Inputs will be verified before confirmation; earlier outputs stay untouched.' if allowed else
                'Recovery is disabled while reconstruction is running.' if value.status == 'running' else
                'No supported retry available. Only failed texturing from a completed mesh can resume here.')

    def start_resume_task(self, action, callback, on_error=None):
        self.resume_button.setEnabled(False)
        self.choose_button.setEnabled(False)
        self.quit_action.setEnabled(False)
        self.resume_task = ResumeTask(action)
        self.resume_task.result.connect(callback)
        self.resume_task.failed.connect(on_error or self.resume_failed)
        self.resume_task.finished.connect(self.resume_task_finished)
        self.resume_task.start()

    def resume_task_finished(self):
        self.resume_task = None
        self.choose_button.setEnabled(True)
        self.quit_action.setEnabled(True)

    def review_resume(self):
        if self.resume_task:
            return
        self.recovery_note.setText('Checking service, completed mesh, source images, and tool fingerprints…')
        self.start_resume_task(lambda: preview(self.run_dir, self.unit), self.confirm_resume)

    def confirm_resume(self, approved):
        # The background check has finished; queue the next action after QThread.finished.
        message = QMessageBox(self)
        message.setWindowTitle('Resume texturing?')
        message.setIcon(QMessageBox.Icon.Question)
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setText('Retry texturing from the saved mesh?')
        message.setInformativeText(
            f"Verified {approved['images']} source images and the completed mesh.\n\n"
            'Only TextureMesh will run: four workers, 48 GiB RAM limit, no swap. '
            'The failed stage starts from its beginning; earlier stages are not repeated.\n\n'
            f"New outputs go into a fresh panel-resume folder under:\n{self.run_dir.parent}\n\n"
            'Existing outputs and failed-attempt evidence are preserved. The same settings may fail again.')
        message.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        message.button(QMessageBox.StandardButton.Yes).setText('Resume texturing')
        message.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if message.exec() == QMessageBox.StandardButton.Yes:
            QTimer.singleShot(0, lambda: self.begin_resume(approved))
        else:
            self.recovery_note.setText('Resume cancelled. No worker or output was changed.')

    def begin_resume(self, approved):
        if self.resume_task:
            QTimer.singleShot(50, lambda: self.begin_resume(approved))
            return
        self.recovery_note.setText('Rechecking confirmed inputs and starting a separate attempt…')
        self.start_resume_task(lambda: launch(approved), self.resume_started)

    def resume_started(self, result):
        self.select_run(result)
        self.recovery_note.setText('New attempt launched. Following its progress; previous outputs are preserved.')

    def connect_poller(self, poller):
        poller.sampled.connect(lambda value: self.render(value) if self.poller is poller else None)
        poller.failed.connect(lambda message: self.poll_failed(message) if self.poller is poller else None)

    def select_run(self, result):
        old = self.poller
        old.requestInterruption()
        if old.isRunning():
            self.retired_pollers.append(old)
            old.finished.connect(lambda: self.retired_pollers.remove(old))
        self.run_dir, self.unit = result
        self.identity.setText(f'{self.run_dir.parent.name} / {self.run_dir.name}')
        self.identity.setToolTip(str(self.run_dir))
        self.previous_status = None
        self.snapshot = None
        self.copy_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.folder.setEnabled(False)
        self.title.setText('Checking selected run…')
        self.stage.setText('Waiting for fresh evidence')
        self.activity.clear()
        self.error.clear()
        self.logs.clear()
        self.checklist.setText('Waiting for fresh evidence')
        self.progress.hide()
        self.operation.setText('Switching observation does not change either reconstruction.')
        self.recovery_note.setText('Waiting for fresh service state before enabling recovery.')
        self.footer.setText('Checking selected run…')
        self.completed.clear()
        for value in self.values.values():
            value.setText('Checking…')
        self.poller = Poller(self.run_dir, self.unit)
        self.connect_poller(self.poller)
        if self.polling_enabled:
            self.poller.start()
        settings = QSettings('Scanner', 'DesktopMonitor')
        settings.setValue('run', str(self.run_dir))
        settings.setValue('unit', self.unit)

    def choose_run(self):
        if self.resume_task:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Choose reconstruction attempt')
        dialog.resize(560, 240)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label('Select an attempt containing state.json and plan.json, and its exact user service. This only switches monitoring.'))
        form = QFormLayout()
        path_edit = QLineEdit(str(self.run_dir))
        unit_edit = QLineEdit(self.unit)
        form.addRow('Attempt folder', path_edit)
        form.addRow('User service', unit_edit)
        layout.addLayout(form)
        browse = QPushButton('Browse folders…')
        def pick():
            selected = QFileDialog.getExistingDirectory(dialog, 'Select attempt', path_edit.text())
            if selected:
                path_edit.setText(selected)
                try:
                    recorded = read_record(Path(selected) / 'plan.json').get('service')
                    unit_edit.setText(recorded if isinstance(recorded, str) else '')
                except (OSError, ValueError):
                    unit_edit.clear()
        browse.clicked.connect(pick)
        layout.addWidget(browse)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            run, unit = Path(path_edit.text().strip()), unit_edit.text().strip()
            self.start_resume_task(lambda: validate_selection(run, unit), self.select_run, self.selection_failed)

    def selection_failed(self, message):
        QMessageBox.warning(self, 'Selection unchanged', message)

    def copy_diagnostics(self):
        if self.snapshot:
            QApplication.clipboard().setText(diagnostic_summary(self.snapshot, self.run_dir, self.unit, self.checks))
            self.footer.setText('Summary copied · Includes local paths; review before sharing.')

    def resume_failed(self, message):
        self.recovery_note.setText(f'Resume blocked: {message}')
        QMessageBox.warning(self, 'Resume not started or needs review', message)

    def poll_failed(self, message: str):
        self.resume_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.title.setText("Monitor unavailable")
        self.error.setText(f"Could not refresh: {message}. Reconstruction has not been changed.")
        self.error.show()
        self.progress.hide()
        self.set_status_icon("unknown")
        QTimer.singleShot(0, self.fit_content_height)

    def toggle_log(self, checked: bool):
        self.logs.setVisible(checked)
        self.log_toggle.setText("Hide recent log" if checked else "Show recent log")
        QTimer.singleShot(0, lambda: self.fit_content_height(shrink=True))

    def toggle_checklist(self, checked: bool):
        self.checklist.setVisible(checked)
        self.check_toggle.setText('Hide checklist' if checked else 'Checklist')
        self.check_toggle.setAccessibleName('Hide completion checklist' if checked else 'Show completion checklist')
        QTimer.singleShot(0, lambda: self.fit_content_height(shrink=True))

    def fit_content_height(self, *, shrink: bool = False):
        if self.isMaximized() or self.isMinimized():
            return
        scroll = self.centralWidget()
        content = scroll.widget().layout()
        content.activate()
        needed = content.totalHeightForWidth(scroll.viewport().width()) + self.menuBar().height()
        # Grow for details and state changes, retaining a scroll fallback for small
        # screens or large system fonts. Never shrink text or hide overflowing data.
        decoration = max(0, self.frameGeometry().height() - self.height())
        available = self.screen().availableGeometry().height() - decoration - 32
        height = min(available, max(600, needed))
        if shrink or height > self.height():
            self.resize(self.width(), height)

    def open_output(self):
        if self.snapshot and self.snapshot.output and self.snapshot.output.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.snapshot.output)))

    def check_tray(self):
        available = self.tray_enabled and QSystemTrayIcon.isSystemTrayAvailable()
        self.hide_button.setEnabled(available)
        self.hide_button.setToolTip("Reconstruction continues independently." if available else "System tray unavailable; minimize to the taskbar instead.")
        if not available and not self.isVisible() and not self.quitting:
            self.show()

    def hide_to_tray(self):
        if self.tray_enabled and QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()

    def show_panel(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_panel()

    def closeEvent(self, event):
        if not self.quitting and self.tray_enabled and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
        else:
            event.accept()
            self.quit_panel()

    def stop_polling(self):
        for poller in [self.poller, *self.retired_pollers]:
            poller.requestInterruption()
            poller.wait(6000)

    def quit_panel(self):
        if self.resume_task:
            QMessageBox.information(self, 'Recovery check in progress', 'Wait for the recovery check or launch to finish before quitting the panel.')
            return
        self.quitting = True
        self.tray.hide()
        self.stop_polling()
        QApplication.instance().quit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path, help="Recovery attempt directory containing state.json and plan.json")
    parser.add_argument("--unit", required=True, help="Exact scanner-*.service user unit to observe")
    parser.add_argument('--restore-selection', action='store_true', help='Restore the last panel selection (application-menu launcher)')
    args = parser.parse_args()
    run = args.run.expanduser().resolve()
    app = QApplication(sys.argv[:1])
    app.setPalette(scanner_palette(app.palette()))
    app.setApplicationName("Scanner Monitor")
    app.setQuitOnLastWindowClosed(False)
    name = 'scanner-desktop-monitor'
    runtime = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation))
    lock = QLockFile(str(runtime / f"{name}.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        socket = QLocalSocket()
        socket.connectToServer(name)
        if socket.waitForConnected(1000):
            socket.write(b"show")
            socket.waitForBytesWritten(1000)
        else:
            QMessageBox.information(None, "Scanner", "The panel is already open. Look for Scanner in the system tray.")
        return 0
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    unit = args.unit
    if args.restore_selection:
        settings = QSettings('Scanner', 'DesktopMonitor')
        saved_run, saved_unit = settings.value('run'), settings.value('unit')
        if isinstance(saved_run, str) and isinstance(saved_unit, str):
            candidate = Path(saved_run)
            if (candidate / 'state.json').is_file() and (candidate / 'plan.json').is_file():
                run, unit = candidate, saved_unit
    try:
        latest = read_record(run.parent / '.panel-latest.json')
        candidate = Path(latest['run'])
        if candidate.parent == run.parent and candidate.name.startswith('panel-resume-'):
            candidate_plan = read_record(candidate / 'plan.json')
            if candidate_plan.get('service') == latest['unit']:
                run, unit = candidate, latest['unit']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    panel = Panel(run, unit)

    def activate():
        connection = server.nextPendingConnection()
        if connection:
            panel.show_panel()
            connection.close()
            connection.deleteLater()

    server.newConnection.connect(activate)
    if not server.listen(name):
        panel.error.setText("Could not enable reopen shortcut; use the tray icon to reopen.")
        panel.error.show()
    app.aboutToQuit.connect(panel.stop_polling)
    panel.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
