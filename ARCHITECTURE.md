# Lecture Saver — Architecture & Implementation Plan

Fully local lecture video → transcript → slide-linked notes.
**No audio, video, image, transcript or metadata ever leaves this machine.**

---

## 0. Environment this plan was written against

| Item | Value | Consequence |
|---|---|---|
| OS | Windows 10 Home 19045 | `ffmpeg.exe`, `\` paths, Media Foundation quirks |
| Python | 3.11.9 (also 3.13.7) | Project pins 3.11 in `.venv` — best wheel coverage for CTranslate2 |
| CPU | i7-1165G7, 4C/8T | Transcription is CPU-bound; int8 quantisation is essential |
| GPU | Intel Iris Xe (**no CUDA**) | `device="cpu"`. GPU path exists but will not be used here |
| RAM | 15.7 GB | `large-v3` int8 fits (~2 GB) but is slow; `small` is the default |
| Free disk | ~13.7 GB | Model + venv + extracted WAV budget must stay modest |
| FFmpeg | **not installed** | Provided by the `imageio-ffmpeg` wheel |
| Fonts | `malgun.ttf` present | Korean PDF rendering (Phase 6) is solved |

---

## 1. Technology stack (recommendation)

**Python 3.11 + PySide6 (Qt Widgets) + QtMultimedia + faster-whisper + FFmpeg.**

### Why

* **One runtime.** The heavy lifting (Whisper, OpenCV, ReportLab) is Python no matter
  what. Electron/Tauri would add Node or Rust *on top of* Python plus an IPC layer —
  more moving parts, more failure modes, worse against priority #1 (reliability)
  and #3 (easy install).
* **Playback quality without codec packs.** Qt >= 6.5 ships an **FFmpeg-based
  multimedia backend** that is the default on Windows. It is bundled inside the
  PySide6 wheel, so MP4/MOV/MKV/AVI decode without the user installing LAV filters or
  an HEVC extension. An HTML5 `<video>` UI could not do this — browsers refuse MKV and
  most MOV variants, which would force us to transcode multi-GB lectures.
* **Real frame-accurate seeking** and a `positionChanged` signal at fine granularity,
  which is exactly what transcript-to-video synchronisation needs.
* **No local web server.** Nothing to bind, nothing to firewall, nothing to
  accidentally expose (§29). Privacy by construction rather than by configuration.
* **Transcript editing UX.** `QListView` + a custom delegate gives a *virtualised*
  list — a 3-hour lecture is ~2,500 segments, and creating 2,500 real widgets (the
  naive approach, and what a DOM-based UI does by default) is what makes these apps
  crawl.
* **Maintainability.** Plain dataclasses + pure functions for all logic; Qt confined
  to `lecture_saver/ui/`. Every rule in §33 is testable without a running GUI.

### Rejected

| Option | Why not |
|---|---|
| Electron + Python sidecar | Two runtimes, IPC, ~200 MB baseline, packaging pain |
| Tauri | Adds a Rust toolchain; still needs the Python sidecar |
| Local FastAPI + browser UI | Browser cannot play MKV/MOV; server surface we do not need |
| `openai-whisper` | ~4x slower and ~2x the RAM of faster-whisper on CPU |
| `python-mpv` for playback | Best-in-class playback, but needs a hand-placed `libmpv-2.dll` |

---

## 2. Folder structure

    Lecture Saver/
      run.py                     launcher  (python run.py)
      requirements.txt           runtime deps
      requirements-dev.txt       + pytest / ruff / mypy
      pyproject.toml             ruff, mypy, pytest config
      README.md
      ARCHITECTURE.md            this file
      lecture_saver/
        app.py                   QApplication bootstrap
        config.py                app dirs, user settings (local JSON only)
        logging_setup.py         rotating file log, local only
        core/                    pure logic, zero Qt imports
          atomic_io.py           crash-safe JSON writes
          timecode.py            seconds <-> HH:MM:SS(.mmm) / SRT / VTT
          models.py              dataclasses + serialisation + schema version
          transcript_store.py    THE single source of truth (§14)
          slides.py              slide / appearance models + detection options
          alignment.py           which speech belongs to which slide (§10)
          captures.py            screenshots + their marks; undo for a sheet
          sentences.py           where a line may be cut so sentences survive
          keywords.py            slide text -> course terms for Whisper's hints
          corrections.py         suggested typo fixes, checked against the lines
          reference.py           the lecture PDF's text; pages for one batch
          chat.py                the Ask AI conversation (chat.json)
          project.py             project dir layout, create/load/save
        media/
          errors.py
          ffmpeg_tools.py        locate / probe / extract audio (with progress)
        transcription/
          whisper_engine.py      faster-whisper wrapper + device fallback
          openvino_engine.py     Turbo on the Intel GPU (OpenVINO), window by window
          chunking.py            VAD speech regions -> 30 s windows, time mapping
          segmentation.py        Whisper words -> lines that end at sentences
          pipeline.py            extract -> transcribe -> persist, resumable
        slides/
          slide_text.py          slide image -> text lines (Windows OCR)
          pdf_text.py            lecture PDF -> text and pictures of its pages (QtPdf)
          descriptors.py         Pass A: decode once, cache frame descriptors
          ink_analysis.py        shape of a change: thickness, replacement
          detector.py            Pass B: pure scoring + state machine
          diagnostics.py         per-candidate gate results
          diagnose.py            --diagnose CLI, replayed from the cache
          pipeline.py            Pass C: capture images, persist, resumable
        study/                   the preview and the review's quiz (sends text too)
          preview_prompt.md      the preview's instructions, word for word
          prompts.py             the two requests; the quiz's JSON schema
          generator.py           ask, validate, shuffle answers, hand back
        ask/                     Ask AI: questions about the lecture (sends text too)
          __init__.py            a question as a turn of the lecture's session
          prompt.py              which transcript goes, once per session; follow-ups
          answer.py              cited times/slides -> links; failure wording
        translate/               the ONE part that can send text off-machine
          engine.py              what an engine is; the errors it may raise
          codex_cli.py           Codex CLI via ChatGPT sign-in (subprocess)
          batching.py            lecture -> batches that break on slides
          runner.py              drive an engine, hand back each answer
        ui/
          theme.py               colours, stylesheet, roles - the whole look
          main_window.py         window, menus, view stack
          start_view.py          new / open project, drag & drop
          lecture_view.py        player + transcript, wired together
          video_player.py        QMediaPlayer widget + transport controls
          video_canvas.py        frames painted with marks over them, threaded
          ink.py                 pen / underline / highlight / eraser / memo
          capture_panel.py       screenshots beside the video: re-mark, replay
          transcript_view.py     virtualised segment list, "Ask AI" on each line
          ask_panel.py           Ask AI chat under the video
          corrections_panel.py   the Typos tab: fix, keep or listen to each word
          reference_row.py       attach the lecture's PDF, for two dialogs
          transcribe_panel.py    model options, progress, cancel
          slide_panel.py         slide navigator: thumbnails + detect controls
          workers.py             QThread wrappers around core pipelines
      tests/

**Phase 4+ modules land here without touching anything above:**
`core/alignment.py`, `slides/similarity.py` (Phase 7 duplicate matching),
`export/pdf.py`, `export/text.py`, `export/deck_matcher.py`.

### Project on disk (§5)

    lecture_01/
      project.json        video ref, metadata, stage states, settings
      transcript.json     segments — the authoritative transcript
      slides.json         slide identities + appearance intervals
      corrections.json    typos an assistant suggested; the transcript is unaffected
      reference.json      the text of the lecture's PDF, if one was attached
      reference_pages/    a picture of each of its pages, up to 60
      study.json          the preview (Markdown) and the quiz, once made
      audio/lecture.wav   16 kHz mono, deletable, regenerable
      slides/001.jpg      representative image per slide
      captures.json       the user's screenshots: time, marking start, marks
      captures/0001.jpg   a screenshot as the video showed it; _marked.jpg with marks
      cache/frame_descriptors.npz   scanned frames; deletable, costs a re-scan
      exports/            Phase 6
      logs/project.log

The source video is **referenced by path, never copied** (§4). Copying is an explicit
opt-in flag on the project record.

---

## 3. Core data models

`project.json`

```jsonc
{
  "schema_version": 1,
  "project_id": "9f2c...",
  "name": "lecture_01",
  "created_at": "2026-09-03T10:00:00+09:00",
  "updated_at": "...",
  "video": {
    "path": "D:/lectures/lecture01.mp4",   // absolute, original location
    "copied_into_project": false,
    "duration": 3612.4, "width": 1920, "height": 1080, "fps": 29.97,
    "size_bytes": 1234567890,
    "container": "mov,mp4,m4a", "video_codec": "h264", "audio_codec": "aac",
    "has_audio": true
  },
  "stages": {                               // §27 processing state, resumable (§26)
    "audio":         {"status": "done", "updated_at": "...", "message": null},
    "transcription": {"status": "done", "updated_at": "...", "message": null},
    "slides":        {"status": "done", "updated_at": "...", "message": null},
    "alignment":     {"status": "not_started"}    // Phase 4
  },
  "transcription": {
    "model_size": "small", "device": "cpu", "compute_type": "int8",
    "language": "auto", "detected_language": "ko", "vad_filter": true
  },
  "slide_detection": { /* every threshold, so a re-run replays them (§10) */ },
  "export_settings": {}                     // Phase 6
}
```

`transcript.json` — **the single source of truth (§14)**

```jsonc
{
  "schema_version": 1,
  "segments": [
    {"id": "segment_001", "start": 12.34, "end": 16.21,
     "original_text": "여기서 러닝레이트를 보시면",
     "edited_text": null,          // null until the user edits (§6)
     "slide_id": null}             // filled in Phase 4
  ]
}
```

`effective_text = edited_text if edited_text is not None else original_text`.
This is a property on the dataclass, so **no view or exporter can compute it
differently.** `original_text` is never written after creation.

`slides.json` separates the two concepts §9 asks for:

```jsonc
{"schema_version": 1,
 "slides": [{"slide_id": 6, "image": "slides/006.jpg", "fingerprint": "00d2..."}],
 "appearances": [{"appearance_id": 12, "slide_id": 6, "start": 751.0, "end": 845.2}]}
