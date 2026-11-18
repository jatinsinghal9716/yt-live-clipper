import json
import time
from pathlib import Path

import pytest

from ytclipper.captions import Word, chunk_words, write_ass, write_srt
from ytclipper.chat import ChatMessage, parse_ytdlp_line, read_ytdlp_chat_file
from ytclipper.clipper import cut_from_segments, probe, render_final, run_ffmpeg
from ytclipper.config import ClipConfig, DetectorConfig
from ytclipper.detector import SpikeDetector, detect_offline, message_weight
from ytclipper.pipeline import clip_window, finish_clip
from ytclipper.recorder import SegmentRecorder


# ---------------------------------------------------------------- chat parsing
def _yt_line(text_runs, author="Bob", ts_usec=1_700_000_000_000_000, offset_ms=None, paid=False):
    kind = "liveChatPaidMessageRenderer" if paid else "liveChatTextMessageRenderer"
    r = {"message": {"runs": text_runs}, "authorName": {"simpleText": author},
         "timestampUsec": str(ts_usec), "id": "x"}
    if paid:
        r["purchaseAmountText"] = {"simpleText": "$5.00"}
    obj = {"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {kind: r}}}]}}
    if offset_ms is not None:
        obj["replayChatItemAction"]["videoOffsetTimeMsec"] = str(offset_ms)
        obj["videoOffsetTimeMsec"] = str(offset_ms)
    return json.dumps(obj)


def test_parse_live_line_wallclock_and_emoji():
    line = _yt_line([{"text": "NO WAY "}, {"emoji": {"emojiId": "💀", "shortcuts": [":skull:"]}},
                     {"emoji": {"emojiId": "UC/x", "shortcuts": [":pog:"], "isCustomEmoji": True}}])
    [m] = parse_ytdlp_line(line)
    assert m.author == "Bob"
    assert m.text == "NO WAY 💀:pog:"
    assert m.t == pytest.approx(1_700_000_000.0)


def test_parse_replay_offset_and_superchat(tmp_path):
    lines = [_yt_line([{"text": "hi"}], offset_ms=5000),
             _yt_line([{"text": "gg"}], offset_ms=1500, paid=True),
             "garbage", ""]
    p = tmp_path / "c.live_chat.json"
    p.write_text("\n".join(lines))
    msgs = read_ytdlp_chat_file(p, use_offset=True)
    assert [m.t for m in msgs] == [1.5, 5.0]
    assert msgs[0].paid and msgs[0].extra["amount"] == "$5.00"


def test_parse_ignores_non_message_actions():
    line = json.dumps({"replayChatItemAction": {"actions": [
        {"addLiveChatTickerItemAction": {"item": {}}},
        {"addChatItemAction": {"item": {"liveChatViewerEngagementMessageRenderer": {}}}}]}})
    assert parse_ytdlp_line(line) == []


# ---------------------------------------------------------------- detector
def test_message_weight():
    assert message_weight("hello") == 1.0
    assert message_weight("CLIP IT!!") > 2.5
    assert message_weight("nooooo way") > 1.0
    assert message_weight("x", paid=True) == 3.0


def _chat(rate_fn, seconds, authors=30, seed=1):
    import random
    rnd = random.Random(seed)
    out, t = [], 0.0
    while t < seconds:
        t += rnd.expovariate(rate_fn(t))
        hype = rate_fn(t) > 2
        out.append(ChatMessage(t=t, author=f"u{rnd.randrange(authors)}",
                               text="NO WAY 💀" if hype else "hello there"))
    return out


def test_detects_spikes_and_merges():
    rate = lambda t: 6.0 if 200 <= t < 225 or 400 <= t < 430 else 0.5
    moments = detect_offline(_chat(rate, 600), DetectorConfig())
    assert len(moments) == 2
    assert 190 <= moments[0].start <= 210 and moments[0].end <= 240
    assert 390 <= moments[1].start <= 410
    assert all(m.intensity >= 2 for m in moments)
    assert moments[0].sample and "no" in moments[0].top_words


def test_no_spike_on_flat_chat_and_single_spammer():
    flat = _chat(lambda t: 1.0, 600)
    assert detect_offline(flat) == []
    spam = flat + [ChatMessage(t=300 + i * 0.1, author="spammer", text="CLIP IT!!") for i in range(80)]
    assert detect_offline(spam) == []


def test_streaming_matches_offline():
    msgs = _chat(lambda t: 7.0 if 300 <= t < 320 else 0.6, 500)
    det = SpikeDetector()
    got = []
    for m in msgs:
        got += det.advance(m.t - 5)
        det.add(m)
    got += det.flush()
    assert [round(m.start) for m in got] == [round(m.start) for m in detect_offline(msgs)]


def test_long_event_is_capped():
    cfg = DetectorConfig(max_event_seconds=60)
    msgs = _chat(lambda t: 8.0 if 200 <= t < 400 else 0.5, 600)
    moments = detect_offline(msgs, cfg)
    assert moments and all(m.end - m.start <= 60 for m in moments)


def test_clip_window():
    from ytclipper.detector import Moment
    m = Moment(start=100, end=120, peak_score=10, baseline=2, total_score=20, messages=10, authors=5)
    assert clip_window(m, ClipConfig()) == (70, 128)
    long = Moment(start=100, end=200, peak_score=10, baseline=2, total_score=20, messages=10, authors=5)
    s, e = clip_window(long, ClipConfig())
    assert e - s == 60


# ---------------------------------------------------------------- captions
WORDS = [Word(0.2, 0.5, "Oh"), Word(0.5, 0.8, "my"), Word(0.8, 1.2, "god."),
         Word(2.5, 2.8, "Did"), Word(2.8, 3.0, "you"), Word(3.0, 3.3, "see"), Word(3.3, 3.8, "that?")]


def test_chunking_and_files(tmp_path):
    chunks = chunk_words(WORDS)
    assert [len(c) for c in chunks] == [3, 3, 1]
    n = write_ass(WORDS, tmp_path / "c.ass", 1080, 1920)
    txt = (tmp_path / "c.ass").read_text()
    assert n == 3 and "PlayResY: 1920" in txt and "{\\kf" in txt and "OH" in txt
    write_srt(WORDS, tmp_path / "c.srt")
    srt = (tmp_path / "c.srt").read_text()
    assert "00:00:00,200 --> 00:00:01,200" in srt and "Did you see that?" in srt


# ---------------------------------------------------------------- media
@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    v = d / "src.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=14",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=14", "-c:v", "libx264",
                "-preset", "ultrafast", "-g", "30", "-c:a", "aac", "-shortest", str(v)])
    return v


