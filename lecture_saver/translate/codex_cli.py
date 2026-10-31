"""Translation through the Codex CLI, signed in with the user's ChatGPT account.

Codex is run once per batch, non-interactively::

    codex exec --skip-git-repo-check --ephemeral --ignore-user-config \\
               --sandbox read-only --color never \\
               --output-last-message ANSWER.md -

Every flag there is load-bearing:

``exec``
    Non-interactive mode. Plain ``codex`` would open the TUI and hang.
``-`` with the prompt on **stdin**
    A batch of lecture transcript is far longer than Windows allows on a
    command line (~32k characters), so the prompt cannot be an argument.
``--output-last-message FILE``
    Writes the final answer, and nothing else, to a file. stdout carries
    progress chatter that would otherwise have to be separated from the
    translation by guesswork.
``--ephemeral``
    No session file is written to ``~/.codex/sessions``. The lecture is being
    sent to OpenAI either way, but it does not also accumulate in a log on
    disk that the user does not know about.
``--sandbox read-only``
    Translating needs no file access. If the model decides to run a command
    anyway it cannot write anything.
``--skip-git-repo-check``
    Codex otherwise refuses to start outside a Git repository.
``--ignore-user-config``
    ``~/.codex/config.toml`` is set up for whatever the user does with Codex
    otherwise, and none of it belongs in a translation. Two parts actively
    break one: a default model their installed Codex is too old to accept
    ("requires a newer version of Codex"), and MCP servers that are started
    for every run and fail if they need authorising. Translating needs no
    tools, so the whole file is skipped. Their sign-in is not in it - auth
    comes from ``CODEX_HOME`` and still applies.
``-m MODEL``
    Always passed. Without it Codex falls back to a built-in default that a
    ChatGPT sign-in may not be allowed to use ("not supported when using
    Codex with a ChatGPT account"). The user's own default is usually their
    most capable model, which burns through their quota on a long lecture,
    so an affordable one is chosen from the models Codex lists for them
    (see :func:`pick_model`).

Questions to Ask AI are the one exception to ``--ephemeral``: a lecture's
conversation runs in one Codex session, started with ``--json`` (which reports
the session's id) and continued with::

    codex exec resume --skip-git-repo-check --ignore-user-config --json \\
               --output-last-message ANSWER.md -m MODEL SESSION_ID -

so the assistant keeps the conversation itself - its own earlier answers
included - and a follow-up carries only what is new instead of the transcript
again. The session keeps the read-only sandbox it was started with. Its file
in ``CODEX_HOME/sessions`` holds the text sent in it, on this computer only;
:func:`forget_codex_session` deletes it when the conversation ends (*New
chat*), and a session whose first turn failed or was stopped is deleted at
once.

Both stdin and the answer file are UTF-8 explicitly: the Windows default is
cp949 here, which mangles Korean.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import (
    EngineAuthError,
    EngineAvailability,
    EngineNotFoundError,
    EngineRateLimited,
    TranslationCancelled,
    TranslationEngine,
    TranslationError,
)

log = logging.getLogger(__name__)

__all__ = ["CODEX_PATH_ENV", "CodexEngine", "find_codex", "forget_codex_session"]

#: Lets the user point at a Codex the search below does not find.
CODEX_PATH_ENV = "LECTURE_SAVER_CODEX"

#: How long one batch may take before it is given up on. Codex reasons for a
#: while on a long batch; the ceiling exists so a wedged process cannot leave
#: the translation hanging forever.
BATCH_TIMEOUT_SECONDS = 900

#: How often the wait loop looks at the cancel flag.
POLL_SECONDS = 0.2

#: Models used when the user chose none, cheapest first. Translation needs no
#: frontier reasoning, and a lecture is many batches.
AFFORDABLE_MODELS = ("gpt-6-luna", "gpt-5.6-luna")


def _no_window() -> dict[str, Any]:
    """Keep a console window from flashing on Windows for every batch."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flags} if flags else {}