```

A returned-to slide adds an **appearance**, not a new slide. Phase 3 assigns one
slide per appearance; the `fingerprint` (64-bit dHash of the representative
frame) is recorded now so Phase 7 can match repeats without re-decoding the
video. Nothing compares fingerprints yet (§12).

---

## 4. Local transcription pipeline

    video --ffmpeg--> 16 kHz mono PCM WAV --faster-whisper--> segments --> transcript.json
          (progress via -progress pipe:1)   (streaming generator)  (flushed every 25 segs)

1. **Locate FFmpeg** — system `ffmpeg` on PATH first, else the static binary from
   `imageio-ffmpeg`. Missing → a typed `FFmpegNotFoundError` with a readable message.
2. **Probe** with `ffprobe` (or `ffmpeg -i` output parsing when only the imageio binary
   exists — it ships no ffprobe) → duration/codecs/fps/audio presence.
3. **Extract audio**: `-vn -ac 1 -ar 16000 -c:a pcm_s16le`. 16 kHz mono is exactly
   Whisper's input rate, so no resampling happens later. ~115 MB/hour — disk is
   checked first, and the WAV is deletable from the UI.
4. **Transcribe** with `WhisperModel(size, device="cpu", compute_type="int8")`.
   `transcribe()` returns a *generator*, so segments stream out and progress is
   `segment.end / duration` — real progress, not a spinner.
5. **Persist incrementally.** A crash or cancel at 80 % leaves 80 % on disk (§26).
6. **Device fallback** (§28): CUDA → CPU on any CTranslate2 init failure, and
   `float16 → int8` on OOM, each logged and surfaced as a friendly notice.

**The Turbo model runs on OpenVINO instead** (`openvino_engine.py`). CTranslate2
can only use CUDA GPUs, so on a laptop with Intel integrated graphics the GPU
sat idle. OpenVINO runs Whisper large-v3-turbo (Intel's int8 build) on that GPU:
measured on an i7-1165G7 / Iris Xe, 5 minutes of Korean lecture took 71-75 s
against 205 s for faster-whisper `medium` on the CPU, back to back, and a whole
58-minute lecture took 13 minutes against `medium`'s 40. The differences from
the faster-whisper path:

* **Windows instead of a stream.** Silero VAD (faster-whisper's copy) finds the
  speech; `chunking.py` packs it, silence removed, into windows of at most 30 s
  and maps window times back onto the lecture. One `generate()` per window gives
  progress and cancellation between windows. Whisper handed only silence invents
  text ("리리리..."), so silence is never sent, and lines that compress better
  than 2.4 (faster-whisper's own loop test) are dropped.
* **Line breaks.** OpenVINO's word times run edge to edge, so the segmenter's
  silence rule never fires. The last word of each of Whisper's own lines is
  marked `ends_chunk` and ends a line instead; words are matched to Whisper's
  lines by letter count, because the word times can lag by a word.
* **Language** is detected on the first window and then held.
* **Fallbacks:** GPU → OpenVINO CPU → faster-whisper (same model, CPU), the last
  only when OpenVINO cannot run at all. A model that cannot be downloaded is an
  error, not a fallback - falling back would only try a bigger download.

**Keywords** (both engines): comma-separated course terms, up to 600 characters,
go to Whisper as `hotwords` - the prompt before *every* window, unlike
`initial_prompt`, which only reaches the first when previous text is not carried
over. OpenVINO refuses a prompt over ~220 tokens, so the terms are halved until
they fit.

**Keywords from the slides.** `slides/slide_text.py` reads each distinct slide
image with Windows' built-in OCR (`Windows.Media.Ocr` through `winrt`; ~0.2 s a
slide, on the background thread that made the engine) and returns lines with
their height. `core/keywords.py` - pure, tested on made-up OCR text - turns
those into terms, RAKE-style: phrases cut at stop words and punctuation, scored
by the slides they appear on, doubled in headings (lines ≥1.5x the median
height), multi-word terms preferred, generic words ("data", "time") only inside a
phrase, text on ≥75 % of slides dropped as header/footer. Consecutive slide
images whose lines mostly match (a slide kept before/with/after the professor's
ink, a build) are versions of one slide: it scores once, plus `log2` of its
versions for the time spent on it - without that, one slide written on in six
images put its sentence fragments at the top - while a phrase read alike on
several versions still counts as read right. A word shorter on one version
than in the same line on another ("Unexpecte externa event") was cut by ink and
is dropped. Phrases ending in a verb ("interrupt occurs", "-ed") or starting a
procedure step ("initialize trap table") are not terms, and a term inside a
longer ranked one gives it its place unless it is on ≥3x the slides. Most of
the module is undoing OCR: a Korean recogniser reads Latin text with its own
confusions ("SpIitting", "tO", "PIOts", "processlng", P(X|Y) as "XIY" or
"TrafficlRain", PCB1 as "PCBI"), pictures as Hangul, and text under a pop-up in
part ("age table", "return-from-tra"); Hangul words one syllable off a commoner
word (샴표/상표) are taken as misreadings. The terms go into the Keywords box,
after what was typed, for the user to check.

**Language:** `auto` (Whisper's own detection), or forced `ko`/`en`. For mixed
Korean/English lectures, forcing `ko` gives markedly better results than `auto`,
because auto-detect samples only the first 30 s and then commits.

**Model choice on this machine.** No CUDA, so int8 CPU. Rough throughput on a
4-core i7-1165G7 (xrealtime, higher is better):

| model | disk | RAM | speed | quality (KO) |
|---|---|---|---|---|
| `base` | 145 MB | ~0.6 GB | ~2.5x | weak |
| **`small`** (default) | 480 MB | ~1.1 GB | **~1.2x** | usable |
| `medium` | 1.5 GB | ~2.6 GB | ~0.45x | good |
| `large-v3` | 3.1 GB | ~4.0 GB | ~0.2x | best |

A 1-hour lecture is roughly 50 min on `small` and several hours on `large-v3`. The UI
states an estimate before starting, and transcription is cancellable.

---

## 5. Slide detection algorithm (Phase 3 — implemented)

Three passes, so re-tuning never re-decodes the video:

    video --ffmpeg--> 320x180 grey frames at 1 fps --> descriptors (cached)
    descriptors --pure function--> slide appearances       (re-runnable in ~1 s)
    appearances --targeted ffmpeg seeks--> slides/NNN.jpg

Each sampled frame is stored twice: an 80x45 grayscale thumbnail and a 320x180
one-bit **ink mask**. About 10.5 KB a frame, ~37 MB per hour at 1 fps.

### The central finding

Magnitude cannot distinguish annotation from a slide change. Measured on
rendered lectures, handwriting on one slide changes **1-6 %** of ink pixels
while a genuine slide whose body differs by one text block changes **0.16 %**.
The annotation is the larger event, so no threshold separates them - moving one
up or down only trades a duplicate for a missed slide.

What separates them is the *shape* of the changed region:

* A pen stroke is several pixels thick and survives an erosion (0.17-0.53 of
  the changed ink).
* Edited text is a one-pixel glyph edge and never does (0.000-0.008 across
  every genuine change tested).

That is why the descriptor carries an ink mask at twice the thumbnail
resolution: thickness is not measurable once text has been averaged away.

### Committing a change

| Gate | Purpose |
|---|---|
| `min_ink_change`, `min_ink_cell_fraction` | noise floor |
| `min_ink_cols` >= 2 | rejects cursors and laser dots |
| `min_content_replaced` (+ `min_replacement_support`) | the previous content actually went away |
| `max_stroke_thickness` | the marks are not pen-shaped |
| `stable_samples` | rejects crossfades and wipes |
| `min_slide_seconds` | debounce |

Content replacement is the **positive** evidence for a change; thickness only
ever explains a rejection. The ordering is load-bearing: leaving an annotated
slide destroys the marks along with the text, and a thickness-first rule would
read that as more annotation and miss the transition.

Rows are deliberately **not** gated. Gating them is what made a slide differing
by a single line of text disappear.

Busy regions - webcam overlays, embedded video - are measured across the whole
lecture (a slide cell changes in ~2 % of intervals, a webcam cell in ~90 %) and
excluded from **both** the thumbnail and the ink comparison.

### Diagnostics

Every candidate boundary records each gate, its value, its limit and whether it
passed, plus the outcome (committed / absorbed / rejected) and the reason.
`python run.py --diagnose <project>` replays this from the cache, so a real
lecture can be tuned in seconds instead of minutes. One bug in the gate
ordering was found purely by reading its output.

### Manual correction

`core/slides.py` provides `delete_appearance`, `merge_with_previous`,
`split_at` and `set_slide_image`. All of them keep the timeline contiguous,
because Phase 4 assigns speech by overlap and must never find a gap. Edits set
`manually_edited`, which the UI uses to warn before a re-run discards them.

### Measured behaviour

| Scenario | Expected | Result |
|---|---|---|
| A: progressive handwriting | 1 slide | 1 |
| B: opaque strokes covering text | 1 slide | 1 |
| C: same template, one text block differs | 2 slides | 2 |
| D: same template, only a number differs | 2 slides | **1** |
| E: cursor + handwriting + webcam, then a change | 2 slides | 2 |
| F: rapid genuine navigation, 6 slides | 6 slides | **4** |
| 10-slide deck with build + crossfade | 10 slides | 10 |

D and F are limitations rather than tuning problems. In D the two frames are
byte-identical as ink masks once scaled for analysis; raising the resolution was
tested and did not recover it while breaking E. In F, a slide that only *adds*
text is structurally identical to a build animation. Both are what the manual
"Add slide here" control exists for.

## 6. Transcript-to-slide alignment (Phase 4)

Pure functions, `core/alignment.py`, no I/O:

```python
def snapped_starts(segments, appearances, *, window) -> list[float]  # moved boundaries
def assign_segments(segments, appearances, *, snap) -> list[int | None]
def align(segments, appearances) -> Alignment                    # both directions
```

Each boundary is first moved to the nearest **break in the speech** within
`SNAP_WINDOW` (6 s): the end of a sentence beats a pause, and a boundary never moves
more than half way towards either neighbour, so a short appearance cannot be emptied
and the order is preserved. A slide changes when the lecturer clicks or starts
writing - almost never when they finish a sentence - and on a deck shown twice,
before and after annotation, that lands mid-sentence at nearly every change.

Each segment is then assigned to the appearance with the **greatest temporal overlap**
of those snapped intervals; ties break toward the earlier appearance; zero-overlap
segments (past the last slide, or in a hand-made gap) attach to the appearance covering
or preceding their start, or `None` when there are no slides. Both lists are in start
order, so the walk is linear.

Only the reading moves: slide times themselves are left exactly as detected, so seeking
and the printed time ranges stay true to the video. Where the sentence end is *inside* a
line - Whisper packs several sentences into one segment - `core/sentences.py` finds the
cut and `TranscriptStore.split_at_slide_changes()` applies it as one undoable edit,
automatically after detection on an untouched transcript and from **Edit → Split lines
at slide changes** at any time.

The link is **derived, never authoritative**. `Project.align()` recomputes it from the
times and writes the result as `slide_id` on each segment (only when something
changed). The main window calls it after transcription, after detection, after every
manual slide correction and on opening; the lecture view computes the same `align()`
for display. A slide edit therefore cannot leave speech linked to the wrong slide.

---

## 7. PDF generation strategy (Phase 6 — design only)

**ReportLab Platypus**, locally, no headless browser.

* Register `C:\Windows\Fonts\malgun.ttf` (+ `malgunbd.ttf`) as a TTF family so Korean,
  English and mixed text all render from one font. If Malgun is absent, fall back
  through a bundled Noto Sans KR, and fail loudly rather than emitting tofu boxes.
* Per slide: `KeepTogether([SlideNumber, Image, TimeRange])` so a slide header never
  orphans from its image; the transcript then flows as ordinary `Paragraph`
  flowables, which page-break naturally mid-transcript (§19).
* Image is scaled to the frame width preserving aspect ratio, capped at ~55 % of page
  height so at least a few lines of transcript share the page.
* Options and presets from §20 map to a `PdfExportOptions` dataclass — the same
  options object the export screen renders from.

---

## 8. Local dependencies

| Dependency | Source | Manual step? |
|---|---|---|
| FFmpeg | `imageio-ffmpeg` wheel (system FFmpeg preferred if present) | **no** |
| Qt + media backend | `PySide6-Essentials` wheel | **no** |
| Whisper weights | Hugging Face, one-time ~480 MB for `small`, cached in `~/.cache` | needs internet **once** |
| OpenCV (Phase 3) | `opencv-python` wheel | no |
| ReportLab (Phase 6) | `reportlab` wheel | no |

Install is `pip install -r requirements.txt`. Nothing else.

---

## 9. Platform-specific considerations

* **Paths**: `pathlib` everywhere; project files store absolute POSIX-style strings and
  are re-normalised on load, so a project stays loadable if opened from another shell.
* **Atomic writes**: `os.replace()` is atomic on NTFS, but *only* onto the same volume —
  the temp file is therefore written beside the target, not in `%TEMP%`.
* **Subprocess windows**: every FFmpeg call passes `CREATE_NO_WINDOW`, otherwise a
  console flashes on screen for each invocation.
* **Qt media backend**: forced to `ffmpeg` via `QT_MEDIA_BACKEND` so Windows Media
  Foundation's narrower codec support is never silently used.
* **Long paths / spaces**: the working directory itself is `C:\Users\Owner\Lecture Saver`
  — every subprocess argument is passed as a list, never a shell string.
* **Disk**: `shutil.disk_usage` is checked before audio extraction and before a model
  download; ~13.7 GB free is enough for `small`/`medium` but not for several
  large lectures' WAVs at once.

---

## 10. Biggest technical risks

1. **CPU-only transcription speed.** No CUDA on this machine. Mitigation: int8,
   `small` default, honest time estimate up front, cancellable, resumable, partial
   results persisted. *This is the #1 UX risk of the whole project.*
2. **Disk headroom (13.7 GB).** Mitigation: pre-flight disk checks, deletable WAV,
   `PySide6-Essentials` over full PySide6.
3. **Qt FFmpeg media backend availability.** If a given wheel lacks the plugin,
   playback silently degrades to Media Foundation. Mitigation: a startup probe that
   reports the actual backend, and a clear error rather than a black video frame.
4. **Whisper timestamp drift** on hour-plus audio, which would misalign slides in
   Phase 4. Mitigation: VAD filtering on by default; segment-level (not word-level)
   timestamps; alignment uses overlap, which tolerates ~1 s of drift.
5. **Slide-detection false positives** (Phase 3) — the classic failure of this app
   category. Mitigation: the three-condition commit rule + grid-locality test above.
6. **Korean glyphs in PDF** (Phase 6). Mitigation: explicit TTF registration and a
   render self-test.
7. **UI responsiveness with ~2,500 segments.** Mitigation: virtualised `QListView`
   from the start, not a widget per segment.

---

## 11. How Phases 1 & 2 are tested

**Automated (`pytest`, no GUI, no lecture video required):**

* `test_timecode.py` — HH:MM:SS / SRT `,mmm` / VTT `.mmm` formatting, rounding at
  59.999 s, hours >= 1, negative and zero guards, round-trip parsing.
* `test_models.py` — `effective_text` precedence, that editing never touches
  `original_text`, empty-string edit != `None`, JSON round-trip, schema version.
* `test_transcript_store.py` — single-source-of-truth behaviour: edit → lookup by
  time, restore-original, dirty flagging, `segment_at(t)` boundary conditions.
* `test_project.py` — create/save/load round-trip, atomic write leaves no partial file
  when serialisation raises, missing-video detection, stage-state persistence.
* `test_ffmpeg_tools.py` — locator precedence and the `ffmpeg -i` metadata parser run
  against **recorded FFmpeg output text**, so no binary is needed.
* `test_media_integration.py` — generates a 3-second synthetic video with FFmpeg
  (`testsrc` + `sine`), then probes it and extracts audio for real.
  Auto-skips if FFmpeg is unavailable. Runs in ~2 s; no lecture file needed (§33).

**Manual smoke test, Phase 1:** launch → drag a video onto the start screen → project
created on disk → video plays, seeks, scrubs → close → reopen project → same state.

**Manual smoke test, Phase 2:** on a short clip, choose model + language →
watch the processing stages → transcript appears with timestamps → click a
segment → video seeks there → playback highlights the current segment → reopen the
project and the transcript is still there.

`python run.py --selftest` runs a non-GUI check of FFmpeg discovery, the Qt import, the
media backend and disk space, so setup problems are diagnosable without the UI.


---

## 12. In-app translation (Phase 7+)

### Why this exists at all

§1 says everything is local, and everything is — except this. A Korean
translation of an English lecture is the one job no local model does well on
the target machine: it has an Intel Iris Xe and no CUDA GPU, where a model
large enough to translate a lecture properly runs at a few tokens a second
and would take hours per lecture.

So translation is the deliberate exception, built to be obvious rather than
quiet:

* **Off by default.** `translation_enabled` starts false in settings.
* **Agreed to once, explicitly.** The dialog names what leaves the machine
  and what does not before the first send.
* **Only text.** An engine is handed a string and returns a string. It is
  never given the project, a path or an image — `batching.batch_markdown`
  emits slide headings and paragraphs, nothing else, and a test asserts no
  path, file name or image link can appear in a prompt.
* **Removable.** Delete `lecture_saver/translate/` and everything else still
  builds: importing a translation the user obtained elsewhere lives in
  `core/translations.py` and does not depend on this package.

### The engine seam

`TranslationEngine` is `prompt in -> answer out`, plus an availability check.
Everything above it — batching, prompting, parsing, saving, progress,
cancellation, the dialog — is written against that and does not know which
assistant is running. A second engine (another CLI, or a local model once the
hardware justifies it) is one class plus an entry in `available_engines()`.
`sends_text_externally` on the class is what the warning keys off, so a local
engine would simply not trigger it.

### Codex specifically

Run per batch as `codex exec`, with the prompt on **stdin** (a batch exceeds
the Windows command-line limit), the answer via `--output-last-message` (so it
is not dug out of progress chatter), `--ephemeral` (no session log of the
lecture on disk), `--sandbox read-only`, and `--ignore-user-config`. The last
one matters more than it looks: the user's `config.toml` carries a default
model their installed Codex may be too old to accept, and MCP servers that
are started for every run and fail if they need authorising. Neither belongs
in a translation. Their sign-in is not in that file and still applies.

Ask AI is the one use that keeps a session. A lecture's conversation is one
Codex session: the first question runs with `--json` instead of
`--ephemeral` (the `thread.started` event gives its id, kept in `chat.json`),
and each follow-up is `codex exec resume <id>`, so the model has its own
earlier answers in view - pasting the history into fresh runs made "그럼 이걸
간단히 해줘" answer the slide on screen instead of the last answer. The
transcript goes in pieces (a slide, or a minute without slides) with a
fingerprint each, so a follow-up carries only pieces the session lacks or the
user corrected since. A session that cannot be resumed is replaced by a fresh
run carrying everything plus the recent turns. *New chat* deletes the session
file from `CODEX_HOME/sessions`; so does a first turn that failed or was
stopped.

### Batching, and why a failure is cheap

Batches break on slide boundaries and never inside one, because the slide is
both the unit the answer is addressed by (`## Slide 8`) and the unit a
translation is stored against.

