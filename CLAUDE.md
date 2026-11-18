# Project memory - YT Live Clipper (handoff from a previous Claude session)

## What the user wants
- A **100% free** tool (no paid APIs, services, or subscriptions, ever) that auto-clips
  **YouTube live streams**. It was inspired by a short video showing a Twitch auto-clipper
  built with Claude Code: streamlink + chat-spike counting + ffmpeg + whisper.
- It must use only reliable, well-known, well-starred open-source GitHub projects.
- It must run on the user's own PC (no hosting). The user chose the PC over a cloud server.
- The user is not deeply technical: prefer one-click scripts and plain-language steps.

## Status (as of handoff)
- The tool is fully built: `ytclipper/` package, about 1,800 lines of Python, MIT licensed.
- **13/13 pytest tests pass** (Linux): chat parser, spike detector, anti-spam,
  recorder, cut, vertical render, caption burn-in, and replay mode.
- **End-to-end demo verified** (`ytclip demo`): a fake 150 s stream with fake chat and two planted
  "BIG MOMENT"s. Both were detected live and clipped, and each moment lands about 30 s into its clip.
  The output was a 1080x1920 vertical clip.
- `install_mac_linux.sh` was tested on a fresh Linux copy and ends with "all good!".
- **NOT yet tested (the previous environment blocked YouTube and Hugging Face):**
  1. A real YouTube live stream (`ytclip live URL`)
  2. Real faster-whisper captions (the model downloads on first run; burn-in itself was tested
     with stand-in words)
  3. `install_windows.bat` / `START_CLIPPING.bat` on real Windows
  4. `ytclip replay URL` against real YouTube

## Mac session (2026-11-17): verified + fixed
- Verified on macOS (Apple Silicon): install, doctor, 14/14 tests, demo, a real 15-min
  `ytclip live` run (3 spikes → 3 vertical clips), real Whisper captions burned in (CBS News).
- Fixed: live chat arrives 12-22 s late (yt-dlp sleeps `timeoutMs` before writing), so every
  live message was dropped by the detector → `live.py` now holds bins open for the observed lag.
- Fixed: PyAV 19 breaks faster-whisper 1.2.1 (`metadata_errors`) → pinned `av<19`.
- Fixed: Homebrew core ffmpeg has no libass/drawtext → installer uses the `homebrew-ffmpeg` tap;
  doctor now fails without libass. Python 3.15 has no faster-whisper wheels → venv uses 3.12.
