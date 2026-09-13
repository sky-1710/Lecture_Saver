# Project: Fully Local Lecture Video Transcription & Slide-Linked Notes App

I want you to build a privacy-first desktop application for processing lecture videos entirely on my local computer.

The core idea is:

**Lecture Video → Local Transcription → Slide Change Detection → Match Speech to Slides → Edit Transcript → Export Slide + Transcript PDF**

The application should automatically transcribe a lecture video, detect when the professor changes slides, and associate everything the professor says while a particular slide is displayed with that slide.

The user must then be able to edit the transcript manually and export the result as a clean PDF containing each slide together with the speech associated with that slide.

The application must also support exporting the transcript by itself.

---

# 1. ABSOLUTE PRIVACY REQUIREMENT

Privacy is one of the most important requirements of this project.

All processing must happen locally on the user's computer.

Lecture videos, audio, slide images, transcripts, and project data must NEVER be uploaded to external servers.

Do NOT use:

* OpenAI API
* Anthropic API
* Google Cloud Speech
* AWS Transcribe
* Azure Speech
* Any cloud transcription service
* Any cloud vision service

Use local models and local libraries only.

After the necessary models and dependencies have been downloaded, the application's core functionality should work without an internet connection.

Do not add telemetry or analytics that transmit lecture information externally.

---

# 2. PRIMARY USER WORKFLOW

The intended workflow is:

1. User imports a lecture video.
2. The application extracts/processes its audio locally.
3. A local Whisper implementation transcribes the lecture.
4. Transcript segments retain accurate timestamps.
5. The application analyzes the video and detects slide changes.
6. Representative images of each slide are captured.
7. Slide timestamps are matched with transcript timestamps.
8. The professor's speech is automatically associated with the slide visible at that time.
9. The user reviews the lecture using a synchronized video/slide/transcript interface.
10. The user manually edits transcription mistakes.
11. The edited transcript becomes the authoritative version used throughout the application.
12. The user exports:

* Slide + Transcript PDF
* Transcript TXT
* Markdown
* SRT
* VTT
* JSON

The most important final output is a readable PDF containing:

**Slide Image + Speech Associated With That Slide**

---

# 3. TARGET PLATFORM

This is initially a single-user desktop application.

It is NOT intended to recreate Slack.

I only want the convenient lecture/video transcription experience found in applications such as Slack, while keeping everything completely private and local.

Choose a reasonable desktop technology stack.

Possible approaches may include:

* Python backend
* PySide6 / Qt
* Tauri
* Electron
* Local web UI

Do not choose a heavyweight architecture without a clear reason.

Before implementing the application, recommend the best stack for this specific use case.

Priorities are:

1. Reliability
2. Privacy
3. Ease of local installation
4. Video playback quality
5. Transcript editing UX
6. Maintainability
7. Performance

---

# 4. VIDEO IMPORT

Support common lecture video formats including:

* MP4
* MOV
* MKV

Additional FFmpeg-compatible formats may also be supported.

The user should be able to:

* Select a video file
* Preferably drag and drop a video
* Create a project from the video

Do not unnecessarily duplicate large video files.

Prefer storing a reference/path to the original video unless copying it is explicitly requested.

---

# 5. PROJECT SYSTEM

Treat each lecture as a separate project.

Example:

lecture_01/
project.json
transcript.json

```
slides/
    001.jpg
    002.jpg
    003.jpg

exports/
    lecture_notes.pdf
    transcript.txt
    transcript.md
    transcript.srt
```

The project should remember:

* Original video path
* Video metadata
* Transcript
* Transcript edits
* Detected slides
* Slide timestamps
* Slide images
* Transcript-to-slide relationships
* Export settings where appropriate

Opening an existing project should restore the previous editing state.

---

# 6. LOCAL TRANSCRIPTION

Use a local Whisper implementation.

Prefer evaluating:

**faster-whisper**

over the original Whisper implementation if it provides better performance for the selected platform.

Requirements:

