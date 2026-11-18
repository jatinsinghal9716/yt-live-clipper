"""Replay mode: for streams that already ended (and still have chat replay).

1. yt-dlp downloads ONLY the chat replay (tiny).
2. The same spike detector runs over it using video offsets.
3. yt-dlp downloads ONLY the seconds around each spike (--download-sections),
   or cuts them from a local file if you already have the VOD.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .captions import Transcriber
from .chat import read_ytdlp_chat_file
from .clipper import FFmpegError, cut_from_file
from .config import ClipConfig, DetectorConfig
from .detector import Moment, detect_offline
from .pipeline import _fmt_hms, clip_window, finish_clip
from .report import write_index

log = logging.getLogger("ytclipper.replay")


@dataclass
class ReplayOptions:
    url: str | None
    out_dir: Path
    top: int = 10
    cookies: str | None = None
    quality: str = "1080"
    video_file: str | None = None     # use a local VOD instead of downloading sections
    chat_file: str | None = None      # use a local *.live_chat.json instead of downloading
    dry_run: bool = False
    stop_file: str | None = None      # stop before the next clip once this file exists


def download_chat(url: str, dest: Path, cookies: str | None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-subs", "--sub-langs",
           "live_chat", "--no-warnings", "-o", str(dest / "chat.%(ext)s")]
    if cookies:
        cmd += ["--cookies", cookies]
    log.info("downloading chat replay (only the chat, no video; long streams take a few minutes)...")
    res = subprocess.run(cmd + [url], capture_output=True, text=True)
    path = dest / "chat.live_chat.json"
    if res.returncode != 0 or not path.exists():
        out = res.stderr + res.stdout
        if "no subtitles for the requested languages" in out:
            raise RuntimeError("this video has no chat replay (the streamer turned it off), "
                               "so there is nothing to detect moments from.")
        raise RuntimeError("could not download chat replay:\n" + out[-1000:])
    return path


def download_section(url: str, start: float, end: float, out: Path, quality: str,
                     cookies: str | None) -> Path:
    fmt = ("bv*+ba/b" if quality == "best"
           else f"bv*[height<={quality}]+ba/b[height<={quality}]/b")
    tmpl = str(out.with_suffix("")) + ".%(ext)s"
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-f", fmt,
           "--download-sections", f"*{max(0, start):.2f}-{end:.2f}", "--force-keyframes-at-cuts",
           "--merge-output-format", "mp4", "-o", tmpl]
    if cookies:
        cmd += ["--cookies", cookies]
    res = subprocess.run(cmd + [url], capture_output=True, text=True)
    produced = sorted(out.parent.glob(out.stem + ".*"))
    produced = [p for p in produced if p.suffix in (".mp4", ".mkv", ".webm")]
    if res.returncode != 0 or not produced:
        raise RuntimeError(f"yt-dlp section download failed: {(res.stderr or res.stdout)[-800:]}")
    return produced[0]


def run_replay(o: ReplayOptions, det_cfg: DetectorConfig, cfg: ClipConfig,
               transcriber: Transcriber | None = None) -> list[dict]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session = Path(o.out_dir) / f"replay-{stamp}"
    clips_dir = session / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    chat_path = Path(o.chat_file) if o.chat_file else download_chat(o.url, session / "chat", o.cookies)
    msgs = read_ytdlp_chat_file(chat_path, use_offset=True)
    log.info("chat replay: %d messages", len(msgs))
    if not msgs:
        raise RuntimeError("chat replay is empty")
    moments = detect_offline(msgs, det_cfg)
    ranked = sorted(moments, key=lambda m: (m.intensity, m.total_score), reverse=True)[: o.top]
    ranked.sort(key=lambda m: m.start)
    log.info("found %d spike(s); keeping top %d", len(moments), len(ranked))
    for i, m in enumerate(ranked, 1):
        s, e = clip_window(m, cfg)
        log.info("  #%d at %s  %.1fx  %d msgs  e.g. %s", i, _fmt_hms(m.start), m.intensity,
                 m.messages, " | ".join(m.sample[:3]))
    if o.dry_run:
        return [dict(m.to_dict(), stream_time=_fmt_hms(m.start)) for m in ranked]

    if cfg.captions and transcriber is None:
        transcriber = Transcriber(cfg.whisper_model, cfg.whisper_device, cfg.whisper_compute, cfg.language)
    results = []
    for i, m in enumerate(ranked, 1):
        if o.stop_file and Path(o.stop_file).exists():
            log.info("stopped - keeping the %d clip(s) already made", len(results))
            break
        s, e = clip_window(m, cfg)
        s = max(0.0, s)
        clip_dir = clips_dir / f"{i:03d}_{_fmt_hms(m.start).replace(':', '-')}"
        clip_dir.mkdir(parents=True, exist_ok=True)
        raw = clip_dir / "raw.mp4"
        try:
            if o.video_file:
                cut_from_file(Path(o.video_file), s, e, raw, crf=cfg.crf, preset=cfg.preset)
            else:
                got = download_section(o.url, s, e, clip_dir / "source", o.quality, o.cookies)
                cut_from_file(got, 0, e - s, raw, crf=cfg.crf, preset=cfg.preset)
                got.unlink(missing_ok=True)
            meta = finish_clip(raw, clip_dir, m, cfg, transcriber, {
                "index": i, "source": o.url or o.video_file, "stream_time": _fmt_hms(s),
                "video_offset": round(s, 2)}, clips_dir)
            meta["path"] = str((clip_dir / "clip.mp4").resolve())
            results.append(meta)
            log.info("CLIP READY #%d: %s", i, meta["path"])
        except (RuntimeError, FFmpegError) as exc:
            log.error("clip #%d failed: %s", i, exc)
    write_index(clips_dir)
    log.info("done: %d clip(s) -> %s", len(results), (clips_dir / "index.html").resolve())
    return results