The runner hands each answer back the moment it arrives instead of returning
them together, and the window saves it before the next batch is asked for.
Three things follow: a run that dies part-way keeps everything before the
failure; a usage limit stops the run rather than failing every remaining
batch for the same reason; and a second run skips whatever is already
translated, so re-running *is* both the retry and the resume. Nothing is
retried automatically — a failed batch has already cost quota, and the usual
cause would fail again immediately.

### Prompt sharing

The rules sent to a CLI and the rules written into the hand-carried request
are the same constants in `export/ai_request.py`. They have to be: both
answers are read by the same parser, so a prompt that drifted between the two
paths would produce answers that parse differently. The two differ only where
they must — the hand-carried one asks for a `.md` file and allows a
"continue" reply, the automated one asks for the answer alone and is sized to
fit in one go.

### The typo check

Whisper's errors are mostly real words: "a learning program", "cloud time",
"process date". No dictionary finds those, but the assistant is reading every
line to translate it, so a run can report them for almost nothing extra. Each
is one line under the Korean, `!오탈자 | wrong | right | 확실 or 의심 | why`, read
out of the answer by `parse_translation_text` so it never reaches the stored
translation. The English above it is still copied as it stands, because the
copy is not what a fix is made from.

**A suggestion is not an edit.** The assistant is sometimes wrong, and the
transcript is the user's (§13), so `corrections.json` is separate and nothing
reaches `edited_text` until the user accepts it — through
`TranscriptStore.edit_many`, an ordinary undoable step, leaving `original_text`
alone. All views and exports then see it the usual way (§14).

