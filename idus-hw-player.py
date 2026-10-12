#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
#  Idus HW-player  ·  v1.0  ·  build 2026-10-11
#  (bump APP_VERSION + BUILD below and this line together on every update)
# ─────────────────────────────────────────────────────────────────────────────
"""
Idus HW-player (Idus Halloween Player) — plays random Halloween videos (e.g. AtmosFX
window effects) on a projector or TV, with a random dark pause between them, and serves
a web GUI to control everything from a PC or a phone.

Self-contained folder: app + icon + settings live together, so one folder backup
captures everything. Only the videos live elsewhere (HW_VIDEO_DIR, default ~/HW-videos).

With no arguments the player starts (this is what the systemd service runs).
Every action also has a flag that talks to a running player (KDE shortcuts / Stream Deck):
  idus-hw-player --status           Show what the player is doing
  idus-hw-player --next             Play a video now (skips the current video or the pause)
  idus-hw-player --stop             Stop the current video (the pause starts)
  idus-hw-player --on | --off       Switch the player on or off (off = dark window)
  idus-hw-player --list             List the videos found in the video folder
  --host HOST                       control a player on another machine (default: this one)
  --port N                          web port (default: HW_PORT or 8080)

Configuration (environment, or idus-hw-player.env next to this file):
  HW_PASSWORD     password for the web GUI and the remote-control flags
  HW_VIDEO_DIR    video folder (default ~/HW-videos)
  HW_PORT         web GUI port (default 8080)
  HW_BIND         address to listen on (default 0.0.0.0 = all interfaces)
  HW_MPV_ARGS     extra mpv arguments, e.g. "--audio-device=alsa/hdmi:CARD=vc4hdmi0,DEV=0"

Dependencies (Raspberry Pi OS / Debian):  sudo apt install mpv   (Python 3.9+ is built in)
"""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import random
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit

# ---------------------------------------------------------------- configuration
APP_NAME = "Idus HW-player"
APP_VERSION = "1.0"
BUILD = "2026-10-11"  # keep in sync with the header comment at the top of this file
REPO_URL = "https://github.com/Idus/idus-hw-player"
KOFI_URL = "https://ko-fi.com/N4N2QZHKK"

# Self-contained folder: icon + settings + env file next to the app (like Idus SC Toolbox)
APP_DIR = Path(__file__).resolve().parent
ICON_FILE = APP_DIR / "idus-hw-player.png"
SETTINGS_FILE = APP_DIR / "idus-hw-player-settings.json"
ENV_FILE = APP_DIR / "idus-hw-player.env"


def _load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines (same file systemd uses) without overriding the real environment."""
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_env_file(ENV_FILE)
VIDEO_DIR = Path(os.environ.get("HW_VIDEO_DIR") or Path.home() / "HW-videos").expanduser()
PORT = int(os.environ.get("HW_PORT") or 8080)
BIND = os.environ.get("HW_BIND") or "0.0.0.0"
PASSWORD = os.environ.get("HW_PASSWORD", "")
MPV_ARGS = shlex.split(os.environ.get("HW_MPV_ARGS", ""))

VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".mpg", ".mpeg", ".wmv"}
MAX_UPLOAD = 8 * 1024**3        # 8 GB per file
DISK_RESERVE = 200 * 1024**2    # always leave 200 MB free on the SD card
RANDOM = object()               # marker for "play a random video" requests

DEFAULT_SETTINGS = {
    "enabled": True,            # off = dark window
    "min_pause": 20.0,          # seconds
    "max_pause": 90.0,
    "schedule": False,          # only play between start_time and end_time
    "start_time": "17:00",
    "end_time": "22:00",
    "volume": 100,
    "muted": False,
    "excluded": [],             # videos left out of the random rotation
}


# ---------------------------------------------------------------- log
_IN_JOURNAL = bool(os.environ.get("JOURNAL_STREAM"))   # systemd adds its own timestamps
LOG_LINES: deque = deque(maxlen=80)                    # shown in the web GUI's log box
_log_lock = threading.Lock()


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    with _log_lock:
        LOG_LINES.append(f"{stamp}  {msg}")
    print(msg if _IN_JOURNAL else f"{stamp}  {msg}", flush=True)


# ---------------------------------------------------------------- show hours
def parse_clock(value) -> dtime:
    if isinstance(value, str):
        try:
            hours, minutes = value.strip().split(":")[:2]
            return dtime(int(hours), int(minutes))
        except ValueError:
            pass
    raise ValueError(f"Invalid time: {value!r} (use HH:MM)")


def within_hours(start: str, end: str, now: dtime | None = None) -> bool:
    """True if the clock is between start and end. The window may span midnight."""
    s, e = parse_clock(start), parse_clock(end)
    now = now or datetime.now().time()
    if s == e:
        return True
    if s < e:
        return s <= now < e
    return now >= s or now < e


# ---------------------------------------------------------------- settings
def validate(changes: dict, current: dict) -> dict:
    if not isinstance(changes, dict):
        raise ValueError("Settings must be a JSON object")
    out = {}
    for key, value in changes.items():
        if key not in DEFAULT_SETTINGS:
            continue
        if key in ("enabled", "schedule", "muted"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        elif key in ("min_pause", "max_pause"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 86400:
                raise ValueError("Pauses must be between 0 and 86400 seconds")
            value = float(value)
        elif key in ("start_time", "end_time"):
            value = parse_clock(value).strftime("%H:%M")
        elif key == "volume":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                raise ValueError("Volume must be between 0 and 100")
            value = int(value)
        elif key == "excluded":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError("excluded must be a list of file names")
            value = sorted(set(value))
        out[key] = value
    merged = {**current, **out}
    if merged["min_pause"] > merged["max_pause"]:
        raise ValueError("The shortest pause can't be longer than the longest")
    return out


class Settings:
    """Thread-safe settings, saved as JSON next to the app so they survive restarts."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = json.loads(json.dumps(DEFAULT_SETTINGS))
        try:
            self.data.update(validate(json.loads(path.read_text("utf-8")), self.data))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log(f"⚠️ could not read {path.name} ({e}) — using defaults")

    def get(self) -> dict:
        with self.lock:
            return {**self.data, "excluded": list(self.data["excluded"])}

    def update(self, changes: dict) -> dict:
        with self.lock:
            self.data.update(validate(changes, self.data))
            self._save()
            return {**self.data, "excluded": list(self.data["excluded"])}

    def set_in_rotation(self, video: str, included: bool) -> None:
        with self.lock:
            excluded = set(self.data["excluded"])
            (excluded.discard if included else excluded.add)(video)
            self.data["excluded"] = sorted(excluded)
            self._save()

    def _save(self) -> None:
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log(f"⚠️ could not save settings: {e}")


