import wave
from unittest.mock import Mock

import httpx
import pytest

from courseai_lectures.bridge import Bridge
from courseai_lectures.state import State
from courseai_lectures.transcribe import Transcriber
from courseai_lectures.watcher import Pending


def config_audio(config):
    config.audio_enabled = True
    config.groq_api_key = "test-key"
    config.groq_audio_model = "whisper-large-v3-turbo"
    config.asr_grounded_prompt = False
    config.retranscribe_low_confidence = False
    config.retranscribe_max_segments = 6
    config.retranscribe_padding_seconds = 2.0
    config.audio_cache = config.state.parent / "audio-cache"
    config.active_course = "KIN120"
    config.lecture_date = "2026-09-28"
    return config


def wav(path, seconds=1):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        for _ in range(seconds):
            handle.writeframes(b"\0\0" * 16000)


def test_request_uses_segment_timestamps_and_safe_prompt_limit(config, monkeypatch):
    config_audio(config)
    source = config.audio / "tiny.wav"
    wav(source)

    seen = {}

    def handle(request):
        body = request.read()
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "text": "hello",
                "segments": [],
                "words": [{"word": "hello", "start": 0.0, "end": 0.4}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    result = Transcriber(config).request(source, "x" * 1000)

    assert result["text"] == "hello"
    assert seen["body"].count(b'name="timestamp_granularities[]"') == 1
    assert b"segment" in seen["body"]
    assert b"word" not in seen["body"]
    assert b"x" * 600 in seen["body"]
    assert b"x" * 601 not in seen["body"]


def test_groq_audio_error_surfaces_safe_server_message(config, monkeypatch):
    config_audio(config)
    source = config.audio / "tiny.wav"
    wav(source)

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                json={"error": {"message": "prompt must be 224 tokens or less"}},
            )
        )
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)

    with pytest.raises(RuntimeError, match="prompt must be 224 tokens or less"):
        Transcriber(config).request(source, "course terms")


def test_real_decoder_chunking_and_cache(config):
    config_audio(config)
    source = config.audio / "random.wav"
    wav(source, 481)
    transcriber = Transcriber(config)
    sizes = []

    def request(path, prompt=""):
        sizes.append(path.stat().st_size)
        return f"Chunk {len(sizes)}"

    transcriber.request = Mock(side_effect=request)
    assert transcriber.transcribe(source) == "Chunk 1\n\nChunk 2"
    assert len(sizes) == 2 and max(sizes) < 25_000_000
    assert transcriber.transcribe(source) == "Chunk 1\n\nChunk 2"
    assert transcriber.request.call_count == 2
    assert source.exists()


def test_successful_chunk_is_not_reuploaded_after_failure(config):
    config_audio(config)
    source = config.audio / "random.wav"
    wav(source, 481)
    transcriber = Transcriber(config)
    transcriber.request = Mock(side_effect=["First", RuntimeError("429")])
    with pytest.raises(RuntimeError):
        transcriber.transcribe(source)
    transcriber.request = Mock(return_value="Second")
    assert transcriber.transcribe(source) == "First\n\nSecond"
    assert transcriber.request.call_count == 1


def test_bridge_keeps_course_context_out_of_asr_by_default(config):
    config_audio(config)
    source = config.audio / "random.wav"
    wav(source)
    state, notion = State(config.state), Mock()
    notion.sync.return_value = "page"
    bridge = Bridge(config, state, notion)
    bridge.grounder.asr_prompt = Mock(return_value="CHEM120 quantum terminology")
    bridge.transcriber.request = Mock(return_value="A lecture about vectors.")

    try:
        assert bridge.process(source)
        bridge.grounder.asr_prompt.assert_not_called()
        assert bridge.transcriber.request.call_args.args[1] == ""
    finally:
        state.close()


def test_bridge_can_enable_grounded_asr_prompt(config):
    config_audio(config)
    config.asr_grounded_prompt = True
    source = config.audio / "random.wav"
    wav(source)
    state, notion = State(config.state), Mock()
    notion.sync.return_value = "page"
    bridge = Bridge(config, state, notion)
    bridge.grounder.asr_prompt = Mock(return_value="KIN120 vector terminology")
    bridge.transcriber.request = Mock(return_value="A lecture about vectors.")

    try:
        assert bridge.process(source)
        bridge.grounder.asr_prompt.assert_called_once()
        assert bridge.transcriber.request.call_args.args[1] == "KIN120 vector terminology"
    finally:
        state.close()


