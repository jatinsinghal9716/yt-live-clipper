"""Live chat sources.

Every source runs in a background thread and pushes ChatMessage objects into a
queue. ChatMessage.t is a wall-clock epoch for live chat, or the offset in
seconds from the start of the video for replay chat.

Backends:
  * yt-dlp          (default) - yt-dlp's built-in YouTube live chat downloader.
                    It appends one JSON line per chat action to a file and
                    flushes immediately, so we simply tail that file.
  * chat-downloader - xenova/chat-downloader Python API (fallback).
  * file            - replays a JSONL file in real time (demo / testing).
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger("ytclipper.chat")

_RENDERERS = (
    "liveChatTextMessageRenderer",
    "liveChatPaidMessageRenderer",
    "liveChatPaidStickerRenderer",
    "liveChatMembershipItemRenderer",
)


@dataclass
class ChatMessage:
    t: float
    author: str
    text: str
    paid: bool = False
    kind: str = "text"
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # chat is untrusted and gets printed to the terminal: drop ESC/control chars
        self.text = "".join(c for c in self.text if c.isprintable())
        self.author = "".join(c for c in self.author if c.isprintable())


# --------------------------------------------------------------------------- #
# yt-dlp live_chat JSON parsing (same format for live and replay)
# --------------------------------------------------------------------------- #
def _runs_to_text(message: dict | None) -> str:
    if not message:
        return ""
    if "simpleText" in message:
        return message["simpleText"]
    parts = []
    for run in message.get("runs", []):
        if "text" in run:
            parts.append(run["text"])
        elif "emoji" in run:
            emoji = run["emoji"]
            shortcuts = emoji.get("shortcuts") or []
            if emoji.get("isCustomEmoji") and shortcuts:
                parts.append(shortcuts[0])
            else:
                parts.append(emoji.get("emojiId") or (shortcuts[0] if shortcuts else ""))
    return "".join(parts)


def parse_ytdlp_line(line: str, use_offset: bool = False) -> list[ChatMessage]:
    """Parse one line of a yt-dlp ``*.live_chat.json`` file.

    use_offset=False -> t is wall clock (timestampUsec), for live streams.
    use_offset=True  -> t is the video offset (videoOffsetTimeMsec), for replays.
    """
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    replay = obj.get("replayChatItemAction") or {}
    offset_ms = obj.get("videoOffsetTimeMsec") or replay.get("videoOffsetTimeMsec")
    out: list[ChatMessage] = []
    for action in replay.get("actions", []):
        item = (action.get("addChatItemAction") or {}).get("item") or {}
        for kind in _RENDERERS:
            r = item.get(kind)
            if not r:
                continue
            if use_offset:
                if offset_ms is None:
                    break
                t = int(offset_ms) / 1000.0
            else:
                ts = r.get("timestampUsec")
                if ts is None:
                    break
                t = int(ts) / 1_000_000.0
            text = _runs_to_text(r.get("message") or r.get("headerSubtext"))
            author = (r.get("authorName") or {}).get("simpleText", "")
            paid = kind in ("liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer")
            extra = {}
            if paid:
                extra["amount"] = (r.get("purchaseAmountText") or {}).get("simpleText", "")
            out.append(ChatMessage(t=t, author=author, text=text, paid=paid,
                                   kind=kind.replace("liveChat", "").replace("Renderer", ""),
                                   extra=extra))
            break
    return out


def read_ytdlp_chat_file(path: str | Path, use_offset: bool = True) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            msgs.extend(parse_ytdlp_line(line, use_offset=use_offset))
    msgs.sort(key=lambda m: m.t)
    return msgs


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
class ChatSource:
    name = "base"

    def __init__(self) -> None:
        self.queue: "queue.Queue[ChatMessage]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.finished = threading.Event()
        self.received = 0

    def start(self) -> "ChatSource":
        self._thread = threading.Thread(target=self._safe_run, name=f"chat-{self.name}", daemon=True)
        self._thread.start()
        return self

    def _safe_run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - report any backend failure
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("chat backend %s failed: %s", self.name, self.error)
        finally:
            self.finished.set()

    def _emit(self, msgs: Iterable[ChatMessage]) -> None:
        for m in msgs:
            self.received += 1
            self.queue.put(m)

    def drain(self) -> list[ChatMessage]:
        out = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                return out

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class YtDlpChatSource(ChatSource):
    """Runs ``yt-dlp --write-subs --sub-langs live_chat`` and tails its output."""

    name = "yt-dlp"

    def __init__(self, url: str, workdir: Path, cookies: str | None = None, wait: bool = False) -> None:
        super().__init__()
        self.url = url
        self.workdir = Path(workdir)
        self.cookies = cookies
        self.wait = wait
        self.proc: subprocess.Popen | None = None

    def _run(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        out_tmpl = str(self.workdir / "chat.%(ext)s")
        target = self.workdir / "chat.live_chat.json"
        cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-subs",
               "--sub-langs", "live_chat", "--no-part", "--no-overwrites",
               "--quiet", "--no-warnings", "-o", out_tmpl]
        if self.cookies:
            cmd += ["--cookies", self.cookies]
        if self.wait:
            cmd += ["--wait-for-video", "30"]
        cmd.append(self.url)
        log.info("chat: starting yt-dlp live chat reader")
        errlog = open(self.workdir / "chat-ytdlp.log", "wb")
        self.proc = subprocess.Popen(cmd, stdout=errlog, stderr=subprocess.STDOUT)
        buf = ""
        fh = None
        try:
            while not self._stop.is_set():
                if fh is None and target.exists():
                    fh = open(target, encoding="utf-8", errors="replace")
                if fh is not None:
                    chunk = fh.read()
                    if chunk:
                        buf += chunk
                        *lines, buf = buf.split("\n")
                        for line in lines:
                            self._emit(parse_ytdlp_line(line, use_offset=False))
                        continue
                if self.proc.poll() is not None:
                    if fh is not None:  # final read
                        rest = buf + fh.read()
                        for line in rest.split("\n"):
                            self._emit(parse_ytdlp_line(line, use_offset=False))
                    if self.proc.returncode != 0 and self.received == 0:
                        tail = (self.workdir / "chat-ytdlp.log").read_text(errors="replace")[-600:]
                        raise RuntimeError(f"yt-dlp chat exited ({self.proc.returncode}): {tail.strip()}")
                    return
                time.sleep(0.5)
        finally:
            if fh:
                fh.close()
            errlog.close()

    def stop(self) -> None:
        super().stop()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class ChatDownloaderSource(ChatSource):
    """Uses xenova/chat-downloader (pure Python, no API key)."""

    name = "chat-downloader"

    def __init__(self, url: str, cookies: str | None = None) -> None:
        super().__init__()
        self.url = url
        self.cookies = cookies

    def _run(self) -> None:
        from chat_downloader import ChatDownloader  # imported lazily

        kwargs = {"cookies": self.cookies} if self.cookies else {}
        chat = ChatDownloader(**kwargs).get_chat(self.url, message_groups=["messages", "superchat"])
        log.info("chat: connected with chat-downloader")
        for item in chat:
            if self._stop.is_set():
                break
            ts = item.get("timestamp")
            if ts is None:
                continue
            paid = "money" in item
            self._emit([ChatMessage(
                t=ts / 1_000_000.0,
                author=(item.get("author") or {}).get("name", ""),
                text=item.get("message") or "",
                paid=paid,
                kind=item.get("message_type", "text_message"),
                extra={"amount": (item.get("money") or {}).get("text", "")} if paid else {},
            )])


class FileChatSource(ChatSource):
    """Replays a JSONL file ({"offset": s, "author": .., "text": .., "paid": bool})
    in real time, starting from ``start_wall``. Used by the demo and tests."""

    name = "file"

    def __init__(self, path: str | Path, start_wall: float | None = None) -> None:
        super().__init__()
        self.path = Path(path)
        self.start_wall = start_wall

    def _run(self) -> None:
        rows = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]
        rows.sort(key=lambda r: r["offset"])
        start = self.start_wall or time.time()
        for r in rows:
            wait = start + r["offset"] - time.time()
            if wait > 0 and self._stop.wait(wait):
                return
            self._emit([ChatMessage(t=start + r["offset"], author=r.get("author", ""),
                                    text=r.get("text", ""), paid=bool(r.get("paid")))])


class AutoChatSource(ChatSource):
    """Tries yt-dlp first; if it fails before producing anything, falls back
    to chat-downloader."""

    name = "auto"

    def __init__(self, url: str, workdir: Path, cookies: str | None = None, wait: bool = False) -> None:
        super().__init__()
        self.url, self.workdir, self.cookies, self.wait = url, workdir, cookies, wait
        self.active: ChatSource | None = None

    def _forward(self, src: ChatSource) -> None:
        self.active = src
        src.start()
        while not self._stop.is_set():
            msgs = src.drain()
            if msgs:
                self._emit(msgs)
            elif src.finished.is_set():
                self._emit(src.drain())
                if src.error:
                    raise RuntimeError(src.error)
                return
            else:
                time.sleep(0.3)
        src.stop()

    def _run(self) -> None:
        try:
            self._forward(YtDlpChatSource(self.url, self.workdir, self.cookies, self.wait))
            return
        except RuntimeError as exc:
            if self.received:
                raise
            ytdlp_err = exc
            log.warning("yt-dlp chat failed (%s) - falling back to chat-downloader", exc)
        try:
            self._forward(ChatDownloaderSource(self.url, self.cookies))
        except RuntimeError as exc:
            # chat-downloader is unmaintained; keep the yt-dlp error visible too
            raise RuntimeError(f"yt-dlp: {ytdlp_err} | chat-downloader: {exc}") from None

    def stop(self) -> None:
        super().stop()
        if self.active:
            self.active.stop()


def make_chat_source(backend: str, url: str, workdir: Path, cookies: str | None = None,
                     chat_file: str | None = None, start_wall: float | None = None,
                     wait: bool = False) -> ChatSource:
    if backend == "file" or chat_file:
        return FileChatSource(chat_file, start_wall=start_wall)
    if backend == "yt-dlp":
        return YtDlpChatSource(url, workdir, cookies, wait)
    if backend == "chat-downloader":
        return ChatDownloaderSource(url, cookies)
    if backend == "auto":
        return AutoChatSource(url, workdir, cookies, wait)
    raise ValueError(f"unknown chat backend {backend!r}")


def iter_lines_offline(path: str | Path) -> Iterator[ChatMessage]:  # convenience
    yield from read_ytdlp_chat_file(path, use_offset=True)
