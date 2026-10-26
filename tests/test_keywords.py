"""Terms picked off slide text for Whisper's keyword hints (§33).

The slide text here is made up, but its faults are the ones Windows' Korean
recogniser really produces on English slides: "SpIitting", "tO", "PIOts",
P(X|Y) read as "XIY", clipped words, pictures read as Hangul.
"""

from lecture_saver.core.keywords import TextLine, extract_keywords

TITLE = 0.045
BODY = 0.023


def slide(*lines):
    """``("text", height), ...`` or plain body-sized ``"text"``."""
    return [TextLine(*line) if isinstance(line, tuple) else TextLine(line, BODY)
            for line in lines]


def lecture(*slides):
    return extract_keywords(list(slides))


def aside(n):
    """A line of two words no other line has, nothing like any other line."""
    def word(k):
        return chr(0xAC00 + k * 397 % 11172) + chr(0xAC00 + k * 1009 % 11172)

    return f"{word(2 * n)} {word(2 * n + 1)}"


def others(count, start=100):
    """Slides on other topics, none like another. Enough of them keep a slide's
    versions under half the lecture, where a line would be a header or footer
    when versions are told apart."""
    return [slide(aside(start + 2 * i), aside(start + 2 * i + 1)) for i in range(count)]


class TestTerms:
    def test_a_heading_and_a_recurring_phrase_lead(self):
        terms = lecture(
            slide(("Model Selection", TITLE), "We estimate the test error with cross validation"),
            slide(("Model Selection", TITLE), "cross validation repeats the split"),
            slide(("Overview", TITLE), "the learning rate is a choice"),
        )
        assert terms[:2] == ["Model Selection", "cross validation"]

    def test_a_generic_word_counts_only_inside_a_phrase(self):
        terms = lecture(*[slide(f"the test set holds {n} samples of data") for n in "abcd"])
        assert "test set" in terms
        assert "test" not in terms
        assert "data" not in terms

    def test_acronyms_must_recur(self):
        terms = lecture(
            slide("the AUC, compared"),
            slide("a higher AUC"),
            slide("a single mention of XYZ"),
        )
        assert "AUC" in terms
        assert "XYZ" not in terms

    def test_a_single_word_needs_three_slides(self):
        twice = lecture(slide("regularization"), slide("regularization"), slide("other"))
        thrice = lecture(*[slide("regularization")] * 3, slide("other"))
        assert "regularization" not in twice
        assert "regularization" in thrice

    def test_three_words_must_end_like_a_term(self):
        terms = lecture(*[slide("the page table maps pages, false positive rate")] * 3)
        assert "false positive rate" in terms
        assert "page table" in terms
        assert "page table maps" not in terms

    def test_a_titled_phrase_of_three_words_is_a_term(self):
        terms = lecture(*[slide("the Receiver Operating Characteristic")] * 2)
        assert "Receiver Operating Characteristic" in terms

    def test_a_long_phrase_seen_once_is_a_sentence(self):
        terms = lecture(
            slide(("Help reveal hidden associations", TITLE)),
            slide("nothing else here"),
        )
        assert "Help reveal hidden associations" not in terms

    def test_a_word_inside_a_better_term_is_not_repeated(self):
        terms = lecture(*[slide("cross validation estimates error")] * 4)
        assert "cross validation" in terms
        assert "validation" not in terms

    def test_plurals_and_case_are_one_term(self):
        terms = lecture(
            slide("Test Sets"), slide("the test set"), slide("a test set"), slide("test sets"),
        )
        assert [t for t in terms if t.lower().startswith("test set")] == ["test set"]

    def test_headers_and_footers_on_every_slide_are_left_out(self):
        slides = [slide((f"Topic {i}", TITLE), "Jane Doe University", "gradient descent")
                  for i in range(10)]
        terms = lecture(*slides)
        assert "Jane Doe University" not in terms
        assert "Jane Doe" not in terms

    def test_a_titled_heading_of_three_words_seen_once_is_a_term(self):
        terms = lecture(slide(("System Call Interface", TITLE), "for user programs"),
                        *others(3))
        assert "System Call Interface" in terms

    def test_a_word_inside_a_longer_term_gives_it_its_place(self):
        # "Execution" heads five slides - more than the term holding it, but
        # not three times as many: the longer term takes its place at the top.
        slides = [slide(("Execution", TITLE), aside(n), aside(n + 10)) for n in range(3)]
        slides += [slide(("Limited Direct Execution", TITLE), aside(n)) for n in range(20, 22)]
        terms = lecture(*slides)
        assert terms[0] == "Limited Direct Execution"
        assert "Execution" not in terms and "Direct Execution" not in terms

    def test_a_word_on_many_more_slides_is_a_term_of_its_own(self):
        slides = [slide("a system call", aside(n)) for n in range(6)]
        terms = lecture(*slides, slide(("System Call Interface", TITLE)), *others(3))
        assert "system call" in terms and "System Call Interface" in terms

    def test_the_same_words_in_another_order_are_a_figure_read_across(self):
        slides = [slide("the system call", aside(n)) for n in range(6)]
        slides += [slide("call system call", aside(n)) for n in range(10, 12)]
        terms = lecture(*slides, *others(4))
        assert "system call" in terms
        assert "call system" not in terms

    def test_a_subject_and_its_verb_are_not_a_term(self):
        slides = [slide("when an interrupt occurs", "an interrupt caused by I/O", aside(n))
                  for n in range(3)]
        terms = lecture(*slides, *others(4))
        assert not any(term.endswith(("occurs", "caused")) for term in terms)
        assert "occurs" not in terms

    def test_a_step_in_a_procedure_is_not_a_term(self):
        slides = [slide("initialize trap table", aside(n)) for n in range(3)]
        terms = lecture(*slides, *others(4))
        assert "trap table" in terms
        assert "initialize trap table" not in terms

    def test_most_terms_returned_is_capped(self):
        slides = [slide(*[f"term{a}{b} phrase{a}{b}" for b in "abcdefghij"])
                  for a in "abcdefghij"] * 2
        assert len(extract_keywords(slides, max_terms=12)) == 12

    def test_nothing_in_nothing_out(self):
        assert lecture() == []
        assert lecture(slide()) == []


