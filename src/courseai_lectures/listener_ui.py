import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import wave
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .bridge import Bridge
from .config import load_config
from .notion import Notion
from .state import State
from .transcribe import AUDIO_TYPES

WINDOW_BG = "#101419"
PANEL_BG = "#151d29"
CARD_BG = "#1a2432"
TEXT = "#edf5ff"
MUTED = "#9ab0c7"
ACCENT = "#62d1b4"
ACCENT_SOFT = "#213e3d"
WARN = "#f5b657"
ERROR = "#ff6b7a"
SUCCESS = "#72d897"
BORDER = "#243041"

AUDIO_EXTENSIONS = tuple(f"*{extension}" for extension in sorted(AUDIO_TYPES))


def resolve_env_path(start: Path | None = None) -> Path:
    candidates = []
    base = (start or Path.cwd()).resolve()
    candidates.append(base / ".env")
    candidates.append(Path("C:/CourseAI/Bridge/.env"))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return base / ".env"


def sanitize_error(error: object, token: str = "", groq_key: str = "") -> str:
    text = str(error or "No details available")
    for value in (token, groq_key):
        if value and value in text:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"sk-[A-Za-z0-9]+", "[REDACTED]", text)
    return text


def parse_routing(row: dict) -> dict:
    routing = row.get("routing") or "{}"
    try:
        return json.loads(routing) if isinstance(routing, str) else routing
    except json.JSONDecodeError:
        return {}


def status_label_for(row: dict | None) -> str:
    if row is None:
        return "WAITING"
    if row.get("status") == "done":
        return "COMPLETE"
    if row.get("status") == "failed":
        return "ERROR"
    if row.get("status") == "processing":
        return "PROCESSING"
    if row.get("pending"):
        return "WAITING"
    return "QUEUED"


def summary_status_for(rows: list[dict], running: bool) -> str:
    if any(row.get("status") == "processing" for row in rows):
        return "PROCESSING"
    if any(row.get("status") == "failed" for row in rows):
        return "ERROR"
    if any(row.get("status") != "done" for row in rows):
        return "WAITING"
    return "IDLE" if running else "PAUSED"


def tonal_chip(status: str) -> str:
    palette = {
        "done": (SUCCESS, "#16392d"),
        "processing": (WARN, "#3b2c11"),
        "failed": (ERROR, "#3d1c22"),
        "pending": (ACCENT, "#233b36"),
    }
    bg, fg = palette.get(status, (MUTED, "#1e2a36"))
    return bg, fg


class ListenerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.env_path = resolve_env_path()
        self.config = load_config(self.env_path)
        self.listener_proc = None
        self.health = {
            "audio": "Unknown",
            "review": "Unknown",
            "notion": "Unknown",
        }
        self._health_labels = {}
        self._poll_lock = threading.Lock()
        self._active_jobs = []
        self._recent_jobs = []
        self._refresh_cancel = False
        self._health_running = False
        self._last_health_error = ""
        self._page = None
        self._recording_stream = None
        self._recording_file = None
        self._recording_path = None
        self.title("CourseAI Listener")
        self.geometry("1280x800")
        self.minsize(1100, 700)
        self.configure(bg=WINDOW_BG)

        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.style.configure("Sidebar.TFrame", background=PANEL_BG)
        self.style.configure("Card.TFrame", background=CARD_BG)
        self.style.configure("Header.TLabel", background=CARD_BG, foreground=TEXT, font=("Segoe UI", 14, "bold"))
        self.style.configure("Muted.TLabel", background=CARD_BG, foreground=MUTED, font=("Segoe UI", 10))
        self.style.configure("Nav.TButton", background=PANEL_BG, foreground=TEXT, borderwidth=0, font=("Segoe UI", 11, "bold"))
        self.style.map("Nav.TButton", background=[("active", "#1d2a36")])
        self.style.configure("Treeview", background="#121c28", fieldbackground="#121c28", foreground=TEXT, borderwidth=0)
        self.style.configure("Treeview.Heading", background="#1d2a36", foreground=TEXT, font=("Segoe UI", 10, "bold"))
        self.style.map("Treeview", background=[("selected", "#1d2a36")])

        self._build_layout()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.start_listener()
        self.refresh_all()
        self._start_health_check()

    def _build_layout(self):
        self.sidebar = tk.Frame(self, width=220, bg=PANEL_BG)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.brand = tk.Label(
            self.sidebar,
            text="CourseAI\nListener",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI", 20, "bold"),
            justify="left",
            padx=22,
            pady=26,
            anchor="w",
        )
        self.brand.pack(fill="x")

        self.listener_state = tk.Label(
            self.sidebar,
            text="● Listening",
            fg=ACCENT,
            bg=PANEL_BG,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            padx=22,
        )
        self.listener_state.pack(fill="x", pady=(0, 18))

        nav_items = ["Dashboard", "Lectures", "Activity", "Settings"]
        for index, text in enumerate(nav_items):
            btn = tk.Button(
                self.sidebar,
                text=text,
                bg=PANEL_BG if index == 0 else "#121a24",
                fg=TEXT,
                font=("Segoe UI", 11, "bold"),
                relief="flat",
                borderwidth=0,
                pady=10,
                highlightthickness=0,
                anchor="w",
                justify="left",
                command=lambda label=text: self._nav_select(label),
            )
            btn.configure(width=18)
            btn.pack(fill="x", padx=12, pady=(0 if index == 0 else 4, 0))

        self.service_box = tk.Frame(self.sidebar, bg=PANEL_BG, pady=18)
        self.service_box.pack(fill="x", side="bottom")

        for name, status in (("Groq", "good"), ("Notion", "good")):
            row = tk.Frame(self.service_box, bg=PANEL_BG)
            row.pack(fill="x", padx=18, pady=5)
            dot = tk.Label(row, text="●", fg=SUCCESS if status == "good" else MUTED, bg=PANEL_BG, font=("Segoe UI", 14))
            dot.pack(side="left")
            tk.Label(row, text=name, fg=TEXT, bg=PANEL_BG, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(8, 0))

        self.main = tk.Frame(self, bg=WINDOW_BG)
        self.main.pack(side="left", fill="both", expand=True, padx=18, pady=18)

        self.topbar = tk.Frame(self.main, bg=WINDOW_BG)
        self.topbar.pack(fill="x", pady=(4, 14))
        tk.Label(self.topbar, text="Listener", bg=WINDOW_BG, fg=TEXT, font=("Segoe UI", 18, "bold")).pack(side="left")
        tk.Label(self.topbar, text="Automatic lecture ingestion", bg=WINDOW_BG, fg=MUTED, font=("Segoe UI", 10)).pack(side="left", padx=(18, 0))

        self.course_var = tk.StringVar()
        course_frame = tk.Frame(self.topbar, bg=WINDOW_BG)
        course_frame.pack(side="right")
        tk.Label(course_frame, text="Active Course:", bg=WINDOW_BG, fg=MUTED, font=("Segoe UI", 10)).pack(side="left")
        self.course_combo = ttk.Combobox(course_frame, textvariable=self.course_var, state="readonly", width=16)
        self.course_combo.pack(side="left", padx=(8, 12))
        self.course_combo.bind("<<ComboboxSelected>>", self._on_active_course_change)

        self.open_btn = tk.Button(course_frame, text="Open Audio Inbox", bg="#1d2a36", fg=TEXT, relief="flat", command=self.open_audio_inbox)
        self.open_btn.pack(side="left", padx=(0, 8))

        self.toggle_btn = tk.Button(course_frame, text="Pause Listener", bg="#1d2a36", fg=TEXT, relief="flat", command=self.toggle_listener)
        self.toggle_btn.pack(side="left")

        self.dashboard = tk.Frame(self.main, bg=WINDOW_BG)
        self.dashboard.pack(fill="both", expand=True)

        self.status_card = tk.Frame(self.dashboard, bg=CARD_BG, bd=0, highlightbackground=BORDER, highlightthickness=1)
        self.status_card.pack(fill="x", pady=(0, 16))
        body = tk.Frame(self.status_card, bg=CARD_BG, padx=18, pady=18)
        body.pack(fill="x")
        self.status_title = tk.Label(body, text="● LISTENING", bg=CARD_BG, fg=ACCENT, font=("Segoe UI", 16, "bold"), justify="left")
        self.status_title.pack(anchor="w")
        self.status_subtitle = tk.Label(body, text="Watching Audio Inbox", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 10), justify="left")
        self.status_subtitle.pack(anchor="w", pady=(12, 3))
        self.status_path = tk.Label(body, text=str(self.config.audio), bg=CARD_BG, fg=MUTED, font=("Segoe UI", 9), wraplength=680, justify="left")
        self.status_path.pack(anchor="w")

        self.health_frame = tk.Frame(body, bg=CARD_BG)
        self.health_frame.pack(fill="x", pady=(18, 0))
        health_labels = [
            ("Groq Audio", "audio"),
            ("Groq Review", "review"),
            ("Notion", "notion"),
        ]
        for label, key in health_labels:
            row = tk.Frame(self.health_frame, bg=CARD_BG)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=f"{label:>14}", bg=CARD_BG, fg=MUTED, font=("Segoe UI", 9), width=14, anchor="w").pack(side="left")
            self._health_labels[key] = tk.Label(row, text="Unknown", bg=CARD_BG, fg=MUTED, font=("Segoe UI", 9, "bold"), anchor="w")
            self._health_labels[key].pack(side="left")

        panels = tk.Frame(self.dashboard, bg=WINDOW_BG)
        panels.pack(fill="both", expand=True)
        left = tk.Frame(panels, bg=WINDOW_BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(panels, bg=WINDOW_BG)
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))

        self.drop_card = tk.Frame(left, bg=CARD_BG, bd=0, highlightbackground=BORDER, highlightthickness=1)
        self.drop_card.pack(fill="x", pady=(0, 16))
        self._fill_drop_card(self.drop_card)

        self.pipeline_card = tk.Frame(left, bg=CARD_BG, bd=0, highlightbackground=BORDER, highlightthickness=1)
        self.pipeline_card.pack(fill="x")
        self._fill_pipeline_card(self.pipeline_card)

        self.recent_card = tk.Frame(right, bg=CARD_BG, bd=0, highlightbackground=BORDER, highlightthickness=1)
        self.recent_card.pack(fill="both", expand=True)
        self._fill_recent_card(self.recent_card)

        self.error_card = tk.Frame(right, bg=CARD_BG, bd=0, highlightbackground=BORDER, highlightthickness=1)
        self.error_card.pack(fill="x", pady=(16, 0))
        self._fill_error_card(self.error_card)

        self._apply_course_options()
        self._set_listener_button_text()

    def _nav_select(self, label: str):
        if self._page is not None:
            self._page.destroy()
            self._page = None
        if label == "Dashboard":
            self.dashboard.pack(fill="both", expand=True)
            return
        self.dashboard.pack_forget()
        self._page = tk.Frame(self.main, bg=WINDOW_BG)
        self._page.pack(fill="both", expand=True)
        tk.Label(self._page, text=label, bg=WINDOW_BG, fg=TEXT,
                 font=("Segoe UI", 18, "bold")).pack(anchor="w", pady=(10, 16))
        if label == "Lectures":
            tree = ttk.Treeview(self._page, columns=("Lecture", "Course", "Date", "Status", "Stage", "Attempts", "Page"),
                                show="headings")
            for col in tree["columns"]:
                tree.heading(col, text=col)
                tree.column(col, width=140)
            tree.pack(fill="both", expand=True)
            for row in self._recent_jobs:
                routing = parse_routing(row)
                tree.insert("", "end", values=(
                    Path(row.get("path", "")).name, routing.get("course", "-"),
                    routing.get("date", "-"), row.get("status", "pending"),
                    row.get("stage", "detected"), row.get("attempts", 0), row.get("page") or "-"))
            tk.Button(self._page, text="Retry failed jobs", command=self.retry_failed_jobs,
                      bg="#1d2a36", fg=TEXT, relief="flat").pack(anchor="w", pady=10)
        elif label == "Activity":
            text = tk.Text(self._page, bg="#121c28", fg=TEXT, relief="flat", wrap="word")
            text.pack(fill="both", expand=True)
            if self.config.log.exists():
                lines = self.config.log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                text.insert("1.0", "\n".join(sanitize_error(line, self.config.token, self.config.groq_api_key)
                                             for line in lines))
            text.configure(state="disabled")
        else:
            tk.Label(self._page, text="Active Course", bg=WINDOW_BG, fg=MUTED).pack(anchor="w")
            combo = ttk.Combobox(self._page, values=sorted(self.config.courses),
                                 textvariable=self.course_var, state="readonly", width=22)
            combo.pack(anchor="w", pady=(4, 16))
            combo.bind("<<ComboboxSelected>>", self._on_active_course_change)
            tk.Label(self._page, text=f"Audio Inbox: {self.config.audio}\n"
                     f"Recording staging: {self.config.audio.parent / 'Recording Staging'}\n"
                     f"Groq API key: {'Configured' if self.config.groq_api_key else 'Not configured'}",
                     bg=WINDOW_BG, fg=MUTED, justify="left").pack(anchor="w")
            tk.Button(self._page, text="Check Services", command=self._start_health_check,
                      bg="#1d2a36", fg=TEXT, relief="flat").pack(anchor="w", pady=16)

    def _apply_course_options(self):
        courses = sorted(self.config.courses)
        self.course_combo["values"] = courses
        state = State(self.config.state)
        try:
            saved = state.get_setting("active_course")
            active = (saved or self.config.active_course or (courses[0] if courses else "")).upper()
            if active and not saved:
                state.set_setting("active_course", active)
        finally:
            state.close()
        if active in courses:
            self.course_var.set(active)
            self.config.active_course = active
        elif courses:
            fallback = courses[0]
            self.course_var.set(fallback)
            self.config.active_course = fallback
            state = State(self.config.state)
            try:
                state.set_setting("active_course", fallback)
            finally:
                state.close()
        else:
            self.course_var.set("")

    def _on_active_course_change(self, _event=None):
        selected = self.course_var.get().strip()
        if not selected:
            return
        self.config.active_course = selected.upper()
        state = State(self.config.state)
        try:
            state.set_setting("active_course", self.config.active_course)
            state.set_setting("active_course_confirmed_at", time.time())
        finally:
            state.close()

    def _selected_route(self, source: Path):
        selected = self.course_var.get().strip().upper()
        if selected not in self.config.courses:
            raise ValueError("Choose the course for this lecture before recording or importing.")
        match = re.fullmatch(r"([A-Za-z0-9]+)_(\\d{4}-\\d{2}-\\d{2})_(.+)", source.stem)
        if match:
            course, day, title = match.groups()
            course = course.upper()
            if course not in self.config.courses:
                raise ValueError(f"Missing course mapping: {course}")
            title = " ".join(title.replace("-", " ").split())
        else:
            course = selected
            if getattr(self.config, "lecture_date", ""):
                day = self.config.lecture_date
            elif source.exists():
                day = datetime.fromtimestamp(source.stat().st_mtime).date().isoformat()
            else:
                day = datetime.now().date().isoformat()
            title = source.stem
        return {"course": course, "date": day, "title": title}

    def _pin_route(self, destination: Path, source: Path, overwrite=False):
        route = self._selected_route(source)
        state = State(self.config.state)
        try:
            state.ensure(destination)
            row = state.get(destination)
            if row.get("routing") and not overwrite:
                return json.loads(row["routing"])
            if row.get("status") == "done" and overwrite:
                raise ValueError(
                    "This file was already completed. Its pinned course was not changed."
                )
            state.set(
                destination,
                routing=json.dumps(route),
                status="staging",
                stage="detected",
                next_retry=time.time() + 30,
                error=None,
            )
            state.set_setting("active_course", route["course"])
            state.set_setting("active_course_confirmed_at", time.time())
        finally:
            state.close()
        return route

    def _queue_pinned(self, destination: Path):
        state = State(self.config.state)
        try:
            state.set(
                destination,
                status="pending",
                stage="detected",
                next_retry=0,
                error=None,
            )
        finally:
            state.close()

    def _fill_drop_card(self, parent):
        inner = tk.Frame(parent, bg=CARD_BG, padx=18, pady=18)
        inner.pack(fill="both")
        tk.Label(inner, text="Audio Inbox", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(
            inner,
            text="Drop a finished lecture recording here\nor copy it into your Audio Inbox.",
            bg=CARD_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            justify="left",
            wraplength=430,
        ).pack(anchor="w", pady=(10, 6))
        tk.Label(
            inner,
            text="Supported:\nM4A · WAV · MP3 · FLAC · OGG · AAC · WMA · MP4",
            bg=CARD_BG,
            fg=TEXT,
            font=("Segoe UI", 9, "bold"),
            justify="left",
            wraplength=430,
        ).pack(anchor="w", pady=(0, 14))

        action_row = tk.Frame(inner, bg=CARD_BG)
        action_row.pack(fill="x")
        self._drop_open_btn = tk.Button(action_row, text="Open Audio Inbox", bg="#1d2a36", fg=TEXT, relief="flat", command=self.open_audio_inbox)
        self._drop_open_btn.pack(side="left", padx=(0, 10))
        self._drop_process_btn = tk.Button(action_row, text="Process File", bg="#1d2a36", fg=TEXT, relief="flat", command=self.process_file_dialog)
        self._drop_process_btn.pack(side="left")
        self.record_btn = tk.Button(
            action_row,
            text="Record from Microphone",
            bg=ACCENT_SOFT,
            fg=TEXT,
            relief="flat",
            command=self.toggle_recording,
        )
        self.record_btn.pack(side="left")
        self.record_status = tk.Label(inner, text="Microphone ready", bg=CARD_BG, fg=MUTED, font=("Segoe UI", 9))
        self.record_status.pack(anchor="w", pady=(10, 0))

    def _fill_pipeline_card(self, parent):
        inner = tk.Frame(parent, bg=CARD_BG, padx=18, pady=18)
        inner.pack(fill="both")
        tk.Label(inner, text="Current Lecture", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        self.pipeline_title = tk.Label(inner, text="No active lecture", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 11, "bold"), justify="left")
        self.pipeline_title.pack(anchor="w", pady=(12, 4))
        self.pipeline_meta = tk.Label(inner, text="", bg=CARD_BG, fg=MUTED, font=("Segoe UI", 9), justify="left")
        self.pipeline_meta.pack(anchor="w")
        self.pipeline_steps = tk.Frame(inner, bg=CARD_BG)
        self.pipeline_steps.pack(anchor="w", pady=(14, 0))

        steps = [
            "Detected",
            "Preparing Audio",
            "Transcribing",
            "Reviewing",
            "Uploading to Notion",
            "Complete",
        ]
        self.pipeline_step_vars = []
        for step in steps:
            row = tk.Frame(self.pipeline_steps, bg=CARD_BG)
            row.pack(anchor="w", pady=4)
            dot = tk.Label(row, text="○", fg=MUTED, bg=CARD_BG, font=("Segoe UI", 11, "bold"))
            dot.pack(side="left")
            text = tk.Label(row, text=step, bg=CARD_BG, fg=MUTED, font=("Segoe UI", 9))
            text.pack(side="left", padx=(8, 0))
            self.pipeline_step_vars.append((dot, text))

    def _fill_recent_card(self, parent):
        inner = tk.Frame(parent, bg=CARD_BG, padx=18, pady=18)
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text="Recent Lectures", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        columns = ("Lecture", "Course", "Date", "Status", "Attempts", "Notion")
        self.recent_tree = ttk.Treeview(inner, columns=columns, show="headings", height=9)
        for col in columns:
            self.recent_tree.heading(col, text=col)
            self.recent_tree.column(col, width=120, anchor="center")
        self.recent_tree.pack(fill="both", expand=True, pady=(12, 0))

    def _fill_error_card(self, parent):
        inner = tk.Frame(parent, bg=CARD_BG, padx=18, pady=18)
        inner.pack(fill="both")
        tk.Label(inner, text="Needs attention", bg=CARD_BG, fg=ERROR, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        self.error_title = tk.Label(inner, text="", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 11, "bold"), justify="left")
        self.error_title.pack(anchor="w", pady=(10, 2))
        self.error_detail = tk.Label(inner, text="", bg=CARD_BG, fg=MUTED, font=("Segoe UI", 9), justify="left", wraplength=340)
        self.error_detail.pack(anchor="w")
        self.error_meta = tk.Label(inner, text="", bg=CARD_BG, fg=MUTED, font=("Segoe UI", 8), justify="left")
        self.error_meta.pack(anchor="w", pady=(8, 0))
        action_row = tk.Frame(inner, bg=CARD_BG)
        action_row.pack(anchor="w", pady=(10, 0))
        self.retry_btn = tk.Button(action_row, text="Retry Now", bg="#1d2a36", fg=TEXT, relief="flat", command=self.retry_failed_jobs)
        self.retry_btn.pack(side="left", padx=(0, 8))
        self.details_btn = tk.Button(action_row, text="View Details", bg="#1d2a36", fg=TEXT, relief="flat", command=self.view_error_details)
        self.details_btn.pack(side="left")

    def _set_listener_button_text(self):
        if self.listener_proc and self.listener_proc.poll() is None:
            self.toggle_btn.config(text="Pause Listener")
        else:
            self.toggle_btn.config(text="Start Listener")

    def __set_status(self, label: str, secondary: str, state_path: str, level: str):
        self.status_title.config(text=f"● {label}", fg=level)
        self.status_subtitle.config(text=secondary)
        self.status_path.config(text=state_path)

    def _refresh_jobs(self):
        try:
            state = State(self.config.state)
            jobs = state.jobs()
            state.close()
        except Exception as exc:
            jobs = []
            self._last_state_error = exc
        self._recent_jobs = sorted(jobs, key=lambda row: (row.get("status") != "done", str(row.get("path", ""))))
        self._active_jobs = [
            row for row in jobs if row.get("status") in {"processing", "pending", "failed"}
        ]

    def _refresh_health(self):
        try:
            config = load_config(self.env_path)
            state = State(config.state)
            notion = Notion(config)
            bridge = Bridge(config, state, notion)
            statuses = {
                "audio": "Connected" if config.audio_enabled and getattr(bridge.transcriber, "config", None) else "Disabled",
                "review": "Connected" if config.groq_enabled and getattr(bridge.reviewer, "config", None) else "Disabled",
                "notion": "Connected",
            }
            try:
                if bridge.transcriber:
                    bridge.transcriber.check()
                    statuses["audio"] = "Connected"
                else:
                    statuses["audio"] = "Disabled"
            except Exception:
                statuses["audio"] = "Unavailable"
            try:
                if bridge.reviewer:
                    bridge.reviewer.check()
                    statuses["review"] = "Connected"
                else:
                    statuses["review"] = "Disabled"
            except Exception:
                statuses["review"] = "Unavailable"
            try:
                notion.prepare()
                statuses["notion"] = "Connected"
            except Exception:
                statuses["notion"] = "Unavailable"
            finally:
                notion.close()
                state.close()
        except Exception:
            statuses = {"audio": "Unavailable", "review": "Unavailable", "notion": "Unavailable"}
        self.health = statuses

    def _start_health_check(self):
        if self._health_running:
            return
        self._health_running = True
        self.health = {key: "Checking…" for key in self.health}
        self._apply_health()

        def worker():
            try:
                self._refresh_health()
            except Exception as exc:
                self._last_health_error = sanitize_error(exc, self.config.token, self.config.groq_api_key)
            finally:
                self._health_running = False
                self.after(0, self._apply_health)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_health(self):
        for key, _label in {
            "audio": "Groq Audio",
            "review": "Groq Review",
            "notion": "Notion",
        }.items():
            value = self.health.get(key, "Unknown")
            color = (
                SUCCESS
                if value == "Connected"
                else WARN
                if value in {"Disabled", "Checking…", "Unknown"}
                else ERROR
            )
            self._health_labels[key].config(text=value, fg=color)

    def _render_dashboard(self):
        rows = self._recent_jobs
        if self.config and self.config.audio:
            self.status_path.config(text=str(self.config.audio))

        running = self.listener_proc is not None and self.listener_proc.poll() is None
        summary = summary_status_for(rows, running)
        self.listener_state.config(text=f"● {'Listening' if running else 'Stopped'}", fg=ACCENT if running else MUTED)
        self.__set_status(summary, "Watching Audio Inbox" if running else "Listener is currently paused", str(self.config.audio), ACCENT if summary not in {"ERROR", "WAITING"} else WARN if summary == "WAITING" else ERROR)

        if summary == "ERROR":
            failed = next((row for row in rows if row.get("status") == "failed"), None)
            if failed:
                message = sanitize_error(failed.get("error", "Failed"), self.config.token, self.config.groq_api_key)
                retry = float(failed.get("next_retry") or 0)
                delay = max(0, int(retry - time.time())) if retry else 0
                self.error_title.config(text=str(Path(failed.get("path", "unknown")).name))
                self.error_detail.config(text=message)
                self.error_meta.config(text=f"Attempt {failed.get('attempts', 0)}   Retry scheduled in {delay} sec")
            else:
                self.error_title.config(text="No failed jobs")
                self.error_detail.config(text="All lecture jobs are healthy.")
                self.error_meta.config(text="")
        else:
            self.error_title.config(text="")
            self.error_detail.config(text="")
            self.error_meta.config(text="")

        current = next((row for row in rows if row.get("status") in {"processing", "pending", "failed"}), None)
        if current:
            routing = parse_routing(current)
            title = Path(str(current.get("path", ""))).name or "Unknown lecture"
            course = routing.get("course") or "UNKNOWN"
            date = routing.get("date") or ""
            self.pipeline_title.config(text=title)
            self.pipeline_meta.config(text=f"{course}  {date}")
            stage_index = {
                "detected": 0,
                "preparing_audio": 1,
                "transcribing": 2,
                "reviewing": 3,
                "uploading": 4,
                "complete": 5,
                "error": 0,
            }.get(current.get("stage"), 0)
            for idx, (dot, label) in enumerate(self.pipeline_step_vars):
                dot.config(text="✓" if idx < stage_index else "○")
                label.config(fg=ACCENT if idx < stage_index else MUTED)
                if idx == stage_index:
                    label.config(fg=TEXT)
                    dot.config(text="●")
        else:
            self.pipeline_title.config(text="No active lecture")
            self.pipeline_meta.config(text="Queue is clear")
            for _idx, (dot, label) in enumerate(self.pipeline_step_vars):
                dot.config(text="○")
                label.config(fg=MUTED)

        self.recent_tree.delete(*self.recent_tree.get_children())
        for row in sorted(rows, key=lambda item: str(item.get("path", "")))[:10]:
            routing = parse_routing(row)
            lecture = Path(str(row.get("path", ""))).name if row.get("path") else "unknown"
            course = routing.get("course") or "-"
            date_iso = routing.get("date") or "-"
            if date_iso and len(date_iso) == 10:
                try:
                    from datetime import datetime
                    date_iso = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%b %-d")
                except ValueError:
                    pass
            status = row.get("status") or "pending"
            attempts = row.get("attempts") or 0
            notion = row.get("page") or "—"
            self.recent_tree.insert("", "end", values=(lecture, course, date_iso, status.title(), attempts, notion if notion != "—" else "—"))

    def refresh_all(self):
        self._refresh_jobs()
        self._apply_health()
        self._render_dashboard()
        self._set_listener_button_text()
        self.after(4000, self.refresh_all)

    def open_audio_inbox(self):
        path = self.config.audio
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))
        else:
            webbrowser.open(path.as_uri())

    def toggle_recording(self):
        if self._recording_stream is not None:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        selected = self.course_var.get().strip().upper()
        if selected not in self.config.courses:
            messagebox.showerror(
                "Choose course",
                "Choose the course for this lecture before recording.",
            )
            return
        if not messagebox.askyesno(
            "Confirm lecture course",
            f"Record this lecture as {selected}?\\n\\n"
            "This course will be pinned to the recording before transcription.",
        ):
            return
        try:
            import sounddevice as sd
        except ImportError:
            messagebox.showerror(
                "Microphone unavailable",
                "The microphone component is not installed. Run Install-Update.ps1 again.",
            )
            return

        staging = self.config.audio.parent / "Recording Staging"
        staging.mkdir(parents=True, exist_ok=True)
        path = staging / f"recording_{datetime.now():%Y%m%d_%H%M%S}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            output = wave.open(str(path), "wb")
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)

            def callback(indata, _frames, _time, status):
                if status:
                    self.after(0, lambda: self.record_status.config(text=f"Microphone: {status}", fg=WARN))
                output.writeframes(indata.tobytes())

            stream = sd.InputStream(
                samplerate=16000,
                channels=1,
                dtype="int16",
                callback=callback,
            )
            stream.start()
        except Exception as exc:
            try:
                output.close()
            except UnboundLocalError:
                pass
            path.unlink(missing_ok=True)
            messagebox.showerror("Microphone unavailable", str(exc))
            return

        self._recording_file = output
        self._recording_path = path
        self._recording_stream = stream
        self.record_btn.config(text="Stop Recording", bg="#6b2737")
        self.record_status.config(text=f"Recording {selected}: {path.name}", fg=ERROR)

    def stop_recording(self):
        stream = self._recording_stream
        output = self._recording_file
        path = self._recording_path
        self._recording_stream = None
        self._recording_file = None
        self._recording_path = None
        try:
            stream.stop()
            stream.close()
            output.close()
        except Exception as exc:
            messagebox.showerror("Recording error", str(exc))
            return
        self.record_btn.config(text="Record from Microphone", bg=ACCENT_SOFT)
        if not path.exists() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            messagebox.showerror("Recording error", "The recording was empty and was not queued.")
            return
        destination = self.config.audio / path.name
        self.config.audio.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = self.config.audio / (
                f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S_%f}{path.suffix}"
            )
        try:
            route = self._pin_route(destination, path)
            path.replace(destination)
            self._queue_pinned(destination)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Recording error", sanitize_error(exc))
            return
        self.record_status.config(
            text=f"Queued {route['course']}: {destination.name}",
            fg=SUCCESS,
        )

    def close_app(self):
        if self._recording_stream is not None:
            self.stop_recording()
        self.stop_listener()
        self.destroy()

    def toggle_listener(self):
        if self.listener_proc and self.listener_proc.poll() is None:
            self.stop_listener()
        else:
            self.start_listener()

    def start_listener(self):
        if self.listener_proc and self.listener_proc.poll() is None:
            return
        cwd = str(self.env_path.parent if self.env_path.exists() else Path.cwd())
        command = [sys.executable, "-m", "courseai_lectures.cli", "watch"]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.listener_proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._set_listener_button_text()

    def stop_listener(self):
        if self.listener_proc is None:
            return
        self.listener_proc.terminate()
        try:
            self.listener_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.listener_proc.kill()
        self.listener_proc = None
        self._set_listener_button_text()

    def retry_failed_jobs(self):
        state = State(self.config.state)
        try:
            for row in state.jobs():
                if row.get("status") == "failed":
                    state.set(row["path"], status="pending", stage="detected", next_retry=0, error=None)
        finally:
            state.close()
        self.refresh_all()

    def view_error_details(self):
        rows = self._recent_jobs
        failed = next((row for row in rows if row.get("status") == "failed"), None)
        if not failed:
            return
        message = sanitize_error(failed.get("error", "Failed"), self.config.token, self.config.groq_api_key)
        if messagebox is not None:
            messagebox.showinfo("Lecture error", message)

    def process_file_dialog(self):
        path = filedialog.askopenfilename(
            title="Select lecture audio or transcript",
            filetypes=[("Supported audio/transcript", " ".join(AUDIO_EXTENSIONS + ("*.txt",)))],
        )
        if not path:
            return
        self.process_file(Path(path))

    def process_file(self, path: Path):
        path = path.resolve()
        selected = self.course_var.get().strip().upper()
        if selected not in self.config.courses:
            messagebox.showerror(
                "Choose course",
                "Choose the course for this lecture before importing it.",
            )
            return
        watched_roots = (self.config.audio.resolve(), self.config.transcripts.resolve())
        try:
            if any(path.is_relative_to(root) for root in watched_roots):
                route = self._pin_route(path, path, overwrite=True)
                self._queue_pinned(path)
                self.record_status.config(
                    text=f"Queued {route['course']}: {path.name}",
                    fg=SUCCESS,
                )
            else:
                destination_root = (
                    self.config.audio
                    if path.suffix.lower() in AUDIO_TYPES
                    else self.config.transcripts
                )
                destination_root.mkdir(parents=True, exist_ok=True)
                destination = destination_root / path.name
                if destination.exists():
                    destination = destination_root / (
                        f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S_%f}{path.suffix}"
                    )
                temporary = destination.with_name(destination.name + ".courseai-part")
                shutil.copy2(path, temporary)
                try:
                    route = self._pin_route(destination, path)
                    os.replace(temporary, destination)
                    self._queue_pinned(destination)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
                self.record_status.config(
                    text=f"Queued {route['course']}: {destination.name}",
                    fg=SUCCESS,
                )
        except (OSError, ValueError) as exc:
            messagebox.showerror("Import error", sanitize_error(exc))
            return
        self.refresh_all()

    def _bind_shortcuts(self):
        self.bind("<Escape>", lambda _event: self.close_app())


def main():
    app = ListenerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
