"""Slide change detection.

Three separable passes (see :mod:`lecture_saver.slides.pipeline`):

* :mod:`~lecture_saver.slides.descriptors` decodes the video once and reduces
  every sampled frame to a compact descriptor.
* :mod:`~lecture_saver.slides.detector` turns a descriptor sequence into slide
  appearances. It is a pure function, so thresholds can be re-tuned and
  re-applied without touching the video.
* :mod:`~lecture_saver.slides.pipeline` orchestrates the two and captures the
  representative images.

The detector is deliberately reachable through one narrow entry point so the
scoring algorithm can be replaced later without disturbing the rest of the
application (§7).
"""