# ---------------------------------------------------------------- videos
def safe_filename(name: str) -> str:
    """Make an uploaded file name harmless: no folders, no hidden files, video types only."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable()).strip().lstrip(".").strip()
    p = PurePosixPath(name)
    if not name or p.suffix.lower() not in VIDEO_EXTS:
        raise ValueError("Only video files can be uploaded (" + ", ".join(sorted(VIDEO_EXTS)) + ")")
    stem = p.stem[:150].rstrip() or "video"
    return stem + p.suffix.lower()


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while (candidate := p.with_name(f"{p.stem} ({n}){p.suffix}")).exists():
        n += 1
    return candidate


def human(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1000:
            return f"{n:.0f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


class Library:
    """Shuffle without repeats: every video plays once before any plays again, and the
    same video never plays twice in a row. The folder is re-read before every video, so
    videos can be added or removed while the player runs. Sub-folders work too."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.lock = threading.Lock()
        self.played: set[str] = set()
        self.last: str | None = None

    def videos(self) -> list[str]:
        out = []
        for p in self.folder.rglob("*"):
            rel = p.relative_to(self.folder)
            if (p.suffix.lower() in VIDEO_EXTS
                    and not any(part.startswith(".") for part in rel.parts)
                    and p.is_file()):
                out.append(rel.as_posix())
        return sorted(out, key=str.casefold)

    def path(self, name: str) -> Path:
        rel = PurePosixPath(name)
        if (not name or rel.is_absolute()
                or any(part in ("..", "") or part.startswith(".") for part in rel.parts)
                or rel.suffix.lower() not in VIDEO_EXTS):
            raise ValueError(f"No video called {name}")
        p = self.folder.joinpath(*rel.parts)
        if not p.is_file():
            raise ValueError(f"No video called {name}")
        return p

    def next(self, excluded) -> str | None:
        with self.lock:
            skip = set(excluded)
            videos = [v for v in self.videos() if v not in skip]
            if not videos:
                return None
            left = [v for v in videos if v not in self.played]
            if not left:
                self.played.clear()
                left = [v for v in videos if v != self.last] or videos
            pick = random.choice(left)
            self.played.add(pick)
            self.last = pick
            return pick

    def mark_played(self, name: str) -> None:
        with self.lock:
            self.played.add(name)
            self.last = name

    def delete(self, name: str) -> None:
        self.path(name).unlink()
        with self.lock:
            self.played.discard(name)


# ---------------------------------------------------------------- mpv
class MpvError(Exception):
    pass


