"""
PC-side conversion tool -- NOT a board script, run this with the project's .venv on your PC.

This project's board firmware has no MP3 decoder: MicroPython/ESP32 doesn't ship one, and
real-time MP3 decoding on-device would need a native C decoder (e.g. libhelix-mp3) compiled
into a custom lvgl_micropython build, not a loose .py file (see music_player.py's module
docstring). So MP3s get decoded to raw 16-bit PCM WAV ahead of time, here on the PC, and only
the WAV goes on the SD card / gets played by music_player.py.

Uses the `miniaudio` package (prebuilt wheel, no ffmpeg needed -- there isn't one in this dev
environment):
    .venv\\Scripts\\python.exe -m pip install miniaudio

Usage:
    .venv\\Scripts\\python.exe mp3_to_wav.py demo1.mp3 [demo1.wav]

Preserves the source file's own channel count and sample rate (miniaudio.decode_file()
otherwise defaults to forcing stereo/44100, which would silently upmix a mono source and
double its size for no reason).
"""
import sys
import wave

import miniaudio


def convert(mp3_path, wav_path=None):
    if wav_path is None:
        wav_path = mp3_path.rsplit(".", 1)[0] + ".wav"

    info = miniaudio.get_file_info(mp3_path)
    decoded = miniaudio.decode_file(
        mp3_path,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=info.nchannels,
        sample_rate=info.sample_rate,
    )

    with wave.open(wav_path, "wb") as wav_file:
        wav_file.setnchannels(decoded.nchannels)
        wav_file.setsampwidth(2)  # SIGNED16
        wav_file.setframerate(decoded.sample_rate)
        wav_file.writeframes(decoded.samples.tobytes())

    return wav_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: mp3_to_wav.py <input.mp3> [output.wav]")
        raise SystemExit(1)

    out = convert(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print("Wrote", out)
