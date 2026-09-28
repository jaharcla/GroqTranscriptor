from pathlib import Path

import pytest

from courseai_lectures.files import parse_filename, read_stable


@pytest.mark.parametrize(
    "name,course,day,title",
    [
        ("KIN120_2026-09-28_3D-Vectors.txt", "KIN120", "2026-09-28", "3D Vectors"),
        ("CHEM120_2026-09-28_Stoichiometry.m4a", "CHEM120", "2026-09-28", "Stoichiometry"),
        ("biol130_2026-09-29_Cell-Membranes.TXT", "BIOL130", "2026-09-29", "Cell Membranes"),
    ],
)
def test_parse(name, course, day, title):
    lecture = parse_filename(Path(name))
    assert (lecture.course, lecture.date, lecture.title) == (course, day, title)


@pytest.mark.parametrize(
    "name",
    [
        "bad.txt",
        "KIN120_2026-02-30_Title.txt",
        "KIN120_2026-09-28_---.txt",
        "KIN120_2026-9-28_Title.txt",
    ],
)
def test_bad_filename(name):
    with pytest.raises(ValueError):
        parse_filename(Path(name))


def test_deterministic_identity():
    assert (
        parse_filename(Path("KIN120_2026-09-28_3D-Vectors.txt")).key
        == parse_filename(Path("elsewhere/kin120_2026-09-28_3d-vectors.txt")).key
    )


class Clock:
    def __init__(self, tick=lambda _: None):
        self.time = 0
        self.tick = tick

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds
        self.tick(self.time)


def test_waits_for_writer(tmp_path):
    path = tmp_path / "lecture.txt"
    path.write_text("first", encoding="utf-8")

    def write(now):
        if now == 1:
            path.write_text("first and final", encoding="utf-8")

    clock = Clock(write)
    assert read_stable(path, 2, 10, clock=clock.now, sleep=clock.sleep) == "first and final"
    assert clock.time >= 3


def test_missing_or_empty_times_out(tmp_path):
    for exists in (False, True):
        path = tmp_path / "empty.txt"
        if exists:
            path.touch()
        clock = Clock()
        with pytest.raises(TimeoutError):
            read_stable(path, 1, 3, clock=clock.now, sleep=clock.sleep)


def test_utf8_bom_and_exact_text(tmp_path):
    path = tmp_path / "lecture.txt"
    path.write_bytes(b"\xef\xbb\xbf  lecture\r\n\r\ntext  ")
    clock = Clock()
    assert read_stable(path, 1, 4, clock=clock.now, sleep=clock.sleep) == "  lecture\r\n\r\ntext  "
