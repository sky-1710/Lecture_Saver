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
          project.py             project dir layout, create/load/save
        media/
          errors.py
          ffmpeg_tools.py        locate / probe / extract audio (with progress)
        transcription/
          whisper_engine.py      faster-whisper wrapper + device fallback
          pipeline.py            extract -> transcribe -> persist, resumable
        slides/
          descriptors.py         Pass A: decode once, cache frame descriptors
          ink_analysis.py        shape of a change: thickness, replacement
          detector.py            Pass B: pure scoring + state machine
          diagnostics.py         per-candidate gate results
          diagnose.py            --diagnose CLI, replayed from the cache
          pipeline.py            Pass C: capture images, persist, resumable
        ui/
          main_window.py         window, menus, view stack
          start_view.py          new / open project, drag & drop
          lecture_view.py        player + transcript, wired together
          video_player.py        QMediaPlayer widget + transport controls
          transcript_view.py     virtualised segment list
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
      audio/lecture.wav   16 kHz mono, deletable, regenerable
      slides/001.jpg      representative image per slide
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
def assign_segments(segments, appearances) -> list[int | None]   # appearance index
def align(segments, appearances) -> Alignment                    # both directions
```

Each segment is assigned to the appearance with the **greatest temporal overlap**;
ties break toward the earlier appearance; zero-overlap segments (past the last slide,
or in a hand-made gap) attach to the appearance covering or preceding their start, or
`None` when there are no slides. Both lists are in start order, so the walk is linear.

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
