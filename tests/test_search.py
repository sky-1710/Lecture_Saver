"""Full-text lecture search (§18)."""

from lecture_saver.core.models import TranscriptSegment
from lecture_saver.core.search import MAX_HITS, search_lecture
from lecture_saver.core.slides import SlideAppearance
from lecture_saver.core.translations import SlideTranslation, TranslationSet


def seg(i, start, text, edited=None):
    return TranscriptSegment(
        id=f"segment_{i:03d}", start=start, end=start + 4, original_text=text, edited_text=edited
    )


SEGMENTS = [
    seg(1, 0, "Today we begin with supervised learning."),
    seg(2, 6, "여기서 러닝레이트를 보시면", edited="여기서 러닝 레이트를 보시면"),
    seg(3, 32, "If the Learning Rate is too large, the model diverges."),
    seg(4, 40, "자타 상품 식별력을 갖췄는가"),
]
SLIDES = [SlideAppearance(1, 1, 0.0, 30.0), SlideAppearance(2, 2, 30.0, 60.0)]


def texts(hits):
    return [(h.slide_number, h.segment_id) for h in hits]


class TestMatching:
    def test_results_carry_slide_number_time_and_segment(self):
        hits = search_lecture("model", SEGMENTS, SLIDES)
        assert texts(hits) == [(2, "segment_003")]
        assert hits[0].start == 32

    def test_case_is_ignored(self):
        assert texts(search_lecture("learning rate", SEGMENTS, SLIDES)) == [(2, "segment_003")]

    def test_every_word_must_appear_in_any_order(self):
        assert texts(search_lecture("diverges large", SEGMENTS, SLIDES)) == [(2, "segment_003")]
        assert search_lecture("diverges banana", SEGMENTS, SLIDES) == []

    def test_korean_spacing_is_ignored_both_ways(self):
        assert texts(search_lecture("러닝레이트", SEGMENTS, SLIDES)) == [(1, "segment_002")]
        assert texts(search_lecture("식별 력", SEGMENTS, SLIDES)) == [(2, "segment_004")]
        assert texts(search_lecture("자타상품", SEGMENTS, SLIDES)) == [(2, "segment_004")]

    def test_a_corrected_word_is_still_found_by_the_original_and_marked(self):
        segments = [seg(1, 0, "the grade in descent", edited="the gradient descent")]
        hits = search_lecture("grade in", segments, SLIDES)
        assert len(hits) == 1 and hits[0].in_original_only
        assert not search_lecture("gradient", segments, SLIDES)[0].in_original_only

    def test_excerpt_spans_mark_the_matches(self):
        hit = search_lecture("model", SEGMENTS, SLIDES)[0]
        start, end = hit.spans[0]
        assert hit.excerpt[start:end] == "model"

    def test_long_text_is_trimmed_around_the_match(self):
        long = "word " * 200 + "needle " + "word " * 200
        hit = search_lecture("needle", [seg(1, 0, long)], SLIDES)[0]
        assert hit.excerpt.startswith("…") and hit.excerpt.endswith("…")
        start, end = hit.spans[0]
        assert hit.excerpt[start:end] == "needle"

    def test_without_slides_results_have_no_slide(self):
        hits = search_lecture("model", SEGMENTS, [])
        assert hits[0].slide_number is None and hits[0].appearance_index is None

    def test_empty_query_finds_nothing(self):
        assert search_lecture("   ", SEGMENTS, SLIDES) == []

    def test_results_are_capped(self):
        many = [seg(i, i, "same word") for i in range(1, MAX_HITS + 50)]
        assert len(search_lecture("word", many, [])) == MAX_HITS


class TestTranslations:
    def test_imported_translations_are_searched_too(self):
        entry = SlideTranslation(2, 2, 30, 60, "학습률이 크면 발산")
        translations = TranslationSet(entries=[entry])
        hits = search_lecture("발산", SEGMENTS, SLIDES, translations)
        assert [(h.kind, h.slide_number) for h in hits] == [("translation", 2)]
        assert hits[0].segment_id is None

    def test_results_stay_in_lecture_order(self):
        translations = TranslationSet(entries=[SlideTranslation(1, 1, 0, 30, "learning 번역")])
        hits = search_lecture("learning", SEGMENTS, SLIDES, translations)
        assert [h.start for h in hits] == sorted(h.start for h in hits)
