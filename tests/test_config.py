from courseai_lectures.config import load_config


def test_config_relative_paths_and_environment_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "TRANSCRIPT_DIR=transcripts\nSTATE_DB=state.sqlite3\n"
        "COURSE_MAP=courses.yaml\nNOTION_TOKEN=file-token\n",
        encoding="utf-8",
    )
    (tmp_path / "courses.yaml").write_text(
        "kin120: '11111111-1111-1111-1111-111111111111'\n", encoding="utf-8"
    )
    monkeypatch.setenv("NOTION_TOKEN", "environment-token")
    config = load_config(env)
    assert config.token == "environment-token"
    assert config.transcripts == tmp_path / "transcripts"
    assert config.state == tmp_path / "state.sqlite3"
    assert config.courses["KIN120"] == "11111111-1111-1111-1111-111111111111"
    assert config.archive_audio is False