class FakeTranscriber:
    def transcribe(self, media):
        return WORDS


def test_recorder_and_full_clip_pipeline(sample_video, tmp_path):
    rec = SegmentRecorder(tmp_path / "buf", input_file=str(sample_video), segment_seconds=2).start()
    assert rec.ended.wait(timeout=40)
    assert len(rec.segments) >= 6
    total = sum(s.duration for s in rec.segments)
    assert 12 <= total <= 16
    a = rec.segments[0].wall_start
    segs = rec.covering(a + 3, a + 9)
    raw = tmp_path / "clip" / "raw.mp4"
    raw.parent.mkdir()
    cut_from_segments(segs, a + 3, a + 9, raw, preset="ultrafast")
    assert abs(probe(raw)["duration"] - 6) < 0.6

    from ytclipper.detector import Moment
    m = Moment(start=a + 30, end=a + 40, peak_score=12, baseline=3, total_score=30, messages=40,
               authors=20, sample=["NO WAY", "CLIP IT"], top_words=["way", "clip"])
    meta = finish_clip(raw, raw.parent, m, ClipConfig(preset="ultrafast"), FakeTranscriber(),
                       {"stream_time": "0:03"}, tmp_path / "clip")
    info = probe(raw.parent / "clip.mp4")
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    assert meta["suggested_title"].startswith("Oh my god")
    assert (raw.parent / "captions.srt").exists() and (raw.parent / "thumb.jpg").exists()
    assert rec.cleanup(time.time() + 1) >= 6 and not list((tmp_path / "buf").glob("*.ts"))


def test_horizontal_without_captions(sample_video, tmp_path):
    raw = tmp_path / "raw.mp4"
    run_ffmpeg(["-i", str(sample_video), "-t", "3", "-c", "copy", str(raw)])
    w, h = render_final(raw, tmp_path / "out.mp4", vertical=False, ass_name=None, preset="ultrafast")
    assert (w, h) == (640, 360)


