import json
from unittest.mock import Mock

import pytest

from courseai_lectures.bridge import Bridge
from courseai_lectures.files import Lecture
from courseai_lectures.review import Reviewer, apply_corrections, normalize_review_result
from courseai_lectures.state import State


def enable(config):
    config.groq_enabled = True
    config.groq_api_key = "test-groq"
    config.groq_model = "openai/gpt-oss-120b"
    config.review_dir = config.state.parent / "reviews"
    config.context_dir = config.state.parent / "context"
    return config


def correction(original, replacement="fixed", reason="test", evidence=None):
    return {
        "original": original,
        "replacement": replacement,
        "reason": reason,
        "evidence": evidence or ["transcript"],
    }


def test_corrections_preserve_other_characters():
    result = {
        "corrections": [
            {
                "original": "new tons",
                "replacement": "newtons",
                "reason": "force unit",
                "evidence": ["transcript"],
            }
        ],
        "flags": ["Check magnitude"],
    }
    assert apply_corrections("Force: 3 new tons.\n", result) == "Force: 3 newtons.\n"


@pytest.mark.parametrize("old", ["absent", "a"])
def test_apply_corrections_remains_strict_for_invalid_anchor(old):
    with pytest.raises(ValueError):
        apply_corrections(
            "a a",
            {"corrections": [correction(old, "b")], "flags": []},
        )


def test_review_normalization_skips_missing_and_ambiguous_anchors():
    raw = "alpha beta beta gamma"
    result = {
        "corrections": [
            correction("missing", "present"),
            correction("beta", "delta"),
            correction("alpha", "ALPHA"),
        ],
        "flags": [],
    }

    normalized = normalize_review_result(raw, result)

    assert normalized["corrections"] == [correction("alpha", "ALPHA")]
    assert len(normalized["flags"]) == 2
    assert "not found" in normalized["flags"][0]
    assert "ambiguous" in normalized["flags"][1]
    assert apply_corrections(raw, normalized) == "ALPHA beta beta gamma"


def test_review_normalization_skips_overlap_and_pathological_expansion():
    raw = "The force was three new tons in this example."
    result = {
        "corrections": [
            correction("three new tons", "three newtons"),
            correction("new tons", "newtons"),
            correction("force", "x" * 500),
        ],
        "flags": [],
    }

    normalized = normalize_review_result(raw, result)

    assert normalized["corrections"] == [correction("three new tons", "three newtons")]
    assert any("overlapped" in flag for flag in normalized["flags"])
    assert any("disproportionately" in flag for flag in normalized["flags"])


def test_review_cache_and_raw_retention(config):
    reviewer = Reviewer(enable(config))
    reviewer.call = Mock(return_value={"corrections": [], "flags": ["Check equation"]})
    raw = "A" * 17001
    lecture = Lecture("KIN120", "2026-09-28", "Lecture")
    output = reviewer.review(raw, lecture)
    assert reviewer.call.call_count == 3
    assert output.endswith(raw)
    assert raw in output.split("CORRECTIONS AND REVIEW FLAGS")[0]
    assert reviewer.review(raw, lecture) == output
    assert reviewer.call.call_count == 3
    assert reviewer.digest(raw, lecture) != reviewer.digest(raw + "B", lecture)


def test_successful_review_chunks_survive_later_failure(config):
    reviewer = Reviewer(enable(config))
    lecture = Lecture("KIN120", "2026-09-28", "Lecture")
    raw = "A" * 17001
    reviewer.call = Mock(
        side_effect=[
            {"corrections": [], "flags": ["part 1"]},
            {"corrections": [], "flags": ["part 2"]},
            RuntimeError("Groq HTTP 429"),
        ]
    )

    with pytest.raises(RuntimeError, match="429"):
        reviewer.review(raw, lecture)
    assert reviewer.call.call_count == 3

    reviewer.call = Mock(return_value={"corrections": [], "flags": ["part 3"]})
    output = reviewer.review(raw, lecture)

    assert reviewer.call.call_count == 1
    assert '"part 1"' in output
    assert '"part 2"' in output
    assert '"part 3"' in output


