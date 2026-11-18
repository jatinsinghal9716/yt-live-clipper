"""Settings shared by every mode. All defaults are tuned for 9:16 shorts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DetectorConfig:
    bin_seconds: float = 10.0       # chat is counted in windows of this size
    baseline_bins: int = 36         # rolling baseline length (36 x 10s = 6 min)
    warmup_bins: int = 6            # bins needed before any spike can fire
    ratio: float = 2.0              # spike must be >= ratio x the baseline median
    z: float = 3.0                  # ... and >= median + z * robust std-dev
    min_score: float = 6.0          # ... and at least this weighted score per bin
    min_authors: int = 3            # ... from at least this many different people
    merge_gap_bins: int = 1         # spike bins this close together become one moment
    max_event_seconds: float = 90.0  # a single moment never grows past this

    @classmethod
    def with_sensitivity(cls, level: str) -> "DetectorConfig":
        presets = {
            "low": dict(ratio=2.6, z=4.0, min_score=10.0, min_authors=5),
            "medium": {},
            "high": dict(ratio=1.6, z=2.2, min_score=4.0, min_authors=2),
        }
        if level not in presets:
            raise ValueError(f"sensitivity must be one of {sorted(presets)}")
        return cls(**presets[level])


@dataclass
class ClipConfig:
    pre_roll: float = 30.0          # seconds kept before the chat spike starts
    post_roll: float = 8.0          # seconds kept after the spike ends
    max_length: float = 60.0        # hard cap (YouTube Shorts friendly)
    min_length: float = 15.0
    vertical: bool = True           # 1080x1920 with blurred background
    captions: bool = True           # burn word-by-word captions with faster-whisper
    whisper_model: str = "base"     # tiny / base / small / medium / large-v3
    whisper_device: str = "auto"    # auto / cpu / cuda
    whisper_compute: str = "int8"   # int8 is fastest on CPU
    language: str | None = None     # None = auto-detect
    crf: int = 20
    preset: str = "veryfast"
    font: str = "Arial"
