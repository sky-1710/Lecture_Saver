# Lecture Saver

Turn a lecture recording into a transcript and, eventually, slide-linked notes —
**entirely on your own computer.**

Video, audio, slide images and transcripts never leave this machine. There is no
cloud transcription service, no analytics, no telemetry, and no local server
listening on a port. The only time an internet connection is needed at all is
the first download of a Whisper model; after that the application works offline.

**Current status: Phases 1–5 are implemented and working.** PDF and transcript
export are not built yet — see
[What works today](#what-works-today) for the exact line.

---

## Install

Requires **Python 3.10 or newer** (developed and tested on 3.11).
FFmpeg does *not* need to be installed separately — it ships inside one of the
Python packages.

```powershell
cd "C:\Users\Owner\Lecture Saver"

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
```

Then check the environment before launching anything:

```powershell
python run.py --selftest
```

Expected output on this machine:

```
Python           : 3.11.9
FFmpeg           : ...\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe
ffprobe          : not found (banner parsing is used)
Qt               : 6.9.3
Media backends   : ffmpegmediaplugin, windowsmediaplugin
FFmpeg backend   : yes
faster-whisper   : 1.2.1
CUDA devices     : 0 (CPU-only)
Free disk        : 13.1 GB

Self-test: PASSED
```

## Run

```powershell
python run.py
```

Other options:

| Command | What it does |
|---|---|
| `python run.py --selftest` | Check FFmpeg, Qt, the media backend and disk space, without opening a window |
| `python run.py --verbose` | Debug-level logging |
| `python run.py --diagnose <project>` | Explain every slide-boundary decision, from the cache |
| `python -m lecture_saver` | Same as `run.py` |

The log file lives at `%APPDATA%\LectureSaver\lecture_saver.log`
(**Help → Open log file** in the app).

---

## Using it

1. **Import a video** — drag an MP4/MOV/MKV onto the start screen, or use
   *Choose a video…*. The file is referenced where it is; it is never copied.
2. **Name the lecture.** A project folder is created under
   `Documents\Lecture Saver\Projects\`.
3. **Transcribe.** Pick a model and language on the right, then
   *Transcribe lecture*. Progress, the current stage and a **Cancel** button are
   shown throughout; the window stays responsive because the work runs on a
   background thread.
4. **Detect slides.** Press *Detect slides* in the left pane. The video is
   scanned at about 100x realtime, each detected slide is saved as a JPEG, and
   the navigator fills with thumbnails. Correct any mistakes by right-clicking
   a slide, or with *Add slide here*.
5. **Navigate.** Click any transcript segment or slide thumbnail to jump the
   video there. As the video plays, the matching transcript segment is
   highlighted and scrolled into view.
6. **Correct the transcript.** Double-click a line to fix it; right-click for
   merge, add, delete and restore original; Ctrl+F to find and replace a
   repeated mistake. Edits save themselves.

Reopening a project restores everything.

### Choosing a model

This machine has **no CUDA GPU**, so transcription runs on the CPU. That makes
the model choice the single biggest decision:

| Model | Download | Speed | Korean quality | 1-hour lecture takes |
|---|---|---|---|---|
| `tiny` | 75 MB | ~4x realtime | poor | ~15 min |
| `base` | 145 MB | ~2.5x | weak | ~25 min |
| **`small`** (default) | 480 MB | ~1.2x | usable | ~50 min |
| `medium` | 1.5 GB | ~0.45x | good | ~2 h |
| `large-v3` | 3.1 GB | ~0.2x | best | ~5 h |

The app shows an estimate before you start, and you can cancel at any time —
segments already transcribed are kept.

For **mixed Korean/English lectures, set Language to Korean** rather than leaving
it on automatic. Auto-detect samples only the first 30 seconds and then commits
to one language for the whole recording.

---

## What works today

### Phase 1 — Foundation ✅

- Project create / open / close, with recent projects
- Drag-and-drop and file-dialog video import (MP4, MOV, MKV, AVI, WebM, and
  other FFmpeg-readable formats)
- Video metadata probing via FFmpeg
- Video playback with seek, scrub, ±10 s, and 0.75x–2x speed
- Projects on disk as plain JSON; crash-safe atomic writes
- Missing-source-video detection with a *Locate video…* recovery path

### Phase 2 — Local transcription ✅

- Audio extraction to 16 kHz mono WAV with real progress
- Local transcription with **faster-whisper** (CTranslate2), CPU int8
- Korean, English, Japanese, Chinese, or auto-detect
- Segment-level timestamps
- Transcript list with click-to-seek and follow-playback highlighting
- Runs off the UI thread, cancellable, and partial results are flushed to disk
  every 25 segments so a crash or cancel never costs the whole run
- Completed audio extraction is reused on a re-run
- GPU→CPU and precision fallbacks, reported rather than hidden

### Phase 3 — Slide detection ✅

- Efficient sampling: one FFmpeg pass at 1 fps, ~100x realtime
- Slide changes decided from the **shape** of the change, not its size — see
  [Slide detection](#slide-detection) for why that distinction is the whole
  problem
- Ignores mouse cursors, laser pointers, highlighters, webcam overlays, build
  animations, crossfades, **and handwritten annotation including opaque pen
  strokes drawn over existing text**
- Representative image captured after the transition settles, and after the
  last step of a build
- **Slides with a lot of handwriting are kept twice**: *Before notes* (the
  clean slide, until the first mark) and *With notes* (the fullest board). When
  the professor erases and carries on, the board just before erasing is marked
  *Before erasing* and the clean slide is not saved again. Switch off with
  `annotation_snapshots.enabled` in `project.json`
- Slide thumbnails, numbers and timestamps in the left pane; click to seek
- **Manual correction**: delete, merge, re-capture, or add a boundary by hand
- **`--diagnose`** reports every candidate boundary with each gate, its value
  and its limit, replayed from the cache in about a second
- Runs off the UI thread with staged progress and cancellation
- Results saved to `slides.json`; reopening never re-detects

### Phase 4 — Transcript/slide alignment ✅

- Every transcript segment is linked to the slide on screen while it was said;
  a sentence crossing a slide change goes to the slide it overlaps most (an
  exact tie stays with the earlier slide)
- Links are recomputed from the times whenever the transcript or the slides
  change — after transcription, detection, every manual slide correction, and
  on opening — so they can never point at a slide that no longer covers the
  speech. The result is stored as `slide_id` on each segment
- **Current slide** tab: the slide's picture, number, *Before notes / With
  notes* label, time range, and only the speech from that slide
- **Full transcript** tab: the whole lecture, as before. Both tabs show the same
  single transcript
- Synchronisation: playback crossing a slide change selects that slide in the
  navigator and switches the transcript to it; clicking a slide shows it and
  seeks there; clicking a segment seeks there. Untick *Follow playback* (under
  the video) to keep reading one slide while the video plays on

### Phase 5 — Transcript editing ✅

- **Inline editing** in both the current-slide and full-transcript tabs:
  double-click a line or press F2, type, and press Enter. Escape puts the line
  back as it was. What you type goes into the one transcript immediately, so
  the other tab - and later every export - shows it too
- **Whisper's output is never overwritten.** Corrections go to `edited_text`;
  edited lines are marked *edited*, and hovering shows the original. Typing the
  original back clears the edit
- Right-click a line: **restore original transcription**, **merge with next
  segment**, **add segment after** (opens straight away for typing), **delete
  segment**. Ctrl+Enter in the editor (or its right-click menu) **splits** the
  segment at the cursor; time is divided in proportion to the text
- Merge and split keep the originals too: a merged line's original is both
  originals joined, and split divides the original at the matching word
- **Undo / Redo** (Ctrl+Z, Ctrl+Y) for every operation, including replace-all.
  Typing a sentence is one undo step, not one per character
- **Find & replace** (Ctrl+F / Ctrl+H): match count, step through matches -
  switching to the slide a match is on - replace one or replace all as a single
  undoable step. Search and replacement text are literal, never patterns
- **Autosave**: written 0.75 s after the last change, atomically, and flushed
  immediately when closing the project or the application, switching project,
  or starting a new transcription. The status next to the edit buttons says
  when everything is saved
- **Reset to original transcription** (edit bar or Edit menu) discards every
  correction and restores the transcript exactly as it was transcribed,
  including merged, split, added and deleted lines. It asks first and Undo
  brings the corrections back. The transcription run keeps Whisper's output
  in `transcript_original.json` for this; a lecture transcribed before that
  file existed can only have its text edits reset
- Structural edits re-link the transcript to slides at once
- While you type, playback never switches the slide or scrolls the list away

### Not built yet

These are **visibly marked as unavailable in the UI** rather than being stubs
that look like they work:

| Phase | Feature |
|---|---|
| 6 | Slide + transcript PDF, TXT / Markdown / SRT / VTT / JSON export |
| 7 | Duplicate slide detection (recognising a returned-to slide), search |

The data model already carries `edited_text` and `slide_id` on every segment, and
`slides.json` already separates a *slide* from its *appearances* and stores a
perceptual fingerprint per slide, so Phase 7 can recognise a returned-to slide
without a migration. Nothing compares fingerprints yet.

---

## Project folder layout

```
Lecture 01/
  project.json                  video reference, metadata, stage states, settings
  transcript.json               the authoritative transcript
  transcript_original.json      Whisper's output as transcribed, kept for
                                "Reset to original"; never touched by editing
  slides.json                   detected slides and their appearance intervals
  audio/lecture.wav             16 kHz mono, regenerable — safe to delete
  slides/001.jpg …              representative image per slide
  cache/frame_descriptors.npz   scanned frames (thumbnails + ink masks),
                                so re-detection skips decoding
  exports/                      Phase 6
  logs/
```

Everything under `audio/` and `cache/` is regenerable and safe to delete; it
only costs a re-scan.

A transcript segment:

```json
{
  "id": "segment_001",
  "start": 12.34,
  "end": 16.21,
  "original_text": "여기서 러닝레이트를 보시면",
  "edited_text": null,
  "slide_id": null
}
```

`original_text` is written once by transcription and never modified by editing
text. Your corrections go to `edited_text`, and everything displayed or exported
uses `edited_text` when present and `original_text` otherwise — so the original
Whisper output is always recoverable with *Restore original transcription*.
`slide_id` is filled in by alignment and recomputed whenever the transcript or
the slides change.

---

## Slide detection

The video is scanned once at 1 fps. Each frame is kept twice: as an 80x45
grayscale thumbnail (how much changed) and as a 320x180 one-bit **ink mask**
(what shape the change is). Together about 10.5 KB a frame, roughly 37 MB per
hour of lecture.

### Why shape, not size

Measured on rendered lectures:

| | share of ink changed |
|---|---|
| A professor's handwriting on one slide | 1 - 6 % |
| A real slide whose body differs by one text block | 0.16 % |

**The annotation is the larger event.** Any threshold big enough to catch the
real change also catches the handwriting, which is why moving thresholds up or
down only trades one failure for the other. What separates them is shape:

* A pen stroke is several pixels thick and survives an erosion - measured
  0.17-0.53 of the changed ink.
* Edited text is a one-pixel glyph edge and never does - measured 0.000-0.008
  across every genuine slide change tested.

### The decision

A slide boundary is committed only when all of these hold:

| Gate | Rejects |
|---|---|
| `min_ink_change`, `min_ink_cell_fraction` | noise |
| `min_ink_cols` >= 2 | mouse cursors, laser dots |
| `min_content_replaced` - the previous content actually went away | annotation, builds, highlighter |
| `max_stroke_thickness` - the marks are not pen-shaped | handwriting, opaque strokes over text |
| `stable_samples` | crossfades, wipes |
| `min_slide_seconds` | rapid flicking back and forth |

Content replacement is the *positive* evidence; thickness only explains a
rejection. That ordering matters: moving on from an annotated slide destroys
the marks as well as the text, and a thickness-first rule would read that as
more annotation and miss the transition.

Busy regions - a webcam overlay, an embedded video - are measured across the
whole lecture and excluded from both comparisons.

### When it gets it wrong

No detector is right for every lecture style, so the navigator lets you fix it:

* **Right-click a slide** to delete it, merge it into the previous one, or
  re-capture its image from wherever the video is now.
* **"Add slide here"** creates a boundary at the current position, for a real
  change that was not detected.

Corrections are saved immediately and survive reopening. Re-running detection
replaces them, and warns you first.

### Working out why

```powershell
python run.py --diagnose "path\to\project"
python run.py --diagnose "path\to\project" --at 12:34
```

This replays detection over the **cached** frame scan - about a second, with no
re-decoding - and prints every candidate boundary with each gate, its value and
its limit:

```
Candidate at 00:00:15
  ink change                0.0075  PASS  >= 0.0008
  ink cells                 0.0312  PASS  >= 0.0040
  ink columns              12.0000  PASS  >= 2.0000
  replacement support     235.0000  PASS  >= 60.0000
  content replaced          0.0000  FAIL  >= 0.1500
  stroke thickness          0.1820  FAIL  < 0.0500
  -> same slide (annotation (thick strokes))
```

It also summarises which gate rejected the most candidates - the one to adjust
first. Every gate is a field under `slide_detection` in `project.json`.

### Re-running with different settings

Changing a threshold and detecting again replays the cached frames instead of
decoding the video: about a second rather than minutes. Changing `sample_fps`,
the thumbnail size or the ink resolution invalidates the cache and forces a
fresh scan.

### Known limitations

* **Two slides differing only in a number or a short equation** are not
  detected. Once scaled down for analysis, `0.001` and `0.010` produce
  identical ink. Raising the analysis resolution was tested and did not recover
  it while making other cases worse. Use *Add slide here*.
* **A slide that only adds text** to the previous one is structurally identical
  to a build animation and is kept as one slide. That is usually what you want
  - the captured image shows the finished content - but a genuinely new slide
  of that shape will be merged. Use *Add slide here*.
* Slides differing only inside a region masked as busy (behind a webcam
  overlay) cannot be seen.
* **A build animation that adds a lot of text** looks like handwriting to the
  *Before notes / With notes* split, so such a slide can be saved as its first
  step and its finished state.
* **Erasing only part of the board** is not recognised as erasing; the saved
  image is still the fullest board.

---

## Development

```powershell
pip install -r requirements-dev.txt

pytest                # 315 tests, ~30 s, no lecture video or model needed
pytest -m scenario    # + renders real lecture clips and detects slides in them
pytest -m model       # + real transcription (downloads the tiny model)
ruff check .
mypy lecture_saver
```

The UI tests run headless on Qt's `offscreen` platform, so the whole suite works
over SSH or in CI.

Architecture, the algorithms planned for Phases 3–7, and the reasoning behind the
stack are in [ARCHITECTURE.md](ARCHITECTURE.md).

### Layout

```
lecture_saver/
  core/            pure logic — models, project storage, transcript state (no Qt)
  media/           FFmpeg discovery, probing, audio extraction
  transcription/   faster-whisper engine and the processing pipeline
  ui/              all Qt lives here
```

`core/` has no Qt or FFmpeg imports, which is what makes the important rules
testable without a GUI or a lecture recording.

---

## Privacy

- No cloud speech or vision APIs — transcription is `faster-whisper` running
  locally on your CPU
- No telemetry, analytics, crash reporting or remote logging
- No local web server and no listening socket
- Project data stays in your project folders; settings stay in
  `%APPDATA%\LectureSaver`
- Network access happens exactly once per model: the Hugging Face download of the
  Whisper weights. Pre-download a model, and the application never needs the
  internet again.
