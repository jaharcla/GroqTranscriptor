from types import SimpleNamespace

import pytest


@pytest.fixture
def config(tmp_path):
    for name in ("Transcripts", "Audio Inbox", "Archive"):
        (tmp_path / name).mkdir()
    return SimpleNamespace(
        token="test-token",
        data_source_id="source",
        database_id="",
        transcripts=tmp_path / "Transcripts",
        audio=tmp_path / "Audio Inbox",
        archive=tmp_path / "Archive",
        state=tmp_path / "state.sqlite3",
        log=tmp_path / "bridge.log",
        stable_seconds=0.001,
        stable_timeout=1,
        retry_seconds=0.01,
        archive_audio=False,
        courses={"KIN120": "11111111-1111-1111-1111-111111111111"},
        props={
            "lecture": "Lecture",
            "course": "Course",
            "date": "Date",
            "capture": "Capture method",
            "processing": "Processing",
            "local_path": "Local transcript path",
            "ingest_id": "Ingest ID",
        },
    )
