"""Study material: a preview to read before the lecture, and a review after it.

Two PDFs, made one at a time. The assistant writes the preview (예습) and the
review's quiz (복습) from the transcript and the lecture's PDF; the notes in the
review are the ones Lecture Saver already makes. The saved preview can also be
gone through as a lecture, read aloud, which asks the assistant for nothing.
Like :mod:`lecture_saver.translate`, this sends the words, and pictures of the
PDF's pages if the box for them is ticked, and only after the user has agreed.

* :mod:`.prompts` - what is asked for, and the shape of the answer
* ``preview_prompt.md`` - the preview's instructions, word for word
* :mod:`.generator` - asking, checking, handing the part back to be saved
* :mod:`.lesson` - the preview as steps, and what a voice says
* :mod:`lecture_saver.core.study` - the data, its validation and storage
* :mod:`lecture_saver.export.study_pdf` - the PDFs
"""
