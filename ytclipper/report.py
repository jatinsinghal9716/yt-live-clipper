"""Writes clips/index.html - a local gallery of every clip (open it in a browser)."""

from __future__ import annotations

import html
import json
from pathlib import Path

_CSS = """
:root{--bg:#0e0f13;--card:#181a21;--fg:#eceef3;--mut:#9aa0ad;--acc:#ff3d7f;--ok:#39d98a}
@media (prefers-color-scheme: light){:root{--bg:#f4f5f8;--card:#fff;--fg:#14161c;--mut:#5d6370}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:24px 16px}
h1{margin:0 0 4px;font-size:22px}p.sub{margin:0 0 20px;color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}
.card{background:var(--card);border-radius:14px;overflow:hidden;display:flex;flex-direction:column}
video{width:100%;aspect-ratio:9/16;background:#000;object-fit:contain}
.b{padding:12px 14px;display:flex;flex-direction:column;gap:6px}
.t{font-weight:650}.m{color:var(--mut);font-size:13px}
.pill{display:inline-block;background:var(--acc);color:#fff;border-radius:99px;padding:1px 8px;
font-size:12px;font-weight:600;margin-right:6px}
.chat{font-size:12.5px;color:var(--mut);max-height:4.5em;overflow:hidden}
a{color:var(--acc)}
"""


def write_index(clips_root: Path) -> Path:
    clips_root = Path(clips_root)
    cards = []
    metas = []
    for meta_path in sorted(clips_root.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta["_dir"] = meta_path.parent.name
        metas.append(meta)
    for meta in metas:
        d = html.escape(meta["_dir"])
        mo = meta.get("moment", {})
        chat = " · ".join(mo.get("sample_chat", [])[:5])
        poster = f' poster="{d}/thumb.jpg"' if (clips_root / meta["_dir"] / "thumb.jpg").exists() else ""
        cards.append(f"""<div class="card"><video controls preload="none"{poster} src="{d}/{html.escape(meta['file'])}"></video>
<div class="b"><div class="t">{html.escape(meta.get('suggested_title', ''))}</div>
<div class="m"><span class="pill">{mo.get('intensity', '?')}x chat</span>at {html.escape(str(meta.get('stream_time', '')))} · {meta.get('duration', 0):.0f}s · {mo.get('messages', 0)} msgs</div>
<div class="chat">{html.escape(chat)}</div>
<div class="m"><a href="{d}/{html.escape(meta['file'])}" download>download</a>{' · <a href="' + d + '/captions.srt" download>captions.srt</a>' if (clips_root / meta['_dir'] / 'captions.srt').exists() else ''}</div></div></div>""")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Live Clips</title>
<style>{_CSS}</style></head><body><h1>Live Clips</h1>
<p class="sub">{len(metas)} clip(s) · ranked by when they happened · refresh to see new ones</p>
<div class="grid">{''.join(cards) or '<p class="sub">No clips yet - waiting for chat to pop off.</p>'}</div></body></html>"""
    out = clips_root / "index.html"
    out.write_text(page, encoding="utf-8")
    return out