def test_bad_model_anchor_is_flagged_instead_of_failing_review(config):
    reviewer = Reviewer(enable(config))
    lecture = Lecture("KIN120", "2026-09-28", "Lecture")
    raw = "Vectors can be represented by magnitude and direction."
    reviewer.call = Mock(
        return_value={
            "corrections": [
                correction(
                    "this anchor does not exist",
                    "corrected wording",
                    "model guessed an anchor",
                )
            ],
            "flags": [],
        }
    )

    output = reviewer.review(raw, lecture)

    reviewed = output.split("REVIEWED TRANSCRIPT\n", 1)[1].split(
        "\n\nCORRECTIONS, REFERENCES, ASR QUALITY AND REVIEW FLAGS", 1
    )[0]
    assert reviewed == raw
    assert "Skipped model correction because its anchor was not found" in output


def test_failure_does_not_upload_or_mark_done(config):
    enable(config)
    path = config.transcripts / "KIN120_2026-09-28_Lecture.txt"
    path.write_text("Original transcript")
    state = State(config.state)
    notion = Mock()
    bridge = Bridge(config, state, notion)
    bridge.reviewer.call = Mock(side_effect=RuntimeError("Groq HTTP 429"))
    try:
        assert not bridge.process(path)
        notion.sync.assert_not_called()
        assert state.get(path)["status"] != "done"
        assert path.read_text() == "Original transcript"
        bridge.reviewer.call = Mock(return_value={"corrections": [], "flags": []})
        notion.sync.return_value = "page"
        assert bridge.process(path)
        assert "RAW TRANSCRIPT (UNCHANGED)" in notion.sync.call_args.args[1]
        assert bridge.process(path)
        assert notion.sync.call_count == 1
    finally:
        state.close()


def test_http_error_does_not_expose_body(config, monkeypatch):
    import httpx

    reviewer = Reviewer(enable(config))
    real_client = httpx.Client
    client = real_client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text="secret transcript test-groq")
        )
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    with pytest.raises(RuntimeError, match="Groq HTTP 401") as exc:
        reviewer.check()
    assert "secret" not in str(exc.value)


def test_truncated_response_rejected(config, monkeypatch):
    import httpx

    reviewer = Reviewer(enable(config))
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "content": json.dumps({"corrections": [], "flags": []})
                            },
                        }
                    ]
                },
            )
        )
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    with pytest.raises(ValueError, match="incomplete"):
        reviewer.check()


def test_random_filename_uses_active_course_and_date(tmp_path):
    from courseai_lectures.files import parse_filename

    path = tmp_path / "Record (voice recorder) 12345.txt"
    path.write_text("lecture")
    lecture = parse_filename(path, "KIN120", "2026-09-28")
    assert lecture.course == "KIN120"
    assert lecture.date == "2026-09-28"
    assert lecture.title == path.stem
    structured = tmp_path / "CHEM120_2026-09-25_Atoms.txt"
    assert parse_filename(structured, "KIN120").course == "CHEM120"


def test_random_route_stays_pinned_when_active_course_changes(config):
    config.active_course = "KIN120"
    config.lecture_date = "2026-09-28"
    config.courses["CHEM120"] = "22222222-2222-2222-2222-222222222222"
    path = config.transcripts / "recording-93812.txt"
    path.write_text("Lecture text")
    state, notion = State(config.state), Mock()
    notion.sync.return_value = "page"
    bridge = Bridge(config, state, notion)
    try:
        assert bridge.process(path)
        config.active_course = "CHEM120"
        config.lecture_date = "2026-09-29"
        path.write_text("Updated same lecture text")
        assert bridge.process(path)
        assert notion.sync.call_args.args[0].course == "KIN120"
        assert notion.sync.call_args.args[0].date == "2026-09-28"
    finally:
        state.close()


def test_unknown_reference_evidence_is_rejected(config):
    raw = "The force was three new tons."
    result = {
        "corrections": [
            correction(
                "three new tons",
                "three newtons",
                evidence=["transcript", "notion:invented-source"],
            )
        ],
        "flags": [],
    }

    normalized = normalize_review_result(raw, result, {"notion:real-source"})

    assert normalized["corrections"] == []
    assert any("unknown evidence" in flag for flag in normalized["flags"])
