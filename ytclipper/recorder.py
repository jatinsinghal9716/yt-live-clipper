"""Rolling live recorder.

A producer (streamlink or yt-dlp) writes the live stream to stdout; ffmpeg
copies it (no re-encode, ~0% CPU) into short MPEG-TS segments. A watcher
thread stamps every finished segment with the wall-clock time it arrived, so a
chat timestamp can be mapped straight onto recorded video while the stream is
still live. Old segments are deleted to keep disk usage bounded.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ytclipper.recorder")


@dataclass
class Segment:
    path: Path
    wall_start: float
    wall_end: float

    @property
    def duration(self) -> float:
        return self.wall_end - self.wall_start


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    # also look next to the running interpreter (venv/bin or venv/Scripts)
    cand = Path(sys.executable).parent / (name + (".exe" if sys.platform == "win32" else ""))
    return str(cand) if cand.exists() else None


def producer_command(backend: str, url: str, quality: str, cookies: str | None,
                     wait: bool = False) -> list[str]:
    if backend == "streamlink":
        exe = _which("streamlink")
        base = [exe] if exe else [sys.executable, "-m", "streamlink"]
        q = "best" if quality == "best" else f"{quality}p,{quality}p60,best"
        return base + ["--stdout", "--loglevel", "error", "--hls-live-edge", "3",
                       "--stream-segment-attempts", "5",
                       *(["--retry-streams", "30", "--retry-max", "0"] if wait else []), url, q]
    if backend == "yt-dlp":
        fmt = "best" if quality == "best" else f"best[height<={quality}]/best"
        cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-part",
               "-f", fmt, "-o", "-"]
        if cookies:
            cmd += ["--cookies", cookies]
        if wait:
            cmd += ["--wait-for-video", "30"]
        return cmd + [url]
    raise ValueError(f"unknown recorder backend {backend!r}")


class SegmentRecorder:
    def __init__(self, workdir: Path, *, url: str | None = None, backend: str = "streamlink",
                 quality: str = "1080", cookies: str | None = None, input_file: str | None = None,
                 segment_seconds: int = 4, wait: bool = False) -> None:
        self.dir = Path(workdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.url, self.backend, self.quality, self.cookies = url, backend, quality, cookies
        self.input_file = input_file
        self.segment_seconds = segment_seconds
        self.wait = wait
        self.segments: list[Segment] = []
        self.lock = threading.Lock()
        self.producer: subprocess.Popen | None = None
        self.ffmpeg: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self.started_wall: float | None = None
        self.ended = threading.Event()

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> "SegmentRecorder":
        list_file = self.dir / "segments.csv"
        if self.input_file:  # demo / test: play a local file in real time
            inp = ["-re", "-i", self.input_file]
            stdin = None
        else:
            cmd = producer_command(self.backend, self.url, self.quality, self.cookies, self.wait)
            log.info("recorder: %s", " ".join(cmd[:3]) + " ...")
            self._prod_log = open(self.dir / "producer.log", "wb")
            self.producer = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=self._prod_log)
            inp = ["-i", "pipe:0"]
            stdin = self.producer.stdout
        ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
              "-fflags", "+genpts", *inp,
              "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
              "-f", "segment", "-segment_time", str(self.segment_seconds),
              "-segment_format", "mpegts", "-reset_timestamps", "1",
              "-segment_list", str(list_file), "-segment_list_type", "csv",
              "-segment_list_flags", "+live",
              str(self.dir / "seg_%06d.ts")]
        self._ff_log = open(self.dir / "ffmpeg-record.log", "wb")
        self.ffmpeg = subprocess.Popen(ff, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                                       stderr=self._ff_log)
        if self.producer and self.producer.stdout:
            self.producer.stdout.close()  # let producer get SIGPIPE if ffmpeg dies
        self.started_wall = time.time()
        self._watcher = threading.Thread(target=self._watch, args=(list_file,), daemon=True,
                                         name="segment-watcher")
        self._watcher.start()
        return self

    def _watch(self, list_file: Path) -> None:
        pos = 0
        buf = ""
        while True:
            done = self.ffmpeg.poll() is not None
            if list_file.exists():
                with open(list_file, encoding="utf-8") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                buf += chunk
                *lines, buf = buf.split("\n")
                now = time.time()
                for line in lines:
                    parts = line.strip().split(",")
                    if len(parts) < 3:
                        continue
                    try:
                        dur = float(parts[2]) - float(parts[1])
                    except ValueError:
                        continue
                    seg = Segment(self.dir / parts[0], now - dur, now)
                    with self.lock:
                        if self.segments and seg.wall_start < self.segments[-1].wall_end:
                            # several segments flushed at once -> lay them end to end
                            seg = Segment(seg.path, self.segments[-1].wall_end,
                                          self.segments[-1].wall_end + dur)
                        self.segments.append(seg)
            if done:
                self.ended.set()
                return
            time.sleep(0.4)

    def alive(self) -> bool:
        return not self.ended.is_set()

    def stop(self) -> None:
        for p in (self.producer, self.ffmpeg):
            if p and p.poll() is None:
                p.terminate()
        for p in (self.producer, self.ffmpeg):
            if p:
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.ended.wait(timeout=5)

    def error_tail(self) -> str:
        out = []
        for name in ("producer.log", "ffmpeg-record.log"):
            p = self.dir / name
            if p.exists():
                txt = p.read_text(errors="replace").strip()
                if txt:
                    out.append(f"[{name}] {txt[-800:]}")
        return "\n".join(out)

    # -- queries ----------------------------------------------------------- #
    def latest_wall(self) -> float | None:
        with self.lock:
            return self.segments[-1].wall_end if self.segments else None

    def covering(self, a: float, b: float) -> list[Segment]:
        with self.lock:
            return [s for s in self.segments if s.wall_end > a and s.wall_start < b and s.path.exists()]

    def wait_until(self, wall_t: float, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            last = self.latest_wall()
            if last is not None and last >= wall_t:
                return True
            if not self.alive():
                return False
            time.sleep(0.5)
        return False

    def cleanup(self, keep_after_wall: float) -> int:
        """Delete segments that ended before ``keep_after_wall``."""
        removed = 0
        with self.lock:
            keep = []
            for s in self.segments:
                if s.wall_end < keep_after_wall:
                    try:
                        s.path.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        keep.append(s)
                else:
                    keep.append(s)
            self.segments = keep
        return removed
