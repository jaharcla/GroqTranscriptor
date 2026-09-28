"""Groq audio transcription with local decoding and per-chunk retry caches."""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import imageio_ffmpeg

from .files import signature

AUDIO_TYPES = {
    ".flac",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".m4a",
    ".ogg",
    ".wav",
    ".webm",
    ".aac",
    ".wma",
}
log = logging.getLogger(__name__)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


class Transcriber:
    def __init__(self, config):
        self.config = config

    def check(self):
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-version"],
            check=True,
            capture_output=True,
            timeout=15,
        )
        with httpx.Client(timeout=30) as client:
            response = client.get(
                f"https://api.groq.com/openai/v1/models/{self.config.groq_audio_model}",
                headers={"Authorization": f"Bearer {self.config.groq_api_key}"},
            )
        if response.is_error:
            raise RuntimeError(f"Groq audio model check HTTP {response.status_code}")

    def wait_stable(self, path):
        started = changed = time.monotonic()
        previous = None
        while time.monotonic() - started < self.config.stable_timeout:
            current = signature(path)
            if current != previous:
                previous, changed = current, time.monotonic()
            elif current[0] > 0 and time.monotonic() - changed >= self.config.stable_seconds:
                return current
            time.sleep(min(0.5, self.config.stable_seconds / 2))
        raise TimeoutError("Audio is empty or still changing; save the completed recording first")

    def request(self, path):
        if path.stat().st_size >= 25_000_000:
            raise ValueError("Prepared audio chunk exceeds the upload limit")
        try:
            with path.open("rb") as audio, httpx.Client(timeout=180) as client:
                response = client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.config.groq_api_key}"},
                    data={
                        "model": self.config.groq_audio_model,
                        "language": "en",
                        "response_format": "json",
                        "temperature": "0",
                    },
                    files={"file": (path.name, audio, "audio/wav")},
                )
        except httpx.HTTPError:
            raise RuntimeError("Groq audio connection failed; queued for retry") from None
        if response.is_error:
            raise RuntimeError(f"Groq audio HTTP {response.status_code}; queued for retry")
        try:
            text = response.json()["text"]
            if not isinstance(text, str):
                raise ValueError()
            return text
        except (ValueError, KeyError, TypeError):
            raise ValueError("Groq audio response has no valid transcript") from None

    def transcribe(self, path):
        if not self.config.groq_api_key:
            raise ValueError("Set GROQ_API_KEY in .env")
        before = self.wait_stable(path)
        # Work on a snapshot and reject changes made while copying the source.
        with tempfile.TemporaryDirectory(prefix="courseai-audio-") as temp:
            temp = Path(temp)
            source = temp / ("source" + path.suffix.lower())
            shutil.copyfile(path, source)
            if signature(path) != before:
                raise RuntimeError("Audio changed while reading; retry after recording is saved")
            digest = hashlib.sha256()
            with source.open("rb") as handle:
                for part in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(part)
            digest.update((self.config.groq_audio_model + "|en|480|v1").encode())
            cache = self.config.audio_cache / digest.hexdigest()
            final = cache / "transcript.json"
            if final.exists():
                return json.loads(final.read_text(encoding="utf-8"))["text"]
            log.info("Preparing saved audio: %s", path.name)
            process = subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "segment",
                    "-segment_time",
                    "480",
                    "-reset_timestamps",
                    "1",
                    str(temp / "chunk-%05d.wav"),
                ],
                capture_output=True,
                timeout=1800,
            )
            if process.returncode:
                raise RuntimeError("Cannot decode this audio; ensure the recording is fully saved")
            chunks = sorted(temp.glob("chunk-*.wav"))
            if not chunks:
                raise ValueError("No audio track found")
            texts = []
            for index, chunk in enumerate(chunks):
                item = cache / f"chunk-{index:05d}.json"
                if item.exists():
                    text = json.loads(item.read_text(encoding="utf-8"))["text"]
                else:
                    log.info("Groq transcribing chunk %d/%d: %s", index + 1, len(chunks), path.name)
                    text = self.request(chunk)
                    atomic_json(item, {"text": text})
                texts.append(text)
            text = "\n\n".join(texts)
            if not text.strip():
                raise ValueError("No speech transcribed; check microphone and recording")
            atomic_json(
                final,
                {
                    "text": text,
                    "model": self.config.groq_audio_model,
                    "source_name": path.name,
                    "chunks": len(chunks),
                },
            )
            # Kept outside the TXT watcher so this does not create a second ingestion.
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "raw-transcript.txt").write_text(text, encoding="utf-8")
            return text
