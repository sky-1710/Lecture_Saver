"""One place that decides how the application looks (§11).

Every colour, radius and spacing decision lives here, so the widgets stay
about behaviour and the look can be changed in one file. Widgets ask for a
*role* - "muted", "danger", "card" - rather than writing hex codes inline:

    label = QLabel("42 slides")
    set_role(label, Role.MUTED)

The stylesheet is applied once to the ``QApplication`` in ``app.py``. Widgets
built in tests are therefore unstyled, which is deliberate: a smoke test should
fail on broken wiring, never on a colour.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget

__all__ = [
    "ACCENT",
    "ACCENT_SOFT",
    "BORDER",
    "DANGER",
    "EDIT_TINT",
    "MUTED",
    "SUBTLE",
    "SURFACE",
    "TEXT",
    "VIDEO_BACKDROP",
    "Role",
    "app_icon",
    "apply_theme",
    "set_flag",
    "key_hints",
    "set_role",
    "stylesheet",
]

# -- tokens -----------------------------------------------------------------

#: Page behind the panels.
WINDOW = "#EFF1F4"
#: Panels, cards, inputs, menus.
SURFACE = "#FFFFFF"
#: Hover fill and quiet rows.
SURFACE_HOVER = "#F4F5F8"
SURFACE_SUNKEN = "#E9EBEF"

BORDER = "#E1E4EA"
BORDER_STRONG = "#CBD1DA"
BORDER_HOVER = "#B3BAC5"

TEXT = "#1B1D22"
MUTED = "#66707D"
SUBTLE = "#949CA8"
DISABLED = "#A8AEB8"

ACCENT = "#2E6DF6"
ACCENT_HOVER = "#255FDF"
ACCENT_PRESSED = "#1E51C0"
ACCENT_SOFT = "#E7EFFE"

WARNING_BG = "#FFF6E6"
WARNING_BORDER = "#F1CB8A"
WARNING_TEXT = "#8A5A00"
DANGER = "#C0392B"
SUCCESS = "#1E7B34"
TRANSLATION = "#1E3A8A"

#: The tint behind a transcript row that is being typed into.
EDIT_TINT = "#FFFBEA"
#: Letterbox around the video picture.
VIDEO_BACKDROP = "#101013"

RADIUS = 8
RADIUS_SMALL = 6

MONO_FAMILY = '"Cascadia Mono", "Consolas", monospace'


class Role:
    """Values for the ``role`` property that :func:`stylesheet` selects on."""

    #: Headline on the start screen.
    DISPLAY = "display"
    #: The lecture title in the workspace header.
    TITLE = "title"
    #: A panel heading - "Slides", "Transcription".
    SECTION = "section"
    #: Secondary information: counts, estimates, time ranges.
    MUTED = "muted"
    #: Tertiary hints the eye should skip until it needs them.
    SUBTLE = "subtle"
    DANGER = "danger"
    WARNING = "warning"
    SUCCESS = "success"
    #: The imported Korean translation.
    TRANSLATION = "translation"
    #: A monospaced block - diagnostics, debug output.
    MONO = "mono"


def set_role(widget: QWidget, role: str | None) -> None:
    """Give ``widget`` a role, or clear it, and repaint it under the new rule."""
    widget.setProperty("role", role or "")
    repolish(widget)


def set_flag(widget: QWidget, name: str, value: object = True) -> None:
    """Set a style property the stylesheet selects on - ``card``, ``banner``."""
    widget.setProperty(name, value)
    repolish(widget)


def key_hints(keys: Sequence[tuple[str, str]]) -> str:
    """``[("D", "draw"), ...]`` as one line of rich text: each key picked out
    in the text colour, what it does in the label's own (muted) colour.

    A narrow pane wraps the line between entries only, never inside a key or
    between a key and what it does.
    """

    def unbroken(text: str) -> str:
        return escape(text).replace(" ", "&nbsp;")

    return "&nbsp;&nbsp;·&nbsp; ".join(
        f'<b style="color:{TEXT};">{unbroken(key)}</b>&nbsp;{unbroken(what)}'
        for key, what in keys
    )


def repolish(widget: QWidget) -> None:
    """Re-run the stylesheet for ``widget`` after a style property changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


