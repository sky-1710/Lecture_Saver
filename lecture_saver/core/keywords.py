"""Course terms from the words on the slides, as keyword hints for Whisper.

A lecturer says the terms the slides show - "cross validation", "ROC", "Bayes'
theorem" - and those are what Whisper most often mishears. Given the text read
off every slide (slide_text.py), this picks the terms worth hinting, most
useful first.

The text comes from OCR, so it is noisy. Three kinds of noise matter:

* **Confusions** of a recogniser reading another script: a Korean recogniser
  reads "Splitting" as "SpIitting", "Independence" as "lndependence" and "to" as
  "tO". An ``I`` after a lower case letter is put back to ``l``, an ``l`` that
  starts a word before a consonant to ``I``; a short word whose case flips
  mid-word is a stop word misread ("tO") or maths ("XiIY", from P(Xi|Y)).
* **Pictures read as letters** - an arrow or a plot becomes "별-수". Lines far
  taller than any text line are dropped, as is any word that mixes scripts or
  carries stray symbols.
* **Small print** in figures and code: lines shorter than :data:`MIN_HEIGHT`
  of the slide are dropped.

Terms are then found RAKE-style: a line is cut into phrases at stop words and
punctuation, and every run of one to :data:`MAX_TERM_WORDS` words is a
candidate. A candidate scores by how many slides show it, counted double on a
slide where it is in a heading (a line clearly taller than the lecture's body
text), and higher again for a phrase of several words - "cross validation" says
more than either word alone. Whisper already writes "data" and "time" right, so
a generic word only counts inside a phrase ("test set", "response time"); any
other single word must recur on three slides, an acronym on two, since stray
capitals are what OCR noise looks like. Text on nearly every slide is a header
or footer - the lecturer's name, the university - and is left out.

Pure: text lines in, ranked terms out.
"""

from __future__ import annotations

import functools
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

__all__ = ["MAX_TERM_WORDS", "MIN_HEIGHT", "TextLine", "extract_keywords"]


@dataclass(frozen=True)
class TextLine:
    """One line of text read off a slide image."""

    text: str
    #: Height of the line's tallest word, as a fraction of the image height.
    height: float


#: Longest phrase considered a term.
MAX_TERM_WORDS = 3

#: Lines smaller than this (of the slide's height) are figure labels or code.
MIN_HEIGHT = 0.012

#: Lines taller than this are a picture read as letters - no slide text is.
MAX_HEIGHT = 0.12

#: A line this much taller than the lecture's typical line is a heading.
HEADING_RATIO = 1.5

#: How many terms to return at most; Whisper's hint budget runs out first.
MAX_TERMS = 60

#: Text on at least this share of the slides is a header or footer...
BOILERPLATE_SHARE = 0.75
#: ...once there are enough slides to tell.
BOILERPLATE_MIN_SLIDES = 8

# Phrase boundaries: punctuation, bullets, brackets, slashes, maths.
_SEPARATORS = re.compile(r"[,.;:!?()\[\]{}<>\"“”‘’`/\\|•▪■□●◦○·…→←↑↓=+*^~#&@%$]+")
_LATIN = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_HANGUL = re.compile(r"[가-힣]+")
_ACRONYM = re.compile(r"[A-Z]{2,6}")
# Letters whose capital and small forms differ only in size, which a recogniser
# for another script mixes up: "PIOts" for "Plots".
_SIZE_ONLY = frozenset("CKOSVWXZ")
_STRAY_L = re.compile(r"^l(?=[bcdfghjkmnpqrstvwxz])")
# "QI", "XO": Q1 and X0 with the digit read as a letter.
_DIGIT_ACRONYM = re.compile(r"[B-DF-HJ-NP-TV-Z][IO]")
# "XIY", "IY", "AIBJ": P(X|Y) with the bar read as an I.
_CONDITIONAL = re.compile(r"[A-Z]?I[A-Z]{0,2}|[A-Z]{1,2}I[A-Z]?")
#: Real acronyms that look like a misread bar.
_KNOWN_I_ACRONYMS = frozenset({
    "AI", "AIC", "API", "BIC", "CI", "CLI", "GI", "GUI", "IO", "IP", "IT", "KPI",
    "MRI", "PCI", "PI", "RAID", "ROI", "UI",
})

