"""Live mode: record + read chat + detect spikes + clip, all while the stream is live."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .captions import Transcriber
from .chat import ChatSource, make_chat_source
from .clipper import cut_from_segments
from .config import ClipConfig, DetectorConfig
from .detector import Moment, SpikeDetector
from .pipeline import _fmt_hms, clip_window, finish_clip
from .recorder import SegmentRecorder
from .report import write_index

log = logging.getLogger("ytclipper.live")


@dataclass
class LiveOptions:
    url: str | None
    out_dir: Path
    recorder: str = "streamlink"      # streamlink | yt-dlp
    chat_backend: str = "auto"        # auto | yt-dlp | chat-downloader
    quality: str = "1080"
    cookies: str | None = None
    buffer_minutes: float = 15.0      # rolling buffer kept on disk
    keep_recording: bool = False
    chat_delay: float = 0.0           # shift chat earlier (+) / later (-) if clips feel off
    chat_grace: float = 6.0           # wait this long for late chat before closing a bin
    max_clips: int = 0                # 0 = unlimited
    duration: float = 0.0             # stop after N seconds (0 = until stream ends)
    wait: bool = False                # wait for a scheduled stream to go live
    stop_file: str | None = None      # stop gracefully once this file exists (web page Stop button)
    # demo / testing
    input_file: str | None = None
    chat_file: str | None = None


class LiveClipper:
    def __init__(self, opts: LiveOptions, det_cfg: DetectorConfig, clip_cfg: ClipConfig,
                 transcriber: Transcriber | None = None) -> None:
        self.o, self.det_cfg, self.cfg = opts, det_cfg, clip_cfg
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.session = Path(opts.out_dir) / f"session-{stamp}"
        self.buffer_dir = self.session / "buffer"
        self.clips_dir = self.session / "clips"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.transcriber = transcriber
        if clip_cfg.captions and transcriber is None:
            self.transcriber = Transcriber(clip_cfg.whisper_model, clip_cfg.whisper_device,
                                           clip_cfg.whisper_compute, clip_cfg.language)
        self.jobs: "queue.Queue[Moment | None]" = queue.Queue()
        self.clips: list[dict] = []
        self.pending_starts: list[float] = []  # never delete buffer still needed by a clip
        self.lock = threading.Lock()
        self.recorded_anything = False

    # ------------------------------------------------------------------ #
    def run(self) -> list[dict]:
        o = self.o
        log.info("session folder: %s", self.session.resolve())
        self.recorder = SegmentRecorder(self.buffer_dir, url=o.url, backend=o.recorder,
                                        quality=o.quality, cookies=o.cookies,
                                        input_file=o.input_file, wait=o.wait).start()
        self.chat: ChatSource = make_chat_source(o.chat_backend, o.url, self.session / "chat",
                                                 o.cookies, o.chat_file,
                                                 start_wall=self.recorder.started_wall,
                                                 wait=o.wait).start()
        self.detector = SpikeDetector(self.det_cfg)
        worker = threading.Thread(target=self._worker, daemon=True, name="clip-worker")
        worker.start()
        write_index(self.clips_dir)

        started = time.time()
        last_status = 0.0
        chat_warned = False
        reason = "stopped"
        lag = 0.0  # YouTube live chat reaches us 10-25 s late; bins must stay open that long
        try:
            while True:
                now = time.time()
                for m in self.chat.drain():
                    m.t -= o.chat_delay
                    if now - m.t < 60:  # ignore the old backlog sent on connect
                        lag = max(lag, now - m.t)
                    self.detector.add(m)
                for moment in self.detector.advance(now - max(o.chat_grace, lag + 2)):
                    self._schedule(moment)

                if now - last_status >= 10:
                    last_status = now
                    self._status(now - started)
                if self.chat.finished.is_set() and self.chat.error and not chat_warned:
                    chat_warned = True
                    log.error("chat stopped: %s (recording continues, but no new clips)", self.chat.error)
                if not self.recorder.alive():
                    reason = "stream ended"
                    tail = self.recorder.error_tail()
                    if not self.recorder.segments:
                        log.error("recorder produced no video. %s", tail or "Is the stream live?")
                    break
                if o.duration and now - started >= o.duration:
                    reason = "duration reached"
                    break
                if o.stop_file and Path(o.stop_file).exists():
                    reason = "Stop pressed"
                    break
                if o.max_clips and len(self.clips) + len(self.pending_starts) >= o.max_clips:
                    reason = "max clips reached"
                    break
                if not o.keep_recording:
                    keep_after = now - o.buffer_minutes * 60
                    with self.lock:
                        if self.pending_starts:
                            keep_after = min(keep_after, min(self.pending_starts) - 5)
                    self.recorder.cleanup(keep_after)
                time.sleep(0.5)
        except KeyboardInterrupt:
            reason = "Ctrl+C"
        log.info("stopping (%s) - finishing pending clips...", reason)
        # give chat a moment to flush, then close every open bin
        for m in self.chat.drain():
            m.t -= o.chat_delay
            self.detector.add(m)
        for moment in self.detector.flush():
            self._schedule(moment)
        self.jobs.put(None)
        try:
            worker.join()
        except KeyboardInterrupt:
            log.warning("interrupted while rendering; partial results kept")
        self.chat.stop()
        self.recorder.stop()
        self.recorded_anything = bool(self.recorder.segments) or bool(self.clips)
        if not o.keep_recording:
            import shutil
            shutil.rmtree(self.buffer_dir, ignore_errors=True)  # temp buffer, clips are kept
        write_index(self.clips_dir)
        log.info("done: %d clip(s) -> %s", len(self.clips), (self.clips_dir / "index.html").resolve())
        return self.clips

    # ------------------------------------------------------------------ #
    def _status(self, elapsed: float) -> None:
        st = self.detector.last_status
        seg = len(self.recorder.segments)
        log.info("[%s] chat msgs=%d | last bin %s | buffer=%d segs | clips=%d (+%d rendering)",
                 _fmt_hms(elapsed), self.chat.received,
                 ", ".join(f"{k}={v}" for k, v in st.items()) or "-", seg, len(self.clips), len(self.pending_starts))

    def _schedule(self, m: Moment) -> None:
        start, end = clip_window(m, self.cfg)
        with self.lock:
            self.pending_starts.append(start)
        log.info("SPIKE! %.1fx normal chat (%d msgs, %d people) - clipping %ds window",
                 m.intensity, m.messages, m.authors, int(end - start))
        self.jobs.put(m)

    def _worker(self) -> None:
        idx = 0
        while True:
            m = self.jobs.get()
            if m is None:
                return
            idx += 1
            try:
                meta = self._make_clip(idx, m)
                if meta:
                    self.clips.append(meta)
                    log.info("CLIP READY #%d: %s  (\"%s\")", idx, meta["path"], meta["suggested_title"])
            except Exception as exc:  # noqa: BLE001 - one bad clip must not stop the session
                log.error("clip #%d failed: %s", idx, exc)
            finally:
                with self.lock:
                    self.pending_starts.remove(clip_window(m, self.cfg)[0])

    def _make_clip(self, idx: int, m: Moment) -> dict | None:
        start, end = clip_window(m, self.cfg)
        # wait until the recorder has received the end of the window
        self.recorder.wait_until(end, timeout=end - time.time() + 90)
        segs = self.recorder.covering(start, end)
        if not segs:
            log.warning("clip #%d: video for this moment is not in the buffer (skipped)", idx)
            return None
        t_rel = start - (self.recorder.started_wall or start)
        name = f"{idx:03d}_{datetime.fromtimestamp(m.start).strftime('%H%M%S')}"
        clip_dir = self.clips_dir / name
        clip_dir.mkdir(parents=True, exist_ok=True)
        raw = clip_dir / "raw.mp4"
        cut_from_segments(segs, start, end, raw, crf=self.cfg.crf, preset=self.cfg.preset)
        meta = finish_clip(raw, clip_dir, m, self.cfg, self.transcriber, {
            "index": idx,
            "source": self.o.url or self.o.input_file,
            "stream_time": _fmt_hms(t_rel),
            "wall_time": datetime.fromtimestamp(start).isoformat(timespec="seconds"),
        }, self.clips_dir)
        meta["path"] = str((clip_dir / "clip.mp4").resolve())
        return meta
