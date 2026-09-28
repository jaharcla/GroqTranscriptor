"""Reproducible ASR benchmarking for CourseAI lecture recordings."""

import hashlib
import json
import re
import subprocess
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import imageio_ffmpeg

from .files import Lecture
from .grounding import CourseGrounder
from .notion import Notion
from .transcribe import Transcriber, _quality_flags

DEFAULT_GROQ_MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")
DEFAULT_PROMPT_MODES = ("plain", "grounded")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "because",
    "but",
    "by",
    "do",
    "for",
    "from",
    "have",
    "he",
    "i",
    "if",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "our",
    "so",
    "that",
    "the",
    "their",
    "this",
    "to",
    "we",
    "what",
    "when",
    "with",
    "you",
    "your",
}


def _slug(value):
    return re.sub(r"[^a-z0-9._-]+", "-", str(value).lower()).strip("-") or "result"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def _duration_seconds(path):
    process = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", process.stderr)
    if not match:
        raise ValueError(f"Could not determine audio duration: {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def sample_windows(duration, seconds=75.0):
    """Return four representative windows without over-weighting the intro/outro."""
    seconds = max(15.0, float(seconds))
    if duration <= seconds * 1.25:
        return [(0.0, duration)]
    windows = []
    for fraction in (0.08, 0.35, 0.62, 0.86):
        center = duration * fraction
        start = max(0.0, min(duration - seconds, center - seconds / 2))
        item = (round(start, 3), min(seconds, duration - start))
        if not windows or abs(item[0] - windows[-1][0]) > 1:
            windows.append(item)
    return windows


def _extract_clip(source, target, start, duration):
    target.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
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
            str(target),
        ],
        capture_output=True,
        timeout=300,
    )
    if process.returncode:
        raise RuntimeError(f"Could not create benchmark sample at {start:.1f}s")


def _tokens(text):
    return re.findall(r"[a-z0-9']+", str(text).lower())


def _repeated_ngram_rate(tokens, width=3):
    if len(tokens) < width:
        return 0.0, []
    ngrams = [tuple(tokens[i : i + width]) for i in range(len(tokens) - width + 1)]
    counts = Counter(ngrams)
    excess = sum(count - 1 for count in counts.values() if count > 1)
    top = [
        (" ".join(ngram), count)
        for ngram, count in counts.most_common(8)
        if count > 1
    ]
    return excess / max(1, len(ngrams)), top


def _immediate_repeat_rate(tokens, max_width=6):
    if not tokens:
        return 0.0
    repeated = 0
    for index in range(len(tokens)):
        available = (len(tokens) - index) // 2
        for width in range(1, min(max_width, available) + 1):
            if tokens[index : index + width] == tokens[index + width : index + 2 * width]:
                repeated += width * 2
                break
    return min(1.0, repeated / len(tokens))


def transcript_metrics(text, quality_flags, audio_seconds, wall_seconds):
    tokens = _tokens(text)
    non_stop = [token for token in tokens if token not in STOPWORDS and len(token) > 1]
    repeated_rate, repeated = _repeated_ngram_rate(tokens)
    immediate_rate = _immediate_repeat_rate(tokens)
    non_stop_counts = Counter(non_stop)
    dominant_share = (
        non_stop_counts.most_common(1)[0][1] / len(non_stop)
        if non_stop
        else 0.0
    )
    low_confidence_seconds = sum(
        max(0.0, float(flag.get("end", 0)) - float(flag.get("start", 0)))
        for flag in quality_flags or []
        if isinstance(flag, dict)
    )
    low_confidence_ratio = min(
        1.0, low_confidence_seconds / max(0.001, float(audio_seconds))
    )
    # This score detects loops / suspicious repetition and ASR uncertainty. It is
    # deliberately not called an accuracy score; WER against a reference is stronger.
    artifact_score = 100 * (
        0.45 * repeated_rate
        + 0.25 * immediate_rate
        + 0.15 * min(1.0, dominant_share)
        + 0.15 * low_confidence_ratio
    )
    return {
        "audio_seconds": round(float(audio_seconds), 3),
        "wall_seconds": round(float(wall_seconds), 3),
        "realtime_factor": round(float(wall_seconds) / max(0.001, float(audio_seconds)), 4),
        "characters": len(text),
        "words": len(tokens),
        "unique_word_ratio": round(len(set(tokens)) / max(1, len(tokens)), 4),
        "quality_flag_count": len(quality_flags or []),
        "low_confidence_seconds": round(low_confidence_seconds, 3),
        "repeated_trigram_rate": round(repeated_rate, 4),
        "immediate_repeat_rate": round(immediate_rate, 4),
        "dominant_nonstopword_share": round(dominant_share, 4),
        "heuristic_artifact_score": round(artifact_score, 3),
        "top_repeated_trigrams": repeated,
        "top_nonstopwords": non_stop_counts.most_common(10),
    }


