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


def make_handler(job: Job, out: Path, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

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
                body = PAGE.encode()
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
            args += ["-o", str(out)]
            job.start(args, mode)
            return self._json({"ok": True})

    return H


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
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,300..800&display=swap" rel="stylesheet">
<style>
:root{
  --fog:#F2F3F7; --paper:#FFFFFF; --ink:#16133A; --mute:#6E6C88; --line:#E2E3EC;
  --hot:#FF2D6F; --hot-deep:#E0185A; --violet:#6A4BFF; --ok:#12A46B;
  --font:"Bricolage Grotesque",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--fog)}
body{margin:0;color:var(--ink);font:400 16px/1.55 var(--font);font-optical-sizing:auto;
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
h1{font-weight:800;font-stretch:78%;font-size:clamp(46px,8.4vw,104px);line-height:.9;
  letter-spacing:-.035em;margin:0;max-width:11ch}
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
h2{font-weight:800;font-stretch:85%;font-size:clamp(30px,4vw,44px);letter-spacing:-.03em;margin:0;line-height:1}
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

<h1>When chat explodes, you get the clip.</h1>
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
    <label><input type="checkbox" id="horiz"> Keep wide 16:9</label>
  </div>
  <div class="err" id="err" role="alert"></div>
</section>

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
let seq = 0, running = false, spikes = 0, ready = 0, clipKey = "";
const post = (path, body) => fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify(body || {})}).then(r => r.json());
const mode = () => document.querySelector("input[name=mode]:checked").value;
const HELP = {
  live: "For a stream that is live now or starting soon. Leave this page open while it runs.",
  replay: "For a stream that already ended and still has chat replay. Only the best moments are downloaded.",
  demo: "A 2.5-minute fake stream with two planted moments. No link needed: try this first.",
};
function syncMode() {
  $("err").textContent = "";
  $("urlbox").hidden = mode() === "demo";
  $("topbox").hidden = mode() !== "replay";
  $("modeHelp").textContent = HELP[mode()];
}
document.querySelectorAll("input[name=mode]").forEach(r => r.addEventListener("change", syncMode));
try { $("url").value = localStorage.getItem("ytclip-url") || ""; } catch (e) {}

/* ---- chat meter: a resting shape, then real 10 s chat scores while a stream runs ---- */
const N = innerWidth < 720 ? 32 : 56;
const meter = $("meter"), bars = [];
for (let i = 0; i < N; i++) { const b = document.createElement("div"); b.className = "bar"; meter.appendChild(b); bars.push(b); }
const rest = Array.from({length: N}, (_, i) => {
  const base = 14 + 9 * Math.sin(i * 0.9) + 7 * Math.sin(i * 2.3 + 1);
  const peak = Math.exp(-((i - N * 0.72) ** 2) / 3.2) * 80;
  return Math.max(6, base + peak);
});
let live = [];            // [{v, spike}]
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
  try { localStorage.setItem("ytclip-url", url); } catch (e) {}
  const r = await post("/api/start", {mode: mode(), url, sensitivity: $("sens").value,
    top: $("top").value, captions: $("caps").checked, horizontal: $("horiz").checked});
  if (r.error) { $("err").textContent = r.error; return; }
  seq = 0; spikes = 0; ready = 0; live = []; $("log").textContent = ""; draw();
  poll();
};
$("stop").onclick = () => post("/api/stop");
$("folder").onclick = () => post("/api/open-folder");

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
    if (bin) live.push({v: +bin[1], spike: false});
    if (l.includes("SPIKE!")) { span.className = "s"; spikes++; if (live.length) live[live.length - 1].spike = true; }
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
    addLines(s.lines); seq = s.seq;
    const was = running; running = s.running;
    $("dot").className = "dot" + (running ? " on" : "");
    $("state").textContent = running ? (s.stopping ? "Stopping" : "Clipping") : (seq ? "Finished" : "Ready");
    $("counts").textContent = seq ? `${plural(spikes, "spike")}, ${plural(ready, "clip")} ready` : "";
    $("hint").textContent = running && !s.stopping ? (HINT[s.mode] || "") : "";
    $("meterR").textContent = live.length ? "live" : "";
    $("start").disabled = running; $("stop").disabled = !running;
    if (s.lines.length || was !== running) loadClips();
  } catch (e) { $("state").textContent = "Lost connection. Is the clipper window still open?"; }
  clearTimeout(poll.t); poll.t = setTimeout(poll, running ? 1500 : 5000);
}
async function loadClips() {
  const clips = await fetch("/api/clips").then(r => r.json());
  const key = clips.map(c => c.video).join("|");
  if (key === clipKey) return;
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
    d.append(ph, t, m, a); g.appendChild(d);
  }
}
syncMode(); intro(); poll(); loadClips();
</script>
</body></html>
"""
