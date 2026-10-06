#!/usr/bin/env python3
"""Deterministically generate audio/beep.wav: 0.12 s, 440 Hz sine, 16-bit mono 22050 Hz."""
import math, struct, sys, wave

out = sys.argv[1]
rate, n = 22050, int(22050 * 0.12)
frames = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate) * (1 - i / n))) for i in range(n))
with wave.open(out, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(frames)
