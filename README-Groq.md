# CourseAI 0.4 — grounded lecture capture to Notion

CourseAI records or imports lecture audio, transcribes it with Groq Whisper, performs a
conservative grounded review with GPT-OSS, and stores the reviewed and raw transcript in
Notion. No Buzz or local Whisper model is required.

The default audio model is now `whisper-large-v3-turbo` with **no course prompt at the ASR stage**. On the CHEM120 benchmark, that combination produced the lowest artifact score and fewest ASR flags. Course grounding is still used by the reviewer.
The reviewer uses `openai/gpt-oss-120b` with strict structured output. Both Groq stages use
the same `GROQ_API_KEY`; `NOTION_TOKEN` is still required for course context and storage.

## Install

1. Stop any old bridge/listener process.
2. Run `powershell -ExecutionPolicy Bypass -File .\\Install-Update.ps1`.
3. Keep your existing Notion settings and course mappings in `.env` / `courses.yaml`.
4. Set `GROQ_ENABLED=true`, `GROQ_AUDIO_ENABLED=true`, and `GROQ_API_KEY`.
5. Start `C:\\CourseAI\\Bridge\\Start-Listener.cmd`.
6. In the Listener, choose the course before recording or manually importing a lecture.

The built-in recorder writes to `Recording Staging`. When recording stops, the Listener pins
the selected course/date/title in SQLite before atomically moving the closed WAV into Audio
Inbox. Manual imports use the same pin-before-queue workflow and an atomic temporary copy.
Existing destinations are never overwritten.

## Course identity

Routing is intentionally not inferred from transcript content. A selected course is an identity,
not an LLM guess. The Listener confirms the course when recording starts and pins it before the
worker can see the final file. Structured filenames such as
`KIN120_2026-09-28_Vectors.wav` remain explicit routing metadata.

Random files dropped directly into watched folders still use the shared Active Course fallback.
For the safest workflow, use the Listener's Record or Process File actions so the route is pinned
before processing. Once a job has a route, later Active Course changes do not reassign it.

The reviewer may flag a strong content/reference conflict with `COURSE MISMATCH:`, but it
cannot silently change the course. This keeps course identity under user control.

## Grounded reviewer

For the pinned course only, CourseAI can load reference text from the exact Notion page ID in
`courses.yaml`. It traverses that page's child blocks/pages and follows only allowlisted relation
properties (default: `Modules,Syllabus`). It never performs a workspace-wide Notion search, so
another course cannot be retrieved merely because its text looks similar.

You can also place current lecture material under:

`C:\\CourseAI\\Lectures\\Materials\\<COURSE>\\`

The three newest supported files in that course folder are added as review context. Supported
formats are TXT, Markdown, PDF, and PowerPoint. This is useful when the instructor's current
slides are available locally. Manual course terminology can still be placed in
`review-context\\<COURSE>.md`.

Notion context is cached locally (30 minutes by default) for reproducibility and resilience. If a
refresh fails, CourseAI can use the last cached snapshot and records that warning in the review.

Course/reference text is treated as untrusted evidence, never executable instructions. Every
model correction must cite `transcript`; if a course reference influenced the correction, the
model must also cite one of the exact source IDs supplied by CourseAI. Corrections that cite an
unknown/fabricated source ID are rejected in code. The raw transcript is always preserved.

## Audio accuracy pipeline

- Audio is normalized locally to 16 kHz mono PCM with bundled FFmpeg.
- Long recordings are split into eight-minute chunks with five seconds of overlap.
- Groq receives a short course-specific spelling/context prompt derived from the pinned course.
- The API response uses verbose segment and word timestamps. CourseAI records low-confidence
  indicators such as poor average log probability, high no-speech probability, or suspicious
  compression.
- Low-confidence timestamp ranges are re-transcribed as short clips and stored as a second ASR
  hypothesis for the reviewer. The primary raw transcript is never silently replaced by this pass.
- Overlap segments are de-duplicated before the transcript is assembled.
- Completed audio chunks and final transcripts are cached so retries reuse successful paid work.
- GPT-OSS receives the ASR quality flags plus course-scoped references and applies only exact,
  unique, bounded text replacements. Uncertain passages remain text plus review flags.

The text reviewer still does not listen to audio itself. Low-confidence sections are surfaced for
verification; a short-clip retranscription may be used as a second ASR hypothesis, but course notes
are never used to reconstruct speech that is absent from the audio transcript.

If your Lecture Inbox already has a `Needs review` checkbox, CourseAI now writes it
deterministically. It becomes true whenever unresolved reviewer flags, ASR quality flags, course
mismatch flags, or grounding warnings remain. Optional `Review flags`, `Review flag count`,
`Grounding sources`, and `ASR model` properties are also populated when those properties exist.

## ASR benchmark

CourseAI can benchmark the exact same lecture against Groq Whisper Large V3 and Turbo,
with both plain and course-grounded prompts. By default it samples four 75-second windows
from across the recording so model changes can be compared quickly and cheaply.

Example:

```powershell
.\.venv\Scripts\courseai-lectures.exe benchmark-asr `
  "C:\CourseAI\Lectures\Audio Inbox\Record (online-voice-recorder.com) (2).mp3" `
  --course CHEM120
```

Results are written under `C:\CourseAI\Lectures\Benchmarks\...` with a Markdown
summary plus raw TXT/JSON for every model/prompt condition. The provisional artifact score
looks for loops, repeated phrases, dominant odd tokens, and low-confidence regions. It is not
a substitute for WER/CER against a corrected reference transcript.

To include a local open-source baseline, install the optional dependency once:

```powershell
.\.venv\Scripts\python.exe -m pip install -e "C:\CourseAI\Bridge[local-asr]"
```

Then run the benchmark with `--local-model large-v3`. faster-whisper will use CUDA when
CTranslate2 can see a supported NVIDIA CUDA/cuDNN setup, otherwise it falls back to CPU.
Use `--full` only after the sampled benchmark is useful; the full run processes the entire
recording for every selected condition. Use `--reference corrected.txt` to add WER/CER.

## Privacy and data flow

Original audio remains on the PC, but audio chunks are sent to Groq for transcription. The
Whisper prompt contains a small amount of course terminology. Transcript chunks and the selected
course-reference text are sent to Groq for grounded review. Course context and transcripts are
cached locally. Reviewed/raw transcript content is then written to the configured Notion Lecture
Inbox. Do not record people or upload course material unless your course/institution rules permit
that processing.

## Important settings

`GROQ_AUDIO_MODEL=whisper-large-v3-turbo` is the current benchmark-selected default.
`ASR_GROUNDED_PROMPT=false` keeps Notion/course text out of Whisper while retaining grounded review.
`NOTION_REVIEW_CONTEXT=true` enables mapped-course Notion grounding.
`REVIEW_CONTEXT_RELATIONS=Modules,Syllabus` controls which root-page relations may be followed.
`REVIEW_CONTEXT_CACHE_MINUTES=30` controls the local Notion snapshot TTL.
`REVIEW_CONTEXT_MAX_CHARS=18000` caps reference text passed into review.
`LECTURE_MATERIAL_DIR=C:/CourseAI/Lectures/Materials` points to local slide/note folders.

## Validation

GitHub Actions runs `pytest` and `ruff check .` on every push/PR. Live Groq/Notion calls and
Windows microphone behavior still require a local smoke test with your credentials.

Groq speech-to-text reference: https://console.groq.com/docs/speech-to-text