* Korean transcription
* English transcription
* Mixed Korean/English lectures
* Segment-level timestamps
* Local inference
* No external API

Each transcript segment should contain at least:

{
"id": "segment_001",
"start": 12.34,
"end": 16.21,
"original_text": "여기서 러닝레이트를 보시면",
"edited_text": null,
"slide_id": 8
}

Preserve the original AI-generated transcription.

Do NOT overwrite original_text when the user edits the transcript.

If edited_text exists, use it as the displayed/exported version.

Otherwise use original_text.

This allows the user to restore a segment to the original transcription later.

---

# 7. SLIDE CHANGE DETECTION

Automatically detect when the professor changes slides in the lecture video.

Do NOT process every frame unless necessary.

Use efficient frame sampling.

Potential techniques include:

* OpenCV
* Perceptual hashing
* SSIM
* Histogram comparison
* Edge/structural similarity
* Combinations of these techniques

The system should detect significant slide changes while ignoring small visual changes.

Do not incorrectly create a new slide because of:

* Mouse cursor movement
* Laser pointer movement
* Professor's webcam overlay
* Small animations
* Video playback embedded in a slide
* Minor highlighting
* Small on-screen UI changes

Use thresholding, temporal consistency, and debounce logic.

Where practical, compare the likely slide region rather than blindly comparing the entire frame.

Design the slide detection module so its algorithm can be improved later without rewriting the rest of the application.

---

# 8. SLIDE REPRESENTATIVE IMAGE

When a new slide is detected:

1. Record the slide start timestamp.
2. Determine the previous slide's end timestamp.
3. Capture a representative frame.
4. Save the representative frame locally.
5. Create a slide record.

Example:

{
"slide_id": 8,
"start": 751.0,
"end": 845.2,
"image": "slides/008.jpg"
}

Avoid capturing transitional frames during slide animations if possible.

Prefer a stable frame shortly after the slide transition.

---

# 9. DUPLICATE / RETURNED SLIDE DETECTION

Professors often move backward and forward through slides.

Example:

Slide 5
→ Slide 6
→ Slide 7
→ Slide 6 again

The application should eventually recognize that the second appearance of Slide 6 is the same slide rather than necessarily treating it as completely new content.

Use techniques such as:

* Perceptual hash comparison
* Image similarity
* Structural similarity

However, do not allow this requirement to delay the initial MVP.

Build the architecture so duplicate slide detection can be improved later.

Also distinguish between:

**slide identity**

and

**slide appearance interval**

because the same slide may appear multiple times at different timestamps.

---

# 10. TRANSCRIPT-TO-SLIDE ALIGNMENT

This is a core feature.

Use transcript timestamps and slide appearance timestamps to automatically determine which speech belongs to which slide.

Example:

Slide 8

12:31 – 14:05

Transcript:

12:34
"Here, if you look at the learning rate..."

12:52
"If this value is too large, the model can diverge."

13:21
"On the other hand, if it is too small, training becomes very slow."

All three transcript segments should automatically be associated with Slide 8.

If a transcript segment crosses a slide boundary, use a reasonable rule.

For example:

Assign the segment to the slide with the greatest temporal overlap.

Keep this logic isolated so it can be changed later.

---

# 11. MAIN APPLICATION UI

The primary desktop interface should preferably have three areas.

LEFT:

Slide navigator

Show:

* Slide thumbnail
* Slide number
* Timestamp

CENTER:

* Video player
* Current slide preview

RIGHT:

* Transcript associated with the current slide
* Editable transcript segments

Additional views/tabs:

* Lecture View
* Full Transcript
* Export

The interface should remain usable at smaller window sizes.

---

# 12. VIDEO / SLIDE / TRANSCRIPT SYNCHRONIZATION

The video, slides, and transcript should be synchronized.

When the user clicks a transcript segment:

→ Seek the video to that timestamp.

When the video plays:

→ Highlight the transcript segment corresponding to the current playback position.

When playback crosses a slide boundary:

