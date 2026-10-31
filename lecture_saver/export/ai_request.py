"""Exports prepared for asking an AI assistant to translate the lecture.

The application never sends anything anywhere (§1, §29). This module only
writes files: the lecture notes as usual, with a Korean translation request at
the top, so the user can hand the file to an assistant of their choice
themselves.

The request is written to stop the two ways such requests go wrong: the
assistant returning the English unchanged (because it was told to keep the
original, or to keep terms in English), and the assistant summarising or
skipping part of a long lecture.
"""

from __future__ import annotations

from pathlib import Path

from ..core.atomic_io import write_text_atomic
from .document import LectureDocument
from .exporter import ExportResult, ProgressCallback
from .options import ExportFormat, PdfOptions, safe_file_stem
from .pdf import PdfExportError, write_pdf
from .text_formats import to_markdown

__all__ = [
    "AI_REQUEST_SUFFIX",
    "REQUEST_CONTINUATION_RULE",
    "REQUEST_CORRECTIONS",
    "REQUEST_DELIVERABLES",
    "REQUEST_EXAMPLE",
    "REQUEST_INTRO",
    "REQUEST_RULES",
    "REQUEST_TAIL",
    "TRANSLATION_REQUEST",
    "ai_request_markdown",
    "ai_request_name",
    "run_ai_request_export",
]

#: Added to the lecture's name to make the request file names, so a folder of
#: requests from several lectures says which is which.
AI_REQUEST_SUFFIX = "ai_translation"


def ai_request_name(title: str, extension: str) -> str:
    """``("Machine Learning 04", "md")`` -> ``Machine_Learning_04_ai_translation.md``.

    The lecture's own name comes first so the file is recognisable wherever it
    is dropped; anything a file name cannot hold becomes an underscore.
    """
    return f"{safe_file_stem(title)}_{AI_REQUEST_SUFFIX}.{extension.lstrip('.')}"

#: The lecture is handed over the same way whoever does the translating: by an
#: assistant the user pastes it into, or by :mod:`lecture_saver.translate`
#: running a CLI. The pieces below are shared so both ask for the identical
#: shape - a prompt that drifts between the two paths would produce answers
#: that parse differently.

#: What is wanted, and the layout of the answer.
REQUEST_INTRO = """# AI 직역 요청

아래는 영어 강의를 슬라이드별로 정리한 전사입니다. 각 줄 앞에 타임스탬프가 있습니다.
**타임스탬프가 붙은 영어 원문 한 줄 → 바로 다음 줄에 그 줄의 한국어 직역** 순서로 적어 주세요.
영어를 지우면 안 됩니다.

## 출력 형식

각 슬라이드마다 아래 형식으로, 결과만 출력해 주세요.

```
## Slide 1

[00:00:12] (Slide 1의 첫 번째 영어 줄 - 전사 그대로)
(그 줄의 한국어 직역)

[00:00:19] (Slide 1의 두 번째 영어 줄 - 전사 그대로)
(그 줄의 한국어 직역)

## Slide 2

[00:02:41] (Slide 2의 첫 번째 영어 줄 - 전사 그대로)
(그 줄의 한국어 직역)
```
"""

#: Only the hand-carried request asks for files: the user has to get the answer
#: back into the application somehow. A CLI answer is read straight from stdout.
REQUEST_DELIVERABLES = """
## 결과물

1. 결과를 **Markdown(.md) 파일**로 만들어 주세요.
   이 파일을 강의 노트 프로그램에 다시 불러와 슬라이드마다 붙입니다.
2. 가능하면 같은 내용을 **PDF 파일**로도 함께 만들어 주세요. (읽기용)
3. 파일을 만들 수 없는 환경이면, 위 출력 형식 그대로 본문에 출력해 주세요.
"""

#: The rules that decide whether an answer is usable. Both paths send these.
REQUEST_RULES = """
## 규칙

1. 전사의 줄 나누기를 그대로 지키세요. 각 줄을 `[HH:MM:SS]` 타임스탬프와 함께
   전사 그대로 적고, **바로 다음 줄에** 그 줄의 한국어 직역을 적으세요.
   줄과 줄 사이에는 빈 줄을 하나 둡니다.
2. 타임스탬프는 전사에 있는 그대로 `[HH:MM:SS]` 형태로 옮기세요. 바꾸거나 빼지 마세요.
3. 영어 줄은 전사 그대로 옮겨 적으세요. 고치거나 다듬거나, 줄을 합치거나 나누지 마세요.
4. 모든 줄을 빠짐없이 옮기세요. 번역만 남기고 영어를 빼면 안 되고, 영어만 남겨도 안 됩니다.
5. 한 줄은 문장 중간에서 끊겨 있을 수 있습니다. 앞뒤 줄의 문맥을 보고 자연스럽게
   옮기되, 각 줄의 직역에는 그 줄에 해당하는 내용만 적으세요.
6. 한국어 직역에서도 전공 용어는 영어로 두세요. (예: learning rate, gradient descent, overfitting)
7. 직역에 가깝게 옮기고, 요약하거나 설명을 덧붙이지 마세요.
8. 교수님의 말은 존댓말로 옮겨 주세요.
9. 슬라이드 제목(`## Slide N`)은 그대로 두세요. 제목 옆의 표시(Before notes, With notes 등)와
   슬라이드 시간 범위는 옮기지 않아도 됩니다.
"""

#: Only for the hand-carried request. A chat window runs out of room on a long
#: lecture; the CLI is sent a few slides at a time instead, so telling it to
#: stop early would truncate a batch that fits perfectly well.
REQUEST_CONTINUATION_RULE = (
    "10. 길어서 한 번에 못 끝내면 슬라이드 경계에서 멈추고, "
    '제가 "계속"이라고 하면 이어서 출력해 주세요.\n'
)