def test_replay_with_local_files(sample_video, tmp_path):
    from ytclipper.replay import ReplayOptions, run_replay
    # build a chat replay with a spike at 8-10s of the 14s video
    lines = []
    for i in range(70):
        t = i * 0.2 if i < 50 else 0  # filler
    for s in range(0, 14):
        lines.append(_yt_line([{"text": "hi"}], author=f"a{s % 4}", offset_ms=s * 1000))
    for k in range(40):
        lines.append(_yt_line([{"text": "NO WAY"}], author=f"b{k}", offset_ms=8000 + k * 50))
    chat = tmp_path / "x.live_chat.json"
    chat.write_text("\n".join(lines))
    det = DetectorConfig(bin_seconds=2, warmup_bins=3, min_score=4, min_authors=3)
    cfg = ClipConfig(pre_roll=3, post_roll=1, min_length=2, captions=False, preset="ultrafast")
    res = run_replay(ReplayOptions(url=None, out_dir=tmp_path / "out", video_file=str(sample_video),
                                   chat_file=str(chat)), det, cfg)
    assert len(res) == 1
    assert Path(res[0]["path"]).exists()
    assert (tmp_path / "out").glob("replay-*/clips/index.html")


def test_suggest_title_skips_one_letter_spam():
    from ytclipper.detector import Moment
    from ytclipper.pipeline import suggest_title

    mk = lambda s: Moment(0, 1, 1, 1, 1, 1, 1, sample=s)
    assert suggest_title("", mk(["C", "a", "bro skywhale is so hard"])) == "bro skywhale is so hard"
    assert suggest_title("", mk(["KEKW", "a"])) == "KEKW"
    assert suggest_title("", mk(["C"])) == "Live highlight"


def test_chat_text_strips_terminal_escapes():
    from ytclipper.chat import ChatMessage
    m = ChatMessage(t=0, author="a\x1b]0;x\x07", text="hi\x1b[2J 💀\n")
    assert m.text == "hi[2J 💀" and "\x1b" not in m.author


def test_web_guards(tmp_path):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from ytclipper.web import Job, make_handler

    (tmp_path / "a.txt").write_text("hello")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(Job(), tmp_path, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(method, path, host=f"127.0.0.1:{port}", body=None, ctype="application/json", rng=None):
        c = http.client.HTTPConnection("127.0.0.1", port)
        h = {"Host": host, "Content-Type": ctype}
        if rng:
            h["Range"] = rng
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        return r.status, r.read()

    try:
        assert req("GET", "/")[0] == 200
        assert req("GET", "/", host="evil.example:80")[0] == 403          # DNS rebinding
        assert req("GET", "/files/../test_core.py")[0] == 404             # path traversal
        assert req("GET", "/files/a.txt", rng="bytes=1-3") == (206, b"ell")
        assert req("POST", "/api/start", body="mode=demo", ctype="application/x-www-form-urlencoded")[0] == 415
        assert req("POST", "/api/start", body='{"mode":"live","url":"file:///etc"}')[0] == 400
    finally:
        srv.shutdown()


def test_hype_words_many_languages():
    from ytclipper.detector import _tokens, message_weight
    for hype in ["भाई क्या बात है", "kya baat hai bhai", "jajajaja", "kkkkkk", "ㅋㅋㅋ", "草", "wkwkwk", "हाहाहा"]:
        assert message_weight(hype) > 1.0, hype
    for plain in ["hello", "नमस्ते", "the house"]:
        assert message_weight(plain) == 1.0, plain
    assert message_weight("क्लिप करो") >= 2.0
    assert _tokens("भाई क्या बात है!") == ["भाई", "क्या", "बात", "है"]  # matras stay inside words


def test_emojis_any_kind():
    from ytclipper.detector import _emoji_count, message_weight
    assert [_emoji_count(t) for t in ["🫠", "👍🏽", "🤦‍♂️", "🇮🇳🇮🇳", ":face-purple-wide-eyes:", "12:30:45"]] == [1, 1, 1, 2, 1, 0]
    assert message_weight("🫠") > message_weight("ok")          # any emoji is a reaction
    assert message_weight("😂😂😂") > message_weight("😂") > message_weight("🫠")  # hype + repeats count more
