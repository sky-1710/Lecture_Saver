"""Study material: a preview to read before the lecture, and a review after it.

Two PDFs, made one at a time. The assistant writes the preview (예습) from the
transcript and the lecture's PDF, and the review's quiz (복습) from the
transcript; the notes in the review are the ones Lecture Saver already makes.
Like :mod:`lecture_saver.translate`, this sends *text* to the assistant and
nothing else, and only after the user has agreed to that.

* :mod:`.prompts` - what is asked for, and the shape of the answer
* ``preview_prompt.md`` - the preview's instructions, word for word
* :mod:`.generator` - asking, checking, handing the part back to be saved
* :mod:`lecture_saver.core.study` - the data, its validation and storage
* :mod:`lecture_saver.export.study_pdf` - the PDFs
"""
