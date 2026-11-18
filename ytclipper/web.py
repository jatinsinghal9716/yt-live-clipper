"""``ytclip web``: a simple local web page (http://127.0.0.1:8765) for non-technical users.

Paste a link, press Start, watch progress, play the clips. It only listens on this
computer, runs one job at a time as a normal ``ytclip`` subprocess, and uses only
the Python standard library.
"""

from __future__ import annotations

import collections
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


class Job:
    """The one running ytclip process and its recent output."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lines: collections.deque[str] = collections.deque(maxlen=400)
        self.seq = 0  # total lines ever, so the page only fetches new ones
        self.mode = ""
        # private 0700 dir: a predictable name in shared /tmp could be pre-created or symlinked
        self.stop_file = Path(tempfile.mkdtemp(prefix="ytclip-")) / "stop"
        self.stopping = False
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str], mode: str) -> None:
        self.stop_file.unlink(missing_ok=True)
        cmd = [sys.executable, "-u", "-m", "ytclipper", *args, "--stop-file", str(self.stop_file)]
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", env=env)
        self.mode, self.stopping = mode, False
        with self.lock:
            self.lines.clear()
        self._add(f"started: {mode}")
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _add(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            self.seq += 1

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self._add(line.rstrip())
        proc.wait()
        self._add("finished." if proc.returncode == 0 else f"finished (exit code {proc.returncode}).")

    def stop(self) -> None:
        if not self.running():
            return
        if self.stopping:  # second click: stop right now
            self.proc.kill()
            return
        self.stopping = True
        self.stop_file.touch()  # graceful: pending clips still finish
        self._add("stopping... finishing the clips already started (click Stop again to quit now)")

    def tail(self, since: int) -> tuple[list[str], int]:
        with self.lock:
            n = min(self.seq - since, len(self.lines))
            return (list(self.lines)[-n:] if n > 0 else []), self.seq


def _clips(out: Path) -> list[dict]:
    items = []
    for meta_path in out.glob("*/clips/*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        d = meta_path.parent
        rel = d.relative_to(out).as_posix()
        items.append({
            "video": f"/files/{rel}/clip.mp4",
            "thumb": f"/files/{rel}/thumb.jpg",
            "srt": f"/files/{rel}/captions.srt" if (d / "captions.srt").exists() else None,
            "title": meta.get("suggested_title", ""),
            "duration": meta.get("duration", 0),
            "stream_time": meta.get("stream_time", ""),
            "hashtags": " ".join(meta.get("hashtags", [])),
            "source": meta.get("source", ""),
            "dir": rel,
            "yt": meta.get("youtube"),
            "mtime": meta_path.stat().st_mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:60]


def _open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - local folder only
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


# caption languages offered on the page (Whisper knows ~100; "Auto" covers the rest)
LANG_CHOICES = [("en", "English"), ("hi", "Hindi"), ("bn", "Bengali"), ("ta", "Tamil"),
                ("te", "Telugu"), ("mr", "Marathi"), ("gu", "Gujarati"), ("kn", "Kannada"),
                ("ml", "Malayalam"), ("pa", "Punjabi"), ("ur", "Urdu"), ("es", "Spanish"),
                ("pt", "Portuguese"), ("fr", "French"), ("de", "German"), ("ru", "Russian"),
                ("ar", "Arabic"), ("tr", "Turkish"), ("id", "Indonesian"), ("tl", "Filipino"),
                ("vi", "Vietnamese"), ("th", "Thai"), ("ja", "Japanese"), ("ko", "Korean"),
                ("zh", "Chinese")]
_LANGS = {code for code, _ in LANG_CHOICES}


class YouTubeState:
    """Connect-in-progress flag and the page's own upload queue (auto-posting runs in the job)."""

    def __init__(self) -> None:
        self.connecting = False
        self.error = ""
        self._uploader = None

    def status(self) -> dict:
        from . import youtube as yt

        st = yt.load_settings()
        return {"configured": yt.client_secret_path() is not None, "connected": yt.TOKEN.is_file(),
                "channel": st.get("channel") or "", "auto": bool(st.get("auto_upload")),
                "rights": bool(st.get("rights_ok")), "connecting": self.connecting, "error": self.error}

    def connect(self) -> None:
        from . import youtube as yt

        self.connecting, self.error = True, ""
        try:
            yt.connect()
        except Exception as exc:  # noqa: BLE001 - shown on the page
            self.error = f"Could not connect YouTube: {exc}"
        finally:
            self.connecting = False

    def upload(self, clip_dir: Path, overrides: dict) -> None:
        from .youtube import Uploader

        if self._uploader is None:
            self._uploader = Uploader()
        self._uploader.add(clip_dir, overrides)


