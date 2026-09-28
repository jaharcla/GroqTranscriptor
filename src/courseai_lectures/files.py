import hashlib
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class Lecture:
    course: str
    date: str
    title: str

    @property
    def key(self):
        identity = f"{self.course}|{self.date}|{self.title.casefold()}"
        return hashlib.sha256(identity.encode()).hexdigest()


def parse_filename(path: Path, active_course="", lecture_date="") -> Lecture:
    match = re.fullmatch(r"([A-Za-z0-9]+)_(\d{4}-\d{2}-\d{2})_(.+)", path.stem)
    if not match:
        if not active_course:
            raise ValueError("Random filename: set ACTIVE_COURSE in .env (for example KIN120)")
        day = lecture_date or datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
        date.fromisoformat(day)
        return Lecture(active_course.upper(), day, path.stem)

    course, day, title = match.groups()
    date.fromisoformat(day)
    title = " ".join(title.replace("-", " ").split())
    if not title:
        raise ValueError("Lecture title cannot be empty")
    return Lecture(course.upper(), day, title)


def signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def read_stable(
    path: Path, quiet: float, timeout: float, *, clock=time.monotonic, sleep=time.sleep
) -> str:
    start = changed = clock()
    previous = None
    while clock() - start < timeout:
        try:
            current = signature(path)
            if current != previous:
                previous, changed = current, clock()
            elif clock() - changed >= quiet and current[0] > 0:
                data = path.read_bytes()
                if signature(path) == current and len(data) == current[0]:
                    text = data.decode("utf-8-sig")
                    if not text.strip():
                        raise ValueError("Transcript is blank")
                    return text
                previous, changed = None, clock()
        except (FileNotFoundError, PermissionError):
            previous, changed = None, clock()
        sleep(min(0.5, quiet / 2))
    raise TimeoutError("Transcript did not become stable and readable before timeout")