# -- stylesheet -------------------------------------------------------------


def stylesheet() -> str:
    """The application-wide Qt stylesheet, built from the tokens above."""
    return f"""
/* ---- surfaces ---------------------------------------------------------- */

QMainWindow, QDialog {{
    background: {WINDOW};
}}

QWidget {{
    color: {TEXT};
}}

QWidget:disabled {{
    color: {DISABLED};
}}

/* Panels, and any frame that opts in with setProperty("card", True). */
QFrame[card="true"] {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}

QWidget[surface="true"] {{
    background: {SURFACE};
}}

/* A strip that separates itself from what is above or below it. */
QFrame[rule="top"] {{
    border: none;
    border-top: 1px solid {BORDER};
}}

QFrame[rule="bottom"] {{
    border: none;
    border-bottom: 1px solid {BORDER};
}}

QFrame[banner="warning"] {{
    background: {WARNING_BG};
    border: 1px solid {WARNING_BORDER};
    border-radius: {RADIUS}px;
}}

QFrame[banner="warning"] QLabel {{
    color: {WARNING_TEXT};
    border: none;
}}

/* ---- text -------------------------------------------------------------- */

QLabel[role="display"] {{
    font-size: 25px;
    font-weight: 600;
}}

QLabel[role="title"] {{
    font-size: 16px;
    font-weight: 600;
}}

QLabel[role="section"] {{
    font-weight: 600;
    color: {TEXT};
}}

QLabel[role="muted"] {{
    color: {MUTED};
}}

QLabel[role="subtle"] {{
    color: {SUBTLE};
}}

QLabel[role="danger"] {{
    color: {DANGER};
}}

QLabel[role="warning"] {{
    color: {WARNING_TEXT};
}}

QLabel[role="success"] {{
    color: {SUCCESS};
}}

QLabel[role="mono"], QTextBrowser[role="mono"], QPlainTextEdit[role="mono"] {{
    font-family: {MONO_FAMILY};
}}

QTextBrowser[role="translation"] {{
    color: {TRANSLATION};
}}

/* ---- menus and chrome -------------------------------------------------- */

QMenuBar {{
    background: {SURFACE};
    border-bottom: 1px solid {BORDER};
    padding: 2px 4px;
}}

QMenuBar::item {{
    background: transparent;
    padding: 5px 10px;
    border-radius: {RADIUS_SMALL}px;
}}

QMenuBar::item:selected {{
    background: {ACCENT_SOFT};
    color: {ACCENT_PRESSED};
}}

QMenu {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 6px;
}}

QMenu::item {{
    padding: 6px 24px 6px 12px;
    border-radius: {RADIUS_SMALL}px;
}}

QMenu::item:selected {{
    background: {ACCENT_SOFT};
    color: {ACCENT_PRESSED};
}}

QMenu::item:disabled {{
    color: {DISABLED};
}}

QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 5px 8px;
}}

QStatusBar {{
    background: {SURFACE};
    border-top: 1px solid {BORDER};
    color: {MUTED};
}}

QStatusBar::item {{
    border: none;
}}

QToolTip {{
    background: #23262C;
    color: #F5F6F8;
    border: none;
    border-radius: {RADIUS_SMALL}px;
    padding: 6px 8px;
}}

/* ---- buttons ----------------------------------------------------------- */

QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SMALL}px;
    padding: 6px 14px;
    min-height: 18px;
    color: {TEXT};
}}

QPushButton:hover {{
    background: {SURFACE_HOVER};
    border-color: {BORDER_HOVER};
}}

QPushButton:pressed {{
    background: {SURFACE_SUNKEN};
}}

QPushButton:disabled {{
    background: {SURFACE_HOVER};
    border-color: {BORDER};
    color: {DISABLED};
}}

/* The one action a screen is really for. */
QPushButton[accent="true"], QPushButton:default {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: #FFFFFF;
    font-weight: 600;
}}

QPushButton[accent="true"]:hover, QPushButton:default:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}

QPushButton[accent="true"]:pressed, QPushButton:default:pressed {{
    background: {ACCENT_PRESSED};
    border-color: {ACCENT_PRESSED};
}}

QPushButton[accent="true"]:disabled, QPushButton:default:disabled {{
    background: #BACCF7;
    border-color: #BACCF7;
    color: #FFFFFF;
}}

QPushButton[quiet="true"] {{
    background: transparent;
    border-color: transparent;
    color: {MUTED};
}}

QPushButton[quiet="true"]:hover {{
    background: {SURFACE_HOVER};
    border-color: {BORDER};
    color: {TEXT};
}}

QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS_SMALL}px;
    padding: 4px 7px;
    color: {MUTED};
}}

QToolButton:hover {{
    background: {SURFACE_HOVER};
    border-color: {BORDER};
    color: {TEXT};
}}

QToolButton:pressed {{
    background: {SURFACE_SUNKEN};
}}

/* The drawing tool in use. */
QToolButton:checked {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT_SOFT};
    color: {ACCENT};
}}

/* A tool button with a menu beside it - the pen and its kinds. */
QToolButton[popupMode="1"] {{
    padding-right: 18px;
}}

QToolButton::menu-button {{
    border: none;
    border-left: 1px solid {BORDER};
    width: 14px;
}}

QToolButton:disabled {{
    color: {DISABLED};
}}

/* ---- inputs ------------------------------------------------------------ */

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SMALL}px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT};
}}

QLineEdit:disabled, QPlainTextEdit:disabled {{
    background: {SURFACE_HOVER};
    color: {DISABLED};
}}

QComboBox {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SMALL}px;
    padding: 5px 8px;
    min-height: 18px;
}}

QComboBox:hover {{
    border-color: {BORDER_HOVER};
}}

QComboBox:focus, QComboBox:on {{
    border-color: {ACCENT};
}}

QComboBox:disabled {{
    background: {SURFACE_HOVER};
    color: {DISABLED};
}}

QComboBox QAbstractItemView {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SMALL}px;
    padding: 4px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
    outline: none;
}}

QCheckBox, QRadioButton {{
    spacing: 7px;
    padding: 2px 0;
}}

QCheckBox:disabled, QRadioButton:disabled {{
    color: {DISABLED};
}}

QGroupBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    margin-top: 14px;
    padding: 12px 12px 10px 12px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 5px;
    color: {TEXT};
}}

/* ---- lists ------------------------------------------------------------- */

QListView, QTreeView, QTableView {{
    background: transparent;
    border: none;
    outline: none;
}}

QListView::item, QTreeView::item {{
    border-radius: {RADIUS_SMALL}px;
    padding: 2px;
}}

QListView::item:hover, QTreeView::item:hover {{
    background: {SURFACE_HOVER};
}}

QListView::item:selected, QTreeView::item:selected {{
    background: {ACCENT_SOFT};
    color: {TEXT};
}}

QTextBrowser {{
    background: transparent;
    border: none;
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}

/* ---- tabs -------------------------------------------------------------- */

QTabBar {{
    background: transparent;
}}

QTabBar::tab {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 5px 10px;
    margin-right: 2px;
    color: {MUTED};
}}

QTabBar::tab:hover {{
    color: {TEXT};
}}

QTabBar::tab:selected {{
    color: {ACCENT};
    border-bottom: 2px solid {ACCENT};
    font-weight: 600;
}}

QTabBar::tab:disabled {{
    color: {DISABLED};
}}

/* ---- progress and sliders ---------------------------------------------- */

QProgressBar {{
    background: {SURFACE_SUNKEN};
    border: none;
    border-radius: 4px;
    min-height: 8px;
    max-height: 8px;
    text-align: center;
    color: {MUTED};
}}

QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 4px;
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: #D5D9E0;
    border-radius: 2px;
}}

QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background: {SURFACE};
    border: 2px solid {ACCENT};
    width: 11px;
    height: 11px;
    margin: -5px 0;
    border-radius: 7px;
}}

QSlider::handle:horizontal:hover {{
    border-color: {ACCENT_HOVER};
}}

QSlider::groove:horizontal:disabled {{
    background: {SURFACE_SUNKEN};
}}

QSlider::sub-page:horizontal:disabled {{
    background: {BORDER_STRONG};
}}

QSlider::handle:horizontal:disabled {{
    border-color: {BORDER_STRONG};
}}

/* ---- scrollbars -------------------------------------------------------- */

QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 2px 2px 2px 0;
}}

QScrollBar::handle:vertical {{
    background: #C4CAD3;
    border-radius: 4px;
    min-height: 30px;
}}

QScrollBar::handle:vertical:hover {{
    background: #A6AEBA;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 0 2px 2px 2px;
}}

QScrollBar::handle:horizontal {{
    background: #C4CAD3;
    border-radius: 4px;
    min-width: 30px;
}}

QScrollBar::handle:horizontal:hover {{
    background: #A6AEBA;
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
    background: transparent;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* ---- splitter ---------------------------------------------------------- */

QSplitter::handle {{
    background: transparent;
}}

QSplitter::handle:hover {{
    background: {ACCENT_SOFT};
}}

QSplitter::handle:pressed {{
    background: {ACCENT};
}}
"""