**Nothing records that a fix was made.** A suggestion is open while its words
are in its line, which is read off the text each time. Undo, *Restore
original*, *Reset*, and typing the word by hand all do the right thing without
touching it, and there is no second copy of "what the line says" to drift. Only
*Keep as it is* is stored, since that leaves the words there; it survives being
asked again. A fresh transcription clears the suggestions, whose segment ids
would otherwise point at unrelated lines.

**Answers are checked, not trusted.** `match_corrections` looks for the words
near the timestamp they were reported under, and drops what it cannot find, so
a suggestion cannot point at something the lecture does not say. Latin words
match whole ("rate" is not in "generate"); Hangul does not, because particles
join the word.

**The PDF.** A slide that says "process state" settles what a transcript cannot.
`slides/pdf_text.py` reads the text with QtPdf, which is PDFium and already in
PySide6-Addons, so there is no new dependency; `reference.json` keeps the text
and the file name, never the path or the file (§4), and `reference_pages/` a
picture of each page for the review (§13). Real decks measured 200–500
characters a page with a text layer on nearly every page, so OCR was left out;
a PDF of pictures says so instead (§32).

Most decks fit under the 8,000-character limit and go whole. Longer documents
would cost more per batch than the batch, so `ReferenceText.pages_for` sends the
pages sharing the most words with that batch's speech, ignoring words on half
the pages or more (titles, footers). The text is marked in the prompt as
material, not instructions, since it comes from a file. It is one more thing
leaving the machine, so the dialog's notice names it whenever it would go, and
only then.