_ENGLISH_STOP = frozenset(
    """
    a about above after again against all also am an and any are aren't as at
    be because been before being below between both but by can cannot could
    did do does doing don't down during each either else etc even ever every
    few for from further get gets getting give given gives go goes going got had
    has have having he her here hers herself him himself his how however i if
    in into is isn't it its itself just let lets like made make makes making
    many may me might more most much must my myself need needs neither no nor
    not now of off often on once one only onto or other others our ours
    ourselves out over own per put rather really same see seen she should since
    so some such than that the their theirs them themselves then there these
    they this those though through thus to too toward towards under until up
    upon us use used uses using very via was we well were what when where
    whether which while who whom whose why will with within without would yet
    you your yours yourself yourselves
    able across actually along already although among another around ask asked
    away back become becomes best better big called came come comes con cont
    continued done e eg either end ends etc example examples far find finds
    first following follows four general good great high higher however ie
    important include includes including instead kind known large larger last
    least less let little long low lower main mean means mostly near new next
    note number numbers old order part particular possible previous problem
    problems question questions recall right says second shown shows similar
    simple small smaller something sometimes still take takes taken there's
    thing things think third three today true two type types usually value
    values various want way ways whole yes vs versus different certain holds
    """.split()
)

#: Words that make a term only together with others: "test" alone is not worth
#: hinting, "test set" is.
_ENGLISH_GENERIC = frozenset(
    """
    algorithm algorithms answer approach arrival assess attempt attribute
    attributes average background basic case cases change changes chart choose
    class classes compute computing condition conditions confidence context
    cost criteria criterion current curve curves data definition definitions
    distribution error errors estimate estimation evaluate evaluation feature
    features figure final function functions goal graph independent input
    inputs instance instances interactive introduction level levels line lines
    list maximize maximizes measure measures method methods minimize model
    models motivation move moves normal order outline output outputs overhead
    overview part parts performance plot plots point points policies policy
    predict prediction problem process processes question random rate rates
    result results review rule rules run running sample samples scheme schemes
    select selected set sets simple size space state states step steps summary
    system systems table task tasks test tests time train training user users
    value values variable variables waiting work works
    based check color compare comparing display global help index information
    learning local maximum minimum multiple optimal relationship represents
    reveal solution solutions update updates useful varies
    """.split()
)

#: Nouns a technical term of three words ends in ("false positive rate",
#: "logical address space"). Without one, three words are usually a term and
#: a verb ("page table maps"). Words that are verbs as often ("estimates",
#: "plots") are left out on purpose.
_HEAD_NOUNS = frozenset(
    """
    address algorithm analysis bit buffer burst cache chart classification
    classifier cost criteria curve data deviation descent distribution entry
    error frame function graph interval layer learning loss matrix memory
    method model network page parameter policy probability process quantum
    queue rate register regression rule sampling scheduling score selection
    series set size slice space switch system table test theorem time training
    tree validation value variable variance vector
    """.split()
)

_KOREAN_STOP = frozenset(
    """
    것 수 등 및 또는 그리고 그러나 하지만 그래서 따라서 이러한 이런 그런 저런 이 그 저
    다음 경우 위한 대한 통해 같은 있는 없는 하는 되는 된다 한다 있다 없다 이다 우리
    여기 때문 정도 가장 매우 모든 각 더 덜 잘 또 즉 예 예시 예를 들어 참고 주의 정리
    요약 문제 방법 사용 결과 내용 부분 이후 이전 위 아래 왼쪽 오른쪽 다른 같이 함께
    """.split()
)

#: Particles stripped off the end of a Hangul word, longest first. 미 is 이 as
#: Korean OCR often reads it ("법인미").
_JOSA = tuple(sorted(
    "으로써 으로서 에서는 에게서 이라는 이라고 에서 에게 으로 라는 이다 입니다 에는 까지 부터 "
    "보다 처럼 마다 은 는 이 가 을 를 의 에 로 와 미".split(),
    key=len, reverse=True,
))

# Verb and adjective endings: a Hangul word ending so is not a term. "표시하"
# is what is left of "표시하는" when OCR splits the word.
_PREDICATE_ENDINGS = (
    "다", "고", "며", "면", "서", "여", "게", "지", "하", "되", "함", "됨", "된",
    "었", "았", "였", "하는", "되는", "적인", "하기",
)


@dataclass
class _Candidate:
    words: int
    hangul: bool
    slides: set[int] = field(default_factory=set)
    heading_slides: set[int] = field(default_factory=set)
    forms: Counter[str] = field(default_factory=Counter)
    first_seen: tuple[int, int] = (0, 0)

    @property
    def score(self) -> float:
        base = len(self.slides) + len(self.heading_slides)
        if self.words > 1:
            return base * 2.0
        return base * (1.5 if _ACRONYM.fullmatch(next(iter(self.forms))) else 1.0)


