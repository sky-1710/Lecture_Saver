"""Reading an assistant's Markdown, and drawing its flowcharts, for the PDF (§19, §32)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lecture_saver.export.flowchart import Edge, Flowchart, layout_chart, parse_flowchart
from lecture_saver.export.markdown_pdf import (
    Code,
    Heading,
    Item,
    Quote,
    Rule,
    TableBlock,
    Text,
    headings,
    inline,
    markdown_story,
    parse_blocks,
)
from lecture_saver.export.pdf import build_pdf, find_korean_font, printable

needs_korean_font = pytest.mark.skipif(
    find_korean_font() is None, reason="no TrueType font with Korean glyphs installed"
)


# -- reading a flowchart -----------------------------------------------------------------


def _source(*lines):
    return "\n".join(["flowchart TD", *lines])


class TestParsingAFlowchart:
    def test_boxes_and_arrows(self):
        chart = parse_flowchart(_source('A["CPU는 하나"] --> B["누구에게 줄까?"]'))
        assert chart.nodes == {"A": "CPU는 하나", "B": "누구에게 줄까?"}
        assert chart.edges == (Edge("A", "B"),)
        assert not chart.across

    def test_it_reads_left_to_right_when_told(self):
        assert parse_flowchart("flowchart LR\nA --> B").across
        assert parse_flowchart("graph LR\nA --> B").across

    def test_graph_with_no_direction_is_top_down(self):
        assert not parse_flowchart("graph\nA --> B").across

    @pytest.mark.parametrize(
        "line",
        [
            "A -->|이유| B", "A -- 이유 --> B", 'A -->|"이유"| B',
            "A == 이유 ==> B", "A -. 이유 .-> B",
        ],
    )
    def test_words_on_a_line(self, line):
        assert parse_flowchart(_source(line)).edges == (Edge("A", "B", "이유"),)

    def test_a_line_without_an_arrow_stays_without_one(self):
        assert parse_flowchart(_source("A --- B")).edges == (Edge("A", "B", "", False),)

    def test_a_chain(self):
        chart = parse_flowchart(_source("A --> B --> C"))
        assert [(e.source, e.target) for e in chart.edges] == [("A", "B"), ("B", "C")]

    def test_several_at_once(self):
        chart = parse_flowchart(_source("A & B --> C & D"))
        assert len(chart.edges) == 4

    def test_a_label_given_once_is_kept(self):
        chart = parse_flowchart(_source('A["처음"] --> B', "B --> A", "A --> C"))
        assert chart.nodes["A"] == "처음"

    @pytest.mark.parametrize(
        ("node", "label"),
        [
            ('A["괄호 (안) 말"]', "괄호 (안) 말"),
            ("A[말]", "말"),
            ("A(말)", "말"),
            ("A([말])", "말"),
            ("A((말))", "말"),
            ("A{말}", "말"),
            ("A[[말]]", "말"),
            ('A["둘째<br/>줄"]', "둘째\n줄"),
        ],
    )
    def test_the_shapes_of_a_box(self, node, label):
        assert parse_flowchart(_source(f"{node} --> B")).nodes["A"] == label

    def test_lines_about_looks_are_passed_over(self):
        chart = parse_flowchart(
            _source(
                "subgraph 묶음", "A --> B", "end", "style A fill:#f9f", "classDef x fill:#fff",
                "%% 주석",
            )
        )
        assert list(chart.nodes) == ["A", "B"]

    def test_statements_on_one_line(self):
        assert len(parse_flowchart(_source("A --> B; B --> C;")).edges) == 2

    def test_the_same_arrow_twice_is_drawn_once(self):
        assert len(parse_flowchart(_source("A --> B", "A -->|이유| B")).edges) == 1

    def test_an_arrow_to_itself_is_left_out(self):
        assert parse_flowchart(_source("A --> A", "A --> B")).edges == (Edge("A", "B"),)

    @pytest.mark.parametrize(
        "source",
        [
            "",
            "sequenceDiagram\nA->>B: hi",
            "mindmap\n  root",
            "flowchart TD",
            _source('A["닫히지 않음 --> B'),
            _source("A ~~> B"),
        ],
    )
    def test_what_cannot_be_read_is_said_to_be(self, source):
        assert parse_flowchart(source) is None


# -- placing the boxes -------------------------------------------------------------------


def _measure(label):
    return (40.0 + 6 * len(label), 30.0)


def _overlap(a, b):
    return (
        abs(a.x - b.x) < (a.width + b.width) / 2
        and abs(a.y - b.y) < (a.height + b.height) / 2
    )


class TestPlacingTheBoxes:
    def test_a_chain_goes_down_the_page(self):
        layout = layout_chart(parse_flowchart(_source("A --> B --> C")), _measure)
        ys = [layout.boxes[n].y for n in "ABC"]
        assert ys == sorted(ys) and len(set(ys)) == 3

    def test_across_it_goes_along_the_page(self):
        layout = layout_chart(parse_flowchart("flowchart LR\nA --> B --> C"), _measure)
        xs = [layout.boxes[n].x for n in "ABC"]
        assert xs == sorted(xs) and len(set(xs)) == 3

    def test_every_box_is_inside_the_picture_and_none_overlap(self):
        chart = parse_flowchart(
            _source(
                "A --> B", "A --> C", "A --> D", "B --> E", "C --> E", "D --> F", "E --> G",
                "F --> G", "A --> G", "H --> E",
            )
        )
        layout = layout_chart(chart, _measure)
        boxes = list(layout.boxes.values())
        for box in boxes:
            assert box.x - box.width / 2 >= -0.01
            assert box.x + box.width / 2 <= layout.width + 0.01
            assert box.y - box.height / 2 >= -0.01
            assert box.y + box.height / 2 <= layout.height + 0.01
        for index, box in enumerate(boxes):
            assert not any(_overlap(box, other) for other in boxes[index + 1:])

    def test_a_line_that_skips_a_layer_goes_around_what_is_in_it(self):
        layout = layout_chart(parse_flowchart(_source("A --> B", "B --> C", "A --> C")), _measure)
        skipping = next(r for r in layout.routes if len(r.points) == 3)
        middle_x, _ = skipping.points[1]
        box = layout.boxes["B"]
        assert abs(middle_x - box.x) > box.width / 2

    def test_a_cycle_is_drawn_with_each_arrow_the_way_it_was_written(self):
        chart = parse_flowchart(_source("A --> B", "B --> C", "C --> A"))
        layout = layout_chart(chart, _measure)
        assert len(layout.routes) == 3
        back = next(r for r in layout.routes if r.points[0][1] > r.points[-1][1])
        assert back.points[-1][1] <= layout.boxes["A"].y + layout.boxes["A"].height

    def test_boxes_with_no_arrows_are_still_placed(self):
        chart = Flowchart(False, {"A": "하나", "B": "둘"}, ())
        layout = layout_chart(chart, _measure)
        assert set(layout.boxes) == {"A", "B"}
        assert not _overlap(layout.boxes["A"], layout.boxes["B"])

    def test_it_says_the_same_thing_every_time(self):
        """A PDF made again must not rearrange a picture the student has seen."""
        chart = parse_flowchart(_source("A --> B", "A --> C", "B --> D", "C --> D"))
        assert layout_chart(chart, _measure) == layout_chart(chart, _measure)


# -- reading the Markdown ----------------------------------------------------------------


class TestReadingMarkdown:
    def test_headings_at_each_level(self):
        blocks = parse_blocks("# 하나\n\n## 둘\n\n### 셋 ###")
        assert blocks == [Heading(1, "하나"), Heading(2, "둘"), Heading(3, "셋")]

    def test_a_paragraph_keeps_its_line_breaks(self):
        assert parse_blocks("**A. 배경**\n왜 필요한가?") == [Text("**A. 배경**\n왜 필요한가?")]

    def test_a_paragraph_ends_where_a_list_begins(self):
        assert parse_blocks("설명\n- 하나\n- 둘") == [
            Text("설명"), Item(0, "-", "하나"), Item(0, "-", "둘"),
        ]

    def test_a_numbered_list_keeps_its_numbers(self):
        assert parse_blocks("3. 셋\n4) 넷") == [Item(0, "3.", "셋"), Item(0, "4)", "넷")]

    def test_a_nested_list(self):
        assert parse_blocks("- 위\n  - 아래\n    - 더 아래") == [
            Item(0, "-", "위"), Item(1, "-", "아래"), Item(2, "-", "더 아래"),
        ]

    def test_a_line_indented_under_an_item_goes_on_with_it(self):
        assert parse_blocks("- 위\n  이어짐\n- 다음") == [
            Item(0, "-", "위\n이어짐"), Item(0, "-", "다음"),
        ]

    def test_a_table(self):
        assert parse_blocks("| a | b |\n|---|:-:|\n| 1 | 2 |\n| 3 | 4 |") == [
            TableBlock((("a", "b"), ("1", "2"), ("3", "4")))
        ]

    def test_a_table_with_a_short_row_is_padded(self):
        assert parse_blocks("| a | b |\n|---|---|\n| 1 |") == [
            TableBlock((("a", "b"), ("1", "")))
        ]

    def test_a_table_without_outer_pipes(self):
        assert parse_blocks("a | b\n--|--\n1 | 2") == [TableBlock((("a", "b"), ("1", "2")))]

    def test_a_pipe_that_belongs_to_a_cell(self):
        assert parse_blocks("| a |\n|---|\n| x \\| y |") == [TableBlock((("a",), ("x | y",)))]

    def test_a_line_with_a_pipe_is_not_a_table_by_itself(self):
        assert parse_blocks("P(A | B)는 조건부 확률") == [Text("P(A | B)는 조건부 확률")]

    def test_code_is_kept_exactly(self):
        assert parse_blocks("```c\nint x;\n\n  # 제목 아님\n```") == [
            Code("c", "int x;\n\n  # 제목 아님")
        ]

    def test_code_never_closed_keeps_everything(self):
        assert parse_blocks("```\n하나\n둘") == [Code("", "하나\n둘")]

    def test_a_quote(self):
        assert parse_blocks("> 하나\n> 둘") == [Quote("하나\n둘")]

    def test_a_rule(self):
        assert parse_blocks("위\n\n---\n\n아래") == [Text("위"), Rule(), Text("아래")]

    def test_the_contents_are_the_big_headings(self):
        text = "## PART 1. **큰** 그림\n\n### 작은 제목\n\n## 최종 정리"
        assert headings(text) == ["PART 1. 큰 그림", "최종 정리"]

    @pytest.mark.parametrize(
        "text", ["", "\n\n", "---", "|", "| a |", "```", "- ", "# ", ">", "****", "`", "*"]
    )
    def test_nothing_breaks_it(self, text):
        parse_blocks(text)


@needs_korean_font
class TestWords:
    @pytest.fixture(autouse=True)
    def fonts(self, tmp_path):
        build_pdf(tmp_path / "fonts.pdf", "fonts", lambda w, h: [_spacer()])

    def test_bold(self):
        assert inline("이건 **중요** 합니다") == "이건 <b>중요</b> 합니다"

    def test_what_looks_like_markup_is_escaped(self):
        assert inline("a < b & c > d") == "a &lt; b &amp; c &gt; d"

    def test_a_star_in_a_formula_is_a_star(self):
        assert inline("2 * x * y 를 계산") == "2 * x * y 를 계산"
        assert inline("a*b*c") == "a*b*c"

    def test_emphasis_is_printed_plainly(self):
        assert inline("정말 *중요* 해요") == "정말 중요 해요"

    def test_code_in_english_is_in_the_code_font(self):
        assert '<font name="Courier">ls -l</font>' in inline("`ls -l` 명령")

    def test_code_with_korean_stays_in_a_font_that_has_it(self):
        assert "Courier" not in inline("`레지스터 r1`")

    def test_a_line_break(self):
        assert inline("하나\n둘") == "하나<br/>둘"
        assert inline("하나<br>둘") == "하나<br/>둘"

    @pytest.mark.parametrize("marker", ["교안 내용", "교수님 설명", "AI 보충 설명"])
    def test_where_a_line_came_from_stands_out(self, marker):
        assert f"<b>[{marker}]</b></font>" in inline(f"[{marker}] 내용")

    def test_a_time_stands_out(self):
        assert "<b>[00:12:34]</b></font>" in inline("여기서 [00:12:34] 말합니다")

    def test_a_page_of_the_slides_stands_out(self):
        assert "<b>[교안 p.3–6]</b></font>" in inline("여기서 [교안 p.3–6] 말합니다")

    def test_a_symbol_the_korean_font_lacks_uses_the_symbol_font(self):
        assert "LectureSaverSymbols" in printable("✓ 맞습니다")
        assert "LectureSaverSymbols" not in printable("맞습니다 → 네")

    def test_invisible_joiners_are_dropped(self):
        assert "️" not in printable("⚠️ 주의")


def _spacer():
    from reportlab.platypus import Spacer

    return Spacer(1, 1)


# -- the page ----------------------------------------------------------------------------


def _pdf_text(path: Path) -> list[str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])  # noqa: F841
    pdf = QPdfDocument()
    pdf.load(str(path))
    pages = [pdf.getAllText(i).text() for i in range(pdf.pageCount())]
    pdf.close()
    return pages


@needs_korean_font
class TestThePage:
    def _pages(self, tmp_path, markdown):
        target = tmp_path / "lesson.pdf"
        build_pdf(target, "lesson", lambda w, h: markdown_story(markdown, w, h))
        return _pdf_text(target)

    def test_everything_reaches_the_page(self, tmp_path):
        text = "\n".join(
            self._pages(
                tmp_path,
                "# 제목\n\n본문 문단\n\n- 항목\n\n> 인용\n\n| 열 |\n|---|\n| 칸 |\n\n"
                "```c\nint x;\n```\n\n---\n\n끝",
            )
        )
        for words in ("제목", "본문 문단", "항목", "인용", "칸", "int x;", "끝"):
            assert words in text

    def test_a_chart_that_cannot_be_drawn_is_printed_as_written(self, tmp_path):
        text = "\n".join(self._pages(tmp_path, "```mermaid\nsequenceDiagram\nA->>B: 안녕\n```"))
        assert "sequenceDiagram" in text and "안녕" in text

    def test_a_chart_too_big_to_read_is_printed_as_written(self, tmp_path):
        boxes = "\n".join(f'R --> N{n}["상자 번호 {n} 입니다 길게 적은 설명"]' for n in range(60))
        text = "\n".join(self._pages(tmp_path, f"```mermaid\nflowchart TD\n{boxes}\n```"))
        assert "flowchart TD" in text and "상자 번호 59" in text

    def test_a_big_chart_stays_on_the_page(self, tmp_path):
        boxes = "\n".join(f'R --> N{n}["상자 번호 {n}"]' for n in range(10))
        pages = self._pages(tmp_path, f"```mermaid\nflowchart TD\n{boxes}\n```")
        assert len(pages) == 1 and "상자 번호 9" in pages[0]

    def test_a_tall_table_flows_over_pages(self, tmp_path):
        rows = "\n".join(f"| 항목 {n} | 설명 {n} |" for n in range(120))
        pages = self._pages(tmp_path, f"| 열 | 뜻 |\n|---|---|\n{rows}")
        assert len(pages) > 1 and "항목 119" in pages[-1]

    def test_a_cell_longer_than_a_page_does_not_lose_the_pdf(self, tmp_path):
        cell = "아주 긴 설명입니다. " * 900
        text = "\n".join(self._pages(tmp_path, f"| 열 |\n|---|\n| {cell} |\n\n끝"))
        assert "끝" in text

    def test_long_code_wraps_and_flows(self, tmp_path):
        code = "\n".join(f"line_{n} = " + "x" * 300 for n in range(200))
        assert len(self._pages(tmp_path, f"```\n{code}\n```")) > 1

    @pytest.mark.parametrize("level", ["##", "###"])
    def test_the_answers_start_a_new_page(self, tmp_path, level):
        pages = self._pages(tmp_path, f"## 최종 정리\n\n질문\n\n{level} 확인 문제 정답\n\n답")
        assert len(pages) == 2
        assert "질문" in pages[0] and "답" in pages[1]

    def _full_page(self):
        return "\n\n".join(["내용을 채우는 문단입니다. " * 12] * 11)

    @pytest.mark.parametrize(
        "next_thing",
        [
            '```mermaid\nflowchart TD\nA["그림 처음"] --> B --> C --> D --> E --> F\n```',
            "| 표의 머리 |\n|---|\n" + "\n".join("| 줄 |" for _ in range(30)),
        ],
    )
    def test_a_heading_stays_with_what_follows_it(self, tmp_path, next_thing):
        heading = "### 혼자 남으면 안 되는 제목"
        pages = self._pages(tmp_path, f"{self._full_page()}\n\n{heading}\n\n{next_thing}")
        with_heading = next(p for p in pages if "혼자 남으면" in p)
        assert "그림 처음" in with_heading or "표의 머리" in with_heading

    def test_a_big_heading_stays_with_its_chart_through_the_rule(self, tmp_path):
        chart = '```mermaid\nflowchart TD\nA["그림 처음"] --> B --> C --> D --> E --> F\n```'
        titles = "## 큰 제목\n\n### 작은 제목"
        pages = self._pages(tmp_path, f"{self._full_page()}\n\n{titles}\n\n{chart}")
        assert "그림 처음" in next(p for p in pages if "큰 제목" in p)

    def test_a_symbol_is_printed_and_not_a_box(self, tmp_path):
        text = "\n".join(self._pages(tmp_path, "맞으면 ✓ 표시, 근사 ≈ 표시"))
        assert "✓" in text and "≈" in text