#: A worked pair, which does more than the prose to pin down the shape.
REQUEST_EXAMPLE = """
## 예시

```
## Slide 3

[00:12:34] If the learning rate is too large,
learning rate가 너무 크면

[00:12:37] the model diverges. If it is too small, training is slow.
모델이 발산합니다. 너무 작으면 학습이 느려집니다.
```
"""

#: Asks for what the transcription got wrong, one line under the Korean, in the
#: shape :func:`lecture_saver.core.corrections.parse_fix_line` reads. The English
#: is still copied as it stands: the answer's timestamps are what its lines are
#: matched back by, and a fix is the user's to accept, not the assistant's to make.
REQUEST_CORRECTIONS = """
## 오탈자 점검

이 전사는 음성 인식이 만든 것이라, 비슷하게 들리는 다른 말로 적히거나 철자가 깨진 곳이
있습니다. (예: running → learning, clock → cloud, state → date, Block → BIOck)
번역하면서 그런 곳을 찾아 주세요.

1. 찾으면 그 줄의 한국어 직역 **바로 다음 줄에** 아래 형식으로 한 줄을 적으세요.
   고칠 곳이 없는 줄에는 아무것도 적지 마세요.
   `!오탈자 | 전사에 적힌 말 | 고친 말 | 확실 또는 의심 | 근거`
2. 전사에 적힌 말은 전사에서 **글자 그대로** 복사하고, 그 줄 안에 있는 말만 적으세요.
   고칠 단어와 뜻을 알 수 있는 앞뒤 단어 정도로 짧게 적으세요.
3. 영어 줄은 위 규칙대로 전사 그대로 두세요. 한국어 직역은 고친 말을 기준으로 옮기세요.
4. `확실`은 강의 자료에 고친 말이 나오거나, 문맥상 다른 뜻일 수 없을 때입니다.
   `의심`은 고친 말이 맞아 보이지만 강의를 직접 들어야 확인할 수 있을 때입니다.
5. 근거는 한국어로 짧게 적으세요. 강의 자료에서 찾았다면 쪽 번호(p.12)도 적으세요.
6. 문법, 말투, 구두점, 대소문자, 말더듬은 고치지 마세요. 교수님이 실제로 그렇게 말했을
   수 있습니다. 듣고 잘못 적은 말만 찾으세요.

```
## Slide 3

[00:05:41] So a process is simply a learning program.
그러므로 process는 단순히 실행 중인 program입니다.
!오탈자 | learning program | running program | 확실 | 강의 자료 p.2에 running program으로 나옴

[00:05:46] The kernel saves the process date at each PCB.
kernel은 process state를 각 PCB에 저장합니다.
!오탈자 | process date | process state | 의심 | 문맥상 state로 보이나 강의 자료에는 없음
```
"""

REQUEST_TAIL = """
---

아래부터 강의 전사입니다.
"""

#: The request, in Markdown. Lines inside the fenced block are shown verbatim.
TRANSLATION_REQUEST = (
    REQUEST_INTRO
    + REQUEST_DELIVERABLES
    + REQUEST_RULES
    + REQUEST_CONTINUATION_RULE
    + REQUEST_EXAMPLE
    + REQUEST_CORRECTIONS
    + REQUEST_TAIL
)


def ai_request_markdown(document: LectureDocument, *, output_dir: Path | None = None) -> str:
    """The request followed by the whole lecture, one timestamped line each.

    The timestamps are what the answer's Korean lines are matched back to, so
    each can be shown directly under the line it translates.
    """
    notes = to_markdown(document, timestamps=True, output_dir=output_dir, translation=False)
    return TRANSLATION_REQUEST.rstrip() + "\n\n" + notes


def run_ai_request_export(
    document: LectureDocument,
    pdf_options: PdfOptions,
    output_dir: Path,
    *,
    on_progress: ProgressCallback | None = None,
) -> ExportResult:
    """Write the request as Markdown and as PDF, each independently.

    ``on_progress`` is called before each file with the same
    ``(format, position, total)`` shape :func:`run_export` uses, so the window
    can show one progress bar for every kind of export.
    """
    folder = Path(output_dir)
    result = ExportResult(output_dir=folder)
    if not document.segments:
        result.failures.append(
            (ExportFormat.MARKDOWN, "There is no transcript to translate yet.")
        )
        return result

    if on_progress is not None:
        on_progress(ExportFormat.MARKDOWN, 0, 2)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / ai_request_name(document.title, "md")
        write_text_atomic(target, ai_request_markdown(document, output_dir=folder))
        result.written.append(target)
    except OSError as exc:
        result.failures.append((ExportFormat.MARKDOWN, f"Could not write the request:\n{exc}"))

    # The PDF always carries the transcript: that is what is being translated.
    options = PdfOptions(
        slide_image=pdf_options.slide_image,
        slide_number=pdf_options.slide_number,
        time_range=pdf_options.time_range,
        transcript=True,
        segment_timestamps=False,
        translation=False,
    )
    if on_progress is not None:
        on_progress(ExportFormat.PDF, 1, 2)
    try:
        target = folder / ai_request_name(document.title, "pdf")
        write_pdf(document, options, target, preface=TRANSLATION_REQUEST)
        result.written.append(target)
    except PdfExportError as exc:
        result.failures.append((ExportFormat.PDF, str(exc)))
    if on_progress is not None:
        on_progress(ExportFormat.PDF, 2, 2)
    return result