class Mpv:
    """One fullscreen mpv that stays open (black) between videos, driven over its IPC socket."""

    def __init__(self, extra_args, audio):
        self.extra_args = list(extra_args)
        self.audio = audio                     # callable returning (volume, muted)
        self.sock_path = os.path.join(tempfile.gettempdir(), f"idus-hw-player-{os.getpid()}.sock")
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.buf = b""
        self.req_id = 0
        self.lock = threading.RLock()

    def start(self) -> None:
        with self.lock:
            self.close()
            volume, muted = self.audio()
            cmd = [
                "mpv",
                "--idle=yes",                  # don't quit when a video ends ...
                "--force-window=yes",          # ... keep a black window instead
                "--fullscreen",
                "--hwdec=auto-safe",
                "--osd-level=0",
                "--osc=no",
                "--cursor-autohide=always",
                "--input-default-bindings=no",
                "--no-input-terminal",
                "--msg-level=all=error,ffmpeg=fatal",   # ffmpeg chatters about missing HW decoders
                f"--volume={volume}",
                f"--mute={'yes' if muted else 'no'}",
                f"--input-ipc-server={self.sock_path}",
                *self.extra_args,
            ]
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
            except FileNotFoundError:
                raise MpvError("mpv is missing — install it with: sudo apt install mpv") from None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise MpvError(f"mpv exited right away (code {self.proc.returncode}), see the error above")
                if os.path.exists(self.sock_path):
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        s.connect(self.sock_path)
                    except OSError:
                        s.close()
                    else:
                        s.settimeout(10)
                        self.sock, self.buf = s, b""
                        return
                time.sleep(0.2)
            raise MpvError("no contact with mpv")

    def close(self) -> None:
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            self.proc = None
            try:
                os.unlink(self.sock_path)
            except FileNotFoundError:
                pass

    def _command(self, *args):
        with self.lock:
            if self.sock is None or self.proc is None or self.proc.poll() is not None:
                raise MpvError("mpv is not running")
            self.req_id += 1
            rid = self.req_id
            self.sock.sendall((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
            while True:
                while b"\n" not in self.buf:
                    data = self.sock.recv(65536)
                    if not data:
                        raise MpvError("mpv closed the connection")
                    self.buf += data
                line, self.buf = self.buf.split(b"\n", 1)
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if reply.get("request_id") != rid:
                    continue                   # mpv events — not needed here
                if reply.get("error") != "success":
                    raise MpvError(f"{args[0]}: {reply.get('error')}")
                return reply.get("data")

    def load(self, path: Path) -> None:
        self._command("loadfile", str(path), "replace")

    def stop(self) -> None:
        self._command("stop")

    def idle(self) -> bool:
        return bool(self._command("get_property", "idle-active"))

    def set_audio(self, volume: int, muted: bool) -> None:
        self._command("set_property", "volume", volume)
        self._command("set_property", "mute", muted)


# ---------------------------------------------------------------- player
STATE_LOG = {
    "off": "⏻ switched off — dark window",
    "outside_hours": "· outside show hours — dark window",
    "no_videos": "⚠️ no videos to play",
}


class Player(threading.Thread):
    """Decides what is on screen. Runs in its own thread; the web GUI only pokes it."""

    def __init__(self, mpv: Mpv, library: Library, settings: Settings):
        super().__init__(name="player", daemon=True)
        self.mpv, self.library, self.settings = mpv, library, settings
        self.wake = threading.Event()
        self.quit = threading.Event()
        self.lock = threading.Lock()
        self._request = None           # video name or RANDOM
        self._stop_video = False
        self.state = "starting"
        self.video: str | None = None
        self.pause_ends: float | None = None

    # --- called from the web server ------------------------------------
    def play_now(self, video: str | None = None) -> None:
        with self.lock:
            self._request = video or RANDOM
        self.wake.set()

    def stop_video(self) -> None:
        with self.lock:
            if self.state == "playing":
                self._stop_video = True
        self.wake.set()

    def poke(self) -> None:
        self.wake.set()

    def shutdown(self) -> None:
        self.quit.set()
        self.wake.set()

    def status(self) -> dict:
        with self.lock:
            left = None
            if self.state == "resting" and self.pause_ends is not None:
                left = max(0, round(self.pause_ends - time.monotonic()))
            return {"state": self.state, "video": self.video, "pause_left": left}

    # --- internals -----------------------------------------------------
    def _set_state(self, state, video=None, pause_ends=None):
        with self.lock:
            self.state, self.video, self.pause_ends = state, video, pause_ends

    def _has_request(self) -> bool:
        with self.lock:
            return self._request is not None

    def _take_request(self):
        with self.lock:
            req, self._request = self._request, None
            return req

    def _take_stop(self) -> bool:
        with self.lock:
            stop, self._stop_video = self._stop_video, False
            return stop

    def _wanted(self) -> str:
        s = self.settings.get()
        if not s["enabled"]:
            return "off"
        if s["schedule"] and not within_hours(s["start_time"], s["end_time"]):
            return "outside_hours"
        return "play"

    def run(self) -> None:
        while not self.quit.is_set():
            try:
                self._cycle()
            except (OSError, MpvError) as e:
                if self.quit.is_set():
                    break
                log(f"❌ mpv problem ({e}) — restarting it")
                self._set_state("starting")
                try:
                    self.mpv.start()
                except MpvError as e2:
                    log(f"❌ could not start mpv: {e2} — retrying in 10 s")
                    self.quit.wait(10)
            except Exception as e:     # unexpected: log it and keep going rather than go silent
                log(f"❌ unexpected error in the player: {e!r}")
                self.quit.wait(5)

    def _cycle(self) -> None:
        req = self._take_request()
        if req is not None:
            if req is RANDOM:
                video = self.library.next(self.settings.get()["excluded"])
                if video is None:
                    return self._idle("no_videos")
            else:
                video = req
                self.library.mark_played(video)
            self._play(video)
            return self._pause_if_wanted()

        wanted = self._wanted()
        if wanted != "play":
            return self._idle(wanted)
        video = self.library.next(self.settings.get()["excluded"])
        if video is None:
            return self._idle("no_videos")
        self._play(video)
        self._pause_if_wanted()

    def _idle(self, state: str) -> None:
        if self.state != state:
            log(STATE_LOG[state])
            self.mpv.stop()
            self._set_state(state)
        self.wake.clear()
        expected = "play" if state == "no_videos" else state
        if self._has_request() or self._wanted() != expected or self.quit.is_set():
            return
        self.wake.wait(10 if state == "no_videos" else 30)

    def _play(self, video: str) -> None:
        try:
            path = self.library.path(video)
        except ValueError:
            log(f"⚠️ {video} is gone — skipping it")
            return
        log(f"▶ playing: {video}")
        self._take_stop()
        enabled_at_start = self.settings.get()["enabled"]
        self._set_state("playing", video=video)
        self.mpv.load(path)

        started = time.monotonic()
        while self.mpv.idle():                 # wait for the video to start ...
            if self.quit.is_set():
                return
            if time.monotonic() - started > 15:
                log(f"⚠️ {video} never started — skipping it")
                return
            time.sleep(0.1)

        while not self.quit.is_set():          # ... and for it to end, or for new orders
            self.wake.clear()
            switched_off = enabled_at_start and not self.settings.get()["enabled"]
            if self._has_request() or self._take_stop() or switched_off:
                self.mpv.stop()
                if not self._has_request():
                    log("■ video stopped")
                return
            if self.mpv.idle():
                return
            self.wake.wait(0.5)

    def _pause_if_wanted(self) -> None:
        if self.quit.is_set() or self._has_request() or self._wanted() != "play":
            return
        s = self.settings.get()
        seconds = random.uniform(s["min_pause"], s["max_pause"])
        ends = time.monotonic() + seconds
        log(f"· pause {seconds:.0f} s")
        self._set_state("resting", pause_ends=ends)
        while not self.quit.is_set():
            self.wake.clear()
            if self._has_request() or self._wanted() != "play":
                return
            left = ends - time.monotonic()
            if left <= 0:
                return
            self.wake.wait(min(left, 30))


# ---------------------------------------------------------------- web GUI
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#16181d">
<link rel="icon" type="image/png" href="/icon.png">
<link rel="apple-touch-icon" href="/icon.png">
<title>Idus HW-player</title>
<style>
/* Palette shared with Idus SC Toolbox; teal + ghost glow come from the HW-player icon */
:root {
  --bg: #16181d;
  --group: #1e222b;
  --border: #2a2f3a;
  --button: #262b36;
  --button-border: #333a47;
  --button-hover: #2f3644;
  --deep: #0f1115;
  --text: #e8eaed;
  --muted: #8a93a3;
  --soft: #9fb4d4;
  --log: #c7d1df;
  --title: #ffd9a0;
  --accent: #ff9a3c;
  --ok: #46d160;
  --warn: #ffb13c;
  --crit: #e0525b;
  --night: #051619;
  --teal: #257573;
  --ghost: #ddf1e2;
  --mono: ui-monospace, "JetBrains Mono", "DejaVu Sans Mono", Menlo, Consolas, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", Cantarell, sans-serif;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { color-scheme: dark; background: var(--bg); }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 var(--sans); }
button, input { font: inherit; }
p { margin: 0; }
.wrap { max-width: 76rem; margin: 0 auto; padding: max(1.25rem, env(safe-area-inset-top)) 1rem 2rem; }

/* Header — centred, like the toolbox: icon, title, version */
.head { display: grid; justify-items: center; gap: .25rem; margin-bottom: 1.5rem; text-align: center; }
.head img { width: 84px; height: 84px; }
.head h1 { margin: .25rem 0 0; font-size: 1.85rem; font-weight: 800; letter-spacing: .5px; color: var(--title); line-height: 1.1; }
.head .ver { font-size: .8rem; color: var(--muted); }

/* Two columns on a PC, one on a phone */
.grid { display: grid; gap: 1rem; align-items: start; }
.col { display: grid; gap: 1rem; min-width: 0; }
@media (min-width: 1000px) {
  .grid { grid-template-columns: minmax(22rem, 26rem) minmax(0, 1fr); gap: 1.25rem; }
  .col { gap: 1.25rem; }
}

.group { background: var(--group); border: 1px solid var(--border); border-radius: 12px; padding: 1rem 1.1rem 1.1rem; min-width: 0; }
.group-head { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-bottom: .75rem; min-height: 2rem; }
.group h2 { margin: 0; font-size: .95rem; font-weight: 700; color: var(--muted); }
.group h2 .count { font-weight: 400; margin-left: .35rem; }

button {
  color: var(--text); background: var(--button); border: 1px solid var(--button-border);
  border-radius: 8px; padding: .55rem .95rem; min-height: 40px; cursor: pointer;
}
button:hover { background: var(--button-hover); }
button:disabled { color: #5a6270; cursor: default; }
button.primary { background: var(--accent); border-color: var(--accent); color: #1a1206; font-weight: 700; }
button.primary:hover { background: #ffab5c; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* Power switch */
.switch { display: inline-flex; align-items: center; gap: .55rem; cursor: pointer; font-weight: 600; user-select: none; }
.switch input { position: absolute; opacity: 0; width: 1px; height: 1px; }
.track { position: relative; flex: none; width: 2.75rem; height: 1.6rem; border-radius: 999px; background: var(--button-border); transition: background .2s; }
.track::after { content: ""; position: absolute; top: 3px; left: 3px; width: calc(1.6rem - 6px); height: calc(1.6rem - 6px); border-radius: 50%; background: var(--text); transition: transform .2s; }
.switch input:checked + .track { background: var(--accent); }
.switch input:checked + .track::after { transform: translateX(1.15rem); background: #1a1206; }
.switch input:focus-visible + .track { outline: 2px solid var(--accent); outline-offset: 2px; }

/* The window: a night scene in the icon's colours, lit when a video plays */
.scene {
  display: grid; place-items: center; padding: 1.75rem 1rem; border-radius: 8px;
  background: radial-gradient(120% 70% at 50% 115%, #126b69 0%, #0b3a3c 38%, var(--night) 75%);
}
.window {
  width: min(52%, 11rem); aspect-ratio: 3 / 4;
  display: grid; grid-template: 1fr 1fr / 1fr 1fr; gap: 6px; padding: 8px;
  background: var(--text); border-radius: 2px;
  box-shadow: 0 0 0 3px rgb(0 0 0 / .35);
}
.pane { background: #070b0e; transition: background .8s; }
.window[data-state="resting"] .pane { background: linear-gradient(140deg, #173c40 0%, #0b1d21 40%, #070b0e 75%); }
.window[data-state="playing"] { animation: haunt 3.6s ease-in-out infinite; }
.window[data-state="playing"] .pane { background: radial-gradient(ellipse at 50% 70%, #f2fff6 0%, var(--ghost) 30%, #95cab0 60%, var(--teal) 100%); }
@keyframes haunt {
  0%, 100% { box-shadow: 0 0 0 3px rgb(0 0 0 / .35), 0 0 60px 12px rgb(149 202 176 / .35); }
  45%      { box-shadow: 0 0 0 3px rgb(0 0 0 / .35), 0 0 42px 6px rgb(149 202 176 / .2); }
  65%      { box-shadow: 0 0 0 3px rgb(0 0 0 / .35), 0 0 70px 16px rgb(149 202 176 / .42); }
}

.status { margin-top: 1rem; }
.state { display: flex; align-items: center; gap: .55rem; font-size: 1.35rem; font-weight: 800; }
.dot { width: .7rem; height: .7rem; border-radius: 50%; background: var(--muted); flex: none; }
.dot.ok { background: var(--ok); } .dot.warn { background: var(--warn); } .dot.crit { background: var(--crit); }
.detail { margin-top: .15rem; color: var(--muted); overflow-wrap: anywhere; min-height: 1.5em; }
.actions { display: flex; flex-wrap: wrap; gap: .6rem; margin-top: 1rem; }

/* Settings */
.row { padding: .8rem 0; border-top: 1px solid var(--border); }
.group-head + .row { border-top: 0; padding-top: 0; }
.label { display: block; font-weight: 600; margin-bottom: .45rem; }
.row > .switch { margin-bottom: .45rem; }
.fields { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem .7rem; }
.hint { color: var(--muted); font-size: .85rem; margin-top: .45rem; }
input[type=number], input[type=time] {
  color: var(--text); background: var(--deep); border: 1px solid var(--button-border);
  border-radius: 8px; padding: .4rem .6rem; min-height: 40px; width: 5.5rem;
}
input[type=time] { width: 8.5rem; }
input:disabled { opacity: .45; }
input[type=range] { accent-color: var(--accent); flex: 1 1 8rem; min-height: 40px; }
input[type=checkbox] { accent-color: var(--accent); width: 1.15rem; height: 1.15rem; margin: 0; flex: none; }
.vol { width: 2.5rem; text-align: right; font-family: var(--mono); color: var(--soft); }
.check { display: inline-flex; align-items: center; gap: .45rem; color: var(--soft); cursor: pointer; min-height: 40px; }
.saved { font-size: .85rem; color: var(--muted); }
.saved.err { color: var(--crit); }

/* Videos */
.drop { border: 1.5px dashed var(--button-border); border-radius: 8px; padding: .6rem .8rem; color: var(--muted); font-size: .9rem; margin-bottom: .75rem; }
@media (hover: none) { .drop { display: none; } }
.videos, .uploads { list-style: none; margin: 0; padding: 0; }
.videos li { display: grid; grid-template-columns: minmax(0, 1fr) auto auto auto; align-items: center; gap: .25rem; border-top: 1px solid var(--border); padding: 0 .25rem; border-radius: 6px; }
.videos li:first-child { border-top: 0; }
.videos li:hover { background: var(--button); }
.videos li.empty { display: block; padding: .9rem .25rem; color: var(--muted); }
.videos label { display: flex; align-items: center; gap: .7rem; min-height: 44px; min-width: 0; cursor: pointer; }
.name { overflow-wrap: anywhere; }
.videos li.out .name { color: var(--muted); }
.videos li.now .name { color: var(--title); font-weight: 600; }
.size { font-family: var(--mono); font-size: .85rem; color: var(--soft); white-space: nowrap; padding: 0 .5rem; }
.icon-btn { width: 40px; height: 40px; padding: 0; background: transparent; border-color: transparent; }
.icon-btn:hover { background: var(--button-hover); border-color: var(--button-border); }
.icon-btn.del { color: var(--crit); }
@media (max-width: 600px) {
  /* Phones: size goes under the name so long names keep their width */
  .videos li { grid-template-columns: minmax(0, 1fr) auto auto; grid-template-areas: "name play del" "size play del"; }
  .videos label { grid-area: name; min-height: 0; padding-top: .55rem; }
  .videos .size { grid-area: size; padding: 0 0 .5rem 1.85rem; font-size: .75rem; }
  .videos .play { grid-area: play; }
  .videos .del { grid-area: del; }
  .group-head { flex-wrap: wrap; }
}
#pick { white-space: nowrap; }
.uploads li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .25rem .75rem; padding: .4rem 0; }
.uploads progress { grid-column: 1 / -1; width: 100%; height: 6px; accent-color: var(--accent); }
.uploads .pct { font-family: var(--mono); font-size: .85rem; color: var(--soft); }
.uploads li.err .pct { color: var(--crit); }
.uploads li.done .pct { color: var(--ok); }
.info { color: var(--muted); font-size: .85rem; margin-top: .6rem; }

/* Log — same look as the toolbox log box */
.log {
  margin: 0; background: var(--deep); color: var(--log); border-radius: 8px;
  padding: .6rem .75rem; font: .8rem/1.55 var(--mono); white-space: pre-wrap; overflow-wrap: anywhere;
  height: 16rem; overflow-y: auto;
}

.foot { text-align: center; color: var(--muted); font-size: .8rem; margin-top: 1.75rem; }
.foot a { color: var(--accent); text-decoration: none; }
.foot a:hover { text-decoration: underline; }

/* Drag-and-drop overlay (PC) */
.dropping {
  position: fixed; inset: 0; display: grid; place-items: center; z-index: 10;
  background: rgb(5 22 25 / .88); border: 3px dashed var(--teal);
  color: var(--ghost); font-size: 1.4rem; font-weight: 800;
}
.toast {
  position: fixed; left: 1rem; right: 1rem; bottom: max(1rem, env(safe-area-inset-bottom)); z-index: 11;
  max-width: 30rem; margin: 0 auto; padding: .7rem 1rem;
  background: #2a1517; border: 1px solid var(--crit); border-radius: 8px;
}
@media (prefers-reduced-motion: reduce) {
  .window[data-state="playing"] { animation: none; box-shadow: 0 0 0 3px rgb(0 0 0 / .35), 0 0 60px 12px rgb(149 202 176 / .35); }
  .pane, .track, .track::after { transition: none; }
}
</style>
</head>
<body>
<div class="wrap">
  <header class="head">
    <img src="/icon.png" alt="" width="84" height="84">
    <h1>Idus HW-player</h1>
    <p class="ver" id="ver"></p>
  </header>

  <div class="grid">
    <div class="col">
      <section class="group" aria-labelledby="h-now">
        <div class="group-head">
          <h2 id="h-now">Now</h2>
          <label class="switch">
            <input type="checkbox" id="enabled" role="switch">
            <span class="track" aria-hidden="true"></span>
            <span id="enabled-text">On</span>
          </label>
        </div>
        <div class="scene" aria-hidden="true">
          <div class="window" id="window" data-state="starting">
            <div class="pane"></div><div class="pane"></div><div class="pane"></div><div class="pane"></div>
          </div>
        </div>
        <div class="status" aria-live="polite">
          <p class="state"><span class="dot" id="dot"></span><span id="state">Starting…</span></p>
          <p class="detail" id="detail"></p>
        </div>
        <div class="actions">
          <button class="primary" id="next">Play a video now</button>
          <button id="stop" hidden>Stop video</button>
        </div>
      </section>

      <section class="group" aria-labelledby="h-settings">
        <div class="group-head">
          <h2 id="h-settings">Settings</h2>
          <span class="saved" id="saved" aria-live="polite"></span>
        </div>
        <div class="row">
          <span class="label" id="l-pause">Pause between videos</span>
          <div class="fields" role="group" aria-labelledby="l-pause">
            <input type="number" id="min_pause" min="0" max="86400" step="1" inputmode="numeric" aria-label="Shortest pause in seconds">
            <span>to</span>
            <input type="number" id="max_pause" min="0" max="86400" step="1" inputmode="numeric" aria-label="Longest pause in seconds">
            <span>seconds</span>
          </div>
        </div>
        <div class="row">
          <label class="switch">
            <input type="checkbox" id="schedule" role="switch">
            <span class="track" aria-hidden="true"></span>
            <span>Only play between</span>
          </label>
          <div class="fields">
            <input type="time" id="start_time" aria-label="Start time">
            <span>and</span>
            <input type="time" id="end_time" aria-label="End time">
          </div>
          <p class="hint" id="clock"></p>
        </div>
        <div class="row">
          <label class="label" for="volume">Volume</label>
          <div class="fields">
            <input type="range" id="volume" min="0" max="100" step="1">
            <span class="vol" id="volume-value"></span>
            <label class="check"><input type="checkbox" id="muted"> Mute</label>
          </div>
        </div>
      </section>
    </div>

    <div class="col">
      <section class="group" aria-labelledby="h-videos">
        <div class="group-head">
          <h2 id="h-videos">Videos<span class="count" id="count"></span></h2>
          <button id="pick">Upload videos</button>
          <input type="file" id="files" multiple hidden accept="video/*,.mp4,.m4v,.mkv,.mov,.avi,.webm,.mpg,.mpeg,.wmv">
        </div>
        <p class="drop">Drop video files anywhere on this page to upload them.</p>
        <ul class="uploads" id="uploads"></ul>
        <ul class="videos" id="videos"></ul>
        <p class="info" id="info"></p>
      </section>

      <section class="group" aria-labelledby="h-log">
        <div class="group-head"><h2 id="h-log">Log</h2></div>
        <pre class="log" id="log" tabindex="0"></pre>
      </section>
    </div>
  </div>

  <footer class="foot">
    Made by Idus · <a href="KOFI_URL" target="_blank" rel="noopener">☕ Tip me on Ko-fi</a> · <a href="REPO_URL" target="_blank" rel="noopener">GitHub</a>
  </footer>
</div>
<div class="dropping" id="dropping" hidden>Drop to upload</div>
<div class="toast" id="toast" role="alert" hidden></div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
let st = null, videoSig = "", logSig = "", pauseEnds = null, offline = false, toastTimer = 0;

const STATES = {
  starting:      { title: "Starting…",          dot: "",     detail: () => "" },
  playing:       { title: "Playing",            dot: "ok",   detail: (s) => s.video || "" },
  resting:       { title: "Pausing",            dot: "warn", detail: () => "" },
  off:           { title: "Off",                dot: "",     detail: () => "The window stays dark until you switch the player on." },
  outside_hours: { title: "Outside show hours", dot: "",     detail: (s) => `Starts playing at ${s.settings.start_time}.` },
  no_videos:     { title: "Nothing to play",    dot: "crit", detail: (s) => s.videos.length ? "Tick at least one video in the list." : "Upload a video to get started." },
};

const seconds = (n) => n < 60 ? `${n} s` : (n % 60 ? `${Math.floor(n / 60)} min ${n % 60} s` : `${n / 60} min`);
function size(b) {
  const f = (v, d) => v.toLocaleString(undefined, { maximumFractionDigits: d });
  if (b >= 1e9) return f(b / 1e9, 1) + " GB";
  if (b >= 1e6) return f(b / 1e6, 0) + " MB";
  return f(Math.max(1, b / 1e3), 0) + " KB";
}
function toast(text) {
  const el = $("toast");
  el.textContent = text; el.hidden = false;
  clearTimeout(toastTimer);
  if (!offline) toastTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

async function api(path, data) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-HWP": "1" },
    body: JSON.stringify(data || {}),
  });
  const reply = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(reply.error || `The player answered with error ${r.status}`);
  return reply;
}
async function act(fn) {
  try { await fn(); } catch (e) { toast(e.message); }
  refresh();
}

async function refresh() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    if (!r.ok) throw new Error();
    st = await r.json();
    if (offline) { offline = false; $("toast").hidden = true; }
    render();
  } catch (e) {
    offline = true;
    toast("Lost contact with the player. Retrying…");
  }
}

function setIfIdle(id, prop, value) {
  const el = $(id);
  if (document.activeElement !== el) el[prop] = value;
}

function render() {
  const s = st, c = s.settings, info = STATES[s.state] || { title: s.state, dot: "", detail: () => "" };
  $("ver").textContent = `v${s.version} · ${s.build}`;
  $("window").dataset.state = s.state;
  $("state").textContent = info.title;
  $("dot").className = "dot " + info.dot;
  if (s.state === "resting" && s.pause_left != null) {
    pauseEnds = Date.now() + s.pause_left * 1000;
    renderCountdown();
  } else {
    pauseEnds = null;
    $("detail").textContent = info.detail(s);
  }
  $("stop").hidden = s.state !== "playing";
  $("next").textContent = s.state === "playing" ? "Next video" : "Play a video now";

  setIfIdle("enabled", "checked", c.enabled);
  $("enabled-text").textContent = $("enabled").checked ? "On" : "Off";
  setIfIdle("min_pause", "value", c.min_pause);
  setIfIdle("max_pause", "value", c.max_pause);
  setIfIdle("schedule", "checked", c.schedule);
  setIfIdle("start_time", "value", c.start_time);
  setIfIdle("end_time", "value", c.end_time);
  $("start_time").disabled = $("end_time").disabled = !$("schedule").checked;
  $("clock").textContent = `The player's clock says ${s.clock}.`;
  setIfIdle("volume", "value", c.volume);
  if (document.activeElement !== $("volume")) $("volume-value").textContent = c.volume;
  setIfIdle("muted", "checked", c.muted);

  renderVideos(s);
  renderLog(s.log);
}

function renderCountdown() {
  if (pauseEnds == null) return;
  const left = Math.max(0, Math.round((pauseEnds - Date.now()) / 1000));
  $("detail").textContent = left > 0 ? `Next video in ${seconds(left)}.` : "Next video any second now.";
}

function iconButton(text, label, cls) {
  const b = document.createElement("button");
  b.type = "button"; b.className = "icon-btn " + cls;
  b.textContent = text; b.title = label; b.setAttribute("aria-label", label);
  return b;
}

function renderVideos(s) {
  const sig = JSON.stringify(s.videos);
  if (sig !== videoSig) {
    videoSig = sig;
    const ul = $("videos");
    ul.replaceChildren();
    if (!s.videos.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "No videos yet. Upload some and the haunting can begin.";
      ul.append(li);
    }
    for (const v of s.videos) {
      const li = document.createElement("li");
      li.dataset.video = v.name;
      li.classList.toggle("out", !v.in_rotation);
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox"; box.className = "rot"; box.checked = v.in_rotation;
      box.setAttribute("aria-label", `Include ${v.name} in the rotation`);
      const name = document.createElement("span");
      name.className = "name"; name.textContent = v.name;
      label.append(box, name);
      const sz = document.createElement("span");
      sz.className = "size"; sz.textContent = size(v.size);
      li.append(label, sz,
        iconButton("▶︎", `Play ${v.name} now`, "play"),
        iconButton("✕", `Delete ${v.name}`, "del"));
      ul.append(li);
    }
    const inRot = s.videos.filter((v) => v.in_rotation).length;
    $("count").textContent = s.videos.length ? `(${inRot} of ${s.videos.length} in rotation)` : "";
    $("info").textContent = (s.videos.length ? "Ticked videos are in the random rotation. " : "")
      + `${size(s.disk_free)} free on the player.`;
  }
  for (const li of $("videos").children) {
    li.classList.toggle("now", s.state === "playing" && li.dataset.video === s.video);
  }
}

function renderLog(lines) {
  const sig = lines.length + (lines[lines.length - 1] || "");
  if (sig === logSig) return;
  logSig = sig;
  const el = $("log");
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  el.textContent = lines.join("\n");
  if (atBottom) el.scrollTop = el.scrollHeight;
}

/* --- Settings --- */
let savedTimer = 0;
async function save(changes) {
  const el = $("saved");
  try {
    const fresh = await api("/api/settings", changes);
    if (st) st.settings = fresh;
    el.textContent = "Saved"; el.classList.remove("err");
  } catch (e) {
    el.textContent = e.message; el.classList.add("err");
  }
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => { el.textContent = ""; }, 4000);
  refresh();
}
$("enabled").addEventListener("change", (e) => {
  $("enabled-text").textContent = e.target.checked ? "On" : "Off";
  save({ enabled: e.target.checked });
});
for (const id of ["min_pause", "max_pause"]) {
  $(id).addEventListener("change", () => {
    if ($("min_pause").value === "" || $("max_pause").value === "") return;
    save({ min_pause: Number($("min_pause").value), max_pause: Number($("max_pause").value) });
  });
}
$("schedule").addEventListener("change", (e) => {
  $("start_time").disabled = $("end_time").disabled = !e.target.checked;
  save({ schedule: e.target.checked, start_time: $("start_time").value, end_time: $("end_time").value });
});
for (const id of ["start_time", "end_time"]) {
  $(id).addEventListener("change", () => {
    if ($("start_time").value && $("end_time").value) save({ start_time: $("start_time").value, end_time: $("end_time").value });
  });
}
$("volume").addEventListener("input", (e) => { $("volume-value").textContent = e.target.value; });
$("volume").addEventListener("change", (e) => save({ volume: Number(e.target.value) }));
$("muted").addEventListener("change", (e) => save({ muted: e.target.checked }));

/* --- Buttons --- */
$("next").addEventListener("click", () => act(() => api("/api/play", {})));
$("stop").addEventListener("click", () => act(() => api("/api/stop", {})));
$("videos").addEventListener("change", (e) => {
  if (!e.target.matches("input.rot")) return;
  const video = e.target.closest("li").dataset.video;
  act(() => api("/api/rotation", { video, in_rotation: e.target.checked }));
});
$("videos").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const video = b.closest("li").dataset.video;
  if (b.classList.contains("play")) {
    act(() => api("/api/play", { video }));
  } else if (b.classList.contains("del")) {
    if (confirm(`Delete ${video} from the player? This can't be undone.`)) act(() => api("/api/delete", { video }));
  }
});

/* --- Upload: button, or drag and drop anywhere on the page --- */
$("pick").addEventListener("click", () => $("files").click());
$("files").addEventListener("change", (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  uploadAll(files);
});
let dragDepth = 0;
const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
window.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; $("dropping").hidden = false; });
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("dragleave", (e) => { if (!hasFiles(e)) return; if (--dragDepth <= 0) { dragDepth = 0; $("dropping").hidden = true; } });
window.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; $("dropping").hidden = true;
  uploadAll([...e.dataTransfer.files]);
});
async function uploadAll(files) {
  for (const f of files) await upload(f);
  refresh();
}
function upload(f) {
  return new Promise((done) => {
    const li = document.createElement("li");
    const name = document.createElement("span"); name.className = "name"; name.textContent = f.name;
    const pct = document.createElement("span"); pct.className = "pct"; pct.textContent = "0 %";
    const bar = document.createElement("progress"); bar.max = 100; bar.value = 0;
    li.append(name, pct, bar);
    $("uploads").append(li);
    const fail = (text) => { li.classList.add("err"); pct.textContent = text; bar.remove(); done(); };
    const x = new XMLHttpRequest();
    x.open("POST", "/api/upload?name=" + encodeURIComponent(f.name));
    x.setRequestHeader("X-HWP", "1");
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      const p = Math.floor(ev.loaded / ev.total * 100);
      bar.value = p; pct.textContent = `${p} %`;
    };
    x.onload = () => {
      let reply = {};
      try { reply = JSON.parse(x.responseText); } catch (e) {}
      if (x.status !== 200) return fail(reply.error || `Error ${x.status}`);
      li.classList.add("done"); pct.textContent = "Uploaded"; bar.remove();
      setTimeout(() => li.remove(), 4000);
      done();
    };
    x.onerror = () => fail("Upload interrupted");
    x.send(f);
  });
}

