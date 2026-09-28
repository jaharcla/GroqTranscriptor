import copy
import json
import os

import httpx
import pytest

from courseai_lectures.bridge import Bridge
from courseai_lectures.notion import Notion, block_text, chunks
from courseai_lectures.state import State


class Server:
    def __init__(self):
        self.pages = []
        self.blocks = {}
        self.page_creates = 0
        self.fail = None
        self.accept = True
        self.writes = []

    def handle(self, request):
        path = request.url.path.removeprefix("/v1/")
        method = request.method
        body = json.loads(request.content) if request.content else None
        target = (
            "page"
            if method == "POST" and path == "pages"
            else "append"
            if method == "PATCH" and path.endswith("/children")
            else None
        )
        should_fail = target and (
            self.fail == target or (target == "append" and self.fail == body["children"][0]["type"])
        )
        if should_fail and not self.accept:
            self.fail = None
            raise httpx.ReadTimeout("test transport timeout")
        if method != "GET":
            self.writes.append((method, path, body))
        if path == "data_sources/source" and method == "GET":
            types = {
                "Lecture": "title",
                "Course": "relation",
                "Date": "date",
                "Local transcript path": "rich_text",
                "Ingest ID": "rich_text",
                "Capture method": "select",
                "Processing": "status",
            }
            result = {
                "properties": {
                    name: {
                        "type": kind,
                        kind: {"options": [{"name": "Audio import"}, {"name": "Transcribed"}]},
                    }
                    for name, kind in types.items()
                }
            }
        elif path.endswith("/query"):
            filt = body["filter"]
            if "rich_text" in filt:
                key = filt["rich_text"]["equals"]
                rows = [
                    p
                    for p in self.pages
                    if p["properties"]["Ingest ID"]["rich_text"][0]["text"]["content"] == key
                ]
            else:
                rows = self.pages
            result = {"results": copy.deepcopy(rows), "has_more": False}
        elif path == "pages" and method == "POST":
            self.page_creates += 1
            result = {"id": f"page-{self.page_creates}", "properties": body["properties"]}
            self.pages.append(copy.deepcopy(result))
            self.blocks[result["id"]] = []
        elif path.startswith("pages/") and method == "PATCH":
            result = next(p for p in self.pages if p["id"] == path.split("/")[1])
            result["properties"].update(body["properties"])
        elif path.endswith("/children"):
            parent = path.split("/")[1]
            if method == "PATCH":
                created = []
                for block in body["children"]:
                    block = copy.deepcopy(block)
                    block["id"] = f"block-{len(self.blocks)}"
                    self.blocks[block["id"]] = []
                    self.blocks[parent].append(block)
                    created.append(block)
                result = {"results": created}
            else:
                result = {"results": copy.deepcopy(self.blocks[parent]), "has_more": False}
        elif path.startswith("blocks/") and method == "PATCH":
            block_id = path.split("/")[1]
            for siblings in self.blocks.values():
                for block in list(siblings):
                    if block["id"] == block_id:
                        if body.get("archived"):
                            siblings.remove(block)
                        else:
                            block.update(body)
            result = {}
        else:
            raise AssertionError((method, path, body))
        if should_fail:
            self.fail = None
            raise httpx.ReadTimeout("test transport timeout")
        return httpx.Response(200, json=copy.deepcopy(result))


@pytest.fixture
def setup(config, monkeypatch):
    monkeypatch.setattr("courseai_lectures.notion.time.sleep", lambda _: None)
    server = Server()
    notion = Notion(
        config,
        httpx.Client(
            base_url="https://api.notion.com/v1/", transport=httpx.MockTransport(server.handle)
        ),
    )
    state = State(config.state)
    path = config.transcripts / "KIN120_2026-09-28_3D-Vectors.txt"
    path.write_text("Hello lecture.\n" * 200, encoding="utf-8")
    bridge = Bridge(config, state, notion)
    yield bridge, server, path
    state.close()
    notion.close()


def transcript(server):
    root = server.blocks[server.pages[0]["id"]]
    return "".join(block_text(b) for b in server.blocks[root[0]["id"]])


def test_duplicate_prevention_restart_and_remote_recovery(setup, tmp_path):
    bridge, server, path = setup
    assert bridge.process(path)
    assert bridge.process(path)
    fresh_state = State(tmp_path / "fresh.sqlite3")
    try:
        assert Bridge(bridge.config, fresh_state, bridge.notion).process(path)
    finally:
        fresh_state.close()
    assert server.page_creates == 1
    assert transcript(server) == path.read_bytes().decode("utf-8")
    assert len(server.blocks["page-1"]) == 1


@pytest.mark.parametrize("kind", ["page", "append", "paragraph"])
def test_accepted_write_lost_response_reconciles(setup, kind):
    bridge, server, path = setup
    server.fail = kind
    assert not bridge.process(path)
    assert bridge.state.get(path)["pending"]
    assert bridge.process(path)
    assert server.page_creates == 1
    assert transcript(server) == path.read_bytes().decode("utf-8")
    assert bridge.state.get(path)["pending"] is None