def extract_keywords(
    slides: Sequence[Sequence[TextLine]], max_terms: int = MAX_TERMS
) -> list[str]:
    """The terms worth hinting to Whisper, most useful first.

    ``slides`` holds the lines read off each slide, one entry per distinct slide
    (not per appearance: a slide shown three times is one slide).
    """
    lines = [
        (index, line) for index, slide in enumerate(slides) for line in slide
        if MIN_HEIGHT <= line.height <= MAX_HEIGHT and line.text.strip()
    ]
    if not lines:
        return []
    body = statistics.median(line.height for _, line in lines)

    candidates: dict[str, _Candidate] = {}
    for position, (slide, line) in enumerate(lines):
        heading = line.height >= body * HEADING_RATIO
        for run in _phrases(line.text):
            for size in range(1, min(MAX_TERM_WORDS, len(run)) + 1):
                for start in range(len(run) - size + 1):
                    words = run[start:start + size]
                    key = " ".join(_key(word) for word in words)
                    found = candidates.get(key)
                    if found is None:
                        found = candidates[key] = _Candidate(
                            words=size, hangul=_is_hangul(words[0]),
                            first_seen=(position, start),
                        )
                    found.slides.add(slide)
                    if heading:
                        found.heading_slides.add(slide)
                    found.forms[" ".join(words)] += 1

    boilerplate = (
        len(slides) * BOILERPLATE_SHARE if len(slides) >= BOILERPLATE_MIN_SLIDES
        else float("inf")
    )
    # Every word, stop words too, to recognise misread and clipped words against.
    hangul_seen: dict[str, set[int]] = {}
    latin_seen: dict[str, set[int]] = {}
    for slide, line in lines:
        for word in _HANGUL.findall(line.text):
            hangul_seen.setdefault(_strip_josa(word), set()).add(slide)
        for word in _LATIN.findall(line.text):
            latin_seen.setdefault(_repair_case(word).lower(), set()).add(slide)

    misread = functools.cache(lambda word: _misread(word, hangul_seen))
    clipped = functools.cache(lambda word: _clipped(word, latin_seen))

    def noisy(key: str, found: _Candidate) -> bool:
        if found.hangul:
            return any(misread(word) for word in key.split())
        return any(clipped(word) for word in next(iter(found.forms)).lower().split())

    eligible = [
        (key, found) for key, found in candidates.items()
        if _eligible(key, found) and len(found.slides) < boilerplate
        and not noisy(key, found)
    ]
    eligible.sort(key=lambda item: (-item[1].score, item[1].first_seen))

    # Gather spares: some will turn out to be said already by a longer term.
    ranked = eligible[: max_terms * 3]
    kept = [(key, found) for key, found in ranked if not _covered(key, found, ranked)]
    return [_display(found) for _, found in kept[:max_terms]]


def _phrases(text: str) -> list[list[str]]:
    """Runs of term words in ``text``, cut at stop words, punctuation and noise."""
    runs: list[list[str]] = []
    for piece in _SEPARATORS.split(text):
        run: list[str] = []
        script = ""
        for raw in piece.split():
            word, word_script = _clean(raw)
            if not word or (run and word_script != script):
                if run:
                    runs.append(run)
                run = []
            if word:
                run.append(word)
                script = word_script
        if run:
            runs.append(run)
    return runs


def _clean(raw: str) -> tuple[str, str]:
    """``(word, script)`` if ``raw`` can be part of a term, else ``("", "")``."""
    word = raw.strip("'-_")
    if word.endswith("'s"):
        word = word[:-2]  # "model's performance" is about the model's performance
    if _LATIN.fullmatch(word):
        word = _repair_case(word)
        if len(word) <= 5 and re.search(r"[a-z][A-Z]", word):
            # "tO", "etC" are stop words misread; "XiIY" is maths.
            return "", ""
        if word.lower() in _ENGLISH_STOP or len(word) < 2 or _DIGIT_ACRONYM.fullmatch(word):
            return "", ""
        if _CONDITIONAL.fullmatch(word) and word not in _KNOWN_I_ACRONYMS:
            return "", ""
        return word, "latin"
    if _HANGUL.fullmatch(word):
        bare = next((word[: -len(p)] for p in _JOSA if word.endswith(p) and len(word) > len(p)), "")
        word = _strip_josa(word)
        if (
            len(word) < 2 or word in _KOREAN_STOP or bare in _KOREAN_STOP
            or word.endswith(_PREDICATE_ENDINGS)
        ):
            return "", ""
        return word, "hangul"
    # Digits, symbols, mixed scripts, Hanja: OCR noise or not a term.
    return "", ""


