"""TTS worker stub — Piper runs here sandboxed (plan.md 5.3b). Real deploy swaps synth()."""
from __future__ import annotations


def synth(text: str, voice: str, speed: float = 1.0) -> bytes:
    """Return WAV bytes. Replace with `piper --model ...` subprocess."""
    import io
    import struct
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(struct.pack("<22050h", *[0] * 22050))
    return buf.getvalue()


if __name__ == "__main__":
    print("tts worker placeholder — queue polling goes here")