# -- palette ----------------------------------------------------------------


def _palette() -> QPalette:
    """Base colours for everything the stylesheet does not reach.

    Custom-painted widgets - the transcript delegate above all - read their
    colours from the palette, so it has to agree with the stylesheet.
    """
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(WINDOW))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(SURFACE_HOVER))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(SUBTLE))
    palette.setColor(QPalette.ColorRole.Button, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#23262C"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#F5F6F8"))
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.Mid, QColor(BORDER_STRONG))
    palette.setColor(QPalette.ColorRole.Midlight, QColor(BORDER))
    palette.setColor(QPalette.ColorRole.Dark, QColor(MUTED))

    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.Text, QColor(DISABLED))
    palette.setColor(disabled, QPalette.ColorRole.WindowText, QColor(DISABLED))
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, QColor(DISABLED))
    palette.setColor(disabled, QPalette.ColorRole.Highlight, QColor(SURFACE_SUNKEN))
    palette.setColor(disabled, QPalette.ColorRole.HighlightedText, QColor(MUTED))
    return palette


def app_icon() -> QIcon:
    """A window icon drawn in code, so the application ships no binary assets."""
    icon = QIcon()
    for size in (16, 32, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        gradient = QLinearGradient(QPointF(0.0, 0.0), QPointF(float(size), float(size)))
        gradient.setColorAt(0.0, QColor("#4C86FF"))
        gradient.setColorAt(1.0, QColor(ACCENT_PRESSED))
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, size, size, size * 0.24, size * 0.24)

        # A slide, with two lines of speech beneath it.
        inset = size * 0.2
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRoundedRect(
            QRectF(inset, inset * 1.1, size - inset * 2, (size - inset * 2) * 0.5),
            size * 0.05, size * 0.05,
        )
        painter.setBrush(QColor(255, 255, 255, 185))
        line = max(1.0, size * 0.07)
        top = inset * 1.1 + (size - inset * 2) * 0.62
        for index, width_factor in enumerate((1.0, 0.62)):
            painter.drawRoundedRect(
                QRectF(
                    inset, top + index * line * 1.7,
                    (size - inset * 2) * width_factor, line,
                ),
                line / 2, line / 2,
            )
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def apply_theme(app: QApplication) -> None:
    """Style the whole application. Called once, from ``app.run``."""
    app.setStyle("Fusion")

    font = QFont(app.font())
    # Segoe UI at 9pt is Windows' default, and a touch tight for long transcripts.
    font.setFamilies(["Segoe UI", "Malgun Gothic", "Noto Sans KR"])
    font.setPointSizeF(max(9.5, font.pointSizeF()))
    app.setFont(font)

    app.setPalette(_palette())
    app.setStyleSheet(stylesheet())
    app.setWindowIcon(app_icon())
