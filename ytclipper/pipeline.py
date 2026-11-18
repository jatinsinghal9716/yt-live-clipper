"""Shared clip post-processing: raw cut -> captions -> final render -> metadata."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .captions import Transcriber, write_ass, write_srt
from .clipper import probe, render_final, thumbnail
from .config import ClipConfig
from .detector import Moment
from .report import write_index

log = logging.getLogger("ytclipper.pipeline")


def clip_window(m: Moment, cfg: ClipConfig) -> tuple[float, float]:
    """Chat reacts AFTER the moment, so most of the clip is before the spike."""
    start = m.start - cfg.pre_roll
    end = m.end + cfg.post_roll
    if end - start > cfg.max_length:
        end = start + cfg.max_length
    if end - start < cfg.min_length:
        end = start + cfg.min_length
    return start, end


def _fmt_hms(sec: float) -> str:
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def suggest_title(transcript: str, m: Moment) -> str:
    text = transcript.strip()
    if text:
        first = text.replace("\n", " ")
        for stop in (". ", "! ", "? "):
            if stop in first[15:]:
                first = first[: first.index(stop, 15) + 1]
                break
        if len(first) > 70:
            first = first[:67].rsplit(" ", 1)[0] + "..."
        return first
    # most-repeated chat line, but skip one-word/one-letter spam like "C" or "a"
    for s in m.sample:
        if len(s.split()) >= 3:
            return s[:70]
    return m.sample[0][:70] if m.sample and len(m.sample[0]) >= 4 else "Live highlight"


def finish_clip(raw: Path, clip_dir: Path, m: Moment, cfg: ClipConfig,
                transcriber: Transcriber | None, meta_extra: dict, clips_root: Path) -> dict:
    t0 = time.time()
    ass_name = None
    transcript = ""
    if cfg.captions and transcriber is not None:
        try:
            info = probe(raw)
            if info["has_audio"]:
                words = transcriber.transcribe(raw)
                if words:
                    w, h = (1080, 1920) if cfg.vertical else (info["width"] - info["width"] % 2,
                                                              info["height"] - info["height"] % 2)
                    write_ass(words, clip_dir / "captions.ass", w, h, font=cfg.font)
                    write_srt(words, clip_dir / "captions.srt")
                    ass_name = "captions.ass"
                    transcript = " ".join(x.text for x in words)
        except Exception as exc:  # noqa: BLE001 - captions are optional, never lose the clip
            log.warning("captions skipped: %s", exc)
    final = clip_dir / "clip.mp4"
    render_final(raw, final, vertical=cfg.vertical, ass_name=ass_name, crf=cfg.crf, preset=cfg.preset)
    dur = probe(final)["duration"]
    thumbnail(final, clip_dir / "thumb.jpg", at=min(cfg.pre_roll, dur / 2))
    tags = ["#shorts", "#live"] + [f"#{w}" for w in m.top_words[:3] if w.isalnum()]
    meta = {
        **meta_extra,
        "file": "clip.mp4",
        "raw_file": raw.name,
        "duration": round(dur, 2),
        "suggested_title": suggest_title(transcript, m),
        "hashtags": tags,
        "transcript": transcript,
        "moment": m.to_dict(),
        "render_seconds": round(time.time() - t0, 1),
    }
    (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    write_index(clips_root)
    return meta
