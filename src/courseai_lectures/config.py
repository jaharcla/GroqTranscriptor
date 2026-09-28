import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import yaml
from dotenv import dotenv_values


@dataclass
class Config:
    token: str
    database_id: str
    data_source_id: str
    transcripts: Path
    audio: Path
    archive: Path
    state: Path
    log: Path
    courses: dict[str, str]
    props: dict[str, str]
    stable_seconds: float = 5
    stable_timeout: float = 300
    retry_seconds: float = 30
    archive_audio: bool = False
    audio_enabled: bool = False
    groq_audio_model: str = "whisper-large-v3"
    audio_cache: Path = Path("audio-cache")
    active_course: str = ""
    lecture_date: str = ""
    groq_enabled: bool = False
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    review_dir: Path = Path("reviews")
    context_dir: Path = Path("review-context")
    materials_dir: Path = Path("lecture-materials")
    notion_context_enabled: bool = True
    notion_context_cache_minutes: float = 30
    notion_context_max_chars: int = 18000
    notion_context_relations: tuple[str, ...] = ("Modules", "Syllabus", "Lecture materials", "Materials")
    retranscribe_low_confidence: bool = True
    retranscribe_max_segments: int = 6
    retranscribe_padding_seconds: float = 2.0


def load_config(env_file: Path) -> Config:
    env_file = env_file.resolve()
    values = {**dotenv_values(env_file), **os.environ}

    def get(name, default=""):
        return str(values.get(name) or default)

    def path(name, default):
        result = Path(get(name, default)).expanduser()
        return (env_file.parent / result).resolve() if not result.is_absolute() else result

    root = "C:/CourseAI/Lectures"
    mapping_file = path("COURSE_MAP", "courses.yaml")
    mapping = (
        yaml.safe_load(mapping_file.read_text(encoding="utf-8")) if mapping_file.exists() else {}
    )
    if not isinstance(mapping, dict):
        raise ValueError("COURSE_MAP must contain a YAML mapping")
    courses = {}
    for code, page in mapping.items():
        courses[str(code).upper()] = str(UUID(str(page)))
    active_course = get("ACTIVE_COURSE").upper().strip()
    if active_course and active_course not in courses:
        active_course = ""
    if not active_course and courses:
        active_course = sorted(courses)[0]
    config = Config(
        audio_enabled=get("GROQ_AUDIO_ENABLED", "false").lower() == "true",
        groq_audio_model=get("GROQ_AUDIO_MODEL", "whisper-large-v3"),
        audio_cache=path("AUDIO_CACHE_DIR", "audio-cache"),
        active_course=active_course,
        lecture_date=get("LECTURE_DATE"),
        groq_enabled=get("GROQ_ENABLED", "false").lower() == "true",
        groq_api_key=get("GROQ_API_KEY"),
        groq_model=get("GROQ_MODEL", "openai/gpt-oss-120b"),
        review_dir=path("REVIEW_DIR", "reviews"),
        context_dir=path("REVIEW_CONTEXT_DIR", "review-context"),
        materials_dir=path("LECTURE_MATERIAL_DIR", f"{root}/Materials"),
        notion_context_enabled=get("NOTION_REVIEW_CONTEXT", "true").lower() == "true",
        notion_context_cache_minutes=float(get("REVIEW_CONTEXT_CACHE_MINUTES", "30")),
        notion_context_max_chars=int(get("REVIEW_CONTEXT_MAX_CHARS", "18000")),
        notion_context_relations=tuple(
            item.strip()
            for item in get(
                "REVIEW_CONTEXT_RELATIONS",
                "Modules,Syllabus,Lecture materials,Materials",
            ).split(",")
            if item.strip()
        ),
        retranscribe_low_confidence=get(
            "RETRANSCRIBE_LOW_CONFIDENCE", "true"
        ).lower() == "true",
        retranscribe_max_segments=max(0, int(get("RETRANSCRIBE_MAX_SEGMENTS", "6"))),
        retranscribe_padding_seconds=max(
            0.0, float(get("RETRANSCRIBE_PADDING_SECONDS", "2"))
        ),
        token=get("NOTION_TOKEN"),
        database_id=get("NOTION_DATABASE_ID"),
        data_source_id=get("NOTION_DATA_SOURCE_ID"),
        transcripts=path("TRANSCRIPT_DIR", f"{root}/Transcripts"),
        audio=path("AUDIO_DIR", f"{root}/Audio Inbox"),
        archive=path("ARCHIVE_DIR", f"{root}/Archive"),
        state=path("STATE_DB", f"{root}/bridge-state.sqlite3"),
        log=path("LOG_FILE", f"{root}/bridge.log"),
        courses=courses,
        props={
            key: get(f"PROP_{key.upper()}", default)
            for key, default in {
                "lecture": "Lecture",
                "course": "Course",
                "date": "Date",
                "capture": "Capture method",
                "processing": "Processing",
                "local_path": "Local transcript path",
                "ingest_id": "Ingest ID",
                "needs_review": "Needs review",
                "review_flags": "Review flags",
                "review_flag_count": "Review flag count",
                "grounding_sources": "Grounding sources",
                "asr_model": "ASR model",
            }.items()
        },
        stable_seconds=float(get("STABLE_SECONDS", "5")),
        stable_timeout=float(get("STABLE_TIMEOUT", "300")),
        retry_seconds=float(get("RETRY_SECONDS", "30")),
        archive_audio=get("ARCHIVE_AUDIO", "false").lower() == "true",
    )
    if min(config.stable_seconds, config.retry_seconds) <= 0:
        raise ValueError("STABLE_SECONDS and RETRY_SECONDS must be positive")
    if config.stable_timeout <= config.stable_seconds:
        raise ValueError("STABLE_TIMEOUT must exceed STABLE_SECONDS")
    if len({config.transcripts, config.audio, config.archive}) != 3:
        raise ValueError("Transcript, audio, and archive folders must be distinct")
    if any(char.isspace() or not char.isascii() for char in config.token):
        raise ValueError("NOTION_TOKEN must not contain whitespace or non-ASCII characters")
    return config
