"""Groq audio transcription with grounded prompting, overlap, metadata, and retry caches."""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import wave
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
CHUNK_SECONDS = 480
OVERLAP_SECONDS = 5
SAMPLE_RATE = 16000
log = logging.getLogger(__name__)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _response(value):
    if isinstance(value, str):
        return {"text": value, "segments": [], "words": []}
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise ValueError("Groq audio response has no valid transcript")
    segments = value.get("segments", [])
    words = value.get("words", [])
    if not isinstance(segments, list):
        segments = []
    if not isinstance(words, list):
        words = []
    return {"text": value["text"], "segments": segments, "words": words}


def _quality_flags(segments):
    flags = []
    for segment in segments:
        start = float(segment.get("start", 0))
        end = float(segment.get("end", start))
        avg_logprob = segment.get("avg_logprob")
        no_speech = segment.get("no_speech_prob")
        compression = segment.get("compression_ratio")
        reasons = []
        if isinstance(avg_logprob, (int, float)) and avg_logprob <= -0.5:
            reasons.append(f"avg_logprob={avg_logprob:.2f}")
        if isinstance(no_speech, (int, float)) and no_speech >= 0.6:
            reasons.append(f"no_speech_prob={no_speech:.2f}")
        if isinstance(compression, (int, float)) and compression >= 2.4:
            reasons.append(f"compression_ratio={compression:.2f}")
        if reasons:
            preview = " ".join(str(segment.get("text", "")).split())[:160]
            flags.append(
                {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "reasons": reasons,
                    "text": preview,
                }
            )
    return flags


def _write_chunks(prepared, directory):
    with wave.open(str(prepared), "rb") as source:
        if (
            source.getnchannels() != 1
            or source.getsampwidth() != 2
            or source.getframerate() != SAMPLE_RATE
        ):
            raise ValueError("Prepared audio is not 16 kHz mono PCM")
        total = source.getnframes()
        chunk_frames = CHUNK_SECONDS * SAMPLE_RATE
        overlap_frames = OVERLAP_SECONDS * SAMPLE_RATE
        step = chunk_frames - overlap_frames
        chunks = []
        start = 0
        index = 0
        while start < total:
            source.setpos(start)
            data = source.readframes(min(chunk_frames, total - start))
            if not data:
                break
            path = directory / f"chunk-{index:05d}.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(SAMPLE_RATE)
                output.writeframes(data)
            chunks.append((path, start / SAMPLE_RATE))
            if start + chunk_frames >= total:
                break
            start += step
            index += 1
        return chunks


