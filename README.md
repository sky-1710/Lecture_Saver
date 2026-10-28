# Lecture Saver

Turn a lecture recording into a transcript and, eventually, slide-linked notes —
**entirely on your own computer.**

Video, audio and slide images never leave this machine. There is no cloud
transcription service, no analytics, no telemetry, and no local server listening
on a port. Transcription, slide detection and every export run on your own
CPU (and, for the Turbo model, the Intel integrated GPU), and once a Whisper
model is downloaded the application works offline.

**Three features are an exception, and they are off until you turn them on.**
*Translate with AI*, *Study pack* and *Ask AI* send the transcript **text** to
an AI assistant — to translate it into Korean, to write a preview and a review
quiz from it, or to answer your questions about it — because no local model on
a laptop without a GPU does any of these well in reasonable time. The video, the audio and the slide images are never
sent — only the words. The application asks, in as many words, before the first
lecture is ever sent, and you can leave it switched off and translate by hand
instead with *AI translation request*.

**Current status: Phases 1–7 are implemented and working**, apart from the
slide-region detection improvement listed under *Not built yet* — see
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

To open it like any other app, add a Desktop and Start-menu shortcut once:

```powershell
python run.py --create-shortcuts     # "Lecture Saver" with its icon, no console window
python run.py --remove-shortcuts     # take them away again
```

The shortcut runs this folder's code directly, so later updates need no rebuild.

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
   *Transcribe lecture*. A percentage, the current stage and a **Cancel** button
   are shown throughout - beside the panel's progress bar, in the status bar, and
   in the window title, so the figure is readable even when the window is
   minimised. The window stays responsive because the work runs on a background
   thread; exports report the same way, one line per file being written.
4. **Detect slides.** Press *Detect slides* in the left pane. The video is
   scanned at about 100x realtime, each detected slide is saved as a JPEG, and
   the navigator fills with thumbnails. Correct any mistakes by right-clicking
   a slide, or with *Add slide here*.
   Lines that a slide change runs through are cut at the nearest full stop, so
   each slide keeps whole sentences; **Edit → Split lines at slide changes**
   repeats that at any time, and Ctrl+Z undoes it.
5. **Navigate.** Click any transcript segment or slide thumbnail to jump the
   video there. As the video plays, the matching transcript segment is
   highlighted and scrolled into view. **←** and **→** move the video a
   second back or forward from anywhere but a text box; held down, they keep
   going.
   **Draw and take screenshots while you watch.** Press **D** (or *Draw* under
   the video) and mark the picture as it keeps playing - at any speed, 2x
   included; Space still pauses. The pen button holds a **free pen**,
   **underline**, **highlight** and an **eraser** (keys 1-4; its arrow picks
   one), and beside it is a typed **memo** (5) - double-click one to change
   it later. The dot beside them picks the **ink**: red, blue, black, green,
   orange, purple or white; the **A** beside it the memo **text size**. Press
   **S** (or *Screenshot*)
   to keep the picture on screen with your marks. *Screenshots* opens them
   beside the video: change any screenshot's marks there, and *Play from*
   replays the stretch you were marking. *Export PDF* at the top of the panel
   (or **File → Export screenshots PDF**) puts them all in one PDF to print,
   two to a page. You need not remember the keys: they are written under the
   video's buttons - the drawing tools' while you draw - and under the
   screenshot being edited.
6. **Correct the transcript.** Double-click a line to fix it; right-click for
   merge, add, delete and restore original; Ctrl+F to find and replace a
   repeated mistake. Edits save themselves.
7. **Export.** Top right: *Export PDF + MD* (Ctrl+E) writes the lecture notes
   PDF - each slide with what was said on it - and the Markdown in one click.
   *More* holds the rest: *More export formats...* (Ctrl+Shift+E) adds TXT,
   SRT, VTT and JSON and every option, and *AI translation request (by hand)*
   writes the notes with a Korean translation request on top, for you to give
   to an AI assistant yourself.
   To have notes appear on an iPad by themselves, install iCloud for Windows and
   pick a folder inside iCloud Drive under **File → Default export folder...**.
   Every exported file starts with the lecture's name
   (`OS02-LimitedDirectExecution_lecture_notes.pdf`), so lectures sit side by
   side there; the choices of your last export are where a new lecture starts.
   AI translation requests stay in the project folder. Lecture Saver still
   uploads nothing itself - iCloud syncs that folder, so its contents do go to
   Apple's servers.
