import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .transcribe import AUDIO_TYPES


class Pending:
    def __init__(self, extensions=None):
        self.extensions = extensions or {".txt"}
        self.paths = set()
        self.lock = threading.Lock()
        self.wake = threading.Event()

    def add(self, path):
        path = Path(path).resolve()
        if path.suffix.lower() in self.extensions:
            with self.lock:
                self.paths.add(path)
                self.wake.set()

    def take(self):
        with self.lock:
            result, self.paths = self.paths, set()
            self.wake.clear()
            return result


class Handler(FileSystemEventHandler):
    def __init__(self, pending):
        self.pending = pending

    def on_created(self, event):
        if event.is_directory:
            for path in Path(event.src_path).rglob("*"):
                if path.is_file():
                    self.pending.add(path)
        else:
            self.pending.add(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.pending.add(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            for path in Path(event.dest_path).rglob("*"):
                if path.is_file():
                    self.pending.add(path)
        else:
            self.pending.add(event.dest_path)


def watch(bridge):
    root = bridge.config.transcripts
    root.mkdir(parents=True, exist_ok=True)
    audio_enabled = getattr(bridge.config, "audio_enabled", False)
    pending = Pending({".txt"} | AUDIO_TYPES if audio_enabled else {".txt"})
    observer = Observer()
    observer.schedule(Handler(pending), str(root), recursive=True)
    roots = [root]
    if audio_enabled:
        bridge.config.audio.mkdir(parents=True, exist_ok=True)
        observer.schedule(Handler(pending), str(bridge.config.audio), recursive=True)
        roots.append(bridge.config.audio)
    observer.start()
    try:
        # Register first, then one startup scan to recover files arriving while offline.
        for watched in roots:
            for path in watched.rglob("*"):
                if path.is_file():
                    pending.add(path)
        logging.info("Watching %s (Ctrl+C to stop)", ", ".join(map(str, roots)))
        while True:
            paths = pending.take()
            paths.update(Path(row["path"]) for row in bridge.state.jobs(due=True))
            for path in sorted(paths):
                row = bridge.state.get(path)
                if row and row["status"] == "failed" and row["next_retry"] > time.time():
                    continue
                bridge.process(path)
            pending.wake.wait(timeout=min(bridge.config.retry_seconds, 5))
    except KeyboardInterrupt:
        logging.info("Watcher stopped")
    finally:
        observer.stop()
        observer.join()
