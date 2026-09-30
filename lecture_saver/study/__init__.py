"""The study pack: a preview to read before the lecture, and a quiz after it.

The assistant writes the preview (예습) and the quiz; the lecture notes in
between are the ones Lecture Saver already makes. Like
:mod:`lecture_saver.translate`, this sends the transcript *text* to the
assistant and nothing else, and only after the user has agreed to that.

* :mod:`.prompts` - what is asked for, and the JSON shape of the answer
* :mod:`.generator` - asking, checking, handing each part back to be saved
* :mod:`lecture_saver.core.study` - the data, its validation and storage
* :mod:`lecture_saver.export.study_pdf` - the PDF
"""
