"""What the assistant is asked for, and the exact JSON shape it must answer in.

Both requests see the *whole* lecture at once. A preview has to know how the
lecture ends to say what matters at the start, and a quiz has to spread its
questions across all of it, so neither can be cut into batches the way the
translation is. Lectures measured so far run to about 12k tokens of
transcript at most (85 minutes), comfortably inside one request.

The schemas follow the strict structured-output rules the Codex CLI passes on:
every property is listed in ``required`` and ``additionalProperties`` is
false. Counts ("exactly 15", "exactly 4 options") are stated in the prompt and
enforced by :mod:`lecture_saver.core.study` on the way in, not in the schema -
``minItems``/``maxItems`` are not reliably accepted by strict mode, and a
schema the service rejects fails the whole run.
"""

from __future__ import annotations

from typing import Any

from ..core.study import QUIZ_LENGTH
from ..export.document import LectureDocument, paragraphs
from ..export.text_formats import section_title

__all__ = [
    "GUIDE_SCHEMA",
    "QUIZ_SCHEMA",
    "guide_prompt",
    "lecture_text",
    "quiz_prompt",
]


def lecture_text(document: LectureDocument) -> str:
    """The lecture as the assistant sees it: slide headings and speech only.

    Same rule as translation: no image, no path, no file name - only words.
    The ``## Slide N`` headings are what lets the answer point back at slides.
    """
    lines: list[str] = []
    for section in document.sections:
        groups = paragraphs(section.segments)
        if not groups:
            continue
        if document.has_slides:
            lines += [f"## {section_title(section)}", ""]
        for group in groups:
            lines += [" ".join(s.effective_text.strip() for s in group), ""]
    return "\n".join(lines).rstrip() + "\n"


def _slide_note(document: LectureDocument) -> str:
    if document.has_slides:
        return (
            '전사는 "## Slide N" 제목으로 슬라이드별로 나뉘어 있습니다. '
            "slides 항목에는 그 번호를 적어 주세요."
        )
    return "이 강의는 슬라이드 구분이 없습니다. slides 항목은 빈 배열로 두세요."


# -- the preview ------------------------------------------------------------------------

GUIDE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "key_points", "terms", "sections"],
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["term", "meaning"],
                "properties": {
                    "term": {"type": "string"},
                    "meaning": {"type": "string"},
                },
            },
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "slides", "explanation", "example"],
                "properties": {
                    "title": {"type": "string"},
                    "slides": {"type": "array", "items": {"type": "integer"}},
                    "explanation": {"type": "string"},
                    "example": {"type": "string"},
                },
            },
        },
    },
}


def guide_prompt(document: LectureDocument) -> str:
    return f"""당신은 대학 강의를 처음 듣는 학생을 돕는 친절한 튜터입니다.
아래 강의 전사를 읽고, 학생이 강의를 듣기 **전에** 읽을 예습 자료를 만들어 주세요.

## 누구를 위한 글인가

이 분야를 처음 접하는 학생입니다. **중학생도 이해할 수 있는 쉬운 한국어**로 쓰세요.

- 전공 용어는 영어 그대로 쓰고, 처음 나올 때 괄호 안에 쉬운 말로 풀어 주세요.
  예: "scheduler(여러 프로그램 중 무엇을 먼저 실행할지 정하는 관리자)"
- 어려운 개념은 일상생활의 비유나 구체적인 예시로 설명하세요.
- 한 문장에 한 가지 생각만 담고, 문장은 짧게 쓰세요.
- 강의에 없는 사실을 지어내지 마세요. 이해를 돕는 비유와 예시는 괜찮지만,
  설명하는 내용 자체는 강의에 근거해야 합니다.
- 강의 전체를 처음부터 끝까지 빠짐없이 다루세요. 주제를 건너뛰지 마세요.

## 만들 것

1. summary: 이 강의가 무엇에 관한 것인지 2~3문장.
2. key_points: 강의를 듣기 전에 꼭 알아야 할 핵심 5~8개.
   가장 중요한 것부터 순서대로, 각각 한두 문장.
3. terms: 꼭 알아야 할 용어 8~15개.
   term은 강의에 나온 영어 용어 그대로, meaning은 쉬운 한국어로 한두 문장.
4. sections: 강의 흐름을 따라 주제별로 나눈 쉬운 설명. 강의 길이에 따라 4~12개.
   - title: 주제 제목 (한국어, 필요하면 영어 용어를 함께)
   - slides: 이 주제를 다루는 슬라이드 번호들
   - explanation: 쉬운 설명. 문단이 여러 개면 빈 줄로 구분하세요.
     교수님이 강조한 부분, 헷갈리기 쉬운 부분을 꼭 짚어 주세요.
   - example: 이해를 돕는 비유나 예시 하나. 적당한 것이 없으면 빈 문자열.

{_slide_note(document)}

---

아래부터 강의 전사입니다.

{lecture_text(document)}"""


# -- the quiz ---------------------------------------------------------------------------

QUIZ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "options", "answer", "explanation", "slides"],
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "string", "enum": ["A", "B", "C", "D"]},
                    "explanation": {"type": "string"},
                    "slides": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
    },
}


def quiz_prompt(document: LectureDocument, length: int = QUIZ_LENGTH) -> str:
    return f"""아래 강의 전사를 바탕으로, 강의를 들은 뒤 복습용으로 풀
객관식 퀴즈 **정확히 {length}문제**를 만들어 주세요.

## 규칙

- question과 options는 **영어**로 쓰세요.
- 보기는 정확히 4개, 정답은 하나입니다. answer는 "A", "B", "C", "D" 중 하나.
- 보기 앞에 "A." 같은 기호를 붙이지 마세요.
- "All of the above", "None of the above", "Both A and B"처럼 다른 보기를
  가리키는 보기는 쓰지 마세요.
- 단순 암기만 묻지 말고, 이해를 확인하는 문제를 고루 섞으세요:
  개념의 정의, 두 개념의 차이, 원리(왜 그런지), 간단한 상황에 적용하기.
- 강의 앞부분부터 끝부분까지 고르게 출제하세요.
- 강의에서 다룬 내용으로만 출제하세요. 강의에 없는 사실을 정답으로 삼지 마세요.
- 오답 보기도 그럴듯하게 만드세요. 한눈에 틀린 보기는 쓰지 마세요.
- explanation은 **한국어**로 2~4문장. 왜 정답이 맞는지, 그리고 헷갈리기 쉬운
  오답이 왜 틀렸는지 설명하세요. 전공 용어는 영어 그대로 쓰세요.
- slides: 이 문제의 근거가 되는 슬라이드 번호들.

{_slide_note(document)}

---

아래부터 강의 전사입니다.

{lecture_text(document)}"""
