import wave
from unittest.mock import Mock

import pytest

from courseai_lectures.bridge import Bridge
from courseai_lectures.state import State
from courseai_lectures.transcribe import Transcriber
from courseai_lectures.watcher import Pending


def config_audio(config):
    config.audio_enabled = True
    config.groq_api_key = "test-key"
    config.groq_audio_model = "whisper-large-v3-turbo"
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


def test_real_decoder_chunking_and_cache(config):
    config_audio(config)
    source = config.audio / "random.wav"
    wav(source, 481)
    transcriber = Transcriber(config)
    sizes = []

    def request(path):
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
