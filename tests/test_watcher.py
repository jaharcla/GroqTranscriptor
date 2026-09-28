import time

from watchdog.events import FileCreatedEvent, FileMovedEvent
from watchdog.observers import Observer

from courseai_lectures.watcher import Handler, Pending


def test_events_filter_and_deduplicate(tmp_path):
    pending = Pending()
    handler = Handler(pending)
    path = tmp_path / "lecture.txt"
    handler.on_created(FileCreatedEvent(str(path)))
    handler.on_created(FileCreatedEvent(str(path)))
    handler.on_created(FileCreatedEvent(str(tmp_path / "audio.wav")))
    handler.on_moved(FileMovedEvent(str(tmp_path / "partial.tmp"), str(path)))
    assert pending.take() == {path.resolve()}
    assert pending.take() == set()


def test_native_observer_receives_completed_rename(tmp_path):
    pending = Pending()
    observer = Observer()
    observer.schedule(Handler(pending), str(tmp_path), recursive=True)
    observer.start()
    try:
        temp = tmp_path / "partial.tmp"
        final = tmp_path / "KIN120_2026-09-28_Title.txt"
        temp.write_text("complete transcript", encoding="utf-8")
        temp.rename(final)
        deadline = time.monotonic() + 5
        received = set()
        while final.resolve() not in received and time.monotonic() < deadline:
            pending.wake.wait(0.1)
            received.update(pending.take())
        assert final.resolve() in received
    finally:
        observer.stop()
        observer.join()