def _edit_distance(left, right):
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row, right_item in enumerate(right, start=1):
        current = [row]
        for column, left_item in enumerate(left, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_item != right_item),
                )
            )
        previous = current
    return previous[-1]


def reference_error_rates(reference, hypothesis):
    reference_words = _tokens(reference)
    hypothesis_words = _tokens(hypothesis)
    reference_chars = list(re.sub(r"\s+", " ", reference.lower()).strip())
    hypothesis_chars = list(re.sub(r"\s+", " ", hypothesis.lower()).strip())
    return {
        "wer": round(
            _edit_distance(reference_words, hypothesis_words)
            / max(1, len(reference_words)),
            4,
        ),
        "cer": round(
            _edit_distance(reference_chars, hypothesis_chars)
            / max(1, len(reference_chars)),
            4,
        ),
    }


def _grounded_prompt(config, source, course):
    if course not in config.courses:
        raise ValueError(f"Missing course mapping: {course}")
    lecture = Lecture(
        course,
        datetime.fromtimestamp(source.stat().st_mtime).date().isoformat(),
        source.stem,
    )
    notion = Notion(config)
    try:
        grounder = CourseGrounder(config, notion)
        context = grounder.context(lecture)
        prompt = grounder.asr_prompt(lecture, context)
    finally:
        notion.close()
    return prompt, {
        "course": course,
        "source_count": len(context.get("sources", [])),
        "source_titles": [
            item.get("title", "") for item in context.get("sources", []) if item.get("title")
        ],
        "warnings": context.get("warnings", []),
    }


def _sample_set(source, output, full, sample_seconds):
    duration = _duration_seconds(source)
    if full:
        return duration, [
            {
                "label": "full",
                "start": 0.0,
                "duration": duration,
                "path": source,
            }
        ]
    samples = []
    sample_dir = output / "samples"
    for index, (start, length) in enumerate(sample_windows(duration, sample_seconds), start=1):
        target = sample_dir / f"sample-{index:02d}-{int(start):05d}s.wav"
        if not target.exists():
            _extract_clip(source, target, start, length)
        samples.append(
            {
                "label": f"sample-{index:02d}",
                "start": start,
                "duration": length,
                "path": target,
            }
        )
    return duration, samples


def _merge_records(records):
    text = "\n\n".join(item["result"]["text"].strip() for item in records)
    flags = []
    for item in records:
        for flag in item["result"].get("quality_flags", []):
            shifted = dict(flag)
            shifted["start"] = round(float(flag.get("start", 0)) + item["start"], 3)
            shifted["end"] = round(float(flag.get("end", 0)) + item["start"], 3)
            flags.append(shifted)
    audio_seconds = sum(float(item["duration"]) for item in records)
    wall_seconds = sum(float(item["wall_seconds"]) for item in records)
    return text, flags, transcript_metrics(text, flags, audio_seconds, wall_seconds)


def _run_groq(config, model, mode, samples, prompt, output):
    run_config = replace(
        config,
        groq_audio_model=model,
        audio_cache=output / "cache" / _slug(f"groq-{model}-{mode}"),
        stable_seconds=0.02,
        stable_timeout=30,
        retranscribe_low_confidence=False,
    )
    transcriber = Transcriber(run_config)
    records = []
    selected_prompt = prompt if mode == "grounded" else ""
    for sample in samples:
        started = time.perf_counter()
        result = transcriber.transcribe_result(sample["path"], selected_prompt)
        records.append(
            {
                **sample,
                "path": str(sample["path"]),
                "wall_seconds": time.perf_counter() - started,
                "result": result,
            }
        )
    text, flags, metrics = _merge_records(records)
    return {
        "backend": "groq",
        "model": model,
        "prompt_mode": mode,
        "prompt": selected_prompt,
        "records": records,
        "text": text,
        "quality_flags": flags,
        "metrics": metrics,
    }