def _write_clip(prepared, target, start_seconds, end_seconds):
    """Extract a short PCM clip without invoking another decoder process."""
    with wave.open(str(prepared), "rb") as source:
        total = source.getnframes()
        start = max(0, min(total, int(start_seconds * SAMPLE_RATE)))
        end = max(start + 1, min(total, int(end_seconds * SAMPLE_RATE)))
        source.setpos(start)
        data = source.readframes(end - start)
    with wave.open(str(target), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(data)


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

    def request(self, path, prompt=""):
        if path.stat().st_size >= 25_000_000:
            raise ValueError("Prepared audio chunk exceeds the upload limit")
        data = [
            ("model", self.config.groq_audio_model),
            ("language", "en"),
            ("response_format", "verbose_json"),
            ("temperature", "0"),
            ("timestamp_granularities[]", "segment"),
            ("timestamp_granularities[]", "word"),
        ]
        if prompt.strip():
            data.append(("prompt", prompt.strip()[:900]))
        try:
            with path.open("rb") as audio, httpx.Client(timeout=180) as client:
                response = client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.config.groq_api_key}"},
                    data=data,
                    files={"file": (path.name, audio, "audio/wav")},
                )
        except httpx.HTTPError:
            raise RuntimeError("Groq audio connection failed; queued for retry") from None
        if response.is_error:
            raise RuntimeError(f"Groq audio HTTP {response.status_code}; queued for retry")
        try:
            body = response.json()
        except ValueError:
            raise ValueError("Groq audio response is not valid JSON") from None
        return _response(body)

    def _repair_candidates(self, prepared, cache, quality_flags, prompt, temp):
        """Attach a second ASR hypothesis for low-confidence regions.

        The primary transcript is never overwritten here. These short-clip candidates
        are evidence for the grounded reviewer and remain auditable in transcript.json.
        """
        if not quality_flags or not getattr(
            self.config, "retranscribe_low_confidence", True
        ):
            return quality_flags

        limit = max(0, int(getattr(self.config, "retranscribe_max_segments", 6)))
        padding = max(
            0.0, float(getattr(self.config, "retranscribe_padding_seconds", 2.0))
        )
        repaired = []
        for index, flag in enumerate(quality_flags):
            item = dict(flag)
            if index >= limit:
                repaired.append(item)
                continue
            start = max(0.0, float(flag.get("start", 0)) - padding)
            end = max(start + 0.25, float(flag.get("end", start)) + padding)
            candidate_cache = cache / (
                f"repair-{index:03d}-{int(start * 1000)}-{int(end * 1000)}.json"
            )
            if candidate_cache.exists():
                candidate = _response(
                    json.loads(candidate_cache.read_text(encoding="utf-8"))
                )
            else:
                clip = temp / f"repair-{index:03d}.wav"
                _write_clip(prepared, clip, start, end)
                repair_prompt = (
                    prompt
                    + " Short uncertain lecture excerpt. Preserve the exact spoken wording "
                    "and use course terminology only when the audio supports it."
                )[:900]
                candidate = _response(self.request(clip, repair_prompt))
                atomic_json(candidate_cache, candidate)
            candidate_text = candidate["text"].strip()
            if candidate_text:
                item["retranscription"] = {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "text": candidate_text,
                    "segments": candidate.get("segments", []),
                    "words": candidate.get("words", []),
                }
            repaired.append(item)
        return repaired

    def transcribe_result(self, path, prompt=""):
        if not self.config.groq_api_key:
            raise ValueError("Set GROQ_API_KEY in .env")
        before = self.wait_stable(path)
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
            digest.update(
                (
                    self.config.groq_audio_model
                    + "|en|480|overlap5|verbose-segments-words|"
                    + prompt
                    + f"|repair={getattr(self.config, 'retranscribe_low_confidence', True)}"
                    + f"|repair-max={getattr(self.config, 'retranscribe_max_segments', 6)}"
                    + "|v3"
                ).encode()
            )
            cache = self.config.audio_cache / digest.hexdigest()
            final = cache / "transcript.json"
            if final.exists():
                return json.loads(final.read_text(encoding="utf-8"))

            log.info("Preparing saved audio: %s", path.name)
            prepared = temp / "prepared.wav"
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
                    str(SAMPLE_RATE),
                    "-c:a",
                    "pcm_s16le",
                    str(prepared),
                ],
                capture_output=True,
                timeout=1800,
            )
            if process.returncode:
                raise RuntimeError("Cannot decode this audio; ensure the recording is fully saved")

            chunks = _write_chunks(prepared, temp)
            if not chunks:
                raise ValueError("No audio track found")

            texts = []
            segments = []
            words = []
            previous_text = ""
            for index, (chunk, offset) in enumerate(chunks):
                item = cache / f"chunk-{index:05d}.json"
                if item.exists():
                    result = _response(json.loads(item.read_text(encoding="utf-8")))
                else:
                    log.info("Groq transcribing chunk %d/%d: %s", index + 1, len(chunks), path.name)
                    continuity = (
                        f" Previous transcript ending: {previous_text[-280:]}"
                        if previous_text
                        else ""
                    )
                    result = _response(self.request(chunk, (prompt + continuity)[:900]))
                    atomic_json(item, result)

                kept = []
                for segment in result["segments"]:
                    local_start = float(segment.get("start", 0))
                    if index and local_start < OVERLAP_SECONDS:
                        continue
                    shifted = dict(segment)
                    shifted["start"] = round(float(segment.get("start", 0)) + offset, 3)
                    shifted["end"] = round(float(segment.get("end", 0)) + offset, 3)
                    kept.append(shifted)
                kept_words = []
                for word in result.get("words", []):
                    local_start = float(word.get("start", 0))
                    if index and local_start < OVERLAP_SECONDS:
                        continue
                    shifted_word = dict(word)
                    shifted_word["start"] = round(local_start + offset, 3)
                    shifted_word["end"] = round(
                        float(word.get("end", local_start)) + offset, 3
                    )
                    kept_words.append(shifted_word)
                words.extend(kept_words)

                if kept:
                    chunk_text = "".join(
                        str(segment.get("text", "")) for segment in kept
                    ).strip()
                    segments.extend(kept)
                else:
                    chunk_text = result["text"].strip()
                if chunk_text:
                    texts.append(chunk_text)
                    previous_text = chunk_text

            text = "\n\n".join(texts)
            if not text.strip():
                raise ValueError("No speech transcribed; check microphone and recording")
            quality_flags = _quality_flags(segments)
            quality_flags = self._repair_candidates(
                prepared,
                cache,
                quality_flags,
                prompt,
                temp,
            )
            result = {
                "schema_version": 3,
                "text": text,
                "model": self.config.groq_audio_model,
                "source_name": path.name,
                "chunks": len(chunks),
                "overlap_seconds": OVERLAP_SECONDS,
                "segments": segments,
                "words": words,
                "quality_flags": quality_flags,
            }
            atomic_json(final, result)
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "raw-transcript.txt").write_text(text, encoding="utf-8")
            return result

    def transcribe(self, path, prompt=""):
        return self.transcribe_result(path, prompt)["text"]