8. **Translate.** Two ways, and both end in the same place. *Translate with
   AI* does it here: it sends the transcript text, a few slides at a time, to
   Codex signed in with your ChatGPT account, and saves each batch as it
   arrives — stop it or hit your usage limit and what is done is kept, and
   running it again carries on from the first untranslated slide. Or keep it
   switched off and use *AI translation request*, which writes the lecture and
   the request to a file for you to hand to an assistant yourself; paste the
   answer into *Import translation*. Either way each slide's translation
   appears under its transcript and in the PDF and Markdown.
9. **Study pack.** One PDF to study from, in the order you use it:
   **예습** (a preview to read before the lecture — the key points first,
   the terms you need, then the whole lecture explained simply enough for a
   middle-schooler, with everyday examples), then the **강의 노트** (each
   slide with what was said), then a **15-question review quiz in English**,
   with the answers and Korean explanations starting on the next page so they
   are not in sight while you answer. The preview and the quiz are written by
   Codex from the transcript text and saved in the project, so making the PDF
   again — after fixing a line, or with other slide options — is instant and
   uses no quota; if you edit the transcript afterwards the window offers to
   write them again. The answers are shuffled across A–D by the application,
   because models tend to put the right answer in the same place.
10. **Search** (Ctrl+Shift+F, or the *Search* tab) to find where anything was
   said; click a result to jump to that slide and moment.
11. **Ask AI** when something does not make sense. *Ask AI* under the video
   opens a chat beside the transcript: ask anything about the lecture. To ask
   about particular words, use the **Ask AI** button that appears on a
   transcript line when you point at it (or select it), or right-click the
   line — *Ask AI about this line...* or *Explain this line with AI*. The line
   waits above the input; type a question, or press Enter with nothing typed to
   have it explained. Gather several lines first to ask about them together.
   Ctrl+Shift+A does the same for the selected line (or the one playing), and
   while editing a line you can select a few words and right-click → *Ask AI
   about “…”*. Answers cite the lecture — click a time such as `00:12:34` or
   *Slide 8* and the video goes there. *This part* sends the slide you are on
   and the slides either side of it (quick); *Whole lecture* sends everything,
   for questions that span it. The conversation is saved with the lecture.

Reopening a project restores everything.

### Choosing a model

**Use Turbo** (the default). It is Whisper large-v3-turbo, run by Intel's
OpenVINO on the integrated GPU (Iris Xe and similar) — which faster-whisper
cannot use, since its GPU support is CUDA-only. Measured on an i7-1165G7:

| Model | Engine | Download | 1-hour lecture takes | Korean quality |
|---|---|---|---|---|
| **Turbo** (default) | OpenVINO, Intel GPU | 830 MB | ~14 min | very good |
| Turbo, no Intel GPU | OpenVINO, CPU | 830 MB | ~25 min* | very good |
| `medium` | faster-whisper, CPU | 1.5 GB | ~35–45 min | good |
| `small` | faster-whisper, CPU | 480 MB | ~10 min* | usable |
| `large-v3` | faster-whisper, CPU | 3.1 GB | ~1.5 h* | best |

A 58-minute Korean lecture took 13 min with Turbo and 40 min with `medium`.
\* The app's estimates, not re-measured end to end.

Against `medium` on the same lecture, Turbo fixed several of its mistakes
(할다는 → 할당하는, 센스티브 → 센시티브, a mix of 벨리/밸리/베리데이션 → 밸리데이션)
and made a couple of its own; with VAD windows it kept a sentence that a
single long-form pass dropped. If OpenVINO cannot start, the lecture is still
transcribed — with faster-whisper on the CPU — and the app says so.

**Keywords.** Type the course terms the lecture uses, comma-separated
(`크로스 밸리데이션, Naive Bayes, TLB`). Whisper is given them as hints before
every 30-second window and spells them that way: on the test lecture this turned
"크루스 밸리데이션" into "크로스 밸리데이션". They are kept per lecture.

