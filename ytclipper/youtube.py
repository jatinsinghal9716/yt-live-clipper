"""Post clips to the user's own YouTube channel (official YouTube Data API v3, free).

Setup (app owner, once): a Google Cloud project with the YouTube Data API enabled and an
OAuth client of type "Desktop app", saved as ``client_secret.json`` in the app folder.
Each user then clicks "Connect YouTube"; their login is stored only on their computer in
``~/.ytclipper/youtube_token.json``.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("ytclipper.youtube")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]  # readonly: show the channel name
HOME = Path(os.environ.get("YTCLIP_HOME", Path.home() / ".ytclipper"))
TOKEN = HOME / "youtube_token.json"
SETTINGS = HOME / "settings.json"
APP_DIR = Path(__file__).resolve().parent.parent


def client_secret_path() -> Path | None:
    for p in (os.environ.get("YTCLIP_CLIENT_SECRET"), Path.cwd() / "client_secret.json",
              APP_DIR / "client_secret.json", HOME / "client_secret.json"):
        if p and Path(p).is_file():
            return Path(p)
    return None


# --------------------------------------------------------------------------- #
# settings + login
# --------------------------------------------------------------------------- #
def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # login token: owner-only
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(**kw) -> dict:
    s = load_settings() | kw
    _write_private(SETTINGS, json.dumps(s, indent=2))
    return s


def credentials():
    """Saved login, refreshed if needed; None when not connected."""
    if not TOKEN.is_file():
        return None
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            _write_private(TOKEN, creds.to_json())
        return creds if creds.valid else None
    except (RefreshError, ValueError, OSError) as exc:  # revoked / expired / corrupt
        log.warning("YouTube login no longer works (%s) - connect again", exc)
        TOKEN.unlink(missing_ok=True)  # so the page shows "Connect YouTube" again
        return None


def connect(timeout: int = 300) -> str:
    """Opens Google's sign-in in the browser; returns the connected channel name."""
    secret = client_secret_path()
    if not secret:
        raise RuntimeError("client_secret.json is missing - see 'Post to YouTube' in the README")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=True, timeout_seconds=timeout,
        authorization_prompt_message="",
        success_message="YouTube is connected. You can close this tab and go back to YT Live Clipper.")
    if not creds:
        raise RuntimeError("sign-in was not finished")
    _write_private(TOKEN, creds.to_json())
    name = channel_name(creds)
    save_settings(channel=name)
    return name


def disconnect() -> None:
    TOKEN.unlink(missing_ok=True)
    save_settings(channel=None, auto_upload=False)


def _service(creds=None):
    from googleapiclient.discovery import build

    creds = creds or credentials()
    if creds is None:
        raise RuntimeError("your YouTube login expired or was removed - click Connect YouTube again")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def channel_name(creds=None) -> str:
    items = _service(creds).channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        raise RuntimeError("this Google account has no YouTube channel yet - create one on youtube.com first")
    return items[0]["snippet"]["title"]


# --------------------------------------------------------------------------- #
# title / description / tags
# --------------------------------------------------------------------------- #
_ANGLE = re.compile(r"[<>]")  # YouTube rejects < and > in titles/descriptions
_STOP = {"live", "stream", "streaming", "the", "and", "with", "for", "from", "new", "now", "part",
         "day", "vs", "feat", "ft", "official", "full", "video", "hindi", "english", "today"}


# Chat comes from strangers but gets published under the user's channel: only reuse
# chat text that has no links/handles/phone numbers and no common abuse words.
# Strict on purpose (a blocklist of domains is easy to dodge): any dot, look-alike dot or
# spelled-out "dot", any 3+ digit run, @handles, slashes and colons rule a chat line out.
_UNSAFE = re.compile(r"[.\u3002\uff0e\uff61\u2024\u00b7\u2022/\\:@]|\(dot\)|\[dot\]|\bdot\b|\d{3,}"
                     r"|discord|telegram|whatsapp|insta|onlyfans", re.I)
