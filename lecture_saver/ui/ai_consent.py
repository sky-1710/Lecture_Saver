"""The one-time agreement before any lecture text is sent to an assistant.

Translation and the study pack send the same thing - the transcript text -
to the same place, so they share one agreement and one setting. Agreeing in
either covers both; the wording names both so nobody agrees to one and is
surprised by the other.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

from ..config import Settings

__all__ = ["consent_text", "ensure_ai_consent"]


def consent_text(engine_label: str) -> str:
    """The wording of the agreement. Kept separate so a test can read it."""
    return (
        "Lecture Saver does everything else on this computer. This feature is "
        "different.<br><br>"
        "To translate the lecture or write study material from it, the lecture's "
        f"<b>transcript text</b> is sent to {engine_label}. "
        "That text leaves this computer.<br><br>"
        "<b>What is never sent:</b> the video, the audio, and the slide images. "
        "Only the words.<br><br>"
        "You can turn this off again at any time, and the rest of the application "
        "keeps working with no internet connection.<br><br>"
        "Send the transcript?"
    )


def ensure_ai_consent(parent: QWidget, settings: Settings, engine_label: str) -> bool:
    """Ask once, the first time, and remember the answer (§29)."""
    if settings.translation_enabled:
        return True
    box = QMessageBox(parent)
    box.setWindowTitle("This sends the transcript out")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(consent_text(engine_label))
    send = box.addButton("Send the transcript", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is not send:
        return False
    settings.translation_enabled = True
    return True