→ Automatically select/display the appropriate slide.

Clicking a slide thumbnail should also seek the video to that slide's timestamp.

This interaction should feel fast and immediate.

---

# 13. BUILT-IN TRANSCRIPT EDITOR

The automatically generated transcript is only a draft.

The user must be able to correct transcription errors directly inside the application.

Each transcript segment should display:

Timestamp

Editable text

Example:

00:12:31

Here, if you look at the learning rate...

00:12:52

If this value is too large, the model can diverge.

Required editing functionality:

* Edit transcript text
* Autosave
* Delete segment
* Add segment
* Merge adjacent segments
* Split a segment
* Undo
* Redo
* Find
* Find & Replace
* Restore original transcription

Editing the transcript must NOT destroy the original Whisper output.

---

# 14. SINGLE SOURCE OF TRUTH

This is architecturally important.

Do NOT maintain separate copies of the transcript for:

* Full Transcript View
* Slide View
* PDF export
* TXT export
* SRT export

There should be one authoritative transcript data model.

All views and exports should reference the same transcript data.

When edited_text exists:

effective_text = edited_text

Otherwise:

effective_text = original_text

This prevents different parts of the application from becoming inconsistent.

---

# 15. AUTOSAVE

Transcript edits should be saved automatically.

Do not perform a disk write after every individual keystroke.

Use debouncing.

Suggested delay:

500–1000 ms

Minimize the chance of losing edits if the application crashes.

Use safe/atomic file writing where practical.

---

# 16. SLIDE-SPECIFIC TRANSCRIPT EDITING

The user should be able to edit transcript text directly while viewing a particular slide.

Example:

[SLIDE IMAGE]

Slide 8

12:31 – 14:05

Transcript:

12:34
Here, if you look at the learning rate...

12:52
If this value is too large, the model can diverge.

13:21
If it is too small, training becomes very slow.

Editing text here must immediately affect the same transcript used by Full Transcript View and all exports.

---

# 17. FULL TRANSCRIPT VIEW

Provide a separate view showing the complete lecture transcript chronologically.

Example:

00:00:12

Today we are going to begin with the basic concepts of machine learning.

00:00:19

Here you can see supervised learning and unsupervised learning...

Each timestamp should be clickable.

Clicking it should seek the video to that position.

The transcript should remain editable from this view as well.

---

# 18. SEARCH

Provide full-text search across the lecture transcript.

Search results should show:

* Matching text
* Slide number
* Timestamp

Clicking a search result should:

1. Select the relevant slide.
2. Seek the video to the relevant timestamp.
3. Highlight the relevant transcript segment.

---

# 19. SLIDE + TRANSCRIPT PDF EXPORT

This is one of the MOST IMPORTANT features.

Generate a single PDF containing each slide followed by everything the professor said while that slide was displayed.

Example:

Lecture Title

Slide 1

[LARGE SLIDE IMAGE]

00:00:12 – 00:02:41

Transcript

Today we are going to begin with the basic concepts of machine learning.

Here you can see supervised learning and unsupervised learning...

---

Slide 2

[LARGE SLIDE IMAGE]

00:02:41 – 00:05:13

Transcript

Now let's look more closely at supervised learning...

PDF requirements:

* Large readable slide image
* Slide number
* Slide time range
* Transcript underneath the slide
* Clean page layout
* Long transcripts must flow naturally across pages
* Korean characters must render correctly
* English characters must render correctly
* Mixed Korean/English must render correctly
* User-edited transcript must be used
* PDF generation must happen locally

The PDF should look like useful lecture notes, not a raw debug report.

Do not simply dump screenshots and text with poor pagination.

---

# 20. PDF EXPORT OPTIONS

Allow options such as:

[x] Slide image
[x] Slide number
[x] Slide time range
[x] Transcript
[ ] Individual transcript timestamps

Provide useful presets:

1. Slide + Transcript
2. Slide + Timestamped Transcript
3. Slides Only
4. Transcript Only

---

# 21. TRANSCRIPT-ONLY EXPORT