def make_handler(job: Job, out: Path, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    yts = YouTubeState()

    def clip_dir(rel: str) -> Path | None:
        d = (out / str(rel)).resolve()
        return d if d.is_relative_to(out.resolve()) and (d / "meta.json").is_file() else None

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:  # keep the terminal quiet
            pass

        def _ok_host(self) -> bool:  # blocks DNS-rebinding pages from driving the server
            if self.headers.get("Host") in allowed_hosts:
                return True
            self.send_error(403)
            return False

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not self._ok_host():
                return
            u = urlparse(self.path)
            if u.path == "/":
                body = _page().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/status":
                since = int((u.query.partition("since=")[2] or "0").split("&")[0] or 0)
                lines, seq = job.tail(since)
                self._json({"running": job.running(), "stopping": job.stopping, "mode": job.mode,
                            "lines": lines, "seq": seq})
            elif u.path == "/api/clips":
                self._json(_clips(out))
            elif u.path == "/api/youtube":
                self._json(yts.status())
            elif u.path == "/api/youtube/meta":
                from .youtube import build_metadata

                d = clip_dir(unquote(u.query.partition("clip=")[2]))
                if d is None:
                    return self.send_error(404)
                self._json(build_metadata(json.loads((d / "meta.json").read_text(encoding="utf-8"))))
            elif u.path.startswith("/files/"):
                self._file(unquote(u.path[len("/files/"):]))
            else:
                self.send_error(404)

        def _file(self, rel: str) -> None:
            path = (out / rel).resolve()
            if not path.is_relative_to(out.resolve()) or not path.is_file():
                self.send_error(404)
                return
            size = path.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):  # Safari will not play video without Range support
                a, _, b = rng[6:].split(",")[0].partition("-")
                try:
                    start, end = (int(a), int(b) if b else size - 1) if a else (size - int(b), size - 1)
                except ValueError:
                    pass
                end = min(end, size - 1)
                if start > end or start < 0:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(path, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = fh.read(min(1 << 16, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_POST(self) -> None:
            # JSON-only POSTs force a CORS preflight, so other websites can't trigger jobs
            if not self._ok_host():
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self.send_error(415)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "bad request"}, 400)
            path = urlparse(self.path).path
            if path == "/api/start":
                return self._start(data)
            if path == "/api/stop":
                job.stop()
                return self._json({"ok": True})
            if path == "/api/open-folder":
                _open_folder(out)
                return self._json({"ok": True})
            if path.startswith("/api/youtube/"):
                return self._youtube(path.rsplit("/", 1)[1], data)
            self.send_error(404)

        def _youtube(self, action: str, d: dict) -> None:
            from . import youtube as yt

            if action == "connect":
                if not d.get("rights"):
                    return self._json({"error": "Tick the box to confirm you'll only post clips you have the rights to."}, 400)
                if not yt.client_secret_path():
                    return self._json({"error": "client_secret.json is missing. See 'Post to YouTube' in the README."}, 400)
                if not yts.connecting:
                    yt.save_settings(rights_ok=True)
                    threading.Thread(target=yts.connect, daemon=True).start()
                return self._json({"ok": True})
            if action == "disconnect":
                yt.disconnect()
                return self._json({"ok": True})
            if action == "auto":
                yt.save_settings(auto_upload=bool(d.get("on")))
                return self._json({"ok": True})
            if action == "upload":
                cd = clip_dir(d.get("clip", ""))
                if cd is None:
                    return self._json({"error": "clip not found"}, 404)
                if not yt.TOKEN.is_file():
                    return self._json({"error": "Connect YouTube first."}, 400)
                tags = d.get("tags") or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                yts.upload(cd, {"title": str(d.get("title") or ""), "description": str(d.get("description") or ""),
                                "tags": [str(t)[:30] for t in tags][:40]})
                return self._json({"ok": True})
            self.send_error(404)

        def _start(self, d: dict) -> None:
            if job.running():
                return self._json({"error": "Already running. Stop it first."}, 409)
            mode = d.get("mode")
            url = str(d.get("url") or "").strip()
            if mode in ("live", "replay") and not (url.startswith("https://") or url.startswith("http://")):
                return self._json({"error": "Paste a full YouTube link (starting with https://)."}, 400)
            args = {"live": ["live", url, "--wait"], "replay": ["replay", url], "demo": ["demo"]}.get(mode)
            if args is None:
                return self._json({"error": "unknown mode"}, 400)
            if mode == "replay":
                args += ["--top", str(max(1, min(30, int(d.get("top") or 5))))]
            sens = d.get("sensitivity")
            if sens in ("low", "medium", "high"):
                args += ["--sensitivity", sens]
            if not d.get("captions", True):
                args.append("--no-captions")
            if d.get("horizontal"):
                args.append("--horizontal")
            if d.get("lang") in _LANGS:
                args += ["--language", d["lang"]]
            if d.get("model") in ("base", "small", "large-v3-turbo"):
                args += ["--whisper-model", d["model"]]
            ys = yts.status()
            if mode in ("live", "replay") and ys["auto"] and ys["connected"]:
                args.append("--upload")  # post every new clip to the connected channel
            args += ["-o", str(out)]
            job.start(args, mode)
            return self._json({"ok": True})

    return H


def _page() -> str:
    opts = "".join(f'<option value="{c}">{n}</option>' for c, n in LANG_CHOICES)
    return PAGE.replace("__LANG_OPTIONS__", opts)


def serve(out: Path, port: int = 8765, open_browser: bool = True) -> int:
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    job = Job()
    for p in range(port, port + 20):  # first free port
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(job, out, p))
            break
        except OSError:
            continue
    else:
        print("no free port found", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{p}/"
    print(f"\n  YT Live Clipper is open in your browser: {url}")
    print("  Keep this window open while clipping. Close it (or press Ctrl+C) to quit.\n")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if job.running():
            job.proc.terminate()
        shutil.rmtree(job.stop_file.parent, ignore_errors=True)
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT Live Clipper</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400..800&display=swap" rel="stylesheet">
<style>
:root{
  --fog:#F2F3F7; --paper:#FFFFFF; --ink:#16133A; --mute:#6E6C88; --line:#E2E3EC;
  --hot:#FF2D6F; --hot-deep:#E0185A; --violet:#6A4BFF; --ok:#12A46B;
  --font:"Plus Jakarta Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--fog)}
body{margin:0;color:var(--ink);font:400 16px/1.6 var(--font);-webkit-font-smoothing:antialiased;
  background:
    radial-gradient(1100px 520px at 88% -8%, #E4DEFF 0%, transparent 60%),
    radial-gradient(900px 480px at -10% 18%, #FFE1EA 0%, transparent 55%),
    var(--fog);
  min-height:100vh}
main{max-width:1080px;margin:0 auto;padding:28px 24px 72px}
:focus-visible{outline:3px solid var(--violet);outline-offset:3px}

/* brand row */
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:56px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;letter-spacing:-.01em}
.rec{width:12px;height:12px;border-radius:50%;background:var(--hot);box-shadow:0 0 0 5px #FF2D6F22}
.link-btn{font:inherit;font-weight:600;font-size:14px;color:var(--ink);background:var(--paper);
  border:1px solid var(--line);border-radius:999px;padding:9px 16px;cursor:pointer}
.link-btn:hover{border-color:var(--ink)}

/* hero */
h1 .p{margin-left:-.09em}
h1{font-weight:800;font-size:clamp(40px,7vw,84px);line-height:1;
  letter-spacing:-.03em;margin:0;max-width:14ch;text-wrap:balance}
.lede{font-size:clamp(17px,1.6vw,20px);color:var(--mute);max-width:46ch;margin:22px 0 0}

/* the chat meter */
.meter-wrap{margin:44px 0 28px;position:relative}
.meter{height:150px;display:flex;align-items:flex-end;gap:5px;border-bottom:2px solid var(--ink);padding:0 2px}
.bar{flex:1;min-width:3px;border-radius:5px 5px 2px 2px;background:#CFCDE3;height:6%;
  transform-origin:bottom;transition:height .5s cubic-bezier(.2,.8,.2,1),background .3s}
.bar.lit{background:linear-gradient(to top,#B9AEFF,var(--violet))}
.bar.spike{background:linear-gradient(to top,var(--violet),var(--hot))}
.meter-cap{display:flex;justify-content:space-between;color:var(--mute);font-size:13px;margin-top:8px}
.meter-cap b{color:var(--ink);font-weight:600}
.tag{position:absolute;top:-6px;transform:translate(-50%,-100%);background:var(--hot);color:#fff;font-weight:700;
  font-size:13px;padding:5px 10px;border-radius:999px;white-space:nowrap;opacity:0;transition:opacity .4s, left .5s}
.tag.on{opacity:1}
.tag::after{content:"";position:absolute;left:50%;bottom:-5px;width:10px;height:10px;background:var(--hot);
  transform:translateX(-50%) rotate(45deg);border-radius:2px;z-index:-1}

/* console */
.console{background:var(--paper);border-radius:30px;padding:26px;box-shadow:0 1px 0 var(--line),0 30px 60px -30px #2A1F7A40}
.modes{display:inline-flex;background:var(--fog);border-radius:999px;padding:5px;gap:4px;flex-wrap:wrap}
.modes label{position:relative;border-radius:999px;padding:9px 18px;cursor:pointer;font-weight:600;font-size:15px;color:var(--mute)}
.modes input{position:absolute;opacity:0;inset:0;cursor:pointer}
.modes label:has(input:checked){background:var(--ink);color:#fff}
.modes label:has(input:focus-visible){outline:3px solid var(--violet);outline-offset:2px}
.mode-help{color:var(--mute);font-size:14px;margin:10px 4px 18px}
.bar-row{display:flex;gap:10px;align-items:stretch}
#urlbox{flex:1;min-width:0}
#url{width:100%;height:62px;font:inherit;font-size:17px;padding:0 24px;border-radius:999px;border:2px solid var(--line);
  background:var(--fog);color:var(--ink)}
#url::placeholder{color:#A09EB8}
#url:focus{outline:none;border-color:var(--violet);background:var(--paper)}
.go{height:62px;font:inherit;font-weight:700;font-size:17px;border:0;border-radius:999px;padding:0 30px;cursor:pointer;
  color:#fff;background:var(--hot);box-shadow:0 12px 26px -10px var(--hot);white-space:nowrap;
  transition:transform .15s, background .15s}
.go:hover{background:var(--hot-deep)}.go:active{transform:scale(.97)}
.stopb{height:62px;font:inherit;font-weight:600;font-size:16px;border-radius:999px;padding:0 24px;cursor:pointer;
  background:transparent;color:var(--ink);border:2px solid var(--line)}
.stopb:hover:not(:disabled){border-color:var(--ink)}
button:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}
.opts{display:flex;flex-wrap:wrap;gap:10px 26px;margin-top:18px;font-size:15px;color:var(--mute);align-items:center}
.opts label{display:flex;align-items:center;gap:8px}
select{font:inherit;color:var(--ink);background:var(--fog);border:0;border-radius:10px;padding:6px 10px}
input[type=checkbox]{accent-color:var(--violet);width:17px;height:17px}
.err{color:var(--hot-deep);font-weight:600;margin-top:14px;min-height:1.4em;font-size:15px}

/* status */
.status{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;margin:30px 4px 0}
.dot{width:11px;height:11px;border-radius:50%;background:#C4C2D6}
.dot.on{background:var(--ok);box-shadow:0 0 0 0 #12A46B66;animation:ping 1.6s infinite}
@keyframes ping{70%{box-shadow:0 0 0 10px #12A46B00}100%{box-shadow:0 0 0 0 #12A46B00}}
#state{font-weight:700;font-size:18px}
#counts{color:var(--mute)}
#hint{color:var(--mute);font-size:15px;margin:6px 4px 0;max-width:70ch}
details{margin:12px 4px 0}
summary{cursor:pointer;color:var(--mute);font-size:14px;width:max-content}
pre{background:var(--ink);color:#D9D6F5;border-radius:18px;padding:16px 18px;margin:10px 0 0;height:240px;overflow:auto;
  font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
pre .s{color:#FF8DB0;font-weight:700}pre .c{color:#6FE3B0;font-weight:700}

/* clips: phone frames */
.clips-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:64px 0 20px}
h2{font-weight:800;font-size:clamp(28px,3.6vw,40px);letter-spacing:-.04em;margin:0;line-height:1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:26px 20px}
.clip{display:flex;flex-direction:column;gap:12px}
.phone{background:var(--ink);border-radius:28px;padding:7px;box-shadow:0 24px 40px -24px #16133A80}
.phone video{display:block;width:100%;aspect-ratio:9/16;object-fit:contain;background:#000;border-radius:22px}
.clip .t{font-weight:700;font-size:16px;line-height:1.3;padding:0 4px;letter-spacing:-.01em}
.clip .m{color:var(--mute);font-size:13.5px;padding:0 4px;display:flex;flex-wrap:wrap;gap:4px 12px}
.clip a{color:var(--hot-deep);font-weight:700;font-size:14px;text-decoration:none;padding:0 4px;width:max-content}
.clip a:hover{text-decoration:underline}
.empty{grid-column:1/-1;border:2px dashed var(--line);border-radius:28px;padding:44px 24px;text-align:center;color:var(--mute)}
.empty b{display:block;color:var(--ink);font-size:20px;margin-bottom:4px}
.note{color:var(--mute);font-size:14px;margin-top:56px;max-width:70ch}

/* post to YouTube */
.ytpanel{background:var(--paper);border-radius:24px;padding:20px 26px;margin-top:16px;
  box-shadow:0 1px 0 var(--line),0 20px 40px -30px #2A1F7A40}
.yt-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:17px}
.yt-logo{display:inline-grid;place-items:center;width:30px;height:21px;border-radius:6px;background:#FF0033;
  color:#fff;font-size:10px}
.yt-state{color:var(--ok);font-weight:600;font-size:15px}
.yt-text{color:var(--mute);font-size:14.5px;margin:8px 0 0;max-width:75ch}
.yt-rights{display:flex;gap:10px;align-items:flex-start;margin-top:14px;font-size:15px;max-width:70ch}
.yt-rights input{margin-top:3px;flex:none}
.yt-actions{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;margin-top:14px}
.yt-connect{font:inherit;font-weight:700;font-size:15px;border:0;border-radius:999px;padding:12px 22px;cursor:pointer;
  background:var(--ink);color:#fff}
.yt-connect:hover:not(:disabled){background:#2A2560}
.yt-auto{display:flex;gap:8px;align-items:center;font-weight:600;font-size:15px}
.clip .yt-btn{font:inherit;font-weight:700;font-size:14px;border:1.5px solid var(--line);background:var(--paper);
  color:var(--ink);border-radius:999px;padding:7px 14px;cursor:pointer;width:max-content;margin:0 4px}
.clip .yt-btn:hover{border-color:var(--ink)}
.clip .yt-note{font-size:13.5px;color:var(--mute);padding:0 4px}
.clip .yt-note.bad{color:var(--hot-deep)}
.ytpanel .err,.yt-edit .err{min-height:0;margin-top:8px}.ytpanel .err:empty,.yt-edit .err:empty{display:none}
#ytDialog{border:0;border-radius:24px;padding:26px;width:min(620px,calc(100vw - 32px));color:var(--ink);
  background:var(--paper);box-shadow:0 40px 80px -30px #16133A80}
#ytDialog::backdrop{background:#16133A55;backdrop-filter:blur(3px)}
#ytDialog h3{margin:0 0 4px;font-size:22px;letter-spacing:-.02em}
#ytDialog .sub{color:var(--mute);font-size:14px;margin:0 0 16px}
.yt-edit{display:flex;flex-direction:column;gap:12px}
.yt-edit label{font-size:13px;color:var(--mute);display:flex;flex-direction:column;gap:4px}
.yt-edit input,.yt-edit textarea{font:inherit;font-size:14px;color:var(--ink);background:var(--fog);border:1.5px solid var(--line);
  border-radius:12px;padding:8px 10px;width:100%;resize:vertical}
.yt-edit input:focus,.yt-edit textarea:focus{outline:none;border-color:var(--violet);background:var(--paper)}
.yt-edit .row{display:flex;gap:10px;flex-wrap:wrap;margin-top:4px}
.yt-edit .yt-btn{font:inherit;font-weight:700;font-size:14px;border:1.5px solid var(--line);background:var(--paper);
  color:var(--ink);border-radius:999px;padding:9px 16px;cursor:pointer}
.yt-edit .go-sm{font:inherit;font-weight:700;font-size:14px;border:0;border-radius:999px;padding:9px 16px;cursor:pointer;
  background:var(--hot);color:#fff}

@media (max-width:720px){
  main{padding:20px 16px 56px}.top{margin-bottom:36px}
  .bar-row{flex-wrap:wrap}#urlbox{flex-basis:100%}.go{flex:1}.go,.stopb{height:56px}
  .console{padding:18px;border-radius:24px}.meter{height:110px;gap:3px}
  .modes{display:flex;flex-wrap:nowrap}.modes label{flex:1;text-align:center;padding:9px 4px;font-size:13.5px;white-space:nowrap}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style></head>
<body><main>
<header class="top">
  <div class="brand"><span class="rec" aria-hidden="true"></span>YT Live Clipper</div>
  <button class="link-btn" id="folder">Open clips folder</button>
</header>

<h1>When chat explodes<span class="p">,</span> you get the clip<span class="p">.</span></h1>
<p class="lede">Paste a YouTube stream. The clipper watches the chat, and the moment it spikes it cuts a vertical clip with captions, ready for Shorts.</p>

<div class="meter-wrap" aria-hidden="true">
  <div class="tag" id="tag">Chat spikes here</div>
  <div class="meter" id="meter"></div>
  <div class="meter-cap"><span id="meterL">Chat activity, every 10 seconds</span><span id="meterR"></span></div>
</div>

<section class="console" aria-label="Start clipping">
  <div class="modes" role="radiogroup" aria-label="What to clip">
    <label><input type="radio" name="mode" value="live" checked>Live stream</label>
    <label><input type="radio" name="mode" value="replay">Ended stream</label>
    <label><input type="radio" name="mode" value="demo">Test run</label>
  </div>
  <p class="mode-help" id="modeHelp"></p>
  <div class="bar-row">
    <div id="urlbox"><input id="url" type="url" aria-label="YouTube link"
      placeholder="Paste a YouTube link" autocomplete="off"></div>
    <button class="go" id="start">Start clipping</button>
    <button class="stopb" id="stop" disabled>Stop</button>
  </div>
  <div class="opts">
    <label>Clips
      <select id="sens"><option value="high">More (quiet chat)</option>
      <option value="medium" selected>Normal</option><option value="low">Only huge moments</option></select></label>
    <label id="topbox" hidden>Best moments
      <select id="top"><option>3</option><option selected>5</option><option>10</option><option>20</option></select></label>
    <label><input type="checkbox" id="caps" checked> Captions</label>
    <label>Spoken language
      <select id="lang"><option value="">Auto-detect</option>__LANG_OPTIONS__</select></label>
    <label>Caption accuracy
      <select id="model"><option value="base" selected>Fast</option>
      <option value="large-v3-turbo" title="About 35 s per clip; downloads 1.6 GB once">Best (slower)</option></select></label>
    <label><input type="checkbox" id="horiz"> Keep wide 16:9</label>
  </div>
  <div class="err" id="err" role="alert"></div>
</section>

<section class="ytpanel" aria-label="Post to YouTube">
  <div class="yt-head"><span class="yt-logo" aria-hidden="true">&#9654;</span><b>Post to YouTube</b>
    <span class="yt-state" id="ytState"></span></div>
  <p class="yt-text" id="ytText"></p>
  <label class="yt-rights" id="ytRightsRow" hidden><input type="checkbox" id="ytRights">
    I'll only post clips I have the rights to: my own streams, or streams whose owner allows it.</label>
  <div class="yt-actions">
    <button class="yt-connect" id="ytConnect" hidden>Connect YouTube</button>
    <label class="yt-auto" id="ytAutoRow" hidden><input type="checkbox" id="ytAuto"> Post every new clip automatically</label>
    <button class="link-btn" id="ytDisconnect" hidden>Disconnect</button>
  </div>
  <div class="err" id="ytErr" role="alert"></div>
</section>

<dialog id="ytDialog" aria-labelledby="ytDlgTitle"></dialog>

<div class="status" aria-live="polite">
  <span class="dot" id="dot"></span><span id="state">Ready</span><span id="counts"></span>
</div>
<div id="hint"></div>
<details id="logbox"><summary>Show technical details</summary><pre id="log"></pre></details>

<div class="clips-head"><h2>Your clips</h2></div>
<div class="grid" id="clips"></div>

<p class="note">Everything runs on this computer, for free. Only post clips you have permission to post:
your own streams, or streamers who allow clipping.</p>
</main>
<script>
const $ = id => document.getElementById(id);
let seq = 0, running = false, mine = false, spikes = 0, ready = 0, clipKey = "";
const post = (path, body) => fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify(body || {})}).then(r => r.json());
const mode = () => document.querySelector("input[name=mode]:checked").value;
const HELP = {
  live: "For a stream that is live now or starting soon. Leave this page open while it runs.",
  replay: "For a stream that already ended and still has chat replay. Only the best moments are downloaded.",
  demo: "A 2.5-minute fake stream with two planted moments. No link needed: try this first.",
};
$("caps").addEventListener("change", () => {
  $("lang").disabled = $("model").disabled = !$("caps").checked; });
function syncMode() {
  $("err").textContent = "";
  $("urlbox").hidden = mode() === "demo";
  $("topbox").hidden = mode() !== "replay";
  $("modeHelp").textContent = HELP[mode()];
}
document.querySelectorAll("input[name=mode]").forEach(r => r.addEventListener("change", syncMode));
try {
  $("url").value = localStorage.getItem("ytclip-url") || "";
  for (const id of ["lang", "model"]) { const v = localStorage.getItem("ytclip-" + id); if (v !== null) $(id).value = v; }
} catch (e) {}

/* ---- chat meter: a resting shape, then real 10 s chat scores while a stream runs ---- */
const N = innerWidth < 720 ? 32 : 56;
const meter = $("meter"), bars = [];
for (let i = 0; i < N; i++) { const b = document.createElement("div"); b.className = "bar"; meter.appendChild(b); bars.push(b); }
const rest = Array.from({length: N}, (_, i) => {
  const base = 14 + 9 * Math.sin(i * 0.9) + 7 * Math.sin(i * 2.3 + 1);
  const peak = Math.exp(-((i - N * 0.72) ** 2) / 3.2) * 80;
  return Math.max(6, base + peak);
});
let live = [], marked = 0;  // [{v, thr, spike}]; bars before `marked` already judged
function draw() {
  const data = live.length ? live.slice(-N) : null;
  const max = data ? Math.max(10, ...data.map(d => d.v)) : 100;
  let tagAt = -1;
  bars.forEach((b, i) => {
    let v, spike;
    if (data) { const d = data[i - (N - data.length)]; v = d ? d.v / max * 96 : 3; spike = d && d.spike; }
    else { v = rest[i]; spike = v > 70; }
    b.style.height = Math.max(3, v) + "%";
    b.className = "bar" + (spike ? " spike" : v > 34 ? " lit" : "");
    if (spike) tagAt = i;
  });
  const tag = $("tag");
  if (tagAt >= 0) { tag.style.left = ((tagAt + 0.5) / N * 100) + "%"; tag.classList.add("on"); }
  else tag.classList.remove("on");
}
function intro() {   // one orchestrated moment on load: bars rise left to right
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return draw();
  bars.forEach((b, i) => setTimeout(() => {
    b.style.height = rest[i] + "%";
    b.className = "bar" + (rest[i] > 70 ? " spike" : rest[i] > 34 ? " lit" : "");
  }, 120 + i * 22));
  setTimeout(draw, 160 + N * 22);
}

$("start").onclick = async () => {
  $("err").textContent = "";
  const url = $("url").value.trim();
  try { localStorage.setItem("ytclip-url", url); localStorage.setItem("ytclip-lang", $("lang").value);
        localStorage.setItem("ytclip-model", $("model").value); } catch (e) {}
  const r = await post("/api/start", {mode: mode(), url, sensitivity: $("sens").value,
    top: $("top").value, captions: $("caps").checked, horizontal: $("horiz").checked,
    lang: $("lang").value, model: $("model").value});
  if (r.error) { $("err").textContent = r.error; return; }
  seq = 0; spikes = 0; ready = 0; live = []; marked = 0; mine = true; $("log").textContent = ""; draw();
  poll();
};
$("stop").onclick = () => post("/api/stop");
$("folder").onclick = () => post("/api/open-folder");

/* ---- post to YouTube ---- */
let yt = {};
async function ytRefresh() {
  try { yt = await fetch("/api/youtube").then(r => r.json()); } catch (e) { return; }
  const show = (id, on) => { $(id).hidden = !on; };
  show("ytRightsRow", yt.configured && !yt.connected);
  show("ytConnect", yt.configured && !yt.connected);
  show("ytAutoRow", yt.connected); show("ytDisconnect", yt.connected);
  $("ytConnect").disabled = yt.connecting;
  $("ytConnect").textContent = yt.connecting ? "Finish signing in in your browser…" : "Connect YouTube";
  $("ytAuto").checked = yt.auto;
  if (yt.rights) $("ytRights").checked = true;
  $("ytState").textContent = yt.connected ? `Connected to ${yt.channel || "your channel"}` : "";
  $("ytText").textContent = !yt.configured
    ? "To turn this on, add the app's client_secret.json file (see 'Post to YouTube' in the README)."
    : !yt.connected
    ? "Connect your channel once. Clips are then posted publicly as Shorts, with a title, description and tags written for you."
    : "Clips are posted publicly as Shorts. Until this app passes YouTube's review, YouTube itself keeps uploads made through it private; switch them to Public in YouTube Studio.";
  if (yt.error) $("ytErr").textContent = yt.error;
}
$("ytConnect").onclick = async () => {
  $("ytErr").textContent = "";
  const r = await post("/api/youtube/connect", {rights: $("ytRights").checked});
  if (r.error) { $("ytErr").textContent = r.error; return; }
  ytRefresh();
};
$("ytDisconnect").onclick = async () => { await post("/api/youtube/disconnect"); ytRefresh(); };
$("ytAuto").onchange = () => post("/api/youtube/auto", {on: $("ytAuto").checked});

function ytActions(c, card) {
  const s = c.yt || {};
  const box = document.createElement("div");
  if (s.state === "posted") {
    const a = document.createElement("a"); a.href = s.url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = "Watch on YouTube"; box.appendChild(a);
  } else if (s.state === "uploading") {
    const n = document.createElement("div"); n.className = "yt-note"; n.textContent = "Posting to YouTube…"; box.appendChild(n);
  } else if (yt.connected) {
    if (s.state === "failed") {
      const n = document.createElement("div"); n.className = "yt-note bad"; n.textContent = "YouTube: " + s.error; box.appendChild(n);
    }
    const b = document.createElement("button"); b.className = "yt-btn";
    b.textContent = s.state === "failed" ? "Try again" : "Post to YouTube";
    b.onclick = () => openEditor(c);
    box.appendChild(b);
  }
  return box;
}
async function openEditor(c) {
  const md = await fetch("/api/youtube/meta?clip=" + encodeURIComponent(c.dir)).then(r => r.json());
  const dlg = $("ytDialog"); dlg.textContent = "";
  const h = Object.assign(document.createElement("h3"), {id: "ytDlgTitle", textContent: "Post to YouTube"});
  const sub = Object.assign(document.createElement("p"), {className: "sub",
    textContent: `Posted publicly to ${yt.channel || "your channel"} as a Short. Edit anything first.`});
  const f = document.createElement("form"); f.className = "yt-edit";
  const field = (label, el) => { const l = document.createElement("label"); l.append(label, el); f.appendChild(l); return el; };
  const ti = field("Title", Object.assign(document.createElement("input"), {maxLength: 100, value: md.title, required: true}));
  const de = field("Description", Object.assign(document.createElement("textarea"), {rows: 9, value: md.description}));
  const ta = field("Tags, separated by commas", Object.assign(document.createElement("input"), {value: md.tags.join(", ")}));
  const row = document.createElement("div"); row.className = "row";
  const go = Object.assign(document.createElement("button"), {type: "submit", className: "go-sm", textContent: "Post publicly"});
  const no = Object.assign(document.createElement("button"), {type: "button", className: "yt-btn", textContent: "Cancel"});
  no.onclick = () => dlg.close();
  row.append(go, no); f.appendChild(row);
  f.onsubmit = async ev => {
    ev.preventDefault(); go.disabled = true;
    const r = await post("/api/youtube/upload", {clip: c.dir, title: ti.value, description: de.value, tags: ta.value});
    if (r.error) { go.disabled = false; err.textContent = r.error; return; }
    dlg.close(); clipKey = ""; setTimeout(loadClips, 800);
  };
  const err = Object.assign(document.createElement("div"), {className: "err"});
  f.appendChild(err);
  dlg.append(h, sub, f); dlg.showModal(); ti.focus();
}

const HINT = {
  live: "Watching the stream and its chat. Clips land below about a minute after chat spikes.",
  replay: "Reading the chat replay, then downloading only the best moments.",
  demo: "Playing the fake stream. Two clips should appear in about 3 minutes.",
};
function addLines(lines) {
  const log = $("log"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  for (const l of lines) {
    const span = document.createElement("span");
    const bin = l.match(/bin_score=([\d.]+)/);
    if (bin && (running || mine)) {  // not an old run's log
      const thr = l.match(/threshold=([\d.]+)/);
      live.push({v: +bin[1], thr: thr ? +thr[1] : Infinity, spike: false});
    }
    if (l.includes("SPIKE!")) {  // confirmed a bit after the fact: mark the bars that crossed the line
      span.className = "s"; spikes++;
      const recent = live.slice(marked);
      const hot = recent.filter(d => d.v >= d.thr);
      (hot.length ? hot : recent.length ? [recent.reduce((a, b) => b.v > a.v ? b : a)] : []).forEach(d => d.spike = true);
      marked = live.length;
    }
    const found = l.match(/found (\d+) spike/);  // replay mode reports all spikes at once
    if (found) { span.className = "s"; spikes = +found[1]; }
    if (l.includes("CLIP READY")) { span.className = "c"; ready++; }
    if (/ERROR|failed|Traceback/.test(l)) $("logbox").open = true;
    span.textContent = l + "\n";
    log.appendChild(span);
  }
  while (log.childNodes.length > 400) log.removeChild(log.firstChild);
  if (atEnd) log.scrollTop = log.scrollHeight;
  if (lines.length) draw();
}
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
async function poll() {
  try {
    const s = await fetch("/api/status?since=" + seq).then(r => r.json());
    const was = running; running = s.running;
    if (running && !was && !mine) {  // page opened while a run is going: show its mode
      const r = document.querySelector(`input[name=mode][value=${s.mode}]`); if (r) { r.checked = true; syncMode(); }
    }
    addLines(s.lines); seq = s.seq;
    $("dot").className = "dot" + (running ? " on" : "");
    $("state").textContent = running ? (s.stopping ? "Stopping" : "Clipping") : (seq ? "Finished" : "Ready");
    $("counts").textContent = seq ? `${plural(spikes, "spike")}, ${plural(ready, "clip")} ready` : "";
    $("hint").textContent = running && !s.stopping ? (HINT[s.mode] || "") : "";
    $("meterR").textContent = live.length ? "live" : "";
    $("start").disabled = running; $("stop").disabled = !running;
    ytRefresh(); loadClips();
  } catch (e) { $("state").textContent = "Lost connection. Is the clipper window still open?"; }
  clearTimeout(poll.t); poll.t = setTimeout(poll, running ? 1500 : 5000);
}
async function loadClips() {
  const clips = await fetch("/api/clips").then(r => r.json());
  const key = clips.map(c => c.video + (c.yt ? c.yt.state : "")).join("|") + (yt.connected ? "+yt" : "");
  if (key === clipKey) return;
  if ($("ytDialog").open || [...document.querySelectorAll(".clip video")].some(v => !v.paused)) return;
  clipKey = key;
  const g = $("clips"); g.textContent = "";
  if (!clips.length) {
    const p = document.createElement("div"); p.className = "empty";
    const b = document.createElement("b"); b.textContent = "No clips yet";
    p.append(b, "Start a stream above, or pick Test run to see one in about 3 minutes.");
    g.appendChild(p); return;
  }
  for (const c of clips) {
    const d = document.createElement("article"); d.className = "clip";
    const ph = document.createElement("div"); ph.className = "phone";
    const v = document.createElement("video");
    v.src = c.video; v.poster = c.thumb; v.controls = true; v.preload = "none"; v.playsInline = true;
    ph.appendChild(v);
    const t = document.createElement("div"); t.className = "t"; t.textContent = c.title || "Highlight";
    const m = document.createElement("div"); m.className = "m";
    for (const x of [`${Math.round(c.duration)} s`, c.stream_time && `at ${c.stream_time}`, c.hashtags]) {
      if (!x) continue; const s = document.createElement("span"); s.textContent = x; m.appendChild(s);
    }
    const a = document.createElement("a"); a.href = c.video; a.download = ""; a.textContent = "Download clip";
    d.append(ph, t, m, a, ytActions(c, d)); g.appendChild(d);
  }
}
syncMode(); intro(); ytRefresh().then(() => { poll(); loadClips(); });
</script>
</body></html>
"""