**Keywords from the slides.** After *Detect slides*, press **From slides** next
to the Keywords box: the text on every slide is read with the OCR built into
Windows (local, offline, nothing to download — about 0.2 s a slide), and the
terms it uses are added after anything you typed — headings and phrases shown on
several slides first (`ROC Curve, Cross Validation, False positive rate, AUC,
leave-one-out cross validation, …`), headers and footers on every slide left out.
A slide kept as several images while the professor writes on it counts as one
slide, words a pen stroke cut short ("externa") are dropped, and a word already
inside a longer term ("Direct" in "Limited Direct Execution") is not repeated.
On a Korean lecture about English slides this fixed "크루스 밸리데이션" and
"배리데이션 셋", and the transcript kept phrases it had dropped without the
hints; the terms then come out spelled as on the slides ("Validation set").
**Check the list before transcribing:** OCR can misread a term, Korean slide text
especially, and a misspelled hint is copied.

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
- Word-level timestamps, so each transcript line ends where a sentence does
  rather than where Whisper's decoder stopped (about 12% slower; it is what
  makes slide boundaries land between sentences)
- Local transcription with **faster-whisper** (CTranslate2), CPU int8
- **Turbo on the Intel GPU** via OpenVINO: VAD-cut 30-second windows, about
  2.7x faster than `medium`, falling back to the CPU and then to faster-whisper
- Keyword hints (course terms) given to Whisper before every window, typed or
  read off the slide images with Windows' built-in OCR
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

### Phase 6 — Export ✅

- **Slide + transcript PDF**: the lecture title, then every slide as a block of
  number, *Before notes / With notes* label, time range and a large picture,
  followed by what was said while it was on screen. Speech is split into
  paragraphs at pauses and flows across pages; slides nobody spoke over say so
- Korean, English and mixed text from one embedded font (Malgun Gothic, or
  Nanum Gothic). With neither installed the export stops with a clear message
  rather than writing a PDF of empty boxes. Decomposed Hangul in file names
  (common in archives made on a Mac) is composed before writing
- PDF options - slide image, number, time range, transcript, per-line
  timestamps - with the four presets: *Slide + Transcript*, *Slide +
  Timestamped Transcript*, *Slides Only*, *Transcript Only*
- **TXT** (`[HH:MM:SS]` blocks, or paragraphs without timestamps), **Markdown**
  (slide headings with linked images), **SRT**, **VTT** and structured
  **JSON** (slides with their transcript, original and edited text, schema
  version)
- Every export uses the corrected transcript; lines you blanked are left out
  (JSON keeps them, with their original)
- Built from a snapshot on a background thread, so editing can continue; each
  format succeeds or fails on its own, and the result lists every file written
  and the reason for any failure, with *Open folder*. Choices are remembered
  per project
- 22-page notes for a 27-minute lecture with 24 slides export in about 1.5 s
- **One-click exports** in the lecture header: *Export PDF + MD* uses the PDF
  options last chosen; *AI translation request* writes
  `<lecture name>_ai_translation.pdf` and `.md` - the whole lecture with a Korean
  translation request at the top (translate every sentence, keep technical
  terms in English, no summarising, an example, and how to continue a long
  lecture). The result window can copy the request text for pasting.
  **Nothing is sent to any AI service**; the files are for you to use. A
  lecture transcribed as Korean asks before creating one

### Drawing on the video and screenshots ✅

- **Draw** (D) puts the marks over the video while it keeps playing; Space
  still pauses and the speed stays as chosen. Switching off hides the marks
  without losing them; *Clear all* removes them, and Ctrl+Z / Ctrl+Y undo and
  redo every change, *Clear all* included
- The **pen** is one button holding four kinds - **free pen** (1), a freehand
  red line for circling and arrows; **underline** (2), a straight line that
  snaps level when it is nearly level; **highlight** (3), a dragged box, or a
  drag along one line of text to band that line; **eraser** (4). Pressing the
  button goes back to the kind used last, its arrow picks another. **Memo** (5)
  is typed where you click as plain text, no box: Enter keeps it, Esc drops
  it, and a memo kept is changed by clicking it with the memo tool or
  double-clicking it with any. Right-click removes a mark with any tool
- **Ink**: the dot after the memo button colours new pen strokes, underlines
  and memos - red, blue, black, green, orange, purple or white (for dark
  slides). Each mark keeps its own; changing the ink while typing a memo
  recolours that memo. Highlights stay yellow
- **Text size**: the **A** after the ink sets the size of new memos - small,
  medium (the default), large or extra large, in proportion to the picture so
  a memo keeps its size on the panel's small view and in the PDF. Each memo
  keeps its own; choose a size while typing or changing one to resize it
- **Screenshot** (S) keeps the frame on screen at the video's full resolution,
  with the marks that are showing. Saving happens in the background - about
  13 ms of the interface's time - so taking one at 2x does not stutter