The user must be able to export the transcript without slides.

Required formats:

* TXT
* Markdown
* SRT
* VTT
* JSON

Optional later formats:

* PDF
* DOCX

Allow the user to choose whether timestamps are included.

Example TXT output:

[00:00:12]
Today we are going to begin with the basic concepts of machine learning.

[00:00:19]
Here you can see supervised learning and unsupervised learning.

All exports must use the user-edited version when edits exist.

---

# 22. STRUCTURED JSON EXPORT

Support a structured export format.

Example:

{
"video": "lecture01.mp4",
"slides": [
{
"slide_id": 1,
"start": 0.0,
"end": 83.4,
"image": "slides/001.jpg",
"transcript": [
{
"id": "segment_001",
"start": 4.2,
"end": 8.1,
"original_text": "Today we will begin with the basic concepts.",
"edited_text": null
}
]
}
]
}

Keep the internal data model well-defined and versionable.

---

# 23. EXPORT SCREEN

Create a simple export interface similar to:

EXPORT

Output:

[x] Slide + Transcript PDF
[x] Transcript TXT
[ ] Markdown
[ ] SRT
[ ] VTT
[ ] JSON

Transcript timestamps:

( ) Include
( ) Exclude

PDF Options:

[x] Slide image
[x] Slide number
[x] Slide time range
[x] Transcript
[ ] Transcript timestamps

[ Export ]

Show a clear success/error message after export and provide the local output path.

---

# 24. OPTIONAL ORIGINAL PPT/PDF SUPPORT

This is NOT required for the first MVP.

However, design the architecture so that later I can optionally provide the professor's original:

* PPT/PPTX
* PDF

If an original slide deck is available, the application should eventually be able to match video frames to original slides.

This would allow the PDF export to use the clean original slide instead of a screenshot from the video.

Do NOT allow this feature to delay the MVP.

The application must work from the lecture video alone.

---

# 25. MODULE ARCHITECTURE

Keep these responsibilities separated:

* Project management
* Video processing
* Audio extraction
* Local transcription
* Slide detection
* Slide similarity / duplicate detection
* Transcript-slide alignment
* Transcript editing
* Search
* Project storage
* PDF generation
* Transcript export
* UI
* Video synchronization

Avoid putting the entire application into one large file.

Use clear interfaces between modules.

I want the codebase to remain understandable and maintainable.

---

# 26. PERFORMANCE

This application may process long university lectures.

Potential lecture length:

30 minutes to several hours.

Therefore:

* Do not load an entire video into memory.
* Do not unnecessarily process every frame.
* Perform transcription and video processing efficiently.
* Display processing progress.
* Avoid freezing the UI during transcription or slide analysis.
* Long-running processing should happen outside the UI thread.
* Allow cancellation where practical.

Intermediate processing results should be saved so that a failure does not require restarting everything from zero.

---

# 27. PROCESSING STATUS

Provide understandable processing states.

For example:

Preparing video

→ Extracting audio

→ Transcribing lecture

→ Detecting slides

→ Matching transcript to slides

→ Saving project

→ Ready

Where possible, show:

* Progress
* Current stage
* Error information

Do not expose unnecessary internal technical logs to normal users.

Keep detailed logs separately for debugging.

---

# 28. ERROR HANDLING

Handle common errors gracefully.

Examples:

* FFmpeg not installed
* Unsupported/corrupted video
* Whisper model unavailable
* Insufficient disk space
* GPU unavailable
* GPU out of memory
* Failed PDF generation
* Original video moved or deleted

Where reasonable, fall back from GPU to CPU instead of simply crashing.

Provide understandable error messages.

---

# 29. SECURITY

Do not add:

* Analytics
* Cloud sync
* Telemetry
* Automatic file uploads
* Remote logging containing transcript/video information

Store all project data locally.

Do not expose a local server to the network unnecessarily.

If a local web server is required by the architecture, bind it to localhost only.

---

# 30. DEVELOPMENT PRIORITIES