Measured on one real Codex batch over a made-up lecture with six planted
errors: all six found, the right `date` beside a planted one left alone, spoken
filler left alone, and every line parsed.


---

## 13. Study: the preview and the review

Two PDFs, made one at a time, because they are read days apart. The preview
(예습) is a lesson taught from the lecture's PDF and transcript. The review (복습)
is the existing slide + transcript notes, a 15-question English quiz, then
answers with Korean explanations from a fresh page.

**What comes first.** A translation run is what finds the typos, and study
material follows the transcript it is given, so `MainWindow._make_study_pdf`
stops a lecture with no transcript, and tells one with slides untranslated to
translate first. That one can go on anyway: a slide that will not translate must
not lock this away. A lecture in Korean (`TRANSLATED_INTO`) has nothing to do.

**The review sends pictures.** A diagram has no words for the text layer to give,
so the quiz is also shown each page of the attached PDF. The PDF itself is not
kept (§4), so `slides/pdf_text.py` draws the pages when it is attached, 1280 px
on the long side, into `reference_pages/`, for the first 60 pages: a textbook
would take minutes to draw and not fit in a request. These are the one thing
beyond words that leaves the machine, and they are pages the user chose to
attach, never frames of the video. `CodexEngine._run` copies them into its scratch
folder and passes `--image p003.jpg`, relative, so no path reaches Codex, one flag
each and before `-m`, because `--image` takes every value after it and would
swallow the `-` that means stdin. The dialog names them whenever they would go,
and a box keeps them home.