- **Screenshots** beside the video lists them in the order of the lecture,
  with their marks on the thumbnails. The one selected can be re-marked with
  the same tools; *Play from* starts 2 s before its first mark was drawn (or
  before the screenshot itself, for one with no marks), selecting the slide
  and the line too. *Delete...* asks first
- **Screenshots PDF** - *Export PDF* in the panel, **File → Export
  screenshots PDF**, or *Screenshots PDF* in *More formats...*: only the
  screenshots, in the order of the lecture, with their marks, two to a page at
  the full page width. Above each: its number, time, slide and where its
  marking began. Written as `<lecture>_screenshots.pdf` to the same folder as
  the other exports; it needs neither slides nor a transcript
- They stay out of the notes PDF unless *My screenshots in the notes too* is
  ticked in *More formats...*; then each sits in the transcript after the
  paragraph being spoken when it was taken. Each screenshot is also kept as
  `captures/0001_marked.jpg`, ready to open or print by itself
- Measured on a 1080p 30 fps video at 2x on this laptop (i7-1165G7): 56 of 59
  frames a second painted while drawing, the interface answering within its
  timer resolution. Normal playback still goes through the GPU video widget;
  only drawing paints frames, on two background threads

### Ask AI ✅

- A chat under the video about the lecture: ask at any moment, or point at
  transcript lines first with the **Ask AI** button on a line, its right-click
  menu, or Ctrl+Shift+A — even a few words selected while editing a line
- Each question carries where you are: the playback position, the slide shown
  and the lines pointed at. *This part* sends that slide and its neighbours
  (without slides, the few minutes around that moment); *Whole lecture* sends
  every line. Corrections you made are what is sent; blanked lines are not
- One lecture's chat is one conversation for the assistant too: it runs in a
  single Codex session, so a follow-up ("그럼 이걸 간단히 해줘") is answered
  with the earlier answers in view. A follow-up sends only what the session
  has not had yet — the question, slides not sent before, a line you
  corrected since — not the transcript again. If the session cannot be
  continued, a new one starts with the recent turns
- Answers are in the language you ask in, and cite the lecture as
  `[00:12:34]` and *Slide 8*; both are links that seek the video and show the
  slide and line
- A question that fails (usage limit, no sign-in, Stop) is put back in the box
  with its lines rather than lost
- Saved per lecture in `chat.json`; *New chat* forgets it, and deletes the
  Codex session too. Set `"ask_model"` in
  `%APPDATA%\LectureSaver\settings.json` to use another model for questions
  than for translation

### Translation import ✅

- *Import translation* reads an AI assistant's answer to the translation
  request - pasted, or loaded from a `.md`/`.txt` file - and attaches each
  `## Slide N` section to that slide. It previews which slides match, which
  numbers are not in the lecture and which slides are still untranslated
- The example inside the request and assistants' notes ("type '계속' to
  continue") are ignored; an answer in several parts can be imported part by
  part
- Stored in `translations.json` against each slide's number *and* time, so
  after slides are corrected or detected again a translation is only shown for
  a slide that still covers the same part of the lecture - never under a
  different slide
- Shown under the transcript on the *Current slide* tab, included in the PDF and
  Markdown as a *한국어 직역* block under each slide's transcript and in JSON,
  optional in *More formats...*, and searchable. Nothing is sent anywhere

### Phase 7 — Search, returned slides, recovery ✅

- **Search** tab (Ctrl+Shift+F): every word in any order, case and **spacing
  ignored** ("러닝 레이트" finds "러닝레이트"), in the corrected text, the
  original transcription and imported translations. Results show the slide
  number, time and the matching words highlighted; clicking one selects the
  slide, seeks the video and selects the line
- **Returned slides** (5 → 6 → 7 → 6): the second visit is recognised as the
  same slide - it shares the slide's identity and picture, shows *Same as
  Slide 6*, and the PDF refers back instead of repeating the picture while
  keeping that visit's speech. Measured on a real lecture, both genuine returns
  were found and nothing else was merged. Right-click → *Treat as a different
  slide* undoes a wrong match. `duplicate_slides` in `project.json` tunes it
- **Continue an unfinished transcription**: after a cancel, a failure or a
  crash, *Continue from HH:MM:SS* transcribes only the rest of the audio,
  keeping what was done and any corrections made to it

### Not built yet

| Feature | Notes |
|---|---|
| Better slide-region detection | Comparing only the area of the frame that holds the slide (e.g. ignoring a large camera view beside it). The busy-region mask covers webcams and embedded video today |
| Original PPT/PDF matching (§24) | Using the professor's own deck instead of video captures |

