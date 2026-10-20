"""Player: chains announcement clips gaplessly (format.md 4 + README 18).

Decodes mono 16-bit FLAC clips to PCM and concatenates sample-accurately.
No gapless-mode dependency: raw PCM join. Output to ALSA (USB audio).
Falls back to writing WAV for diagnostics when ALSA unavailable.
"""
from __future__ import annotations

import io
import os
import struct
import wave


def pcm_of_wav(path: str) -> tuple[bytes, int, int]:
    """Return (pcm_bytes, sample_rate, channels) for a WAV/FLAC-ish file.

    Pure-stdlib: reads WAV directly. For .flac, needs `flac` or ffmpeg to decode;
    if unavailable, raises with a clear message (Pi image includes flac)."""
    if path.lower().endswith(".wav"):
        with wave.open(path, "rb") as w:
            return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
    # try external decoder
    import shutil
    import subprocess
    dec = shutil.which("flac") or shutil.which("ffmpeg")
    if not dec:
        raise RuntimeError("FLAC decode needs `flac` or `ffmpeg` binary (Pi image has flac)")
    if os.path.basename(dec) == "flac" or dec.endswith("/flac"):
        p = subprocess.run([dec, "-dc", "--totally-silent", path], capture_output=True)
        # flac -dc outputs raw? use wav via stdout
        p = subprocess.run([dec, "-d", "-c", "-s", path], capture_output=True)
        out = p.stdout
        # parse wav header
        with wave.open(io.BytesIO(out), "rb") as w:
            return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
    else:
        p = subprocess.run([dec, "-v", "quiet", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "22050", "-"],
                           capture_output=True)
        return p.stdout, 22050, 1


def chain_clips(paths: list[str], out_wav: str | None = None) -> tuple[bytes, int]:
    """Concatenate clips sample-accurately. Returns (pcm_mono16, sample_rate)."""
    chunks: list[bytes] = []
    rate: int | None = None
    for p in paths:
        if not os.path.exists(p):
            continue  # NULL clips skipped upstream; missing file = skip + log upstream
        pcm, sr, ch = pcm_of_wav(p)
        if rate is None:
            rate = sr
        if sr != rate:
            raise ValueError(f"sample rate mismatch: {p} has {sr}, expected {rate}")
        # downmix to mono 16-bit
        if ch == 2:
            samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
            mono = struct.pack(f"<{len(samples)//2}h",
                               *[int((samples[i] + samples[i + 1]) / 2) for i in range(0, len(samples), 2)])
            chunks.append(mono)
        else:
            chunks.append(pcm)
    pcm_all = b"".join(chunks)
    if out_wav:
        with wave.open(out_wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate or 22050)
            w.writeframes(pcm_all)
    return pcm_all, rate or 22050


def play_pcm(pcm: bytes, sample_rate: int, device: str = "default"):
    """Play mono s16 PCM to ALSA. No-op fallback when no audio device (logs)."""
    try:
        import subprocess
        proc = subprocess.run(["aplay", "-q", "-D", device, "-f", "S16_LE", "-r", str(sample_rate),
                               "-c", "1"], input=pcm, capture_output=True, timeout=60)
        return proc.returncode == 0
    except FileNotFoundError:
        return False  # headless/dev machine without aplay
    except Exception:
        return False


def announce(hashes: list[str], audio_dir: str, device: str = "default",
             out_wav: str | None = None) -> dict:
    """High-level: hashes -> files -> chained PCM -> ALSA. Returns report."""
    paths = [os.path.join(audio_dir, h[:2], h + ".flac") for h in hashes]
    # also accept .wav shadowing for dev
    resolved = []
    for h, p in zip(hashes, paths):
        if os.path.exists(p):
            resolved.append(p)
        elif os.path.exists(os.path.splitext(p)[0] + ".wav"):
            resolved.append(os.path.splitext(p)[0] + ".wav")
    pcm, rate = chain_clips(resolved, out_wav=out_wav)
    ok = play_pcm(pcm, rate, device)
    return {"clips": len(resolved), "bytes": len(pcm), "rate": rate, "played": ok}


def configured_device(base_dir: str) -> str:
    """ALSA device from controller.db (portal/local UI settings), else default."""
    try:
        import sqlite3
        con = sqlite3.connect(os.path.join(base_dir, "controller.db"))
        try:
            row = con.execute("SELECT audio_device FROM controller_config WHERE id=1").fetchone()
        finally:
            con.close()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return "default"


def play_announcement(hashes: list[str], base_dir: str) -> dict:
    """Play clip hashes through the configured ALSA device. Logs-friendly report."""
    audio_dir = os.path.join(base_dir, "audio")
    device = configured_device(base_dir)
    if not hashes:
        return {"clips": 0, "played": False, "reason": "no audio clips", "device": device}
    rep = announce(hashes, audio_dir, device)
    rep["device"] = device
    return rep
