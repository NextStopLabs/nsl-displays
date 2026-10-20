import io
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pi.nsl_pi.player import chain_clips  # noqa: E402


def _tone(path, freq=440, secs=0.2, rate=22050):
    import math
    import struct
    n = int(rate * secs)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n}h", *[int(10000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]))


def test_chain_sample_accurate_no_gap(tmp_path):
    a = str(tmp_path / "a.wav")
    b = str(tmp_path / "b.wav")
    _tone(a, 440)
    _tone(b, 660)
    pcm, rate = chain_clips([a, b])
    assert rate == 22050
    with wave.open(a, "rb") as w:
        na = w.getnframes()
    with wave.open(b, "rb") as w:
        nb = w.getnframes()
    assert len(pcm) == (na + nb) * 2  # exact join, no padding


def test_same_clip_stored_once():
    import hashlib
    h = hashlib.sha256(b"same").hexdigest()
    assert h == hashlib.sha256(b"same").hexdigest()