setInterval(renderCountdown, 1000);
setInterval(() => { if (!document.hidden) refresh(); }, 2000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
refresh();
</script>
</body>
</html>
""".replace("KOFI_URL", KOFI_URL).replace("REPO_URL", REPO_URL)

CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")


def make_handler(library: Library, settings: Settings, player: Player, mpv: Mpv, password: str):

    def status() -> dict:
        s = settings.get()
        excluded = set(s["excluded"])
        videos = []
        for name in library.videos():
            try:
                size = (library.folder / name).stat().st_size
            except OSError:
                continue
            videos.append({"name": name, "size": size, "in_rotation": name not in excluded})
        with _log_lock:
            lines = list(LOG_LINES)
        return {
            **player.status(),
            "settings": {k: v for k, v in s.items() if k != "excluded"},
            "videos": videos,
            "disk_free": shutil.disk_usage(library.folder).free,
            "clock": datetime.now().strftime("%H:%M"),
            "log": lines,
            "version": APP_VERSION,
            "build": BUILD,
        }

    class Handler(BaseHTTPRequestHandler):
        server_version = "Idus-HW-player"
        sys_version = ""

        def log_message(self, format, *args):   # no access log (the GUI polls every 2 s)
            pass

        # --- helpers --------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, code: int, message: str) -> None:
            self._json({"error": message}, code)

        def _authorized(self) -> bool:
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    given = base64.b64decode(header[6:], validate=True).decode("utf-8").partition(":")[2]
                except (ValueError, UnicodeDecodeError):
                    given = ""
                if hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8")):
                    return True
                time.sleep(1)                   # slow down password guessing
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Idus HW-player", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _read_json(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValueError("Invalid Content-Length") from None
            if not 0 <= length <= 65536:
                raise ValueError("Request too large")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise ValueError("Invalid JSON") from None
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            return data

        # --- GET --------------------------------------------------------
        def do_GET(self):
            if not self._authorized():
                return
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8",
                           {"Content-Security-Policy": CSP, "Referrer-Policy": "no-referrer",
                            "X-Frame-Options": "DENY"})
            elif path == "/api/status":
                self._json(status())
            elif path == "/icon.png":
                try:
                    self._send(200, ICON_FILE.read_bytes(), "image/png", {"Cache-Control": "max-age=86400"})
                except OSError:
                    self._error(404, "No icon")
            else:
                self._error(404, "Not found")

        # --- POST -------------------------------------------------------
        def do_POST(self):
            if not self._authorized():
                return
            # A custom header means other websites can't send commands here (CSRF protection)
            if self.headers.get("X-HWP") != "1":
                self.close_connection = True
                return self._error(403, "Missing X-HWP header")
            url = urlsplit(self.path)
            try:
                if url.path == "/api/upload":
                    return self._upload(parse_qs(url.query).get("name", [""])[0])
                data = self._read_json()

                if url.path == "/api/settings":
                    data.pop("excluded", None)
                    old = settings.get()
                    new = settings.update(data)
                    if (new["volume"], new["muted"]) != (old["volume"], old["muted"]):
                        try:
                            mpv.set_audio(new["volume"], new["muted"])
                        except (OSError, MpvError):
                            pass                # mpv restarts with the right audio anyway
                    if new["enabled"] != old["enabled"]:
                        log("⏻ switched on" if new["enabled"] else "⏻ switched off")
                    player.poke()
                    return self._json({k: v for k, v in new.items() if k != "excluded"})

                if url.path == "/api/play":
                    video = data.get("video")
                    if video is not None:
                        if not isinstance(video, str):
                            raise ValueError("Invalid video name")
                        library.path(video)
                    player.play_now(video)
                    return self._json({"ok": True})

                if url.path == "/api/stop":
                    player.stop_video()
                    return self._json({"ok": True})

                if url.path == "/api/rotation":
                    video, included = data.get("video"), data.get("in_rotation")
                    if not isinstance(video, str) or not isinstance(included, bool):
                        raise ValueError("Give video and in_rotation (true/false)")
                    library.path(video)
                    settings.set_in_rotation(video, included)
                    player.poke()
                    return self._json({"ok": True})

                if url.path == "/api/delete":
                    video = data.get("video")
                    if not isinstance(video, str):
                        raise ValueError("Say which video to delete")
                    library.delete(video)
                    settings.set_in_rotation(video, True)   # drop it from the excluded list
                    log(f"🗑 deleted {video}")
                    player.poke()
                    return self._json({"ok": True})

                self._error(404, "Not found")
            except ValueError as e:
                self._error(400, str(e))
            except OSError as e:
                log(f"❌ {url.path} failed: {e}")
                self._error(500, "Something went wrong on the player")

        def _upload(self, name: str) -> None:
            filename = safe_filename(name)
            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise ValueError("Content-Length is missing") from None
            if length <= 0:
                raise ValueError("The file is empty")
            if length > MAX_UPLOAD:
                return self._error(413, "The file is too large (max 8 GB)")
            free = shutil.disk_usage(library.folder).free
            if length > free - DISK_RESERVE:
                return self._error(507, f"Not enough space, only {free / 1e9:.1f} GB free on the player")

            tmp = library.folder / f".upload-{secrets.token_hex(6)}.part"
            try:
                with open(tmp, "wb") as f:
                    left = length
                    while left:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            raise ConnectionError("connection lost")
                        f.write(chunk)
                        left -= len(chunk)
                target = unique_path(library.folder / filename)
                os.replace(tmp, target)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                log(f"❌ upload of {filename} failed: {e}")
                try:
                    self._error(500, "The upload failed")
                except OSError:
                    pass
                return
            log(f"✅ uploaded {target.name} ({human(length)})")
            player.poke()
            self._json({"ok": True, "name": target.name})

    return Handler


# ---------------------------------------------------------------- remote control (CLI)
def remote(a) -> int:
    """Send one command to a running player over its web API."""
    base = f"http://{a.host}:{a.port}"
    headers = {"X-HWP": "1", "Content-Type": "application/json"}
    if PASSWORD:
        headers["Authorization"] = "Basic " + base64.b64encode(f"hw:{PASSWORD}".encode()).decode()

    def call(path, data=None):
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(base + path, data=body, headers=headers,
                                     method="GET" if data is None else "POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")

    try:
        if a.on or a.off:
            call("/api/settings", {"enabled": bool(a.on)})
            print("✅ switched on" if a.on else "✅ switched off")
        if a.stop:
            call("/api/stop", {})
            print("✅ stopped the current video")
        if a.next:
            call("/api/play", {})
            print("✅ playing a video")
        if a.status:
            time.sleep(0.5 if (a.on or a.off or a.stop or a.next) else 0)
            s = call("/api/status")
            what = {"playing": f"▶ playing: {s['video']}",
                    "resting": f"· pausing, next video in {s['pause_left']} s",
                    "off": "⏻ off (dark window)",
                    "outside_hours": f"· outside show hours (starts {s['settings']['start_time']})",
                    "no_videos": "⚠️ no videos to play",
                    "starting": "· starting"}.get(s["state"], s["state"])
            on = sum(v["in_rotation"] for v in s["videos"])
            print(f"{APP_NAME} v{s['version']} · {s['build']}\n{what}\n{on} of {len(s['videos'])} videos in rotation")
        return 0
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("❌ wrong or missing password — set HW_PASSWORD or run this on the player itself")
        else:
            print(f"❌ the player answered with error {e.code}")
    except (urllib.error.URLError, OSError) as e:
        print(f"❌ cannot reach the player at {base} ({getattr(e, 'reason', e)})")
    return 1


# ---------------------------------------------------------------- start
def serve(port: int) -> int:
    try:
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"❌ cannot use the video folder {VIDEO_DIR}: {e}")
        return 1
    library = Library(VIDEO_DIR)
    settings = Settings(SETTINGS_FILE)
    if not PASSWORD:
        log("⚠️ no password set (HW_PASSWORD) — anyone on the network can control the player")

    def audio():
        s = settings.get()
        return s["volume"], s["muted"]

    mpv = Mpv(MPV_ARGS, audio)
    player = Player(mpv, library, settings)
    try:
        server = ThreadingHTTPServer((BIND, port), make_handler(library, settings, player, mpv, PASSWORD))
    except OSError as e:
        log(f"❌ cannot start the web server on port {port}: {e}")
        return 1
    server.daemon_threads = True

    try:
        mpv.start()
    except MpvError as e:
        log(f"❌ cannot start mpv: {e}")
        server.server_close()
        return 1

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log(f"🎃 {APP_NAME} v{APP_VERSION} · {BUILD} — videos in {VIDEO_DIR}, web GUI on port {port}")
    player.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        player.shutdown()
        player.join(timeout=5)
        server.server_close()
        mpv.close()
        log("stopped")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="idus-hw-player",
        description=f"{APP_NAME} — random Halloween videos with random dark pauses, "
                    "controlled from a web GUI. With no options the player starts.")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} v{APP_VERSION} · {BUILD}")
    ap.add_argument("--status", action="store_true", help="show what a running player is doing")
    ap.add_argument("--next", action="store_true", help="play a video now")
    ap.add_argument("--stop", action="store_true", help="stop the current video")
    onoff = ap.add_mutually_exclusive_group()
    onoff.add_argument("--on", action="store_true", help="switch the player on")
    onoff.add_argument("--off", action="store_true", help="switch the player off (dark window)")
    ap.add_argument("--list", action="store_true", help="list the videos in the video folder and exit")
    ap.add_argument("--host", default="127.0.0.1", help="player to control (default: this machine)")
    ap.add_argument("--port", type=int, default=PORT, help=f"web port (default: {PORT})")
    a = ap.parse_args()

    if a.list:
        videos = Library(VIDEO_DIR).videos() if VIDEO_DIR.is_dir() else []
        for v in videos:
            print(v)
        print(f"\n{len(videos)} videos in {VIDEO_DIR}")
        return 0
    if a.status or a.next or a.stop or a.on or a.off:
        return remote(a)
    return serve(a.port)


if __name__ == "__main__":
    sys.exit(main())