class TestVersionsOfOneSlide:
    """The professor's notes, a pop-up or a build make one slide several images."""

    def test_versions_of_one_slide_count_once(self):
        # Six versions of the slide the professor wrote on no longer outweigh
        # a term on four slides of its own.
        written_on = [slide("the console interrupt", "from either side")] * 6
        elsewhere = [slide("the trap table", aside(n)) for n in range(4)]
        terms = lecture(*written_on, *elsewhere, *others(4))
        assert terms.index("trap table") < terms.index("console interrupt")

    def test_a_slide_written_on_at_length_keeps_its_terms(self):
        # Seen once, a phrase could be an OCR accident; read alike on three
        # versions of a slide it is not.
        terms = lecture(*[slide("gradient descent finds the minimum")] * 3, *others(4))
        assert "gradient descent" in terms

    def test_a_word_a_pen_stroke_cut_short_is_dropped(self):
        def interrupt_slide(event):
            return slide(("Interrupt and trap", TITLE), event, "raised by an I/O device")

        clean = interrupt_slide("Unexpected external event")
        inked = interrupt_slide("Unexpecte externa event")
        terms = lecture(clean, *[inked] * 5, *others(7))
        words = " ".join(terms).split()
        assert "Unexpecte" not in words and "externa" not in words

    def test_a_plural_on_another_version_is_no_cut(self):
        terms = lecture(slide("the test set"), slide("test sets"), slide("a test set"))
        assert "test set" in terms

    def test_slides_sharing_a_heading_are_not_versions(self):
        slides = [slide(("Paging", TITLE), f"{topic} page table", f"{topic} lookup")
                  for topic in ("inverted", "hashed", "linear")]
        terms = lecture(*slides, *others(3))
        assert "page table" in terms


