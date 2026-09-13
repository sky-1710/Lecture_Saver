"""Local-only logging.

Detailed technical output goes to a rotating file so problems are debuggable,
while the UI shows short, understandable messages (§27). No handler in this
application sends anything over a network (§29).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import log_file

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    if root.handlers:  # already configured
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    try:
        handler: logging.Handler = RotatingFileHandler(
            log_file(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(console)