Prioritize in this order:

1. Actually works
2. Fully local/private
3. Reliable project saving
4. Accurate transcription
5. Reliable transcript timestamps
6. Reasonable slide detection
7. Good transcript editing UX
8. High-quality PDF output
9. Performance
10. Advanced features

Do not overengineer the first version.

---

# 31. DEVELOPMENT PHASES

DO NOT attempt to implement the entire application in one giant step.

Implement it incrementally.

## Phase 1 — Application Foundation

Implement:

* Project structure
* Desktop window
* Video selection
* Video playback
* Project creation/loading

The application must actually run.

## Phase 2 — Local Transcription

Implement:

* Audio extraction
* Local faster-whisper integration
* Transcript generation
* Segment timestamps
* Transcript display
* Click transcript → seek video

At the end of Phase 2, I should be able to import a lecture video, transcribe it locally, see the transcript, and click transcript segments to navigate the video.

## Phase 3 — Slide Detection

Implement:

* Frame sampling
* Slide change detection
* Stable slide screenshot extraction
* Slide timestamps
* Slide thumbnails

## Phase 4 — Transcript/Slide Alignment

Implement:

* Map transcript segments to slide appearance intervals
* Slide + associated transcript view
* Video/slide/transcript synchronization

## Phase 5 — Transcript Editing

Implement:

* Inline editing
* original_text / edited_text
* Autosave
* Undo/Redo
* Add/Delete
* Merge/Split
* Find/Replace
* Restore original

## Phase 6 — Export

Implement:

* Slide + Transcript PDF
* TXT
* Markdown
* SRT
* VTT
* JSON

Make PDF layout polished and readable.

## Phase 7 — Improvements

Implement/improve:

* Duplicate slide detection
* Search
* Processing recovery
* UI polish
* Performance optimization
* Better slide-region detection

---

# 32. IMPORTANT IMPLEMENTATION RULE

DO NOT fake functionality.

Do not create buttons that appear functional but are not connected to actual implementations.

Do not use placeholder/mock transcription when real local transcription should be implemented.

Do not silently replace local processing with a cloud API.

If a feature is not implemented yet, clearly mark it as unavailable rather than pretending it works.

Do not implement future phases prematurely if doing so reduces the reliability of the current phase.

---

# 33. TESTING

Add reasonable tests for important non-UI logic.

Especially test:

* Timestamp formatting
* Transcript effective_text behavior
* Transcript-slide alignment
* Slide boundary behavior
* Export formatting
* Project save/load
* Autosave behavior

Use small test fixtures rather than requiring full lecture videos for every test.

---

# 34. FIRST TASK — DO THIS BEFORE WRITING THE FULL APPLICATION

First inspect the current environment and then provide a concise implementation plan.

Before writing significant application code:

1. Recommend the technology stack.
2. Explain why it fits this application.
3. Propose the project/folder structure.
4. Define the core data models.
5. Explain the local transcription pipeline.
6. Explain the slide detection algorithm.
7. Explain transcript-to-slide alignment.
8. Explain the PDF generation strategy.
9. Identify required local dependencies such as FFmpeg.
10. Identify platform-specific considerations.
11. Identify the biggest technical risks.
12. Explain how you will test Phase 1 and Phase 2.

Then create the project skeleton.

After that, implement ONLY Phase 1 and Phase 2.

Do NOT implement Phases 3–7 yet.

The Phase 1–2 deliverable must be a genuinely runnable application, not a UI mockup.

At the end:

* Run the available tests.
* Run relevant lint/type checks.
* Fix errors you encounter.
* Provide exact instructions for installing dependencies and launching the application.
* Clearly state what currently works.
* Clearly state what is intentionally deferred to later phases.

Do not ask me to manually create files that you can create yourself.

Make reasonable engineering decisions independently when requirements are not ambiguous.

If there is a choice between a sophisticated architecture and a simpler architecture that reliably accomplishes the goal, prefer the simpler architecture for the MVP.
