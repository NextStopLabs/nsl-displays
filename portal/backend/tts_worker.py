"""TTS worker - Piper runs here sandboxed (plan.md 5.3b)."""
from __future__ import annotations


def synth(text: str, voice: str, speed: float = 1.0) -> bytes:
    """Return canonical WAV bytes (mono 16-bit @22050Hz).

    Placeholder is an audible two-tone chime, never digital silence, so a
    broken play chain can't masquerade as success. Replace the chime body
    with a `piper --model ...` subprocess when voices are installed.
    """
    import io
    import math
    import struct
    import wave
    rate = 22050
    n_chars = max(1, min(len(text), 60))
    secs = min(4.0, 0.6 + 0.06 * n_chars)
    n = int(rate * secs)
    frames = []
    for i in range(n):
        t = i / rate
        f = 660.0 if t < secs / 2 else 880.0
        env = 0.6 + 0.4 * math.sin(math.pi * t / secs)
        frames.append(int(12000 * env * math.sin(2 * math.pi * f * t)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n}h", *frames))
    return buf.getvalue()


if __name__ == "__main__":
    print("tts worker placeholder - queue polling goes here")
