# CourseAI 0.3 — audio to Notion using Groq

No Buzz or local Whisper model is needed. Your recording is uploaded to Groq
Whisper (whisper-large-v3-turbo), then the transcript is reviewed by
openai/gpt-oss-120b using your bundled Transcript Evaluator skill, then saved in
Notion with the original transcript, reviewed version and correction/flag record.
Both stages use the same GROQ_API_KEY. NOTION_TOKEN is still needed for storage.

## Install

1. Stop the existing bridge with Ctrl+C and close Buzz for this workflow.
2. Extract the ZIP into a separate folder.
3. In that folder, run:
   powershell -ExecutionPolicy Bypass -File .\Install-Update.ps1
4. Your existing .env opens. Keep existing Notion credentials. Fill GROQ_API_KEY;
   ensure GROQ_ENABLED=true and GROQ_AUDIO_ENABLED=true. ACTIVE_COURSE=KIN120
   is supplied for today's test. Installation preserves existing config values.
5. Double-click C:\CourseAI\Bridge\Start-Listener.cmd. It checks connections,
   opens Audio Inbox, and starts the watcher. Keep its terminal open.
6. Save/copy the FINISHED recording into C:\CourseAI\Lectures\Audio Inbox.
   Random filenames work. Do not record live into the watched folder.

For today's already-recorded test, just copy that saved audio into Audio Inbox.
No manual TXT export is needed. A local transcript copy lives beneath
C:\CourseAI\Bridge\audio-cache; do not copy that into the watched TXT folder.
Your original audio is kept in place.

## Course and date

Use the CourseAI Listener's Active Course selector to choose the active mapped
course. The app reads the real course mapping from courses.yaml and keeps the
backend routing in local state instead of forcing manual .env edits. A random
recording's initial course/date/title is pinned in local state. Changing the
active course does not reassign old jobs. Unprocessed old audio in Audio Inbox
is processed under the current selected course. Only put the intended test
recording there when starting for the first time.

The local source file's modified date is the fallback lecture date. Set
LECTURE_DATE=YYYY-MM-DD for an older recording if its file date is wrong; leave
blank for normal use. COURSE_YYYY-MM-DD_Title filenames override the fallback.
Use distinct filenames per recording; do not overwrite an old lecture's file.

## Reliability and limitations

- The bundled FFmpeg decoder (installed automatically with imageio-ffmpeg) prepares
  16 kHz mono WAV chunks of eight minutes, each below Groq's 25 MB upload cap.
  This is local audio decoding, not local AI. No separate FFmpeg setup is needed
  on supported Windows wheels. Chunks do not overlap; speech at boundaries may
  need audio confirmation. Only the first audio track is used.
- Completed audio chunks and final transcripts are cached, so retries generally
  reuse paid work. A lost API response can still require repeating a request.
- Internet and available Groq quota are required. Free access is subject to your
  account's limits; the program does not enable billing or guarantee free use.
- Save the recording outside Audio Inbox, then copy it in after closing it. A
  quiet file interval alone cannot prove a paused recording has finished.
- Existing Notion duplicate prevention, write journal and retries remain in use.
  Failed transcription/review remains pending; no empty success is fabricated.
- Original audio is never moved by direct audio ingestion. ARCHIVE_AUDIO applies
  only to the older TXT path. The bridge still accepts existing TXT transcripts.
- Notion's Local transcript path currently identifies the input file (audio for
  direct audio jobs); raw TXT is retained in audio-cache. Processing stays
  Transcribed; flags are in the page body, not an automatic Ready to study claim.
- The skill snapshot is from your Notion Transcript Evaluator, retrieved Sept 28.
  Review applies anchored corrections only and preserves uncertain wording and
  all raw text. Filler is retained. It does not automatically fetch course sources,
  route Modules or edit Course/Module pages. Optional sourced terminology can go
  in review-context/KIN120.md (max 24,000 characters).
- Audio transcription is automatic; the subsequent text checker does not listen
  again to the audio. It cannot certify that every word is accurate.
- The installer backs up source, preserves .env/courses.yaml, and adds only missing
  settings. If you previously set GROQ_AUDIO_ENABLED=false, change it to true.

## Checks

42 tests passed on Linux, including actual audio decoding/chunking, cached retry,
no duplicate upload, failure handling, original preservation and review tests.
Lint passed. One original Windows-only audio archival test was excluded because
it intentionally rejects Linux. Windows installer and live Groq/Notion calls need
your local check. No real key or audio was uploaded during development.

Groq reference: https://console.groq.com/docs/speech-to-text
