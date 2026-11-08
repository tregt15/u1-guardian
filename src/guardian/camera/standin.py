"""Stand-in video source for testing without a camera.

FFmpeg makes a test pattern. A strip at the top shows a running clock as
24 black and white blocks, a binary counter of the frame number. Code can
read it back from any frame (see read_frame_clock), so tests can check
which moment a clip really shows. No font and no OCR needed, so it works
the same on Windows and Linux.

The clock is "seconds since this FFmpeg process started". It restarts at
zero if capture restarts.
"""

from __future__ import annotations

import subprocess

WIDTH = 320
HEIGHT = 240
FPS = 10
CLOCK_BITS = 24
BLOCK_W = 12
STRIP_H = 16


def lavfi_source() -> str:
    return f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}"


def clock_filter() -> str:
    """FFmpeg filter chain that burns the binary frame counter into the top strip."""
    parts = [f"drawbox=x=0:y=0:w={WIDTH}:h={STRIP_H}:color=black:t=fill"]
    for k in range(CLOCK_BITS):
        parts.append(
            f"drawbox=x={k * BLOCK_W}:y=0:w={BLOCK_W}:h={STRIP_H}:color=white:t=fill"
            f":enable='eq(mod(floor(n/{1 << k}),2),1)'"
        )
    return ",".join(parts)


def decode_clock(gray: bytes) -> float:
    """Read the clock (seconds) from one raw 8-bit gray frame of WIDTH x HEIGHT."""
    if len(gray) != WIDTH * HEIGHT:
        raise ValueError(f"expected {WIDTH * HEIGHT} bytes, got {len(gray)}")
    n = 0
    for k in range(CLOCK_BITS):
        x0 = k * BLOCK_W + BLOCK_W // 2 - 3
        y0 = STRIP_H // 2 - 3
        total = 0
        for y in range(y0, y0 + 6):
            row = y * WIDTH
            total += sum(gray[row + x0 : row + x0 + 6])
        if total / 36 > 128:
            n |= 1 << k
    return n / FPS


def read_frame_clock(ffmpeg: str, clip, at_s: float) -> float:
    """Decode the frame at `at_s` seconds into `clip` and return its burned-in clock."""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(at_s, 0):.3f}",
        "-i", str(clip), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=60)
    if proc.returncode != 0 or len(proc.stdout) != WIDTH * HEIGHT:
        raise RuntimeError(f"could not read frame at {at_s}s from {clip}: {proc.stderr[-200:]!r}")
    return decode_clock(proc.stdout)
