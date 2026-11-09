"""Build one clip from pinned segments, then check it with ffprobe.

Copy mode only: no re-encoding. H.264 goes into MP4. Anything else (for
example MJPEG) stays in Matroska, because MP4 is a poor fit for it.
UNVERIFIED: the real U1 camera format. Spec section 6.3 says an MJPEG source
may need one encode at finalize. That is not built yet.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


from guardian import hostinfo


class AssemblyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    video_codec: str
    width: int
    height: int


def probe(ffprobe: str, path: Path) -> ProbeResult:
    """Read a video file with ffprobe. Raises AssemblyError if it is not a readable video."""
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssemblyError(f"ffprobe failed on {path.name}: {exc}") from exc
    if proc.returncode != 0:
        raise AssemblyError(f"ffprobe error on {path.name}: {proc.stderr.decode('utf-8', 'replace')[-300:]}")
    try:
        info = json.loads(proc.stdout)
        video = next(s for s in info["streams"] if s.get("codec_type") == "video")
        duration = float(info["format"]["duration"])
        return ProbeResult(duration, video["codec_name"], int(video["width"]), int(video["height"]))
    except (StopIteration, KeyError, ValueError) as exc:
        raise AssemblyError(f"{path.name} has no readable video stream or duration") from exc


def _concat_line(path: Path) -> str:
    # concat demuxer syntax. Forward slashes work on Windows too. Escape single quotes.
    return "file '" + path.resolve().as_posix().replace("'", "'\\''") + "'\n"


def _concat_text(segments: list[Path], durations: list[float] | None) -> str:
    """Body of the concat list. With durations (JPEG frames), each file lasts exactly that long,
    so clip time equals real time. The last file is listed again because the concat demuxer
    ignores the duration of the final entry."""
    if durations is None:
        return "".join(_concat_line(p) for p in segments)
    if len(durations) != len(segments):
        raise AssemblyError("durations and frames do not match")
    out = []
    for p, d in zip(segments, durations):
        out.append(_concat_line(p) + f"duration {max(d, 0.04):.3f}\n")
    out.append(_concat_line(segments[-1]))
    return "".join(out)


def assemble(ffmpeg: str, ffprobe: str, segments: list[Path], out_dir: Path,
             durations: list[float] | None = None) -> Path:
    """Join segments in order into out_dir/clip.mp4 (or clip.mkv). Returns the clip path.
    Pass `durations` when the inputs are single JPEG frames (snapshot cameras)."""
    if not segments:
        raise AssemblyError("no segments to assemble")
    out_dir.mkdir(parents=True, exist_ok=True)
    first = probe(ffprobe, segments[0])
    suffix = ".mp4" if first.video_codec == "h264" else ".mkv"
    clip = out_dir / f"clip{suffix}"
    list_file = out_dir / "concat.txt"
    list_file.write_text(_concat_text(segments, durations), encoding="utf-8")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy",
    ]
    if suffix == ".mp4":
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(clip))
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssemblyError(f"ffmpeg concat failed: {exc}") from exc
    finally:
        list_file.unlink(missing_ok=True)
    if proc.returncode != 0 or not clip.exists():
        clip.unlink(missing_ok=True)
        raise AssemblyError(f"ffmpeg concat error: {proc.stderr.decode('utf-8', 'replace')[-300:]}")
    return clip


def make_playable(ffmpeg: str, clip: Path) -> Path:
    """Browsers cannot play MJPEG. Re-encode once to H.264 MP4 (the only re-encode in Guardian),
    at low CPU priority, at most 1280 pixels wide, 5 frames a second. Clip time is kept, so the
    trigger marker stays right. The original file is renamed to clip_original.mkv and kept.
    Returns the MP4 path. Raises AssemblyError and leaves the original untouched on failure."""
    out_dir = clip.parent
    mp4 = out_dir / "clip.mp4"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip), "-an",
        # JPEG frames are full range (yuvj420p). Convert to normal limited range yuv420p, which every
        # browser and phone decodes. Browsers also need an even width and height.
        "-vf", "scale='min(1280,iw)':-2:in_range=full:out_range=limited,fps=5,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        "-movflags", "+faststart", str(mp4),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=900, **hostinfo.low_priority_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        mp4.unlink(missing_ok=True)
        raise AssemblyError(f"could not make the playable copy: {exc}") from exc
    if proc.returncode != 0 or not mp4.exists():
        mp4.unlink(missing_ok=True)
        raise AssemblyError(f"playable copy failed: {proc.stderr.decode('utf-8', 'replace')[-200:]}")
    original = out_dir / "clip_original.mkv"
    if clip.suffix == ".mkv" and clip != original:
        clip.replace(original)
    return mp4
