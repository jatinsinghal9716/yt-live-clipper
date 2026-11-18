"""Builds a synthetic 'live stream' + chat so you can test the whole pipeline
offline: ``ytclip demo``. Nothing is downloaded."""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

from .clipper import run_ffmpeg

DEMO_SECONDS = 150
SPIKES = (62, 118)  # seconds into the demo where "something happens"

_CALM = ["hi chat", "what game is this", "first time here", "hello from brazil", "nice setup",
         "how long is stream", "is this ranked", "love the music", "anyone else here", "gm",
         "which settings", "ok", "lets see", "chilling", "what rank are you"]
_HYPE = ["NO WAY", "WHAT 💀", "CLIP IT", "omg omg", "LETS GOOO", "insane", "KEKW", "bro what",
         "HOLY", "W", "😂😂😂", "clip that!!", "crazy", "wtf just happened", "no wayyy", "LMAO"]


def _has_flite() -> bool:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return " flite " in res.stdout


def make_demo(dest: Path, seed: int = 7) -> tuple[Path, Path]:
    dest.mkdir(parents=True, exist_ok=True)
    video = dest / "demo_stream.mp4"
    chat = dest / "demo_chat.jsonl"
    rnd = random.Random(seed)

    # ---- chat ---------------------------------------------------------- #
    rows = []
    authors = [f"viewer{i}" for i in range(60)]
    t = 0.0
    while t < DEMO_SECONDS:
        t += rnd.expovariate(0.6)  # ~0.6 msg/s background chatter
        rows.append({"offset": round(t, 2), "author": rnd.choice(authors[:25]), "text": rnd.choice(_CALM)})
    for s in SPIKES:
        t = s + 3.0  # chat reacts a few seconds after the moment
        while t < s + 22:
            t += rnd.expovariate(6.0)
            rows.append({"offset": round(t, 2), "author": rnd.choice(authors),
                         "text": rnd.choice(_HYPE), "paid": rnd.random() < 0.02})
    rows.sort(key=lambda r: r["offset"])
    chat.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    # ---- video --------------------------------------------------------- #
    vf = ("drawbox=x=0:y=0:w=iw:h=90:color=black@0.5:t=fill,"
          "drawtext=text='DEMO LIVE  %{pts\\:hms}':x=30:y=25:fontsize=40:fontcolor=white")
    for s in SPIKES:
        vf += (f",drawbox=x=0:y=0:w=iw:h=ih:color=red@0.35:t=fill:enable='between(t,{s},{s + 6})'"
               f",drawtext=text='BIG MOMENT':x=(w-tw)/2:y=(h-th)/2:fontsize=110:fontcolor=yellow"
               f":borderw=6:enable='between(t,{s},{s + 6})'")
    inputs = ["-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={DEMO_SECONDS}",
              "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={DEMO_SECONDS}"]
    afilter = "[1:a]volume=0.05[bed]"
    mix = ["[bed]"]
    if _has_flite():
        lines = ["Oh my god. Did you see that? That was absolutely insane.",
                 "No way. He actually hit that shot. Clip it right now."]
        for i, (s, text) in enumerate(zip(SPIKES, lines)):
            inputs += ["-f", "lavfi", "-i", f"flite=text='{text}':voice=slt"]
            afilter += (f";[{2 + i}:a]aresample=48000,volume=2.0,adelay={int(s * 1000)}|{int(s * 1000)},"
                        f"apad[s{i}]")
            mix.append(f"[s{i}]")
    afilter += f";{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0[a]"
    try:
        run_ffmpeg([*inputs, "-filter_complex", f"[0:v]{vf}[v];{afilter}", "-map", "[v]", "-map", "[a]",
                    "-t", str(DEMO_SECONDS), "-c:v", "libx264", "-preset", "veryfast", "-g", "60",
                    "-c:a", "aac", "-shortest", str(video)])
    except Exception:  # drawtext may be missing on some ffmpeg builds -> plain fallback
        run_ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={DEMO_SECONDS}",
                    "-f", "lavfi", "-i", f"sine=frequency=220:duration={DEMO_SECONDS}",
                    "-c:v", "libx264", "-preset", "veryfast", "-g", "60", "-c:a", "aac", "-shortest",
                    str(video)])
    return video, chat
