"""Command line: ``ytclip live|replay|demo|doctor``."""

from __future__ import annotations

import argparse
import importlib.util
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import ClipConfig, DetectorConfig


def _common(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("output")
    g.add_argument("-o", "--out", default="clips_output", help="output folder (default: ./clips_output)")
    g.add_argument("--horizontal", action="store_true", help="keep 16:9 instead of 9:16 vertical")
    g.add_argument("--no-captions", action="store_true", help="skip faster-whisper captions")
    g.add_argument("--whisper-model", default="base",
                   help="tiny|base|small|medium|large-v3 (bigger = better + slower). default: base")
    g.add_argument("--device", default="auto", help="auto|cpu|cuda for whisper (default auto)")
    g.add_argument("--language", default=None, help="force caption language, e.g. en, hi, es")
    g.add_argument("--pre", type=float, default=30.0, help="seconds before the spike (default 30)")
    g.add_argument("--post", type=float, default=8.0, help="seconds after the spike (default 8)")
    g.add_argument("--max-length", type=float, default=60.0, help="max clip seconds (default 60)")
    g.add_argument("--font", default="Arial", help="caption font name")
    d = p.add_argument_group("spike detection")
    d.add_argument("--sensitivity", choices=["low", "medium", "high"], default="medium",
                   help="high = more clips (small streams), low = fewer (huge streams)")
    d.add_argument("--bin", type=float, default=10.0, help="chat window seconds (default 10)")
    d.add_argument("--min-score", type=float, default=None, help="override minimum score per window")
    d.add_argument("--cookies", default=None, help="cookies.txt (only for members-only/age-gated streams)")
    p.add_argument("--stop-file", default=None, help=argparse.SUPPRESS)  # used by `ytclip web`


def _configs(a) -> tuple[DetectorConfig, ClipConfig]:
    det = DetectorConfig.with_sensitivity(a.sensitivity)
    det.bin_seconds = a.bin
    if a.min_score is not None:
        det.min_score = a.min_score
    clip = ClipConfig(pre_roll=a.pre, post_roll=a.post, max_length=a.max_length,
                      vertical=not a.horizontal, captions=not a.no_captions,
                      whisper_model=a.whisper_model, whisper_device=a.device,
                      language=a.language, font=a.font)
    return det, clip


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ytclip",
        description="Free auto-clipper for YouTube live streams: watches live chat, "
                    "detects hype spikes and cuts captioned vertical clips while the stream is live.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lv = sub.add_parser("live", help="clip a stream that is live right now")
    lv.add_argument("url", help="YouTube live URL, e.g. https://www.youtube.com/watch?v=ID "
                                "or https://www.youtube.com/@channel/live")
    lv.add_argument("--recorder", choices=["streamlink", "yt-dlp"], default="streamlink")
    lv.add_argument("--chat", dest="chat_backend", choices=["auto", "yt-dlp", "chat-downloader"],
                    default="auto")
    lv.add_argument("--quality", default="1080", help="max height: 480|720|1080|best (default 1080)")
    lv.add_argument("--buffer-minutes", type=float, default=15.0,
                    help="minutes of stream kept on disk for clipping (default 15)")
    lv.add_argument("--keep-recording", action="store_true", help="keep the full recording too")
    lv.add_argument("--chat-delay", type=float, default=0.0,
                    help="seconds to shift chat earlier if clips start too late")
    lv.add_argument("--max-clips", type=int, default=0, help="stop after N clips (0 = no limit)")
    lv.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until stream ends)")
    lv.add_argument("--wait", action="store_true",
                    help="stream not live yet? keep checking every 30s until it starts")
    _common(lv)

    rp = sub.add_parser("replay", help="clip a stream that already ended (uses chat replay)")
    rp.add_argument("url", nargs="?", help="YouTube URL of the past live stream")
    rp.add_argument("--top", type=int, default=10, help="keep the N biggest moments (default 10)")
    rp.add_argument("--quality", default="1080")
    rp.add_argument("--video-file", help="use an already-downloaded VOD instead of downloading sections")
    rp.add_argument("--chat-file", help="use an already-downloaded *.live_chat.json")
    rp.add_argument("--dry-run", action="store_true", help="only list the moments, don't cut")
    _common(rp)

    dm = sub.add_parser("demo", help="offline self-test with a fake stream + fake chat (~3 min)")
    _common(dm)

    sub.add_parser("doctor", help="check that ffmpeg & dependencies are installed")

    wb = sub.add_parser("web", help="open the simple web page (paste a link, press Start)")
    wb.add_argument("-o", "--out", default="clips_output", help="output folder (default: ./clips_output)")
    wb.add_argument("--port", type=int, default=8765)
    wb.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    return ap


