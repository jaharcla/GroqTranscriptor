import hashlib
import json
import logging
import os
from pathlib import Path

from .files import Lecture, parse_filename, read_stable
from .grounding import CourseGrounder
from .review import Reviewer
from .transcribe import AUDIO_TYPES, Transcriber

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, config, state, notion):
        self.config, self.state, self.notion = config, state, notion
        self.transcriber = Transcriber(config) if getattr(config, "audio_enabled", False) else None
        self.reviewer = Reviewer(config) if getattr(config, "groq_enabled", False) else None
        self.grounder = CourseGrounder(config, notion)

    def audio_matches(self, path):
        try:
            relative = path.relative_to(self.config.transcripts)
        except ValueError:
            relative = Path(path.name)
        result = []
        for root in (self.config.audio, self.config.archive, self.config.transcripts):
            parent = root / relative.parent
            if parent.is_dir():
                result.extend(
                    p
                    for p in parent.iterdir()
                    if p.is_file()
                    and p.stem.casefold() == path.stem.casefold()
                    and p.suffix.lower() in AUDIO_TYPES
                )
        return list(dict.fromkeys(result))

    def archive_audio(self, path):
        for audio in self.audio_matches(path):
            log.info("Original audio found: %s", audio)
            if not self.config.archive_audio or audio.is_relative_to(self.config.archive):
                continue
            if not audio.is_relative_to(self.config.audio):
                continue
            target = self.config.archive / audio.relative_to(self.config.audio)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(f"Archive target already exists; source preserved: {target}")
            if os.name != "nt":
                raise RuntimeError("Opt-in audio archival is supported only on Windows")
            audio.rename(target)
            log.info("Archived audio: %s", target)

    def process(self, path):
        path = Path(path).resolve()
        is_audio = path.suffix.lower() in AUDIO_TYPES and self.transcriber is not None
        if path.suffix.lower() != ".txt" and not is_audio:
            log.error("Expected a .txt transcript or supported audio file: %s", path)
            return False
        self.state.ensure(path)
        try:
            initial = self.state.get(path)
            saved_route = initial.get("routing")
            active_course = self.state.get_setting(
                "active_course", getattr(self.config, "active_course", "")
            )
            lecture = (
                Lecture(**json.loads(saved_route))
                if saved_route
                else parse_filename(
                    path,
                    active_course,
                    getattr(self.config, "lecture_date", ""),
                )
            )
            if lecture.course not in self.config.courses:
                raise ValueError(f"Missing course mapping: {lecture.course}")
            if not saved_route:
                self.state.set(
                    path,
                    stage="detected",
                    routing=json.dumps(
                        {
                            "course": lecture.course,
                            "date": lecture.date,
                            "title": lecture.title,
                        }
                    ),
                )

            context = (
                self.grounder.context(lecture)
                if is_audio or self.reviewer is not None
                else {"course": lecture.course, "sources": [], "warnings": []}
            )
            asr_quality = []
            if is_audio:
                if initial["status"] != "done":
                    self.state.set(path, status="processing", stage="preparing_audio")
                else:
                    self.state.set(path, stage="preparing_audio")
                prompt = self.grounder.asr_prompt(lecture, context)
                self.state.set(path, stage="transcribing")
                result = self.transcriber.transcribe_result(path, prompt)
                text = result["text"]
                asr_quality = result.get("quality_flags", [])
            else:
                text = read_stable(
                    path,
                    self.config.stable_seconds,
                    self.config.stable_timeout,
                )

            digest = hashlib.sha256(text.encode()).hexdigest()
            if self.reviewer:
                digest = self.reviewer.digest(text, lecture, context, asr_quality)
            row = self.state.get(path)
            if row["status"] == "done" and row["digest"] == digest and row["key"] == lecture.key:
                self.state.set(path, stage="complete")
                log.info("Already ingested: %s", path)
                return True

            self.state.set(path, key=lecture.key, status="processing", stage="reviewing")
            for other in self.state.jobs():
                if other["key"] == lecture.key and other["pending"] and other["path"] != str(path):
                    self.notion.prepare()
                    self.notion.recover(self.state, other["path"])
            if self.reviewer:
                text = self.reviewer.review(text, lecture, context, asr_quality)
            self.state.set(path, stage="uploading")
            page = self.notion.sync(lecture, text, path, self.state)
            if not is_audio:
                self.archive_audio(path)
            self.state.set(
                path,
                digest=digest,
                page=page,
                status="done",
                stage="complete",
                attempts=0,
                next_retry=0,
                error=None,
            )
            log.info("Ingested: %s -> Notion %s", path, page)
            return True
        except Exception as exc:
            error = str(exc)
            if self.config.token:
                error = error.replace(self.config.token, "[REDACTED]")
            if getattr(self.config, "groq_api_key", ""):
                error = error.replace(self.config.groq_api_key, "[REDACTED]")
            self.state.fail(path, error, self.config.retry_seconds)
            self.state.set(path, stage="error")
            log.error("Failed: %s: %s", path, error)
            return False