def test_unknown_create_not_blindly_repeated(setup):
    bridge, server, path = setup
    server.fail, server.accept = "page", False
    assert not bridge.process(path)
    assert not bridge.process(path)
    assert server.page_creates == 0
    assert "unknown outcome" in bridge.state.get(path)["error"]
    bridge.state.journal(path, None)
    assert bridge.process(path)
    assert server.page_creates == 1


def test_update_replaces_only_managed_text(setup):
    bridge, server, path = setup
    assert bridge.process(path)
    note = {
        "id": "user-note",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"text": {"content": "My notes"}}]},
    }
    server.blocks["page-1"].append(note)
    path.write_text("Corrected lecture", encoding="utf-8")
    assert bridge.process(path)
    assert transcript(server) == "Corrected lecture"
    assert server.blocks["page-1"][-1] == note
    assert server.page_creates == 1


def test_processing_set_only_after_complete(setup):
    bridge, server, path = setup
    server.fail = "append"
    assert not bridge.process(path)
    assert "Processing" not in server.pages[0]["properties"]
    assert bridge.process(path)
    assert server.pages[0]["properties"]["Processing"] == {"status": {"name": "Transcribed"}}


def test_missing_course_leaves_no_page(setup):
    bridge, server, path = setup
    bridge.config.courses = {}
    assert not bridge.process(path)
    assert not server.pages
    assert path.exists()


def test_audio_preserved_by_default(setup):
    bridge, _, path = setup
    audio = bridge.config.audio / path.with_suffix(".wav").name
    audio.write_bytes(b"audio")
    assert bridge.process(path)
    assert audio.read_bytes() == b"audio"
    assert path.exists()


def test_unicode_chunks_preserve_every_character():
    text = "  a\r\n😀" * 2000
    result = chunks(text)
    assert "".join(result) == text
    assert all(len(x.encode("utf-16-le")) // 2 <= 2000 for x in result)


def test_unknown_write_blocks_same_lecture_at_other_path(setup):
    bridge, server, path = setup
    server.fail, server.accept = "page", False
    assert not bridge.process(path)
    folder = path.parent / "copy"
    folder.mkdir()
    copied = folder / path.name
    copied.write_bytes(path.read_bytes())
    assert not bridge.process(copied)
    assert server.page_creates == 0


@pytest.mark.skipif(os.name != "nt", reason="audio archival uses Windows rename semantics")
def test_opt_in_archive_preserves_subfolders_and_never_overwrites(setup):
    bridge, server, path = setup
    bridge.config.archive_audio = True
    sub = path.parent / "week-1"
    sub.mkdir()
    nested = sub / path.name
    path.rename(nested)
    audio = bridge.config.audio / "week-1" / nested.with_suffix(".mp3").name
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    archived = bridge.config.archive / "week-1" / audio.name
    archived.parent.mkdir()
    archived.write_bytes(b"older recording")
    assert not bridge.process(nested)
    assert audio.read_bytes() == b"audio"
    assert archived.read_bytes() == b"older recording"
    archived.unlink()
    assert bridge.process(nested)
    assert not audio.exists()
    assert archived.read_bytes() == b"audio"
    assert nested.exists()
    assert server.page_creates == 1


def test_real_state_restart_skips_completed_work(setup):
    bridge, server, path = setup
    assert bridge.process(path)
    state = State(bridge.config.state)
    try:
        count = len(server.writes)
        assert Bridge(bridge.config, state, bridge.notion).process(path)
        assert len(server.writes) == count
    finally:
        state.close()


def test_rate_limit_honors_retry_after(config, monkeypatch):
    waits = []
    monkeypatch.setattr("courseai_lectures.notion.time.sleep", waits.append)
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"code": "rate_limited"})
        return httpx.Response(200, json={"ok": True})

    notion = Notion(
        config,
        httpx.Client(base_url="https://api.notion.com/v1/", transport=httpx.MockTransport(handle)),
    )
    try:
        assert notion.request("GET", "pages/test") == {"ok": True}
        assert len(calls) == 2
        assert 2 in waits
    finally:
        notion.close()


def test_children_paginate(config, monkeypatch):
    monkeypatch.setattr("courseai_lectures.notion.time.sleep", lambda _: None)

    def handle(request):
        if "start_cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={"results": [{"id": "first"}], "has_more": True, "next_cursor": "cursor-2"},
            )
        assert request.url.params["start_cursor"] == "cursor-2"
        return httpx.Response(200, json={"results": [{"id": "last"}], "has_more": False})

    notion = Notion(
        config,
        httpx.Client(base_url="https://api.notion.com/v1/", transport=httpx.MockTransport(handle)),
    )
    try:
        assert notion.children("parent") == [{"id": "first"}, {"id": "last"}]
    finally:
        notion.close()
