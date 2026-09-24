"""Scanner colors expressed through Qt roles, retaining the native widget style."""
from PySide6.QtGui import QColor, QPalette

SAGE = '#a2b5a3'
GREEN = '#58746d'
RUST = '#834d37'
BLUE = '#136899'
TEAL = '#24505d'
# Supporting neutrals keep small text readable; teal on sage is only 4.06:1.
INK = '#142e35'
SURFACE = '#f1f4f0'


def scanner_palette(base: QPalette) -> QPalette:
    palette = QPalette(base)
    roles = {
        QPalette.ColorRole.Window: SAGE,
        QPalette.ColorRole.WindowText: INK,
        QPalette.ColorRole.Base: SURFACE,
        QPalette.ColorRole.AlternateBase: SAGE,
        QPalette.ColorRole.Text: INK,
        QPalette.ColorRole.Button: SURFACE,
        QPalette.ColorRole.ButtonText: TEAL,
        QPalette.ColorRole.Highlight: BLUE,
        QPalette.ColorRole.HighlightedText: '#ffffff',
        QPalette.ColorRole.Accent: BLUE,
        QPalette.ColorRole.ToolTipBase: SURFACE,
        QPalette.ColorRole.ToolTipText: INK,
        QPalette.ColorRole.PlaceholderText: GREEN,
        QPalette.ColorRole.Light: SURFACE,
        QPalette.ColorRole.Midlight: SAGE,
        QPalette.ColorRole.Mid: GREEN,
        QPalette.ColorRole.Dark: TEAL,
        QPalette.ColorRole.Shadow: INK,
        QPalette.ColorRole.BrightText: '#ffffff',
        QPalette.ColorRole.Link: BLUE,
        QPalette.ColorRole.LinkVisited: RUST,
    }
    for role, color in roles.items():
        # Explicit roles also prevent the desktop's inactive dark palette leaking in.
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, QColor(SAGE))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(GREEN))
    return palette


def message_palette(base: QPalette, color: str) -> QPalette:
    palette = QPalette(base)
    palette.setColor(QPalette.ColorRole.Window, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(color))
    return palette