_ABUSE = {"fuck", "fucking", "fucker", "shit", "bitch", "cunt", "dick", "pussy", "whore", "slut", "nigga",
          "nigger", "faggot", "retard", "porn", "nude", "nudes", "sex", "rape", "kys",
          "bc", "mc", "bsdk", "chutiya", "chutiye", "madarchod", "behenchod", "bhenchod", "bhosdi", "bhosdike",
          "randi", "gandu", "lund", "lauda", "loda", "harami", "kutta", "kutte", "kamina",
          "चूतिया", "मादरचोद", "बहनचोद", "भोसड़ी", "रंडी", "गांडू", "हरामी"}


def safe_chat(text: str) -> bool:
    """True when a chat line is OK to publish in a title, description or tag."""
    from .detector import _tokens

    t = (text or "").strip()
    return (len(t.split()) <= 8 and not _UNSAFE.search(t)
            and not ({w.lower() for w in _tokens(t)} & _ABUSE))


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _ANGLE.sub("", s or "")).strip()


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0].rstrip(" ,.-|") + "…"


def build_metadata(meta: dict) -> dict:
    """Title (<=100), description (<=5000) and tags (<=500 chars) from a clip's meta.json."""
    from .detector import _tokens

    src_title = _clean(meta.get("source_title", ""))
    src_channel = _clean(meta.get("source_channel", ""))
    src_url = meta.get("source_url") or meta.get("source", "")
    if not str(src_url).startswith("http"):
        src_url = ""
    said = _clean(meta.get("suggested_title", ""))
    said = said if safe_chat(said) else ""  # may be a chat line
    hook = _hook(meta)
    base = f"{hook} | {src_title}" if hook and src_title else hook or src_title or said
    title = _cut(base or "Stream highlight", 100 - len(" #Shorts")) + " #Shorts"

    tags: list[str] = []
    chat_words = [w for w in meta.get("moment", {}).get("top_words", []) if safe_chat(w)]
    for t in ([src_channel] if src_channel else []) + chat_words + \
            [w for w in _tokens(src_title.lower()) if len(w) > 2 and w not in _STOP and not w.isdigit()][:8] + \
            ["shorts", "live stream", "stream highlights", "clips", "highlights"]:
        t = _clean(t)[:30]
        if t and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    budget, kept = 450, []
    for t in tags:  # YouTube counts quotes around tags with spaces, plus commas
        cost = len(t) + (2 if " " in t else 0) + 1
        if cost > budget:
            break
        kept.append(t)
        budget -= cost

    hashtags = " ".join(h for h in meta.get("hashtags", []) if h.lower() != "#shorts" and safe_chat(h[1:]))
    chat = [f'"{_cut(_clean(c), 60)}"' for c in meta.get("moment", {}).get("sample_chat", [])
            if _clean(c) and safe_chat(c)][:3]
    lines = [_cut(_clean(meta.get("transcript", "")), 300) or said or src_title, ""]
    if src_title or src_url:
        lines.append(f"🎥 Clipped from: {src_title or 'live stream'}")
    if src_channel:
        lines.append(f"📺 Channel: {src_channel}")
    if src_url:
        lines.append(f"🔗 Full stream: {src_url}")
    if meta.get("stream_time"):
        lines.append(f"⏱ Moment at {meta['stream_time']} in the stream")
    if chat:
        lines += ["", "💬 Chat reacted: " + ", ".join(chat)]
    lines += ["", f"#Shorts {hashtags}".strip(), "", "Clipped automatically with YT Live Clipper."]
    return {"title": title, "description": _cut(_clean_desc("\n".join(lines)), 5000), "tags": kept}


def _hook(meta: dict) -> str:
    """The catchy first half of a title: a short, complete spoken sentence, else chat's top reaction."""
    for sent in re.split(r"(?<=[.!?])\s+", _clean(meta.get("transcript", ""))):
        if 3 <= len(sent.split()) <= 12 and sent[-1:] in ".!?":
            return sent
    for c in meta.get("moment", {}).get("sample_chat", []):
        c = _clean(re.sub(r":[a-z][a-z0-9_-]+:", "", c, flags=re.I))  # YouTube emoji codes
        if 2 <= len(c) <= 40 and safe_chat(c):
            return f'Chat: "{c}"'
    return ""


def _clean_desc(s: str) -> str:
    return "\n".join(_ANGLE.sub("", line).rstrip() for line in s.splitlines())


