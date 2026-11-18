"""Chat-spike detector.

Chat is bucketed into fixed bins (default 10 s). Each message gets a weight
(hype words, emotes, "clip it", super chats count extra; one spammer cannot
carry a bin). A bin is a spike when its score beats a rolling robust
baseline (median + z * MAD) by a ratio, with enough distinct people talking.
Neighbouring spike bins merge into one Moment.

Works in streaming mode (live: add() + advance(now)) and offline mode
(replay: detect_offline(messages)).
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field

from .chat import ChatMessage
from .config import DetectorConfig

# Spikes come from message COUNT and distinct people, which works in every language.
# These lists only add a small bonus for hype words, in many languages and scripts.
HYPE_TOKENS = {
    # English / Twitch / YouTube slang
    "lol", "lmao", "lmfao", "rofl", "omg", "omfg", "wtf", "wow", "woah", "whoa",
    "pog", "poggers", "pogchamp", "kekw", "kek", "lul", "lulw", "w", "ww", "www",
    "holy", "insane", "crazy", "noway", "bro", "goat", "gg", "letsgo", "hype",
    "clip", "clipit", "clipped", "sheesh", "bruh", "dead", "hahaha", "haha",
    "yooo", "yoo", "ayo", "nooo", "noo", "what", "huh", "omegalul", "monkas",
    "f", "rip", "legend", "clutch", "massive", "unreal", "jaw", "op", "goated", "fire",
    # Hindi / Hinglish (roman + Devanagari)
    "bhai", "bhaiya", "kya", "mast", "zabardast", "jabardast", "bawal", "bawaal", "gajab",
    "gazab", "kamaal", "kamal", "maza", "mazaa", "pagal", "paagal", "arey", "arre", "oye",
    "uff", "haye", "khatarnak", "khatarnaak", "dhamaka", "jhakaas", "faad", "phaad", "tagda",
    "goosebumps", "bhayankar", "waah", "wah", "shabash", "ekdum",
    "भाई", "क्या", "वाह", "गजब", "ग़ज़ब", "कमाल", "जबरदस्त", "ज़बरदस्त", "मस्त", "बवाल",
    "हाहा", "अरे", "क्लिप", "धमाका", "खतरनाक", "पागल", "शाबाश",
    # other Indian languages (roman)
    "semma", "mass", "keka", "darun", "balle", "kalakkal", "thalaiva", "jhakkas",
    # Spanish / Portuguese / French / German / Italian
    "jaja", "jajaja", "increible", "increíble", "golazo", "vamos", "kkkk", "caramba", "nossa",
    "mano", "vamo", "mdr", "ptdr", "incroyable", "krass", "geil", "pazzesco",
    # Russian / Turkish / Indonesian / Filipino / Vietnamese / Arabic
    "ахаха", "ору", "жесть", "вау", "имба", "капец", "efsane", "vay", "wkwk", "anjir",
    "gila", "mantap", "grabe", "lodi", "petmalu", "vãi", "đỉnh", "واو", "ههههه",
    # Japanese / Korean / Chinese
    "やばい", "すごい", "神", "대박", "와", "미쳤다", "牛", "卧槽", "太强了", "厉害",
}
HYPE_PHRASES = ("no way", "let's go", "lets go", "clip it", "clip that", "oh my god", "what the",
                "kya baat", "kya scene", "op bolte", "chha gaye", "chaa gaye", "vera level",
                "oye hoye", "dios mio", "dios mío", "क्या बात", "छा गए", "मज़ा आ गया", "मजा आ गया")
# laughter / hype typed as repeated letters or without spaces, in many scripts
_LAUGH = re.compile(r"(?:ha){2,}|(?:ja){2,}|(?:he){3,}|(?:wk){2,}|(?:ха){2,}|(?:हा){2,}"
                    r"|k{4,}|w{3,}|5{3,}|6{3,}|ㅋ{2,}|ㅎ{2,}|草|哈{2,}|笑|ه{3,}|xd", re.I)
_CLIP_WORDS = ("clip", "क्लिप", "クリップ", "클립", "剪辑", "klip")
HYPE_EMOJI = set("💀😂🤣🔥😱😭😮🤯👏🙌💯😳🫨‼❗❓")
PER_AUTHOR_CAP = 3.0
_STRETCH = re.compile(r"(.)\1{2,}")


def _tokens(text: str) -> list[str]:
    """Words in any script: letters, digits and combining marks (Hindi matras etc.)."""
    out, cur = [], []
    for c in text:
        if c.isalnum() or unicodedata.category(c)[0] == "M":
            cur.append(c)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def message_weight(text: str, paid: bool = False) -> float:
    t = text.lower()
    toks = _tokens(t)
    words = {_STRETCH.sub(r"\1\1", w) for w in toks} | set(toks)
    w = 1.0
    if (words & HYPE_TOKENS or any(p in t for p in HYPE_PHRASES) or _LAUGH.search(t)
            or any(c in HYPE_EMOJI for c in text)):
        w += 0.5
    if any(k in t for k in _CLIP_WORDS):
        w += 1.0
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 4 and all(c.isupper() for c in letters):
        w += 0.25
    if text.count("!") >= 2 or "??" in text:
        w += 0.25
    if paid:
        w += 2.0
    return min(w, 4.0)


@dataclass
class Moment:
    start: float              # chat-clock seconds (wall epoch live, video offset replay)
    end: float
    peak_score: float
    baseline: float
    total_score: float
    messages: int
    authors: int
    sample: list[str] = field(default_factory=list)
    top_words: list[str] = field(default_factory=list)

    @property
    def intensity(self) -> float:
        return round(self.peak_score / max(self.baseline, 1.0), 2)

    def to_dict(self) -> dict:
        return {
            "start": self.start, "end": self.end, "peak_score": round(self.peak_score, 2),
            "baseline": round(self.baseline, 2), "intensity": self.intensity,
            "total_score": round(self.total_score, 2), "messages": self.messages,
            "authors": self.authors, "top_words": self.top_words, "sample_chat": self.sample,
        }


@dataclass
class _Bin:
    score: float = 0.0
    count: int = 0
    authors: Counter = field(default_factory=Counter)
    author_score: Counter = field(default_factory=Counter)
    texts: list[str] = field(default_factory=list)


_STOP = {"the", "a", "an", "is", "it", "to", "i", "you", "and", "of", "that", "this", "in",
         "he", "she", "on", "for", "was", "my", "me", "so", "be", "just", "are", "do", "with",
         "hai", "ki", "ka", "ke", "ko", "se", "ye", "wo", "na", "ho", "aur", "bhi", "mein",
         "है", "की", "का", "के", "को", "से", "ये", "वो", "तो", "ना", "हो", "और", "भी", "में",
         "que", "de", "la", "el", "es", "y", "en", "un", "le", "et", "les", "der", "die", "das"}


class SpikeDetector:
    def __init__(self, cfg: DetectorConfig | None = None) -> None:
        self.cfg = cfg or DetectorConfig()
        self.bins: dict[int, _Bin] = defaultdict(_Bin)
        self.history: deque[float] = deque(maxlen=self.cfg.baseline_bins)
        self.next_bin: int | None = None
        self._event: list[tuple[int, _Bin, float, float]] | None = None  # (idx, bin, score, baseline)
        self._quiet = 0
        self.last_status: dict = {}

    # -- input ------------------------------------------------------------- #
    def add(self, msg: ChatMessage) -> None:
        idx = int(msg.t // self.cfg.bin_seconds)
        if self.next_bin is not None and idx < self.next_bin:
            return  # arrived after its bin closed; ignore
        b = self.bins[idx]
        b.authors[msg.author] += 1
        # anti-spam: one person can add at most PER_AUTHOR_CAP to a bin
        used = b.author_score[msg.author]
        w = max(0.0, min(message_weight(msg.text, msg.paid), PER_AUTHOR_CAP - used))
        b.author_score[msg.author] = used + w
        b.score += w
        b.count += 1
        if len(b.texts) < 200:
            b.texts.append(msg.text)
        if self.next_bin is None:
            self.next_bin = idx

    # -- stats ------------------------------------------------------------- #
    def _threshold(self) -> tuple[float, float] | None:
        if len(self.history) < self.cfg.warmup_bins:
            return None
        med = statistics.median(self.history)
        mad = statistics.median(abs(x - med) for x in self.history)
        thr = max(self.cfg.min_score, med * self.cfg.ratio, med + self.cfg.z * 1.4826 * mad)
        return thr, med

    # -- processing -------------------------------------------------------- #
    def advance(self, now: float) -> list[Moment]:
        """Close every bin that ended before ``now`` and return finished moments."""
        out: list[Moment] = []
        if self.next_bin is None:
            return out
        last_closed = int(now // self.cfg.bin_seconds) - 1
        while self.next_bin <= last_closed:
            out.extend(self._close(self.next_bin))
            self.next_bin += 1
        return out

    def flush(self) -> list[Moment]:
        out = []
        if self.bins:
            out.extend(self.advance((max(self.bins) + 1) * self.cfg.bin_seconds + 0.001))
        if self._event:
            out.append(self._finish())
        return out

    def _close(self, idx: int) -> list[Moment]:
        b = self.bins.pop(idx, None) or _Bin()
        th = self._threshold()
        out: list[Moment] = []
        is_spike = False
        if th is not None:
            thr, med = th
            is_spike = b.score >= thr and len(b.authors) >= self.cfg.min_authors
            self.last_status = {"bin_score": round(b.score, 1), "messages": b.count,
                                "baseline": round(med, 1), "threshold": round(thr, 1)}
        else:
            self.last_status = {"bin_score": round(b.score, 1), "messages": b.count,
                                "warming_up": f"{len(self.history)}/{self.cfg.warmup_bins}"}
        self.history.append(b.score)

        if is_spike:
            if self._event is None:
                self._event = []
            self._event.append((idx, b, b.score, th[1]))
            self._quiet = 0
            dur = (idx - self._event[0][0] + 1) * self.cfg.bin_seconds
            if dur >= self.cfg.max_event_seconds:
                out.append(self._finish())
        elif self._event is not None:
            self._quiet += 1
            if self._quiet > self.cfg.merge_gap_bins:
                out.append(self._finish())
        return out

    def _finish(self) -> Moment:
        ev, self._event, self._quiet = self._event, None, 0
        bs = self.cfg.bin_seconds
        authors: Counter = Counter()
        words: Counter = Counter()
        texts: list[str] = []
        for _, b, _, _ in ev:
            authors.update(b.authors)
            texts.extend(b.texts)
            for t in b.texts:
                for w in set(_tokens(t.lower())):
                    if w not in _STOP and len(w) > 1:
                        words[w] += 1
        peak = max(e[2] for e in ev)
        baseline = statistics.median(e[3] for e in ev)
        common = Counter(t.strip() for t in texts if t.strip()).most_common(8)
        return Moment(
            start=ev[0][0] * bs, end=(ev[-1][0] + 1) * bs, peak_score=peak, baseline=baseline,
            total_score=sum(e[2] for e in ev), messages=sum(e[1].count for e in ev),
            authors=len(authors), sample=[t for t, _ in common],
            top_words=[w for w, _ in words.most_common(6)],
        )


def detect_offline(messages: list[ChatMessage], cfg: DetectorConfig | None = None) -> list[Moment]:
    det = SpikeDetector(cfg)
    out: list[Moment] = []
    for m in sorted(messages, key=lambda m: m.t):
        out.extend(det.advance(m.t))
        det.add(m)
    out.extend(det.flush())
    return out