class TestOcrRepairs:
    def test_a_capital_i_after_small_letters_is_an_l(self):
        terms = lecture(*[slide(("Data SpIitting", TITLE), "Cross VaIidation")] * 2)
        assert "Data Splitting" in terms
        assert "Cross Validation" in terms

    def test_letters_that_differ_only_in_size_are_put_back(self):
        terms = lecture(*[slide(("Basic PIOts", TITLE))] * 2)
        assert "Basic Plots" in terms

    def test_a_leading_l_before_a_consonant_is_an_i(self):
        terms = lecture(*[slide("Conditional lndependence holds")] * 2)
        assert "Conditional Independence" in terms

    def test_misread_stop_words_do_not_join_terms(self):
        terms = lecture(*[slide("HOW tO estimate the posterior probability Of a class")] * 3)
        assert "posterior probability" in terms
        assert not any(word in " ".join(terms).split() for word in ("HOW", "tO", "Of"))

    def test_a_conditional_bar_read_as_i_is_maths_not_an_acronym(self):
        terms = lecture(*[slide("P(XIY) and P(YIX) and IY, but AIC and BIC")] * 2)
        assert "XIY" not in terms and "YIX" not in terms and "IY" not in terms
        assert "AIC" in terms and "BIC" in terms

    def test_digits_read_as_letters_are_dropped(self):
        terms = lecture(*[slide("queues QI and QO, then MLFQ")] * 2)
        assert "QI" not in terms and "QO" not in terms
        assert "MLFQ" in terms

    def test_a_clipped_word_is_not_a_term(self):
        slides = [slide("the page table maps pages")] * 4 + [slide("age table entry")] * 2
        terms = lecture(*slides)
        assert "page table" in terms
        assert not any(term.startswith("age") for term in terms)

    def test_an_ing_read_as_lng_is_put_back(self):
        terms = lecture(*[slide("parallel processlng units")] * 3)
        assert "parallel processing units" in terms or "parallel processing" in terms
        assert not any("processlng" in term for term in terms)

    def test_a_contraction_is_a_stop_word(self):
        terms = lecture(*[slide("the OS wouldn't be in control")] * 3)
        assert not any("wouldn't" in term for term in terms)

    def test_a_hyphenated_phrase_needs_two_slides_like_any_phrase(self):
        terms = lecture(slide("a return-from-trap instruction"), slide("then return-from-trap"),
                        *others(2))
        assert "return-from-trap" in terms

    def test_a_clipped_compound_is_dropped(self):
        slides = [slide("the return-from-trap")] * 3 + [slide("the return-from-tra")] * 2
        assert "return-from-tra" not in lecture(*slides)

    def test_letters_joined_by_a_hyphen_are_maths(self):
        assert "x-m" not in lecture(*[slide("the distance x-m squared")] * 3)

    def test_a_digit_after_an_acronym_read_as_a_letter_is_dropped(self):
        terms = lecture(*[slide("save state into PCBI, reload state from PCB")] * 3)
        assert "PCB" in terms
        assert "PCBI" not in terms

    def test_a_conditional_bar_between_words_is_maths(self):
        terms = lecture(*[slide("P(TrafficIRain) and P(UmbrellaIRain)")] * 3)
        assert not any("Rain" in term for term in terms)

    def test_pictures_and_small_print_are_ignored(self):
        terms = lecture(*[slide(("별-수 그림", 0.29), ("tiny figure label", 0.008),
                                "gradient descent")] * 3)
        assert "gradient descent" in terms
        assert not any("figure" in term or "별" in term for term in terms)


class TestHangul:
    def test_particles_come_off_and_compounds_stay(self):
        terms = lecture(*[slide("상표권은 상표를 보호한다", "상표권의 범위")] * 2)
        assert "상표권" in terms
        assert "상표권은" not in terms

    def test_predicate_fragments_are_not_terms(self):
        terms = lecture(*[slide("상품을 표시하 기호")] * 3)
        assert "표시하" not in terms

    def test_a_one_syllable_misreading_of_a_commoner_word_is_dropped(self):
        # Six slides of other text keep 상표 under the header/footer share.
        slides = [slide("상표")] * 6 + [slide("샴표")] * 2 + [slide("기타 내용")] * 6
        terms = lecture(*slides)
        assert "상표" in terms
        assert "샴표" not in terms

    def test_word_pairs_need_three_slides(self):
        assert "지리적 표시" not in lecture(*[slide("지리적 표시")] * 2)
        assert "지리적 표시" in lecture(*[slide("지리적 표시")] * 3)

    def test_scripts_do_not_mix_in_one_term(self):
        terms = lecture(*[slide("상표 trademark law")] * 3)
        assert not any("상표" in term and "trademark" in term for term in terms)