**Whole lecture, one request each.** Unlike translation neither can be batched:
a preview must know how the lecture ends, and a quiz must cover all of it.
Measured lectures reach ~12k transcript tokens (85 min), well inside one
request. Each is asked for only when its PDF is made, so a lecture studied only
one way spends only that.

**The preview's instructions are the user's, word for word.** They live in
`study/preview_prompt.md`, not in code. They were written for a chat, so
`prompts._PREVIEW_FORMAT` follows them with what a page needs: Markdown under
fixed headings, flowcharts in the subset below, no LaTeX, no emoji, and the
check questions' answers at the end, since a PDF cannot be asked for them later.
The instructions number PART 1 to 5 and then a final summary, and say "PART 1 to
7"; the format section settles that. The deck goes whole with `[p.N]` page
numbers and the speech with `[00:12:34]` times, so a claim can name its source.
Without a PDF the assistant is told so, and not to write as if it had seen one.

**The preview is writing, the quiz is data.** The quiz uses `ask(..., schema=)`,
which Codex enforces with `--output-schema`. The schema follows strict mode
(every property required, no extras); counts are enforced in `core/study.py`,
not the schema, because `minItems` is not reliably accepted and a rejected
schema fails the run. Validation drops what cannot be printed honestly — three
options, an answer letter pointing nowhere, a slide number the lecture does not
have — and refuses a quiz with fewer than 15 usable questions rather than
printing a short one. The preview is kept as the Markdown it came in: a lesson
has no fields to check, so an answer is refused only when it is too short to be
one, and everything else reaches the page.

