"""Make browser-playable MP4 copies of older incidents that only have an MJPEG .mkv clip.

    python tools/make_playable.py --config config.yaml

New incidents get this copy automatically (blackbox.make_playable_copy). This is only for clips
saved before that existed. The original is kept as clip_original.mkv. Safe to run twice.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from guardian.blackbox.assembler import AssemblyError, make_playable, probe
from guardian.camera.capture import FFmpegMissing, check_ffmpeg
from guardian.config import ConfigError, load_config
from guardian.storage.db import Database


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path)
    args = ap.parse_args()
    try:
        cfg = load_config(args.config)
        ffmpeg, ffprobe = check_ffmpeg(cfg.camera)
    except (ConfigError, FFmpegMissing) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    data = cfg.storage.resolved_data_dir()
    db = Database(cfg.storage.db_path)
    done = skipped = failed = 0
    for row in db.list_incidents():
        rel = row["recording_path"]
        if not rel or not rel.endswith(".mkv"):
            skipped += 1
            continue
        clip = data / rel
        try:
            if not clip.is_file():
                raise AssemblyError("clip file is missing")
            if probe(ffprobe, clip).video_codec == "h264":
                skipped += 1
                continue
            mp4 = make_playable(ffmpeg, clip)
            db.update_incident(row["id"], recording_path=mp4.relative_to(data).as_posix())
            print(f"ok    {row['uid']}")
            done += 1
        except AssemblyError as exc:
            print(f"FAIL  {row['uid']}: {exc}")
            failed += 1
    print(f"{done} converted, {skipped} skipped, {failed} failed")
    db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
