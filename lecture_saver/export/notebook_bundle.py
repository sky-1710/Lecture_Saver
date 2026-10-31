"""A folder of what Gemini Notebook needs for a video, to upload by hand.

Gemini Notebook (formerly NotebookLM) makes a narrated video from what it is
given, but it has no way in for an application on a personal account. So this
gathers the lecture's PDF, the transcript and the preview in one folder, with
the words to paste, and the user carries them across. Writing the folder sends
nothing: what goes to Google goes when the user uploads it there.

The transcript is the edited one (§14), and the preview is its saved text laid
out again, so a folder made after fixing a line has the fix in it. A file left
out this time is removed, so the folder never holds an old copy of something.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..core.atomic_io import write_text_atomic
from ..core.study import StudySet
from .document import LectureDocument
from .options import safe_file_stem
from .study_pdf import write_preview_pdf
from .text_formats import to_txt

__all__ = ["Bundle", "REQUEST_FILE", "bundle_folder_name", "video_request", "write_bundle"]

#: Added to the lecture's name: ``OS03-Process_gemini_notebook``.
FOLDER_SUFFIX = "gemini_notebook"
#: The words to paste into the video's settings.
REQUEST_FILE = "붙여넣을 문구.txt"

_DECK = "교안.pdf"
_TRANSCRIPT = "전사문.txt"
_PREVIEW = "예습.pdf"

_REQUEST = """대학생이 교수님 강의를 듣기 전에 보는 예습 강의를 만들어 주세요.
모든 설명은 한국어로 하고, 전문 용어는 처음 나올 때 한국어와 영어를 함께 말해 주세요.
단순 요약이 아니라, 이 강의가 어떤 질문에서 시작해 어떤 답에 도달하는지
하나의 이야기로 설명해 주세요.
각 개념은 정의보다 왜 필요한지를 먼저 설명해 주세요.
교수님이 생략한 배경지식과 개념 사이의 연결을 채워 주고,
일상적인 비유와 구체적인 사례를 들어 주세요."""

_WITH_DECK = "전사문은 음성 인식 결과라 오류가 있을 수 있으니, 용어는 교안을 기준으로 삼아 주세요."
_WITHOUT_DECK = "전사문은 음성 인식 결과라 오류가 있을 수 있으니, 문맥에 맞게 이해해 주세요."
_NOT_PUT_IN_THEIR_MOUTH = "교수님이 말하지 않은 내용을 교수님이 말했다고 하지 마세요."
_WITH_PREVIEW = """예습 자료 PDF의 구성과 설명 순서를 따라 진행해 주세요.
예습 자료의 [AI 보충 설명]은 AI가 덧붙인 내용이니 교수님 말씀처럼 설명하지 말아 주세요."""


@dataclass(frozen=True)
class Bundle:
    folder: Path
    #: The words to paste, as they are in the folder.
    request: str


def bundle_folder_name(title: str) -> str:
    return f"{safe_file_stem(title)}_{FOLDER_SUFFIX}"


def video_request(*, has_deck: bool, has_preview: bool) -> str:
    """The words to paste, saying only what the folder holds."""
    lines = [_REQUEST, _WITH_DECK if has_deck else _WITHOUT_DECK, _NOT_PUT_IN_THEIR_MOUTH]
    if has_preview:
        lines.append(_WITH_PREVIEW)
    return "\n".join(lines) + "\n"


def write_bundle(
    document: LectureDocument, study: StudySet, deck: Path | None, output_dir: Path
) -> Bundle:
    """Write the folder under ``output_dir``. ``deck`` is the lecture's PDF, if kept."""
    stem = safe_file_stem(document.title)
    folder = Path(output_dir) / bundle_folder_name(document.title)
    folder.mkdir(parents=True, exist_ok=True)

    deck_target = folder / f"{stem}_{_DECK}"
    if deck is not None:
        shutil.copyfile(deck, deck_target)
    else:
        deck_target.unlink(missing_ok=True)

    write_text_atomic(folder / f"{stem}_{_TRANSCRIPT}", to_txt(document, timestamps=True))

    preview_target = folder / f"{stem}_{_PREVIEW}"
    if study.guide is not None:
        write_preview_pdf(document, study, preview_target)
    else:
        preview_target.unlink(missing_ok=True)

    request = video_request(has_deck=deck is not None, has_preview=study.guide is not None)
    write_text_atomic(folder / REQUEST_FILE, request)
    return Bundle(folder, request)