def _repair_case(word: str) -> str:
    """Undo a recogniser's case confusions: "PIOts" -> "Plots".

    Only next to small letters, so an acronym ("ROC", "MLFQ") is left alone.
    """
    letters = list(word)

    def small(index: int) -> bool:
        return 0 <= index < len(letters) and letters[index].islower()

    for index in range(1, len(letters)):
        if letters[index] in _SIZE_ONLY and (small(index + 1) or small(index - 1)):
            letters[index] = letters[index].lower()
    for index in range(1, len(letters)):
        if letters[index] == "I" and (small(index + 1) or small(index - 1)):
            letters[index] = "l"
    return _STRAY_L.sub("I", "".join(letters))


def _strip_josa(word: str) -> str:
    for particle in _JOSA:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[: -len(particle)]
    return word


def _key(word: str) -> str:
    """What two spellings of one word have in common: case and plural ignored."""
    key = word.lower()
    if len(key) > 3 and key.endswith("s") and not key.endswith(("ss", "us", "is")):
        key = key[:-1]
    return key


def _is_hangul(word: str) -> bool:
    return bool(_HANGUL.fullmatch(word))


def _eligible(key: str, found: _Candidate) -> bool:
    shown = len(found.slides)
    in_heading = bool(found.heading_slides)
    if found.hangul:
        # Hangul OCR misplaces spaces, so word pairs are often accidents of
        # the line break; a compound written as one word is the safe term.
        if found.words == 1:
            return shown >= 2 or in_heading
        return found.words == 2 and shown >= 3
    if found.words > 2:
        # A long phrase seen once is a sentence, not a term; nor is one that
        # does not end the way terms do.
        words = next(iter(found.forms)).split()
        titled = all(word[:1].isupper() for word in words)
        return shown >= 2 and (titled or key.split()[-1] in _HEAD_NOUNS)
    if found.words > 1:
        return shown >= 2 or in_heading
    word = next(iter(found.forms))
    if _ACRONYM.fullmatch(word):
        return shown >= 2 or in_heading
    if word.isascii() and (key in _ENGLISH_GENERIC or word.lower() in _ENGLISH_GENERIC):
        return False
    return (shown >= 3 or in_heading) and len(word) >= 5


def _misread(word: str, seen: dict[str, set[int]]) -> bool:
    """Whether this Hangul word is a misreading of a commoner one.

    OCR turns 상표 into 샴표 on some slides: one syllable off, from a word on
    many more slides. A wrong spelling is worse than none as a hint - Whisper
    would copy it.
    """
    shown = len(seen.get(word, ()))
    return any(
        len(other) == len(word) and other != word
        and sum(a != b for a, b in zip(word, other, strict=True)) == 1
        and len(slides) >= 2 * shown
        for other, slides in seen.items()
    )


def _clipped(word: str, seen: dict[str, set[int]]) -> bool:
    """Whether this short word is the visible end of a longer one.

    Text under a pop-up or past the edge of the frame is read in part: "age
    table" is "page table" with its first letter covered. Such a fragment is
    the tail - or, shorter still, the head - of a word on many more slides.
    """
    if len(word) > 5:
        return False
    shown = len(seen.get(word, ()))
    return any(
        len(other) > len(word)
        and (other.endswith(word) or (len(word) <= 3 and other.startswith(word)))
        and len(slides) >= 2 * shown
        for other, slides in seen.items()
    )


def _covered(key: str, found: _Candidate, ranked: Iterable[tuple[str, _Candidate]]) -> bool:
    """Whether a longer ranked term already says this one.

    "validation" adds nothing beside "cross validation" when it appears on
    hardly more slides; on many more slides it is a term in its own right.
    """
    return any(
        other_key != key
        and f" {key} " in f" {other_key} "
        and len(found.slides) <= 1.25 * len(other.slides)
        for other_key, other in ranked
    )


def _display(found: _Candidate) -> str:
    """The spelling seen most; between equals, the capitalised one."""
    return max(found.forms.items(), key=lambda item: (item[1], item[0][:1].isupper()))[0]
