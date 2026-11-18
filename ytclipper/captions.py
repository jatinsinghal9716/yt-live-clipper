"""Local, free captions with faster-whisper (SYSTRAN/faster-whisper).

Produces word timestamps, then writes:
  * captions.ass - punchy 1-3 word "shorts style" captions burned into the video
  * captions.srt - normal sentence captions to upload alongside the clip
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ytclipper.captions")


@dataclass
class Word:
    start: float
    end: float
    text: str


class Transcriber:
    """Loads the model once (lazily) and is safe to share between threads."""

    def __init__(self, model: str = "base", device: str = "auto", compute_type: str = "int8",
                 language: str | None = None) -> None:
        self.model_name, self.device, self.compute_type, self.language = model, device, compute_type, language
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("captions: loading faster-whisper '%s' (first run downloads it once, free)",
                     self.model_name)
            try:
                self._model = WhisperModel(self.model_name, device=self.device,
                                           compute_type=self.compute_type)
            except ValueError:  # e.g. int8 unsupported on this GPU -> safe default
                self._model = WhisperModel(self.model_name, device=self.device, compute_type="default")
        return self._model

    def transcribe(self, media: str | Path) -> list[Word]:
        with self._lock:
            model = self._load()
            segments, _info = model.transcribe(str(media), language=self.language,
                                               word_timestamps=True, vad_filter=True,
                                               condition_on_previous_text=False)
            words: list[Word] = []
            for seg in segments:
                for w in seg.words or []:
                    txt = w.word.strip()
                    if txt:
                        words.append(Word(float(w.start), float(w.end), txt))
            return words


# --------------------------------------------------------------------------- #
def _ts_ass(t: float) -> str:
    t = max(t, 0.0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ts_srt(t: float) -> str:
    t = max(t, 0.0)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def chunk_words(words: list[Word], max_words: int = 3, max_gap: float = 0.6,
                max_chars: int = 18) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap
                    or len(" ".join(x.text for x in cur + [w])) > max_chars
                    or cur[-1].text.endswith((".", "?", "!"))):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def write_ass(words: list[Word], path: Path, width: int, height: int, font: str = "Arial") -> int:
    vertical = height > width
    size = round(min(width, height) * (0.085 if vertical else 0.07))
    margin_v = round(height * (0.24 if vertical else 0.08))
    outline = max(3, size // 14)
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Pop,{font},{size},&H0000FFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},2,2,60,60,{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    chunks = chunk_words(words)
    for i, ch in enumerate(chunks):
        start = ch[0].start
        end = max(ch[-1].end, start + 0.35)
        if i + 1 < len(chunks):
            end = min(end + 0.25, chunks[i + 1][0].start)
        parts = []
        for j, w in enumerate(ch):  # words turn yellow as they are spoken (karaoke fill)
            nxt = ch[j + 1].start if j + 1 < len(ch) else w.end
            k = max(1, int(round((nxt - w.start) * 100)))
            parts.append(("{\\kf%d}" % k) + _ass_escape(w.text.upper()))
        text = "{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}" + " ".join(parts)
        lines.append(f"Dialogue: 0,{_ts_ass(start)},{_ts_ass(end)},Pop,,0,0,0,,{text}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def write_srt(words: list[Word], path: Path, max_chars: int = 42) -> None:
    out, cur, idx = [], [], 1

    def flush():
        nonlocal idx, cur
        if cur:
            out.append(f"{idx}\n{_ts_srt(cur[0].start)} --> {_ts_srt(cur[-1].end)}\n"
                       f"{' '.join(w.text for w in cur)}\n")
            idx += 1
            cur = []

    for w in words:
        if cur and (len(" ".join(x.text for x in cur + [w])) > max_chars
                    or w.start - cur[-1].end > 1.0):
            flush()
        cur.append(w)
        if w.text.endswith((".", "?", "!")):
            flush()
    flush()
    path.write_text("\n".join(out), encoding="utf-8")