def doctor() -> int:
    ok = True
    print(f"python      {sys.version.split()[0]}  {'OK' if sys.version_info >= (3, 9) else 'NEED 3.9+'}")
    for exe in ("ffmpeg", "ffprobe"):
        path = shutil.which(exe)
        ok &= bool(path)
        print(f"{exe:<11} {path or 'MISSING  -> install ffmpeg (see README)'}")
    if shutil.which("ffmpeg"):
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        has_ass = " ass " in filters
        ok &= has_ass
        print(f"libass      {'OK' if has_ass else 'MISSING (captions cannot be burned in; use a full ffmpeg build)'}")
    for mod, name in (("yt_dlp", "yt-dlp"), ("streamlink", "streamlink"),
                      ("chat_downloader", "chat-downloader"), ("faster_whisper", "faster-whisper")):
        found = importlib.util.find_spec(mod) is not None
        ok &= found
        print(f"{name:<11} {'OK' if found else 'MISSING -> pip install -r requirements.txt'}")
    print("\nall good!" if ok else "\nsomething is missing (see above)")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "huggingface_hub"):  # model download spams one line per request
        logging.getLogger(noisy).setLevel(logging.ERROR)
    if a.cmd == "doctor":
        return doctor()
    if a.cmd == "web":
        from .web import serve

        return serve(Path(a.out), a.port, open_browser=not a.no_browser)
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found. Run `ytclip doctor` and see the README install steps.", file=sys.stderr)
        return 2
    det, clip = _configs(a)
    out = Path(a.out)

    if a.cmd == "live":
        from .live import LiveClipper, LiveOptions

        opts = LiveOptions(url=a.url, out_dir=out, recorder=a.recorder, chat_backend=a.chat_backend,
                           quality=a.quality, cookies=a.cookies, buffer_minutes=a.buffer_minutes,
                           keep_recording=a.keep_recording, chat_delay=a.chat_delay,
                           max_clips=a.max_clips, duration=a.duration, wait=a.wait,
                           stop_file=a.stop_file)
        lc = LiveClipper(opts, det, clip)
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            # an idle Mac sleeps mid-stream and the buffer/chat freeze (lid-close still sleeps it)
            import os
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        lc.run()
        return 0 if lc.recorded_anything else 1

    if a.cmd == "replay":
        from .replay import ReplayOptions, run_replay

        if not a.url and not (a.chat_file and a.video_file):
            print("replay needs a URL (or both --chat-file and --video-file)", file=sys.stderr)
            return 2
        try:
            res = run_replay(ReplayOptions(url=a.url, out_dir=out, top=a.top, cookies=a.cookies,
                                           quality=a.quality, video_file=a.video_file,
                                           chat_file=a.chat_file, dry_run=a.dry_run,
                                           stop_file=a.stop_file), det, clip)
        except RuntimeError as exc:  # expected failures (no chat replay, ...) - no traceback
            print(f"\nreplay failed: {exc}", file=sys.stderr)
            return 1
        return 0 if res or a.dry_run else 1

    if a.cmd == "demo":
        from .demo import DEMO_SECONDS, make_demo
        from .live import LiveClipper, LiveOptions

        logging.info("building demo stream + chat ...")
        video, chat = make_demo(out / "demo_assets")
        logging.info("replaying it as a 'live stream' for %ds (watch the SPIKE lines)", DEMO_SECONDS)
        opts = LiveOptions(url=None, out_dir=out, input_file=str(video), chat_file=str(chat),
                           chat_grace=2.0, stop_file=a.stop_file)
        clips = LiveClipper(opts, det, clip).run()
        return 0 if clips else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