def _bundled_candidates() -> list[Path]:
    """Codex installations that put no ``codex`` on the PATH.

    The VS Code ChatGPT extension is the common case on Windows: it unpacks
    Codex into a folder named after the build, so the path changes with every
    update and cannot be hard-coded::

        %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<build>\\codex.exe

    The newest build wins, which is the one the extension is using.
    """
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "OpenAI" / "Codex" / "bin")
    roots.append(Path.home() / ".codex" / "bin")

    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for name in ("codex.exe", "codex"):
            found += [p for p in root.glob(f"*/{name}") if p.is_file()]
            direct = root / name
            if direct.is_file():
                found.append(direct)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def find_codex(override: str | Path | None = None) -> Path | None:
    """Locate the Codex program: an explicit setting, the PATH, then bundles."""
    for candidate in (override, os.environ.get(CODEX_PATH_ENV)):
        if candidate:
            path = Path(candidate).expanduser()
            return path if path.is_file() else None

    from shutil import which

    on_path = which("codex")
    if on_path:
        return Path(on_path)

    bundled = _bundled_candidates()
    return bundled[0] if bundled else None


def codex_home() -> Path:
    """Where Codex keeps its sign-in and caches: ``CODEX_HOME`` or ``~/.codex``."""
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def pick_model(home: Path | None = None) -> str:
    """The first of :data:`AFFORDABLE_MODELS` this account can use.

    Codex caches the models the signed-in account offers in
    ``models_cache.json``. When that file is missing or unreadable the first
    choice is used anyway: leaving ``-m`` off is known to fail.
    """
    cache = (home or codex_home()) / "models_cache.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        offered = {m.get("slug") for m in data.get("models", []) if isinstance(m, dict)}
    except (OSError, ValueError, AttributeError):
        log.info("No readable Codex model list at %s", cache)
        return AFFORDABLE_MODELS[0]
    for slug in AFFORDABLE_MODELS:
        if slug in offered:
            return slug
    return AFFORDABLE_MODELS[0]