**Markdown and flowcharts are drawn here.** `export/markdown_pdf.py` reads what
an assistant writes - headings, lists, tables, quotes, code - into ReportLab
flowables, and picks out the `[교안 내용]`, `[교수님 설명]`, `[AI 보충 설명]`
markers. A Mermaid renderer needs a browser, and no graph layout library is a
dependency, so `export/flowchart.py` reads `flowchart` source and lays it out in
layers: longest-path ranks after turning back edges, edges that skip a layer
routed through it as boxes of no size, then barycentre sweeps for order and
position. It is deterministic, so a PDF made again keeps its pictures. What
cannot be read, or would be too small to read even turned on its side, prints as
its source: nothing the assistant wrote is lost. Characters Malgun Gothic lacks
(`✓`, `≈`, subscripts) come from a symbol font, `pdf.printable()`.

**The application shuffles the answers.** Models place correct answers
unevenly; `balance_answers` reorders each question's options with a seed
from the transcript fingerprint, so the spread is even and a rebuilt PDF
prints the same quiz. Questions whose options refer to each other ("Both A
and B") are left alone.

**Saved means free.** Each part is written to `study.json` on the UI thread
as it arrives, before its PDF is laid out, with the fingerprint of the
transcript it came from. Rebuilding the PDF reads it back and sends nothing. An
edit to the *text* (not the timing) marks the part stale, and the dialog offers
— does not force — a rewrite, since most edits are a corrected word.

**Notes are not re-laid-out.** The review's notes are `pdf.notes_story()`, the
same code as the notes PDF, and every PDF goes through `pdf.build_pdf()` for the
font, footer and atomic write; the two can never drift apart.

---

## 14. Drawing on the video and screenshots

**Why the video is painted by the app while drawing.** On Windows a
`QVideoWidget` is a native window, and nothing can be painted over it. Frames
therefore go to a `QVideoSink` of the player's own first and are handed on to
whichever surface shows: the `QVideoWidget` normally (GPU scaling, no cost),
or - while Draw is on - `VideoCanvas`, which paints the frame with the marks
over it. Handing a frame to the widget's own sink by hand was checked on
screen before relying on it. Switching converts the current frame at once, so
neither direction flashes a blank picture. Two alternatives were measured and
dropped: feeding the hidden widget too while drawing (it cost a tenth of the
frames at 2x), and switching the player's output to the canvas
(`setVideoOutput` during playback stalled it for over a second).

**2x playback.** Converting a 1080p frame (`toImage`, ~11 ms) and scaling it
to the view (~4 ms with `QPainter`; `QImage.scaled` took ~20 ms) is too much
for the interface thread at 60 frames a second. Two background threads do it,
newest frame wins: a frame that arrives while both are busy replaces the one
waiting, so the picture never queues behind the sound. Both calls release the
GIL (the interface thread kept >90% of its speed in a spin test). Measured on
1080p30 at 2x: 56 of 59 frames painted, interface timer at its normal 14 ms.

**The threads never end.** Qt keeps a GPU context for each thread that calls
`toImage`. A converting thread that exited - Python thread or `QThread` alike
- left the next `QMediaPlayer.setSource(QUrl())` hanging forever, i.e.
closing a lecture after drawing froze the app. Threads that stay alive do
not; so the pool starts on first use and its threads wait idle for the rest
of the run. Hiding the canvas only stops handing them frames, and never waits
for one being converted: the result of a renderer stopped meanwhile is
dropped, checked under the same lock `stop()` takes.

**Marks are data, not pixels.** `core/captures.py`: each mark is kept in
fractions of the picture (pen strokes simplified with Ramer-Douglas-Peucker),
so the same marks draw on the live video, the panel's small view, the
thumbnails and the full-size picture; screenshots stay editable. The frame is
saved untouched and a `_marked.jpg` copy is rendered by the same painting
code for the PDF - no second renderer to drift from the screen.

**Saving does not stutter playback.** A screenshot is listed at once from the
picture in memory; its JPEGs are written on a background thread (~150 ms
otherwise) and `captures.json` is only saved once the pictures it names are on
disk. Closing, switching lecture and exporting wait for the writes.

**Where a screenshot belongs.** To the slide appearance on screen at its time
(as alignment does for speech). Exported on its own, as
`ExportFormat.SCREENSHOTS` (`pdf.write_screenshots_pdf`): only the
screenshots, a page each, the page the picture's shape and the picture filling
it - drawn on a ReportLab canvas rather than laid out on A4, with the time and
slide as bookmarks so nothing covers the frame. In the notes only when asked (`PdfOptions.screenshots`, off by default), after the
paragraph being spoken when it was taken. *Play* starts 2 s before the first
mark drawn for it - the moment the user started picking things out - not at
the screenshot.