- `ytclip live` runs `caffeinate` on macOS (an idle Mac slept mid-test). Closing the lid on battery still sleeps it.
- chat-downloader fallback is dead (can't parse YouTube pages); Auto mode now reports both errors.
- `ytclip replay` verified on real YouTube (penguinz0 3.4 h VOD aSDcA7Nz-fA): chat replay
  (7,612 msgs) downloads in ~45 s, 30 spikes found, top 3 → 3 captioned vertical clips in ~7 min.
  Alignment looked right: the streamer's "oh no" lands just before chat's "NOOOO / F" spike.
  Fixed: replay errors (e.g. chat replay turned off, as on NASA streams) now print one clear
  line instead of a traceback; the HF download logger is `httpx2` (now silenced too).
- Published at https://github.com/jatinsinghal9716/yt-live-clipper (public, MIT).
- 2026-11-18: added the `ytclip web` page; tested in a browser (test run, ended stream, Stop, playback).
- Multilingual: detector tokenizes any script (`_tokens`, keeps Hindi matras), hype words/laughter in
  ~20 languages incl. Hindi/Hinglish. Whisper auto-detect misfires on short noisy Indic clips (a Telugu PUBG
  stream came out as garbage), so the page offers a spoken-language pick + `large-v3-turbo` "Best" accuracy.
  Captions drop segments Whisper flags as unreliable. Devanagari/Telugu burn-in renders via font fallback (Mac).
- Still untested: Windows scripts and the web page on Windows; live-mode alignment on a stream with real speech.

## Next steps (original handoff)
1. Install: Windows `install_windows.bat` (or `python -m venv .venv`, activate it,
   `pip install -e .`); Mac/Linux `bash install_mac_linux.sh`. Then `ytclip doctor`
   should say "all good!".
2. `ytclip demo` (offline self-test, ~3-4 min). This also tests real Whisper captions now
   (the demo audio has synthetic speech only if the user's ffmpeg has the `flite` filter;
   otherwise it's a tone and captions will be empty, which is fine).
3. Real test: find any currently-live YouTube stream with active chat and run
   `ytclip live "<url>" --duration 900 --sensitivity high`. Confirm that `SPIKE!` and
   `CLIP READY` lines appear, check that the clip shows the right moment, and check caption quality.
4. Fix whatever breaks. Likely spots: the yt-dlp live chat file name/tailing
   (`ytclipper/chat.py` `YtDlpChatSource`), streamlink quality string, and Windows paths
   in the ffmpeg concat list / `ass` filter.

## Architecture (`ytclipper/`)
| File | Role |
|---|---|
| `cli.py` | `ytclip live / replay / demo / doctor` (argparse) |
| `config.py` | `DetectorConfig` (+ low/medium/high sensitivity presets), `ClipConfig` |
| `recorder.py` | streamlink (default) or yt-dlp → stdout → ffmpeg `-c copy` segment muxer (4 s .ts files). A watcher thread stamps each segment with its **wall-clock arrival time**; rolling buffer cleanup |
| `chat.py` | Sources run in threads and push `ChatMessage(t, author, text, paid)` into a queue. `YtDlpChatSource` runs `yt-dlp --skip-download --write-subs --sub-langs live_chat --no-part` and **tails** `chat.live_chat.json` (yt-dlp flushes one JSON line per action). `ChatDownloaderSource` is the fallback, `AutoChatSource` tries yt-dlp first, and `FileChatSource` serves demo/tests. `parse_ytdlp_line` handles live (timestampUsec → wall clock) and replay (videoOffsetTimeMsec) |
| `detector.py` | 10 s bins; message weight 1 plus bonuses (hype words/emoji, "clip", CAPS, `!!`, super chat), capped at 4. **Per-author cap of 3 points per bin** (anti-spam). Spike = score ≥ max(min_score, 2×median, median + 3×1.4826×MAD) and ≥ 3 authors, over a rolling 36-bin baseline with 6 warm-up bins. Adjacent spikes merge (gap 1 bin), max 90 s. Has streaming `add/advance/flush` and `detect_offline` |
| `pipeline.py` | `clip_window` (30 s pre-roll, 8 s post, 15-60 s), `finish_clip`: whisper → ASS + SRT → render → thumb → meta.json → index.html |
| `captions.py` | faster-whisper (lazy-loaded once, thread-safe, int8, VAD, word timestamps). ASS: 1-3 word uppercase chunks, karaoke `\kf` fill (spoken words turn yellow), pop-in scale, lower third. Also writes SRT |
| `clipper.py` | ffmpeg concat-demuxer cut from segments (re-encode libx264 veryfast crf 20), vertical 1080x1920 with blurred-background overlay, `ass=` burn-in run with cwd=clip dir (avoids path escaping) |
| `live.py` | `LiveClipper` orchestrator: main loop drains chat, runs `detector.advance(now - grace)`, and queues moments to a single clip-worker thread. The worker waits for the recorder to pass the window end, then cuts. Buffer cleanup never deletes segments a pending clip needs. Ctrl+C finishes pending clips, deletes the buffer (unless `--keep-recording`), writes the index |
| `replay.py` | Ended streams: downloads only the chat replay, then `detect_offline`, ranks the top N, then `yt-dlp --download-sections "*s-e" --force-keyframes-at-cuts` downloads just those seconds (or cuts from `--video-file`) |
| `report.py` | `clips/index.html` gallery (light/dark) |
| `demo.py` | Builds a synthetic stream (testsrc2 + drawtext + optional flite speech) and JSONL chat |
| `web.py` | `ytclip web`: stdlib local page on 127.0.0.1:8765 (Host-header + JSON-only POST guards). Runs one `ytclip` subprocess, streams its log, lists/serves clips (with Range for Safari). Stop = touches a `--stop-file` that live/demo/replay poll (works on Windows, unlike Ctrl+C) |

Output: `clips_output/session-<time>/clips/NNN_HHMMSS/{clip.mp4, raw.mp4, captions.srt, captions.ass, thumb.jpg, meta.json}` plus `clips/index.html`.

## Free dependencies (chosen deliberately)
- **yt-dlp** (most maintained YouTube tool): live chat reading + replay chat + section downloads
- **streamlink**: live HLS recording (`--retry-streams 30 --retry-max 0` when `--wait`)
- **chat-downloader** (xenova): chat fallback. Its last release was in 2023, so it's only the backup
- **faster-whisper** (SYSTRAN): local captions; the model comes from Hugging Face, free
- **FFmpeg** with libass (system install: winget `Gyan.FFmpeg` / brew / apt)
- Python 3.9+

## Decisions already made (don't re-ask)
- Detection is chat-spike based (as in the video). No paid LLMs or APIs.
- Default output is a vertical 9:16 Shorts clip with captions, ≤ 60 s.
- Live mode is primary. Replay mode is a bonus for ended streams with chat replay.
- Runs locally on the user's PC. Cloud servers are not recommended: YouTube bot-blocks
  datacenter IPs, and Colab/GitHub Actions/Replit free tiers can't run long jobs. Oracle Cloud
  Always Free is the only free 24/7 fallback if ever needed (may need `--cookies`).
- Remind the user to only post clips they have rights to (own streams or streamers who allow clipping).

## Useful commands
```
ytclip web            # the page START_CLIPPING.bat / .command open
ytclip doctor
ytclip demo [--no-captions]
ytclip live "<url>" [--wait] [--sensitivity low|medium|high] [--whisper-model tiny|base|small]
                    [--device cuda] [--horizontal] [--no-captions] [--chat-delay 5] [--quality 720]
ytclip replay "<url>" [--top 10] [--dry-run]
pytest -q
```

## Possible future improvements (not requested yet)
- Auto-upload to YouTube Shorts (YouTube Data API is free but needs OAuth setup)
- Audio-loudness spikes as a second signal next to chat
