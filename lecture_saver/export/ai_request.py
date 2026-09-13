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
from .exporter import ExportResult
from .options import ExportFormat, PdfOptions
from .pdf import PdfExportError, write_pdf
from .text_formats import to_markdown

__all__ = [
    "AI_REQUEST_MARKDOWN",
    "AI_REQUEST_PDF",
    "TRANSLATION_REQUEST",
    "ai_request_markdown",
    "run_ai_request_export",
]

AI_REQUEST_MARKDOWN = "ai_translation_request.md"
AI_REQUEST_PDF = "ai_translation_request.pdf"

#: The request, in Markdown. Lines inside the fenced block are shown verbatim.
TRANSLATION_REQUEST = """# AI 직역 요청

아래는 영어 강의를 슬라이드별로 정리한 전사입니다. 다음 규칙에 따라 한국어 직역을 만들어 주세요.

## 출력 형식

각 슬라이드마다 아래 형식으로, 번역 결과만 출력해 주세요.

```
## Slide 1
(Slide 1 전사의 한국어 직역)

## Slide 2
(Slide 2 전사의 한국어 직역)
```

## 규칙

1. 모든 영어 문장을 빠짐없이 한국어 문장으로 번역하세요. 영어 문장을 그대로 두면 안 됩니다.
2. 영어로 남기는 것은 전공 용어 단어뿐입니다. (예: learning rate, gradient descent, overfitting)
3. 직역에 가깝게 옮기고, 요약하거나 설명을 덧붙이지 마세요. 문장 수와 순서를 원문과 맞춰 주세요.
4. 교수님의 말은 존댓말로 옮겨 주세요.
5. 슬라이드 제목 옆의 표시(Before notes, With notes 등)와 시간은 옮기지 않아도 됩니다.
6. 길어서 한 번에 못 끝내면 슬라이드 경계에서 멈추고, 제가 "계속"이라고 하면 이어서 출력해 주세요.

## 예시

```
원문: If the learning rate is too large, the model can diverge.
번역: learning rate가 너무 크면 모델이 발산할 수 있습니다.
```

---

아래부터 강의 전사입니다.
"""


def ai_request_markdown(document: LectureDocument, *, output_dir: Path | None = None) -> str:
    """The request followed by the whole lecture, as paragraphs per slide.

    Timestamps are left out: paragraphs give the assistant whole sentences and
    their context, which translates far better than one fragment per line.
    """
    notes = to_markdown(document, timestamps=False, output_dir=output_dir)
    return TRANSLATION_REQUEST.rstrip() + "\n\n" + notes


def run_ai_request_export(
    document: LectureDocument, pdf_options: PdfOptions, output_dir: Path
) -> ExportResult:
    """Write the request as Markdown and as PDF, each independently."""
    folder = Path(output_dir)
    result = ExportResult(output_dir=folder)
    if not document.segments:
        result.failures.append(
            (ExportFormat.MARKDOWN, "There is no transcript to translate yet.")
        )
        return result

    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / AI_REQUEST_MARKDOWN
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
    )
    try:
        target = folder / AI_REQUEST_PDF
        write_pdf(document, options, target, preface=TRANSLATION_REQUEST)
        result.written.append(target)
    except PdfExportError as exc:
        result.failures.append((ExportFormat.PDF, str(exc)))
    return result
