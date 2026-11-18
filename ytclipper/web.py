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
<style>
:root{--bg:#f5f5f7;--card:#fff;--fg:#16171b;--mut:#62666f;--line:#e2e3e8;--acc:#e8285e;--acc2:#c81d4e;
--ok:#14895a;--log:#f0f1f4;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#0f1013;--card:#181a1f;--fg:#eceef2;--mut:#9ca1ab;
--line:#2a2d35;--log:#111216;--ok:#3ccf8e;--warn:#f0a44b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:880px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:26px;margin:0}
.sub{color:var(--mut);margin:4px 0 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:18px}
h2{font-size:17px;margin:0 0 14px}
.modes{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px}
.modes label{border:1.5px solid var(--line);border-radius:12px;padding:10px 12px;cursor:pointer}
.modes input{position:absolute;opacity:0}
.modes label:has(input:checked){border-color:var(--acc);background:color-mix(in srgb,var(--acc) 8%,transparent)}
.modes label:has(input:focus-visible){outline:2px solid var(--acc);outline-offset:2px}
.modes b{display:block;font-size:15px}.modes span{font-size:13px;color:var(--mut)}
input[type=url]{width:100%;font:inherit;padding:12px 14px;border:1.5px solid var(--line);border-radius:12px;
background:var(--bg);color:var(--fg)}
input[type=url]:focus{outline:none;border-color:var(--acc)}
.row{display:flex;flex-wrap:wrap;gap:10px 22px;margin:14px 0 18px;font-size:15px;align-items:center}
select{font:inherit;padding:4px 8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.btns{display:flex;gap:10px;flex-wrap:wrap}
button{font:inherit;font-weight:600;border:0;border-radius:12px;padding:12px 22px;cursor:pointer}
.go{background:var(--acc);color:#fff}.go:hover{background:var(--acc2)}
.ghost{background:transparent;color:var(--fg);border:1.5px solid var(--line)}
button:disabled{opacity:.45;cursor:not-allowed}
.err{color:var(--acc);margin-top:10px;min-height:1.2em;font-size:14px}
.status{display:flex;align-items:center;gap:10px;font-weight:600;margin-bottom:10px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mut)}
.dot.on{background:var(--ok);animation:p 1.4s infinite}@keyframes p{50%{opacity:.35}}
.counts{color:var(--mut);font-weight:400;font-size:14px}
pre{background:var(--log);border-radius:10px;padding:12px;margin:0;height:220px;overflow:auto;
font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
pre .s{color:var(--warn);font-weight:700}pre .c{color:var(--ok);font-weight:700}
details summary{cursor:pointer;color:var(--mut);font-size:14px;margin-top:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.clip{border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.clip video{width:100%;aspect-ratio:9/16;background:#000;object-fit:contain;display:block}
.clip .b{padding:10px 12px;font-size:14px;display:flex;flex-direction:column;gap:4px}
.clip .t{font-weight:600}.clip .m{color:var(--mut);font-size:12.5px}
.clip a{color:var(--acc);font-weight:600;text-decoration:none}
.empty{color:var(--mut)}
.note{color:var(--mut);font-size:13px;margin-top:24px}
@media (max-width:560px){.modes{grid-template-columns:1fr}}
</style></head>
<body><main>
<h1>YT Live Clipper</h1>
<p class="sub">Paste a YouTube link. When chat goes wild, it cuts a vertical clip with captions.</p>

<section class="card">
  <h2>1. What do you want to clip?</h2>
  <div class="modes" role="radiogroup">
    <label><input type="radio" name="mode" value="live" checked><b>Live stream</b><span>Live now or starting soon</span></label>
    <label><input type="radio" name="mode" value="replay"><b>Ended stream</b><span>Past stream with chat replay</span></label>
    <label><input type="radio" name="mode" value="demo"><b>Test run</b><span>Fake stream, no link needed</span></label>
  </div>
  <div id="urlbox">
    <h2>2. Paste the YouTube link</h2>
    <input id="url" type="url" placeholder="https://www.youtube.com/watch?v=...  or  https://www.youtube.com/@channel/live" autocomplete="off">
  </div>
  <div class="row">
    <label>Clips per chat:
      <select id="sens"><option value="high">More clips (quiet chat)</option>
      <option value="medium" selected>Normal</option><option value="low">Only huge moments</option></select></label>
    <label id="topbox" hidden>Best moments:
      <select id="top"><option>3</option><option selected>5</option><option>10</option><option>20</option></select></label>
    <label><input type="checkbox" id="caps" checked> Captions</label>
    <label><input type="checkbox" id="horiz"> Keep wide (16:9)</label>
  </div>
  <div class="btns">
    <button class="go" id="start">Start clipping</button>
    <button class="ghost" id="stop" disabled>Stop</button>
  </div>
  <div class="err" id="err" role="alert"></div>
</section>

<section class="card">
  <div class="status"><span class="dot" id="dot"></span><span id="state">Not running</span>
    <span class="counts" id="counts"></span></div>
  <div id="hint" class="counts"></div>
  <details id="logbox"><summary>Show details</summary><pre id="log"></pre></details>
</section>

<section class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px">
    <h2 style="margin:0">Your clips</h2>
    <button class="ghost" id="folder" style="padding:8px 14px">Open clips folder</button>
  </div>
  <div class="grid" id="clips"></div>
</section>
<p class="note">Everything runs on this computer, for free. Only post clips you have permission to post
(your own streams, or streamers who allow clipping).</p>
</main>
<script>
const $ = id => document.getElementById(id);
let seq = 0, running = false, spikes = 0, ready = 0, clipKey = "";
const post = (path, body) => fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify(body || {})}).then(r => r.json());
const mode = () => document.querySelector("input[name=mode]:checked").value;
function syncMode() {
  $("err").textContent = "";
  $("urlbox").hidden = mode() === "demo";
  $("topbox").hidden = mode() !== "replay";
}
document.querySelectorAll("input[name=mode]").forEach(r => r.addEventListener("change", syncMode));
try { $("url").value = localStorage.getItem("ytclip-url") || ""; } catch (e) {}

$("start").onclick = async () => {
  $("err").textContent = "";
  const url = $("url").value.trim();
  try { localStorage.setItem("ytclip-url", url); } catch (e) {}
  const r = await post("/api/start", {mode: mode(), url, sensitivity: $("sens").value,
    top: $("top").value, captions: $("caps").checked, horizontal: $("horiz").checked});
  if (r.error) { $("err").textContent = r.error; return; }
  seq = 0; spikes = 0; ready = 0; $("log").textContent = "";
  poll();
};
$("stop").onclick = () => post("/api/stop");
$("folder").onclick = () => post("/api/open-folder");

const HINT = {
  live: "Watching the stream and chat. Clips appear below about a minute after chat spikes. Leave this tab open.",
  replay: "Reading the chat replay, then downloading only the best moments.",
  demo: "Playing a 2.5-minute fake stream with two planted moments.",
};
function addLines(lines) {
  const log = $("log"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  for (const l of lines) {
    const span = document.createElement("span");
    if (l.includes("SPIKE!")) { span.className = "s"; spikes++; }
    const found = l.match(/found (\d+) spike/);  // replay mode reports all spikes at once
    if (found) { span.className = "s"; spikes = +found[1]; }
    if (l.includes("CLIP READY")) { span.className = "c"; ready++; }
    if (/ERROR|failed|Traceback/.test(l)) $("logbox").open = true;
    span.textContent = l + "\n";
    log.appendChild(span);
  }
  while (log.childNodes.length > 400) log.removeChild(log.firstChild);
  if (atEnd) log.scrollTop = log.scrollHeight;
}
async function poll() {
  try {
    const s = await fetch("/api/status?since=" + seq).then(r => r.json());
    addLines(s.lines); seq = s.seq;
    const was = running; running = s.running;
    $("dot").className = "dot" + (running ? " on" : "");
    $("state").textContent = running ? (s.stopping ? "Stopping…" : "Running") : (seq ? "Finished" : "Not running");
    $("counts").textContent = seq ? `${spikes} chat spike${spikes == 1 ? "" : "s"} · ${ready} clip${ready == 1 ? "" : "s"} ready` : "";
    $("hint").textContent = running && !s.stopping ? (HINT[s.mode] || "") : "";
    $("start").disabled = running; $("stop").disabled = !running;
    if (s.lines.length || was !== running) loadClips();
  } catch (e) { $("state").textContent = "Lost connection: is the clipper window still open?"; }
  clearTimeout(poll.t); poll.t = setTimeout(poll, running ? 1500 : 5000);
}
async function loadClips() {
  const clips = await fetch("/api/clips").then(r => r.json());
  const key = clips.map(c => c.video).join("|");
  if (key === clipKey) return;
  clipKey = key;
  const g = $("clips"); g.textContent = "";
  if (!clips.length) {
    const p = document.createElement("p"); p.className = "empty";
    p.textContent = "No clips yet. Start one above, or try the Test run first.";
    g.appendChild(p); return;
  }
  for (const c of clips) {
    const d = document.createElement("div"); d.className = "clip";
    const v = document.createElement("video");
    v.src = c.video; v.poster = c.thumb; v.controls = true; v.preload = "none"; v.playsInline = true;
    const b = document.createElement("div"); b.className = "b";
    const t = document.createElement("div"); t.className = "t"; t.textContent = c.title || "Highlight";
    const m = document.createElement("div"); m.className = "m";
    m.textContent = `${Math.round(c.duration)} s · at ${c.stream_time} · ${c.hashtags}`;
    const a = document.createElement("a"); a.href = c.video; a.download = ""; a.textContent = "Download clip";
    b.append(t, m, a); d.append(v, b); g.appendChild(d);
  }
}
syncMode(); poll(); loadClips();
</script>
</body></html>
"""