---

## Project folder layout

```
Lecture 01/
  project.json                  video reference, metadata, stage states, settings
  transcript.json               the authoritative transcript
  translations.json             imported slide translations
  chat.json                     the Ask AI conversation
  captures.json                 screenshots: time, where marking began, marks
  captures/0001.jpg …           each screenshot as the video showed it, and
  captures/0001_marked.jpg …    with your marks drawn in (what gets printed)
  transcript_original.json      Whisper's output as transcribed, kept for
                                "Reset to original"; never touched by editing
  slides.json                   detected slides and their appearance intervals
  audio/lecture.wav             16 kHz mono, regenerable — safe to delete
  slides/001.jpg …              representative image per slide
  cache/frame_descriptors.npz   scanned frames (thumbnails + ink masks),
                                so re-detection skips decoding
  exports/                      <lecture>_lecture_notes.pdf, <lecture>_screenshots.pdf,
                                <lecture>_transcript.txt/.md/...
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

pytest                # 967 tests, no lecture video or model needed
pytest -m scenario    # + renders real lecture clips and detects slides in them
pytest -m model       # + real transcription (downloads the tiny model; runs
                      #   Turbo on OpenVINO too if its model is downloaded)
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
  transcription/   faster-whisper and OpenVINO engines, and the processing pipeline
  ui/              all Qt lives here
```

`core/` has no Qt or FFmpeg imports, which is what makes the important rules
testable without a GUI or a lecture recording.

---

## Privacy

- No cloud speech or vision APIs — transcription is `faster-whisper` on your
  CPU, or OpenVINO on your Intel GPU, both running locally. OpenVINO installs an
  `openvino-telemetry` package; only its model-conversion tool (`ovc`) uses it,
  and the app never runs that. A Turbo transcription, model loading included,
  was watched for network connections and made none.
- Slide text for keywords is read by Windows' own OCR (`Windows.Media.Ocr`),
  which runs on this machine — no cloud vision service.
- No telemetry, analytics, crash reporting or remote logging
- No local web server and no listening socket
- Project data stays in your project folders; settings stay in
  `%APPDATA%\LectureSaver`
- Network access happens exactly once per model: the Hugging Face download of the
  Whisper weights. Pre-download a model, and the application never needs the
  internet again.

### The one exception: *Translate with AI*, *Study pack* and *Ask AI*

Everything above holds with these switched off, which is how they ship. They
share one agreement — agree in any of them and all three may send — and
switching it on changes this, and only this:

| | Sent to the assistant |
|---|---|
| Transcript text (the words, slide by slide) | **yes** |
| Slide numbers and their labels | **yes** |
| Your questions, and the earlier turns of that lecture's chat (*Ask AI*) | **yes** |
| Video file | no |
| Audio | no |
| Slide images | no |
| File names and paths | no |
| Your name, e-mail or any identifier from the app | no |

- Nothing is sent until you agree to it once, in a dialog that says what
  leaves the computer. The setting lives in `%APPDATA%\LectureSaver`.
- All three run through the **Codex CLI**, signed in with your own ChatGPT
  account. Lecture Saver stores no API key and never sees your sign-in — it
  runs the program you already have and reads back what it prints.
- Codex is run with `--sandbox read-only`, so it cannot touch your files.
  Translation and the study pack also run with `--ephemeral`, so the lecture
  is not written into a session log on disk. *Ask AI* is the exception: a
  conversation is a Codex session, which Codex keeps in
  `%USERPROFILE%\.codex\sessions` on this computer (the text sent in it, and
  the answers) so that follow-ups can continue it. *New chat* deletes that
  file; a session whose first question failed or was stopped is deleted at
  once.
- Turn it off again and the rest of the application keeps working with no
  internet connection.

*Ask AI* sends only when you ask: your question, the lines you pointed at,
the transcript of the part you asked about (or of the whole lecture, if you
chose that) and, when a conversation has to be started again, its recent
turns. Within one conversation each piece of transcript goes once.

The study pack's preview and quiz are made from the same transcript text,
in two requests of the whole lecture — an overview and a quiz both need to
see all of it. The notes part of the study pack is built on your computer
from your project, exactly like the notes PDF.

To translate without sending anything, use *AI translation request* instead:
it writes the lecture and the request to a file for you to carry to an
assistant yourself, and *Import translation* brings the answer back.