@dataclass
class CodexEngine(TranslationEngine):
    """Runs one batch per call. Holds no state between batches.

    Each batch is an independent Codex run with the rules repeated, rather
    than one conversation continued: a fresh run cannot drift part-way through
    a lecture, and a batch that fails can be retried on its own.
    """

    key: str = "codex"
    label: str = "Codex CLI (ChatGPT sign-in)"
    sends_text_externally: bool = True
    supports_sessions: bool = True

    #: Explicit path to the program, when the user set one.
    binary: str | Path | None = None
    #: Passed to ``-m``. Empty means an affordable one, see :func:`pick_model`.
    model: str = ""
    #: Where Codex's model list is read from; ``None`` is :func:`codex_home`.
    home: Path | None = None
    timeout: int = BATCH_TIMEOUT_SECONDS

    def resolve(self) -> Path:
        path = find_codex(self.binary)
        if path is None:
            raise EngineNotFoundError(
                "Codex could not be found on this computer.\n\n"
                "Lecture Saver looks for it on the PATH and in the folder the "
                "VS Code ChatGPT extension installs it into. If Codex is "
                "somewhere else, set its full path in Settings, or set the "
                f"{CODEX_PATH_ENV} environment variable."
            )
        return path

    def availability(self) -> EngineAvailability:
        """Report the version. Deliberately does not check the sign-in.

        There is no way to ask Codex whether it is signed in without starting
        a run, and a run spends the user's quota. A sign-in problem surfaces
        on the first batch instead, with a message that says what to do.
        """
        try:
            path = self.resolve()
        except EngineNotFoundError as exc:
            return EngineAvailability(ready=False, detail="Not installed", remedy=str(exc))
        try:
            result = subprocess.run(
                [str(path), "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, **_no_window(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return EngineAvailability(
                ready=False,
                detail="Could not be started",
                remedy=f"Codex is at {path} but would not run:\n{exc}",
            )
        version = (result.stdout or result.stderr).strip().splitlines()
        return EngineAvailability(ready=True, detail=version[0] if version else str(path))

    def _command(
        self,
        path: Path,
        answer_file: Path,
        schema_file: Path | None = None,
        *,
        keep: bool = False,
        resume: str | None = None,
        images: Sequence[str] = (),
    ) -> list[str]:
        model = ["-m", self.model or pick_model(self.home)]
        if resume:
            # ``resume`` takes no --sandbox or --color: the session keeps the
            # read-only sandbox it was started with.
            cmd = [
                str(path), "exec", "resume",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--json",
                "--output-last-message", str(answer_file),
            ]
            return [*cmd, *model, resume, "-"]
        cmd = [
            str(path), "exec",
            "--skip-git-repo-check",
            # A kept session is the start of a conversation; --json reports its id.
            "--json" if keep else "--ephemeral",
            "--ignore-user-config",
            "--sandbox", "read-only",
            "--color", "never",
            "--output-last-message", str(answer_file),
        ]
        if schema_file is not None:
            # The model is held to this JSON schema, so the answer is data to
            # validate rather than prose to scrape.
            cmd += ["--output-schema", str(schema_file)]
        # Each with its own flag and the model after them: --image takes as many
        # values as follow it, and would otherwise swallow the "-" below.
        for name in images:
            cmd += ["--image", name]
        # A trailing "-" tells Codex the prompt arrives on stdin.
        return [*cmd, *model, "-"]

    def translate(self, prompt: str, *, cancel: threading.Event) -> str:
        return self.ask(prompt, cancel=cancel)

    def ask(
        self,
        prompt: str,
        *,
        cancel: threading.Event,
        schema: dict[str, Any] | None = None,
        images: Sequence[Path] = (),
    ) -> str:
        return self._run(prompt, cancel, schema=schema, images=images)

    def converse(
        self, prompt: str, *, session: str | None = None, cancel: threading.Event
    ) -> tuple[str, str | None]:
        found: list[str] = []
        answer = self._run(prompt, cancel, keep=True, resume=session, session_out=found)
        return answer, session or (found[0] if found else None)

    def forget_session(self, session: str) -> None:
        forget_codex_session(session)

    def _run(
        self,
        prompt: str,
        cancel: threading.Event,
        *,
        schema: dict[str, Any] | None = None,
        keep: bool = False,
        resume: str | None = None,
        session_out: list[str] | None = None,
        images: Sequence[Path] = (),
    ) -> str:
        path = self.resolve()
        # Codex treats its working directory as the workspace root. A scratch
        # folder keeps it away from the lecture project and from whatever
        # directory the application happens to have been started in.
        # ignore_cleanup_errors: a stopped run can hold a file in the folder
        # for a moment, and on Windows the cleanup would raise over the stop.
        with tempfile.TemporaryDirectory(
            prefix="lecture-saver-ai-", ignore_cleanup_errors=True
        ) as scratch:
            workspace = Path(scratch)
            answer_file = workspace / "answer.md"
            schema_file = None
            if schema is not None:
                schema_file = workspace / "schema.json"
                schema_file.write_text(json.dumps(schema), encoding="utf-8")
            # Copied here and named from here, so that no path to the project,
            # or to this user's home, is in what Codex is told.
            for image in images:
                shutil.copy(image, workspace / image.name)
            new_session = keep and not resume
            try:
                process = subprocess.Popen(
                    self._command(
                        path, answer_file, schema_file, keep=keep, resume=resume,
                        images=[image.name for image in images],
                    ),
                    cwd=str(workspace),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    **_no_window(),
                )
            except OSError as exc:
                raise TranslationError(f"Codex could not be started:\n{exc}") from exc

            try:
                stdout, stderr = self._communicate(process, prompt, cancel)
            except TranslationError:
                # Stopped before it said its id: the new session is found by
                # the scratch folder it ran in, which no other run shares.
                if new_session:
                    _forget_sessions_in(workspace)
                raise

            if process.returncode != 0:
                # A new session whose first turn failed is no conversation:
                # its file would only sit in CODEX_HOME holding the text.
                if new_session and (thread := _thread_id(stdout)):
                    forget_codex_session(thread)
                raise self._failure(process.returncode, stdout, stderr)
            if session_out is not None and (thread := _thread_id(stdout)):
                session_out.append(thread)
            # With --json, stdout is a stream of events, not an answer.
            answer = self._read_answer(answer_file, "" if keep or resume else stdout)
            if not answer.strip():
                raise TranslationError(
                    "Codex finished but returned an empty answer.\n\n"
                    "This usually means the run was interrupted. Try again; "
                    "whatever was already saved is kept."
                )
            return answer

    def _communicate(
        self, process: subprocess.Popen[str], prompt: str, cancel: threading.Event
    ) -> tuple[str, str]:
        """Feed the prompt in and wait, watching the cancel flag throughout.

        ``Popen.communicate`` has to be given a timeout to be interruptible at
        all, so it is called in short slices; each expiry is just another turn
        of the loop, not a failure.
        """
        waited = 0.0
        sent = False
        while True:
            if cancel.is_set():
                self._stop(process)
                raise TranslationCancelled("Translation stopped.")
            try:
                # The prompt goes in on the first pass only; afterwards stdin
                # is already closed and communicate() must be called with None.
                stdout, stderr = process.communicate(
                    input=None if sent else prompt, timeout=POLL_SECONDS
                )
                return stdout or "", stderr or ""
            except subprocess.TimeoutExpired:
                sent = True
                waited += POLL_SECONDS
                if waited >= self.timeout:
                    self._stop(process)
                    raise TranslationError(
                        f"Codex did not answer within {self.timeout // 60} minutes "
                        "and was stopped.\n\nTry again, or choose fewer slides per "
                        "batch in Settings."
                    ) from None

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    @staticmethod
    def _read_answer(answer_file: Path, stdout: str) -> str:
        """The answer file, or stdout if Codex wrote no file."""
        try:
            if answer_file.is_file():
                return answer_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log.warning("Could not read the Codex answer file", exc_info=True)
        return stdout

    @staticmethod
    def _failure(returncode: int, stdout: str, stderr: str) -> TranslationError:
        """Turn an exit code into something worth showing a user (§28)."""
        combined = f"{stderr}\n{stdout}".lower()
        tail = (stderr.strip() or stdout.strip())[-800:]

        signed_out = ("not logged in", "unauthorized", "please run codex login",
                      "authentication")
        if any(word in combined for word in signed_out):
            return EngineAuthError(
                "Codex is installed but not signed in.\n\n"
                "Open a terminal and run:\n    codex login\n\n"
                "Then try the translation again."
            )
        if "requires a newer version of codex" in combined:
            return TranslationError(
                "The model this translation asked for needs a newer Codex than "
                "the one installed.\n\n"
                "Update Codex (run 'codex update', or update the ChatGPT "
                "extension in VS Code), or set a different model in Settings."
            )
        if "not supported when using codex with a chatgpt account" in combined:
            return TranslationError(
                "The model this translation asked for cannot be used with a "
                "ChatGPT sign-in.\n\n"
                "Set \"translate_model\" in %APPDATA%\\LectureSaver\\settings.json "
                f"to one your account offers (for example {AFFORDABLE_MODELS[0]})."
                f"\n\n{tail}"
            )
        limited = ("rate limit", "usage limit", "quota", "429", "too many requests")
        if any(word in combined for word in limited):
            return EngineRateLimited(
                "Your ChatGPT usage limit has been reached.\n\n"
                "Slides translated so far have been saved. Run the translation "
                "again later and it will carry on from where it stopped."
            )
        offline = ("network", "connection", "dns", "timed out", "unreachable")
        if any(word in combined for word in offline):
            return TranslationError(
                "Codex could not reach OpenAI. Check the internet connection "
                f"and try again.\n\n{tail}"
            )
        return TranslationError(f"Codex failed (exit code {returncode}).\n\n{tail}")


def _thread_id(stdout: str) -> str | None:
    """The session id in ``--json`` output (its "thread.started" event)."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("thread_id"):
            return str(event["thread_id"])
    return None


def forget_codex_session(session: str) -> int:
    """Delete a session's files from ``CODEX_HOME``; how many went.

    Codex names them ``rollout-<time>-<session id>.jsonl`` under
    ``sessions/<year>/<month>/<day>/``. Only names ending in this id are
    touched, and the id is checked to look like one first.
    """
    if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", session or ""):
        return 0
    removed = 0
    for folder in ("sessions", "archived_sessions"):
        root = codex_home() / folder
        if not root.is_dir():
            continue
        for path in root.rglob(f"*{session}.jsonl"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                log.warning("Could not delete Codex session file %s", path, exc_info=True)
    return removed


def _forget_sessions_in(workspace: Path) -> int:
    """Delete recent session files whose run had ``workspace`` as its folder.

    Each run gets a scratch folder of its own, and Codex writes it into the
    first line of the session file (``"cwd"``), so the match is exact.
    """
    root = codex_home() / "sessions"
    if not root.is_dir():
        return 0
    needle = json.dumps(str(workspace))[1:-1]  # as it is escaped in the file
    cutoff = time.time() - 6 * 3600
    removed = 0
    for path in root.rglob("rollout-*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            with path.open(encoding="utf-8", errors="replace") as handle:
                first = handle.readline(8192)
            if needle in first:
                path.unlink()
                removed += 1
        except OSError:
            log.warning("Could not check Codex session file %s", path, exc_info=True)
    return removed