# --------------------------------------------------------------------------- #
# upload
# --------------------------------------------------------------------------- #
def _friendly(exc: Exception) -> str:
    text = str(exc)
    for key, msg in (
        ("quotaExceeded", "this app's free daily YouTube upload limit is used up - it resets at midnight Pacific time"),
        ("uploadLimitExceeded", "your channel hit YouTube's daily upload limit - try again tomorrow"),
        ("youtubeSignupRequired", "this Google account has no YouTube channel yet - create one on youtube.com"),
        ("invalidTitle", "YouTube rejected the title"),
        ("invalidTags", "YouTube rejected the tags"),
        ("forbidden", "YouTube refused the upload for this account"),
    ):
        if key in text:
            return msg
    return text[-300:]


def _write_meta(clip_dir: Path, meta: dict) -> None:
    (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def upload_clip(clip_dir: Path, overrides: dict | None = None) -> str:
    """Uploads clip.mp4 publicly; records the result in meta.json; returns the video URL."""
    from googleapiclient.http import MediaFileUpload

    clip_dir = Path(clip_dir)
    meta = json.loads((clip_dir / "meta.json").read_text(encoding="utf-8"))
    md = build_metadata(meta) | {k: v for k, v in (overrides or {}).items() if v}
    md["title"] = _cut(_clean(md["title"]), 100) or "Stream highlight #Shorts"
    md["description"] = _cut(_clean_desc(md["description"]), 5000)
    meta["youtube"] = {"state": "uploading", "title": md["title"]}
    _write_meta(clip_dir, meta)
    try:
        body = {
            "snippet": {"title": md["title"], "description": md["description"],
                        "tags": md["tags"], "categoryId": "24"},  # 24 = Entertainment
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(clip_dir / "clip.mp4"), mimetype="video/mp4",
                                chunksize=8 * 1024 * 1024, resumable=True)
        req = _service().videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            _, resp = req.next_chunk(num_retries=3)
        url = f"https://youtube.com/shorts/{resp['id']}"
        meta["youtube"] = {"state": "posted", "id": resp["id"], "url": url, "title": md["title"],
                           "privacy": resp.get("status", {}).get("privacyStatus", "public"),
                           "posted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _write_meta(clip_dir, meta)
        return url
    except Exception as exc:  # noqa: BLE001 - record any failure next to the clip
        meta["youtube"] = {"state": "failed", "error": _friendly(exc), "title": md["title"]}
        _write_meta(clip_dir, meta)
        raise RuntimeError(meta["youtube"]["error"]) from None


class Uploader:
    """Uploads clips one at a time in the background, so clipping never waits for YouTube."""

    def __init__(self) -> None:
        self.q: "queue.Queue[tuple[Path, dict | None] | None]" = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True, name="youtube-upload")
        self.thread.start()

    def add(self, clip_dir: Path, overrides: dict | None = None) -> None:
        self.q.put((Path(clip_dir), overrides))

    def _run(self) -> None:
        while (item := self.q.get()) is not None:
            clip_dir, overrides = item
            log.info("YOUTUBE: uploading %s ...", clip_dir.name)
            try:
                log.info("YOUTUBE POSTED: %s", upload_clip(clip_dir, overrides))
            except Exception as exc:  # noqa: BLE001 - one failed upload must not stop the rest
                log.error("YOUTUBE upload failed for %s: %s", clip_dir.name, exc)

    def finish(self) -> None:
        """Wait for queued uploads (called when clipping stops)."""
        if self.q.qsize():
            log.info("finishing %d YouTube upload(s)...", self.q.qsize())
        self.q.put(None)
        self.thread.join()


def stream_info(url: str | None) -> dict:
    """Title / channel / canonical link of the stream, for descriptions. Never raises."""
    if not url:
        return {}
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)  # resolves @channel/live too
        title = re.sub(r"\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "", info.get("title") or "")  # yt-dlp adds a date to live titles
        return {"source_title": title, "source_channel": info.get("channel") or info.get("uploader") or "",
                "source_url": info.get("webpage_url") or url}
    except Exception as exc:  # noqa: BLE001 - descriptions just get less detail
        log.debug("stream info failed: %s", exc)
        return {}