def test_audio_bridge_imports_once(config):
    config_audio(config)
    source = config.audio / "random.wav"
    wav(source)
    state, notion = State(config.state), Mock()
    notion.sync.return_value = "page"
    bridge = Bridge(config, state, notion)
    bridge.transcriber.request = Mock(return_value="A lecture about vectors.")
    try:
        assert bridge.process(source)
        assert bridge.process(source)
        assert bridge.transcriber.request.call_count == 1
        assert notion.sync.call_count == 1
        assert source.exists()
        assert notion.sync.call_args.args[0].course == "KIN120"
    finally:
        state.close()


def test_audio_failure_never_uploads_empty_page(config):
    config_audio(config)
    source = config.audio / "random.wav"
    wav(source)
    state, notion = State(config.state), Mock()
    bridge = Bridge(config, state, notion)
    bridge.transcriber.request = Mock(side_effect=RuntimeError("Groq audio HTTP 401"))
    try:
        assert not bridge.process(source)
        notion.sync.assert_not_called()
        assert state.get(source)["status"] == "failed"
    finally:
        state.close()


def test_watcher_accepts_audio_when_enabled(tmp_path):
    from courseai_lectures.transcribe import AUDIO_TYPES

    pending = Pending(AUDIO_TYPES | {".txt"})
    path = tmp_path / "random.M4A"
    pending.add(path)
    assert path.resolve() in pending.take()


def test_verbose_segments_are_shifted_and_low_confidence_is_flagged(config):
    config_audio(config)
    source = config.audio / "random.wav"
    wav(source, 481)
    transcriber = Transcriber(config)
    transcriber.request = Mock(
        side_effect=[
            {
                "text": "First chunk",
                "segments": [
                    {
                        "start": 470.0,
                        "end": 476.0,
                        "text": " first boundary",
                        "avg_logprob": -0.2,
                        "no_speech_prob": 0.01,
                        "compression_ratio": 1.2,
                    }
                ],
            },
            {
                "text": "Duplicate boundary then second",
                "segments": [
                    {
                        "start": 1.0,
                        "end": 4.0,
                        "text": " duplicate boundary",
                        "avg_logprob": -0.2,
                        "no_speech_prob": 0.01,
                        "compression_ratio": 1.2,
                    },
                    {
                        "start": 5.2,
                        "end": 5.8,
                        "text": " second",
                        "avg_logprob": -0.8,
                        "no_speech_prob": 0.01,
                        "compression_ratio": 1.2,
                    },
                ],
            },
        ]
    )

    result = transcriber.transcribe_result(source, "course terms")

    assert "duplicate boundary" not in result["text"]
    assert "second" in result["text"]
    assert result["segments"][-1]["start"] > 480
    assert result["quality_flags"]
    assert "avg_logprob" in result["quality_flags"][0]["reasons"][0]


def test_low_confidence_segment_gets_cached_retranscription_candidate(config):
    config_audio(config)
    config.retranscribe_low_confidence = True
    config.retranscribe_max_segments = 1
    source = config.audio / "uncertain.wav"
    wav(source)
    transcriber = Transcriber(config)
    transcriber.request = Mock(
        side_effect=[
            {
                "text": "blood quality of the microplastics",
                "segments": [
                    {
                        "start": 0.1,
                        "end": 0.8,
                        "text": " blood quality of the microplastics",
                        "avg_logprob": -0.9,
                        "no_speech_prob": 0.01,
                        "compression_ratio": 1.1,
                    }
                ],
                "words": [
                    {"word": "blood", "start": 0.1, "end": 0.2},
                ],
            },
            {
                "text": "blood volume in the microvasculature",
                "segments": [],
                "words": [],
            },
        ]
    )

    result = transcriber.transcribe_result(source, "cardiovascular physiology")

    assert result["text"] == "blood quality of the microplastics"
    candidate = result["quality_flags"][0]["retranscription"]
    assert candidate["text"] == "blood volume in the microvasculature"
    assert transcriber.request.call_count == 2

    cached = transcriber.transcribe_result(source, "cardiovascular physiology")
    assert cached["quality_flags"][0]["retranscription"]["text"] == candidate["text"]
    assert transcriber.request.call_count == 2