def _run_faster_whisper(model_name, mode, samples, prompt):
    try:
        import ctranslate2
        from faster_whisper import WhisperModel
    except ImportError:
        return {
            "backend": "faster-whisper",
            "model": model_name,
            "prompt_mode": mode,
            "skipped": True,
            "reason": (
                "faster-whisper is not installed. Install CourseAI's local-asr extra "
                "to benchmark the local alternative."
            ),
        }

    cuda = ctranslate2.get_cuda_device_count() > 0
    device = "cuda" if cuda else "cpu"
    compute_type = "float16" if cuda else "int8"
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
    except Exception as exc:
        if not cuda:
            return {
                "backend": "faster-whisper",
                "model": model_name,
                "prompt_mode": mode,
                "skipped": True,
                "reason": f"Local model could not start: {exc}",
            }
        device, compute_type = "cpu", "int8"
        try:
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
        except Exception as cpu_exc:
            return {
                "backend": "faster-whisper",
                "model": model_name,
                "prompt_mode": mode,
                "skipped": True,
                "reason": f"CUDA and CPU local model startup failed: {cpu_exc}",
            }
    selected_prompt = prompt if mode == "grounded" else None
    records = []
    for sample in samples:
        started = time.perf_counter()
        segments, info = model.transcribe(
            str(sample["path"]),
            language="en",
            beam_size=5,
            vad_filter=True,
            word_timestamps=True,
            initial_prompt=selected_prompt,
        )
        parsed = []
        words = []
        for segment in segments:
            parsed.append(
                {
                    "start": float(segment.start),
                    "end": float(segment.end),
                    "text": segment.text,
                    "avg_logprob": getattr(segment, "avg_logprob", None),
                    "no_speech_prob": getattr(segment, "no_speech_prob", None),
                    "compression_ratio": getattr(segment, "compression_ratio", None),
                }
            )
            for word in getattr(segment, "words", []) or []:
                words.append(
                    {
                        "word": word.word,
                        "start": float(word.start),
                        "end": float(word.end),
                        "probability": getattr(word, "probability", None),
                    }
                )
        result = {
            "text": "".join(item["text"] for item in parsed).strip(),
            "segments": parsed,
            "words": words,
            "quality_flags": _quality_flags(parsed),
            "language_probability": getattr(info, "language_probability", None),
        }
        records.append(
            {
                **sample,
                "path": str(sample["path"]),
                "wall_seconds": time.perf_counter() - started,
                "result": result,
            }
        )
    text, flags, metrics = _merge_records(records)
    return {
        "backend": "faster-whisper",
        "model": model_name,
        "prompt_mode": mode,
        "device": device,
        "compute_type": compute_type,
        "prompt": selected_prompt or "",
        "records": records,
        "text": text,
        "quality_flags": flags,
        "metrics": metrics,
    }


