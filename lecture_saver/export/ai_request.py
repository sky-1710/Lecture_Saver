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

import re
from pathlib import Path

from ..core.atomic_io import write_text_atomic
from .document import LectureDocument
from .exporter import ExportResult, ProgressCallback
from .options import ExportFormat, PdfOptions
from .pdf import PdfExportError, write_pdf
from .text_formats import to_markdown

__all__ = [
    "AI_REQUEST_SUFFIX",
    "TRANSLATION_REQUEST",
    "ai_request_markdown",
    "ai_request_name",
    "run_ai_request_export",
]

#: Added to the lecture's name to make the request file names, so a folder of
#: requests from several lectures says which is which.
AI_REQUEST_SUFFIX = "ai_translation"

#: Used when the lecture has no usable name at all.
_FALLBACK_NAME = "lecture"

#: Everything Windows forbids in a file name, plus whitespace.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\s]+')


def ai_request_name(title: str, extension: str) -> str:
    """``("Machine Learning 04", "md")`` -> ``Machine_Learning_04_ai_translation.md``.

    The lecture's own name comes first so the file is recognisable wherever it
    is dropped; anything a file name cannot hold becomes an underscore.
    """
    safe = _UNSAFE.sub("_", title).strip("_. ")
    # A name made only of separators (or only dots) leaves nothing to use.
    safe = safe or _FALLBACK_NAME
    return f"{safe}_{AI_REQUEST_SUFFIX}.{extension.lstrip('.')}"

#: The request, in Markdown. Lines inside the fenced block are shown verbatim.
TRANSLATION_REQUEST = """# AI 직역 요청

아래는 영어 강의를 슬라이드별로 정리한 전사입니다.
**영어 원문 한 문단 → `---` → 한국어 직역 한 문단 → `---`** 순서로 짝지어 주세요.
영어를 지우면 안 됩니다.

## 출력 형식

각 슬라이드마다 아래 형식으로, 결과만 출력해 주세요.

```
---
## Slide 1
---
(Slide 1의 첫 번째 영어 문단 - 전사 그대로)
---
(그 문단 전체의 한국어 직역)
---
(Slide 1의 두 번째 영어 문단 - 전사 그대로)
---
(그 문단 전체의 한국어 직역)
---

---
## Slide 2
---
(Slide 2의 첫 번째 영어 문단 - 전사 그대로)
---
(그 문단 전체의 한국어 직역)
---
```

## 결과물

1. 결과를 **Markdown(.md) 파일**로 만들어 주세요.
   이 파일을 강의 노트 프로그램에 다시 불러와 슬라이드마다 붙입니다.
2. 가능하면 같은 내용을 **PDF 파일**로도 함께 만들어 주세요. (읽기용)
3. 파일을 만들 수 없는 환경이면, 위 출력 형식 그대로 본문에 출력해 주세요.

## 규칙

1. 전사의 문단 나누기를 그대로 지키세요. 영어 문단을 먼저 그대로 적고,
   **`---` 한 줄로 구분한 뒤** 그 문단 전체의 한국어 직역을 적으세요.
   한국어 직역 뒤에도 `---` 한 줄을 두고 다음 영어 문단을 적습니다.
   슬라이드 제목(`## Slide N`)의 앞과 뒤에도 `---`를 둡니다.
   (.md 파일로 만들 때는 `---` 앞뒤에 빈 줄을 하나씩 두세요.
   빈 줄이 없으면 바로 위 문단이 제목처럼 크게 보입니다.)
2. 영어 문단은 전사 그대로 옮겨 적으세요. 고치거나 다듬거나, 문단을 합치거나 나누지 마세요.
3. 모든 문단을 빠짐없이 옮기세요. 번역만 남기고 영어를 빼면 안 되고, 영어만 남겨도 안 됩니다.
4. 한국어 문단에서도 전공 용어는 영어로 두세요. (예: learning rate, gradient descent, overfitting)
5. 직역에 가깝게 옮기고, 요약하거나 설명을 덧붙이지 마세요.
   문단 안의 문장 수와 순서를 원문과 맞춰 주세요.
6. 교수님의 말은 존댓말로 옮겨 주세요.
7. 슬라이드 제목 옆의 표시(Before notes, With notes 등)와 시간은 옮기지 않아도 됩니다.
8. 길어서 한 번에 못 끝내면 슬라이드 경계에서 멈추고, 제가 "계속"이라고 하면 이어서 출력해 주세요.

## 예시

```
---
## Slide 3
---
If the learning rate is too large, the model diverges. If it is too small, training is slow.
---
learning rate가 너무 크면 모델이 발산합니다. 너무 작으면 학습이 느려집니다.
---
```

---

아래부터 강의 전사입니다.
"""


def ai_request_markdown(document: LectureDocument, *, output_dir: Path | None = None) -> str:
    """The request followed by the whole lecture, as paragraphs per slide.

    Timestamps are left out: paragraphs give the assistant whole sentences and
    their context, which translates far better than one fragment per line.
    """
    notes = to_markdown(document, timestamps=False, output_dir=output_dir, translation=False)
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
