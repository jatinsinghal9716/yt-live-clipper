"""Cutting and rendering with ffmpeg."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .recorder import Segment

log = logging.getLogger("ytclipper.clipper")


class FFmpegError(RuntimeError):
    pass


def run_ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise FFmpegError(f"ffmpeg failed ({res.returncode}): {res.stderr.strip()[-1500:]}")


def probe(path: Path) -> dict:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise FFmpegError(res.stderr.strip())
    data = json.loads(res.stdout)
    info = {"duration": float(data.get("format", {}).get("duration") or 0), "has_audio": False,
            "width": 0, "height": 0}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info["width"]:
            info["width"], info["height"] = int(s["width"]), int(s["height"])
        if s.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def _enc(crf: int, preset: str) -> list[str]:
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart"]


def cut_from_segments(segments: list[Segment], start_wall: float, end_wall: float, out: Path,
                      crf: int = 20, preset: str = "veryfast") -> float:
    """Cut [start_wall, end_wall] out of the rolling buffer. Returns real duration."""
    if not segments:
        raise FFmpegError("no recorded segments cover this moment")
    lst = out.with_suffix(".concat.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for s in segments:
            p = str(s.path.resolve()).replace("'", "'\\''")
            fh.write(f"file '{p}'\n")
    offset = max(0.0, start_wall - segments[0].wall_start)
    duration = max(1.0, min(end_wall, segments[-1].wall_end) - max(start_wall, segments[0].wall_start))
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-ss", f"{offset:.3f}",
                "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a:0?", *_enc(crf, preset), str(out)])
    lst.unlink(missing_ok=True)
    return duration


def cut_from_file(src: Path, start: float, end: float, out: Path, crf: int = 20,
                  preset: str = "veryfast") -> None:
    run_ffmpeg(["-ss", f"{max(0.0, start):.3f}", "-i", str(src), "-t", f"{end - start:.3f}",
                "-map", "0:v:0", "-map", "0:a:0?", *_enc(crf, preset), str(out)])


def render_final(raw: Path, out: Path, *, vertical: bool, ass_name: str | None,
                 crf: int = 20, preset: str = "veryfast") -> tuple[int, int]:
    """Make the shareable clip: optional 9:16 blurred-background layout plus
    burned-in captions. ``ass_name`` is a file name inside raw's folder (run
    with cwd there so the subtitles path needs no escaping on any OS)."""
    info = probe(raw)
    if vertical:
        w, h = 1080, 1920
        graph = (f"[0:v]split=2[a][b];"
                 f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                 f"gblur=sigma=30,eq=brightness=-0.08[bg];"
                 f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    else:
        w, h = info["width"] - info["width"] % 2, info["height"] - info["height"] % 2
        graph = f"[0:v]scale={w}:{h},setsar=1[v0]"
    if ass_name:
        graph += f";[v0]ass={ass_name}[v]"
    else:
        graph += ";[v0]null[v]"
    run_ffmpeg(["-i", raw.name, "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
                *_enc(crf, preset), out.name], cwd=raw.parent)
    return w, h


def thumbnail(clip: Path, out: Path, at: float) -> None:
    try:
        run_ffmpeg(["-ss", f"{at:.2f}", "-i", str(clip), "-frames:v", "1", "-vf", "scale=360:-2",
                    "-q:v", "4", str(out)])
    except FFmpegError as exc:
        log.debug("thumbnail failed: %s", exc)