def _write_result_files(output, results):
    for item in results:
        if item.get("skipped"):
            continue
        stem = _slug(f"{item['backend']}-{item['model']}-{item['prompt_mode']}")
        (output / f"{stem}.txt").write_text(item["text"] + "\n", encoding="utf-8")
        (output / f"{stem}.json").write_text(
            json.dumps(item, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _markdown_report(summary):
    lines = [
        "# CourseAI ASR Benchmark",
        "",
        f"- Source: `{summary['source']}`",
        f"- SHA-256: `{summary['source_sha256']}`",
        f"- Course: **{summary['course']}**",
        f"- Source duration: {summary['source_duration_seconds']:.1f}s",
        f"- Benchmarked audio per condition: {summary['benchmarked_audio_seconds']:.1f}s",
        f"- Mode: {'full recording' if summary['full'] else 'representative samples'}",
        f"- Grounding sources: {summary['grounding']['source_count']}",
        "",
        "The artifact score is a heuristic for loops, repeated phrases, dominant odd tokens, "
        "and low-confidence spans. It is **not** an accuracy metric. WER/CER against a corrected "
        "reference should decide the final model when available.",
        "",
        "| Backend | Model | Prompt | Wall s | RTF | Flags | Repeat % | Artifact | WER | CER |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary["results"]:
        if item.get("skipped"):
            lines.append(
                f"| {item['backend']} | {item['model']} | {item['prompt_mode']} | "
                f"skipped | - | - | - | - | - | - |"
            )
            continue
        metrics = item["metrics"]
        lines.append(
            "| {backend} | {model} | {prompt} | {wall:.1f} | {rtf:.3f} | {flags} | "
            "{repeat:.2f} | {artifact:.2f} | {wer} | {cer} |".format(
                backend=item["backend"],
                model=item["model"],
                prompt=item["prompt_mode"],
                wall=metrics["wall_seconds"],
                rtf=metrics["realtime_factor"],
                flags=metrics["quality_flag_count"],
                repeat=metrics["repeated_trigram_rate"] * 100,
                artifact=metrics["heuristic_artifact_score"],
                wer=metrics.get("wer", "-"),
                cer=metrics.get("cer", "-"),
            )
        )
    lines.extend(["", "## Provisional ordering", ""])
    for index, item in enumerate(summary.get("provisional_order", []), start=1):
        lines.append(
            f"{index}. **{item['name']}** — {item['basis']} = {item['value']}"
        )
    lines.extend(["", "## Grounding", ""])
    if summary["grounding"]["warnings"]:
        for warning in summary["grounding"]["warnings"]:
            lines.append(f"- Warning: {warning}")
    else:
        lines.append("- No grounding warnings.")
    titles = []
    for title in summary["grounding"]["source_titles"]:
        if title not in titles:
            titles.append(title)
    for title in titles[:20]:
        lines.append(f"- {title}")
    return "\n".join(lines) + "\n"


def benchmark_asr(
    config,
    source,
    course,
    *,
    models=DEFAULT_GROQ_MODELS,
    prompt_modes=DEFAULT_PROMPT_MODES,
    full=False,
    sample_seconds=75.0,
    local_model=None,
    reference=None,
    output=None,
):
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    course = str(course).upper().strip()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = (
        Path(output).resolve()
        if output
        else (config.state.parent / "Benchmarks" / f"{timestamp}-{_slug(source.stem)}").resolve()
    )
    output.mkdir(parents=True, exist_ok=True)

    grounded_prompt, grounding = _grounded_prompt(config, source, course)
    source_duration, samples = _sample_set(source, output, full, sample_seconds)
    results = []
    for model in models:
        for mode in prompt_modes:
            results.append(
                _run_groq(config, model, mode, samples, grounded_prompt, output)
            )
    if local_model:
        for mode in prompt_modes:
            results.append(
                _run_faster_whisper(local_model, mode, samples, grounded_prompt)
            )

    reference_text = None
    if reference:
        reference_path = Path(reference).resolve()
        reference_text = reference_path.read_text(encoding="utf-8-sig")
        for item in results:
            if item.get("skipped"):
                continue
            item["metrics"].update(reference_error_rates(reference_text, item["text"]))

    candidates = [item for item in results if not item.get("skipped")]
    if reference_text:
        ordered = sorted(candidates, key=lambda item: item["metrics"]["wer"])
        provisional = [
            {
                "name": f"{item['backend']} / {item['model']} / {item['prompt_mode']}",
                "basis": "WER",
                "value": item["metrics"]["wer"],
            }
            for item in ordered
        ]
    else:
        ordered = sorted(
            candidates,
            key=lambda item: item["metrics"]["heuristic_artifact_score"],
        )
        provisional = [
            {
                "name": f"{item['backend']} / {item['model']} / {item['prompt_mode']}",
                "basis": "heuristic artifact score",
                "value": item["metrics"]["heuristic_artifact_score"],
            }
            for item in ordered
        ]

    benchmarked_seconds = sum(float(item["duration"]) for item in samples)
    summary = {
        "schema_version": 1,
        "source": str(source),
        "source_sha256": _sha256(source),
        "course": course,
        "source_duration_seconds": source_duration,
        "benchmarked_audio_seconds": benchmarked_seconds,
        "full": bool(full),
        "sample_seconds": float(sample_seconds),
        "samples": [
            {
                "label": item["label"],
                "start": item["start"],
                "duration": item["duration"],
                "path": str(item["path"]),
            }
            for item in samples
        ],
        "grounding": grounding,
        "reference": str(Path(reference).resolve()) if reference else None,
        "results": results,
        "provisional_order": provisional,
    }
    _write_result_files(output, results)
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "summary.md").write_text(_markdown_report(summary), encoding="utf-8")
    return output, summary
