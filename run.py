"""Launcher.

    python run.py                        start the application
    python run.py --selftest             check the environment, no window
    python run.py --verbose              debug-level logging
    python run.py --diagnose <project>   explain every slide-boundary decision,
                                         replayed from the cached frame scan
        [--at MM:SS]                     ...only around one moment
        [--all]                          ...every candidate, not just the first 25
"""

from __future__ import annotations

import sys

from lecture_saver.app import run

if __name__ == "__main__":
    sys.exit(run())
