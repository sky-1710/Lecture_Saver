"""Exports: slide + transcript PDF, and the transcript as TXT, Markdown, SRT,
VTT and JSON (§19-§23).

Every export reads the one transcript through :mod:`.document`, which groups it
by slide with the same alignment the application shows, and uses
``effective_text`` - so exports always carry the user's corrections (§14).
"""
