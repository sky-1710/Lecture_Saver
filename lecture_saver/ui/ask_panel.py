"""The Ask AI panel: questions about the lecture, answered under the video.

The user can ask at any moment - "what did the professor mean just now?" - or
point at transcript lines first: the "Ask AI" button on a line (or its
right-click menu) puts that line above the input, and sending with nothing
typed asks for it to be explained. Several lines can be gathered before
asking.

Answers cite the lecture by time and slide; those citations are links that
seek the video or show the slide. The conversation is the lecture's own and
comes back when the project is reopened (``chat.json``). For the assistant it
is one conversation too - a session it keeps, which every follow-up continues
(see :func:`lecture_saver.ask.ask_question`) - until *New chat*.

Asking is the user's explicit act and the only way this panel sends anything:
the question, the transcript text of the part asked about (or of the whole
lecture, if chosen), and the conversation so far. Never the video, the audio
or the slide images. The first question asks for the same agreement as
translation (:mod:`lecture_saver.ui.ai_consent`).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QKeyEvent, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..ask import Reply, ask_engine_from_settings, forget_session
from ..ask.answer import SEEK_SCHEME, SLIDE_SCHEME, prepare_answer
from ..ask.prompt import (
    SCOPE_LECTURE,
    SCOPE_NEARBY,
    Focus,
    build_follow_up,
    build_prompt,
    question_text,
)
from ..config import Settings
from ..core.chat import ROLE_ASSISTANT, ROLE_USER, ChatMessage, Quote
from ..core.project import Project, ProjectError
from ..core.timecode import format_hms
from ..translate.engine import TranslationEngine
from . import theme
from .ai_consent import ensure_ai_consent
from .theme import Role, set_flag, set_role
from .workers import AskWorker, run_in_thread

log = logging.getLogger(__name__)

__all__ = ["AskPanel", "QuestionEdit"]

#: The choices of how much transcript goes with a question.
SCOPES = (("This part", SCOPE_NEARBY), ("Whole lecture", SCOPE_LECTURE))
#: A quoted line is shown up to this many characters above the input.
QUOTE_PREVIEW_CHARS = 70


class QuestionEdit(QPlainTextEdit):
    """The question box: Enter sends, Shift+Enter starts a new line."""

    submitted = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


def _markdown_html(markdown: str) -> str:
    """Markdown as the HTML fragment a QTextBrowser shows inside a page."""
    document = QTextDocument()
    document.setMarkdown(markdown)
    match = re.search(r"<body[^>]*>(.*)</body>", document.toHtml(), re.S)
    return match.group(1) if match else html.escape(markdown)


def _short(text: str, limit: int = QUOTE_PREVIEW_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class AskPanel(QFrame):
    """Chat about the lecture. Bind a project, then ask."""

    #: Follow a time an answer cited: seconds.
    seekRequested = Signal(float)
    #: Show a slide an answer cited: its number.
    slideRequested = Signal(int)
    #: The panel's own close button.
    hideRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_flag(self, "card")
        self._project: Project | None = None
        self._settings: Settings | None = None
        #: Where the user is now: asked for at the moment a question is sent.
        self._focus: Callable[[], Focus] = Focus
        #: Builds the engine for one question; replaced in tests.
        self.engine_factory: Callable[[], TranslationEngine] | None = None
        self._quotes: list[Quote] = []
        self._thread: QThread | None = None
        self._worker: AskWorker | None = None
        #: The question in flight, already in the conversation.
        self._pending: ChatMessage | None = None
        #: Whether it continues the conversation's session.
        self._continuing = False
        self._error = ""

        title = QLabel("Ask AI")
        set_role(title, Role.SECTION)
        self._scope_box = QComboBox()
        for label, scope in SCOPES:
            self._scope_box.addItem(label, scope)
        self._scope_box.setToolTip(
            "How much of the transcript goes with a question:\n"
            "· This part - the slide asked about and the slides either side of it\n"
            "· Whole lecture - everything, for questions that span the lecture "
            "(slower)"
        )
        self._clear_button = QToolButton()
        self._clear_button.setText("New chat")
        self._clear_button.setToolTip(
            "Forget this lecture's conversation and start again - the assistant forgets it too"
        )
        self._hide_button = QToolButton()
        self._hide_button.setText("✕")
        self._hide_button.setToolTip("Hide the panel. The conversation is kept.")
        header = QHBoxLayout()
        header.setContentsMargins(12, 8, 8, 4)
        header.setSpacing(6)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._scope_box)
        header.addWidget(self._clear_button)
        header.addWidget(self._hide_button)

        self._view = QTextBrowser()
        self._view.setOpenLinks(False)
        self._view.setFrameShape(QFrame.Shape.NoFrame)

        # The lines pointed at, waiting above the input until the question goes.
        self._quote_box = QWidget()
        self._quote_layout = QVBoxLayout(self._quote_box)
        self._quote_layout.setContentsMargins(0, 0, 0, 0)
        self._quote_layout.setSpacing(2)
        self._quote_box.setVisible(False)

        self._input = QuestionEdit()
        self._input.setPlaceholderText(
            "Ask about the lecture. Enter sends · Shift+Enter for a new line"
        )
        self._input.setFixedHeight(58)
        self._send_button = QPushButton("Ask")
        set_flag(self._send_button, "accent")
        self._status = QLabel("")
        self._status.setWordWrap(True)
        set_role(self._status, Role.SUBTLE)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self._status, 1)
        row.addWidget(self._send_button)

        footer = QVBoxLayout()
        footer.setContentsMargins(12, 4, 12, 10)
        footer.setSpacing(4)
        footer.addWidget(self._quote_box)
        footer.addWidget(self._input)
        footer.addLayout(row)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(self._view, 1)
        layout.addLayout(footer)

        self._view.anchorClicked.connect(self._on_link)
        self._input.submitted.connect(self.ask)
        self._send_button.clicked.connect(self._ask_or_stop)
        self._clear_button.clicked.connect(lambda: self.clear_conversation())
        self._hide_button.clicked.connect(self.hideRequested)
        self._scope_box.currentIndexChanged.connect(self._on_scope_changed)
        self._update_controls()
        self.refresh()

    # -- wiring --------------------------------------------------------------

    def set_settings(self, settings: Settings) -> None:
        """Where the agreement to send text is kept, and the engine chosen."""
        self._settings = settings
        index = self._scope_box.findData(settings.get("ask_scope", SCOPE_NEARBY))
        if index >= 0:
            self._scope_box.blockSignals(True)
            self._scope_box.setCurrentIndex(index)
            self._scope_box.blockSignals(False)
        self.refresh()

    def set_focus_provider(self, focus: Callable[[], Focus]) -> None:
        """Called when a question is sent: where the user is in the lecture."""
        self._focus = focus

    def bind(self, project: Project | None) -> None:
        """Show ``project``'s conversation. A question still in flight for the
        previous lecture is stopped: its answer would land in the wrong place."""
        if project is not self._project:
            self.shutdown()
            self._quotes = []
            self._error = ""
            self._input.clear()
            self._refresh_quotes()
        self._project = project
        self._update_controls()
        self.refresh()

    @property
    def scope(self) -> str:
        return str(self._scope_box.currentData())

    @property
    def is_busy(self) -> bool:
        return self._thread is not None

    @property
    def quotes(self) -> list[Quote]:
        return list(self._quotes)

    def focus_input(self) -> None:
        self._input.setFocus()

    # -- the lines pointed at ---------------------------------------------------

    def add_quote(self, quote: Quote) -> None:
        """Point at a line: it waits above the input and goes with the question."""
        if not any(q.segment_id == quote.segment_id and q.text == quote.text for q in self._quotes):
            self._quotes.append(quote)
            self._quotes.sort(key=lambda q: q.start)
        self._error = ""
        self._refresh_quotes()
        self._update_controls()
        self.focus_input()

    def remove_quote(self, quote: Quote) -> None:
        self._quotes = [q for q in self._quotes if q != quote]
        self._refresh_quotes()
        self._update_controls()

    def _refresh_quotes(self) -> None:
        while self._quote_layout.count():
            item = self._quote_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        for quote in self._quotes:
            chip = QToolButton()
            chip.setText(f"{format_hms(quote.start)}  {_short(quote.text)}   ✕")
            chip.setToolTip(f"{quote.text}\n\nGoes with the question. Click to take it off.")
            chip.setStyleSheet(
                f"QToolButton {{ text-align: left; padding: 3px 8px; border-radius: 6px;"
                f" background: {theme.ACCENT_SOFT}; color: {theme.TEXT}; border: none; }}"
            )
            chip.clicked.connect(lambda _=False, q=quote: self.remove_quote(q))
            self._quote_layout.addWidget(chip)
        self._quote_box.setVisible(bool(self._quotes))
        if self._quotes and not self._input.toPlainText().strip():
            self._input.setPlaceholderText(
                "Ask about the line above, or press Enter to have it explained"
            )
        else:
            self._input.setPlaceholderText(
                "Ask about the lecture. Enter sends · Shift+Enter for a new line"
            )

    # -- asking ------------------------------------------------------------------

    def _ask_or_stop(self) -> None:
        if self.is_busy:
            self.stop()
        else:
            self.ask()

    def _engine(self) -> TranslationEngine:
        if self.engine_factory is not None:
            return self.engine_factory()
        return ask_engine_from_settings(self._settings)

    def ask(self, question: str | None = None) -> bool:
        """Send the typed question (or ``question``) with the lines pointed at.

        Returns whether a question went out.
        """
        project = self._project
        if project is None or self.is_busy:
            return False
        text = question_text(
            self._input.toPlainText() if question is None else question, self._quotes
        )
        if not text:
            return False
        if project.transcript.is_empty:
            self._error = (
                "Transcribe the lecture first - questions are answered from its transcript."
            )
            self.refresh()
            return False
        engine = self._engine()
        if self._settings is None or not ensure_ai_consent(self, self._settings, engine.label):
            return False

        from ..export.document import build_document

        # Snapshots: the worker never reads the live transcript, which the
        # user may keep editing while the answer is written.
        document = build_document(project)
        where = self._focus()
        focus = Focus(position=where.position, slide=where.slide, quotes=tuple(self._quotes))
        history = list(project.chat.messages)
        fresh = build_prompt(document, text, focus=focus, scope=self.scope, history=history)
        # A follow-up continues the session the conversation is in, carrying
        # only what that session has not been sent.
        session = project.chat.session
        follow_up = None
        if session is not None and engine.supports_sessions and session.engine == engine.key:
            follow_up = build_follow_up(
                document, text, focus=focus, scope=self.scope, history=history, sent=session.sent
            )

        message = ChatMessage(
            role=ROLE_USER, text=text, position=focus.position, slide=focus.slide,
            quotes=focus.quotes, scope=self.scope,
        )
        project.chat.add(message)
        self._save()
        self._pending = message
        self._error = ""
        self._quotes = []
        self._input.clear()
        self._refresh_quotes()

        self._continuing = follow_up is not None
        self._worker = AskWorker(engine, fresh, follow_up=follow_up, session=session)
        self._thread = QThread(self)
        self._worker.answered.connect(self._on_answered)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        run_in_thread(self._worker, self._thread)
        self._update_controls()
        self.refresh()
        return True

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._status.setText("Stopping...")

    def shutdown(self) -> None:
        """Stop a question in flight and wait for its thread to end."""
        if self._worker is not None:
            self._worker.cancel()
            for signal in (self._worker.answered, self._worker.failed, self._worker.cancelled):
                signal.disconnect()
        self._finish_thread()
        if self._pending is not None:
            self._take_back_pending(restore=False)
        self._update_controls()

    def _finish_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(15000)
        self._thread = None
        self._worker = None

    def _on_answered(self, reply: Reply) -> None:
        self._finish_thread()
        self._pending = None
        if reply.dropped is not None and (
            reply.session is None or reply.session.id != reply.dropped.id
        ):
            forget_session(reply.dropped)
        if self._project is not None:
            chat = self._project.chat
            chat.session = reply.session
            chat.add(ChatMessage(role=ROLE_ASSISTANT, text=reply.text.strip()))
            self._save()
        self._update_controls()
        self.refresh()

    def _on_failed(self, message: str) -> None:
        self._finish_thread()
        self._take_back_pending(restore=True)
        self._error = message
        self._update_controls()
        self.refresh()

    def _on_cancelled(self) -> None:
        self._finish_thread()
        self._take_back_pending(restore=True)
        self._update_controls()
        self.refresh()
        self._status.setText("Stopped. The question is back in the box.")

    def _take_back_pending(self, *, restore: bool) -> None:
        """A question that got no answer leaves the conversation; with
        ``restore`` it goes back in the box so it can be sent again."""
        message, self._pending = self._pending, None
        if message is None or self._project is None:
            return
        chat = self._project.chat
        if message in chat.messages:
            chat.messages.remove(message)
            self._save()
        if restore:
            if not self._input.toPlainText().strip():
                self._input.setPlainText(message.text)
            for quote in message.quotes:
                if quote not in self._quotes:
                    self._quotes.append(quote)
            self._refresh_quotes()

    def _save(self) -> None:
        if self._project is None:
            return
        try:
            self._project.save_chat()
        except ProjectError as exc:
            log.warning("Could not save the conversation: %s", exc)
            self._error = str(exc)

    def clear_conversation(self, *, confirm: bool = True) -> None:
        project = self._project
        if project is None or self.is_busy or project.chat.is_empty:
            return
        if confirm:
            answer = QMessageBox.question(
                self, "New chat",
                "Forget this lecture's conversation with the assistant?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        forget_session(project.chat.session)
        project.chat.clear()
        self._error = ""
        self._save()
        self._update_controls()
        self.refresh()

    def _on_scope_changed(self, _index: int) -> None:
        if self._settings is not None:
            self._settings.set("ask_scope", self.scope)

    def _update_controls(self) -> None:
        busy = self.is_busy
        has_project = self._project is not None
        self._send_button.setText("Stop" if busy else "Ask")
        self._send_button.setEnabled(has_project)
        self._clear_button.setEnabled(
            has_project and not busy and self._project is not None
            and not self._project.chat.is_empty
        )
        if busy and self._continuing:
            self._status.setText(
                "Asking... (same conversation - only what is new is sent)"
            )
        elif busy:
            self._status.setText(
                "Asking... (the whole lecture takes longer)"
                if self.scope == SCOPE_LECTURE else "Asking... (usually 10-60 seconds)"
            )
        else:
            self._status.setText("")

    # -- the conversation --------------------------------------------------------

    def refresh(self) -> None:
        parts: list[str] = []
        project = self._project
        messages = project.chat.messages if project is not None else []
        if not messages:
            parts.append(self._intro_html())
        for message in messages:
            parts.append(
                self._user_html(message) if message.is_user else self._answer_html(message)
            )
        if self.is_busy:
            parts.append(f"<p style='color:{theme.MUTED}'><i>Writing an answer…</i></p>")
        if self._error:
            text = html.escape(self._error).replace("\n", "<br>")
            parts.append(f"<p style='color:{theme.DANGER}'>{text}</p>")
        bar = self._view.verticalScrollBar()
        self._view.setHtml("".join(parts))
        # Tied to the bar: dropped, not run, if the panel is gone by then.
        QTimer.singleShot(0, bar, lambda: bar.setValue(bar.maximum()))

    def _intro_html(self) -> str:
        engine = "the assistant"
        if self._settings is not None:
            try:
                engine = html.escape(ask_engine_from_settings(self._settings).label)
            except Exception:  # noqa: BLE001 - only wording
                log.debug("No engine label", exc_info=True)
        return (
            f"<p style='color:{theme.MUTED}'>Ask about anything in the lecture you did "
            "not follow. To ask about particular lines, use <b>Ask AI</b> on a "
            "transcript line (or right-click it) - then type a question, or just "
            "press Enter to have it explained.</p>"
            f"<p style='color:{theme.SUBTLE}; font-size:small'>Your question, the "
            "transcript text of the part you ask about (or of the whole lecture) and "
            f"this conversation are sent to {engine}. The video, the audio and the "
            "slide images are never sent.</p>"
        )

    def _seek_link(self, seconds: float) -> str:
        return (
            f"<a href='{SEEK_SCHEME}:{int(seconds)}' style='text-decoration:none'>"
            f"{format_hms(seconds)}</a>"
        )

    def _user_html(self, message: ChatMessage) -> str:
        where = []
        if message.slide is not None and self._has_slide(message.slide):
            where.append(f"<a href='{SLIDE_SCHEME}:{message.slide}' style='text-decoration:none'>"
                         f"Slide {message.slide}</a>")
        if message.position is not None:
            where.append(self._seek_link(message.position))
        if message.scope == SCOPE_LECTURE:
            where.append("whole lecture")
        meta = f" <span style='color:{theme.SUBTLE}'>· {' · '.join(where)}</span>" if where else ""
        quotes = "".join(
            f"<div style='color:{theme.MUTED}'>{self._seek_link(q.start)} "
            f"{html.escape(q.text)}</div>"
            for q in message.quotes
        )
        text = html.escape(message.text).replace("\n", "<br>")
        return (
            "<table width='100%' cellpadding='8' cellspacing='0' "
            f"style='background:{theme.ACCENT_SOFT}; margin-top:10px'><tr><td>"
            f"<b style='color:{theme.ACCENT}'>You</b>{meta}{quotes}<div>{text}</div>"
            "</td></tr></table>"
        )

    def _answer_html(self, message: ChatMessage) -> str:
        project = self._project
        duration = project.video.duration if project is not None else None
        slides = (
            {a.appearance_id for a in project.slides.appearances} if project is not None else set()
        )
        body = _markdown_html(prepare_answer(message.text, duration=duration, slides=slides))
        return (
            f"<p style='margin-top:12px'><b style='color:{theme.SUCCESS}'>AI</b> &nbsp;"
            f"<a href='copy:{message.id}' style='font-size:small; text-decoration:none'>Copy</a>"
            f"</p>{body}"
        )

    def _has_slide(self, number: int) -> bool:
        project = self._project
        return project is not None and any(
            a.appearance_id == number for a in project.slides.appearances
        )

    def _on_link(self, url: QUrl) -> None:
        scheme, target = url.scheme(), url.path()
        if scheme == SEEK_SCHEME:
            try:
                self.seekRequested.emit(float(target))
            except ValueError:
                log.debug("Bad seek link %s", url.toString())
        elif scheme == SLIDE_SCHEME and target.isdigit():
            self.slideRequested.emit(int(target))
        elif scheme == "copy" and self._project is not None:
            message = next((m for m in self._project.chat.messages if m.id == target), None)
            if message is not None:
                QApplication.clipboard().setText(message.text)
                self._status.setText("Answer copied.")
