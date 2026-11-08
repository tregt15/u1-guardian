"""Fire fake incidents at known times and check the saved clips.

Examples (run from the repo, with the venv active and FFmpeg on PATH):

    python tools/simulate_trigger.py                  # 100 triggers, fast, fake clock
    python tools/simulate_trigger.py --count 20 --codec mjpeg
    python tools/simulate_trigger.py --live           # real time, one trigger, about 1 minute

Each clip is checked against the burned-in clock of the stand-in video.
Exit code is 0 only if every incident passed. Diagnostic only: this never
touches a printer.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import shutil
import sys
import tempfile
from pathlib import Path

from guardian import sim
from guardian.camera.capture import FFmpegMissing


def _report(env: sim.SimEnv, uids: list[str]) -> int:
    failed = 0
    worst = 0.0
    for uid in uids:
        problems, stats = sim.verify_incident(env, uid)
        if stats:
            worst = max(worst, abs(stats["trigger_error_s"]))
        if problems:
            failed += 1
            print(f"FAIL {uid}")
            for p in problems:
                print(f"     {p}")
    ok = len(uids) - failed
    print(f"\n{ok} of {len(uids)} incidents passed. Worst trigger position error: {worst:.2f} s "
          f"(allowed: {env.cfg.camera.segment_length_s:g} s).")
    return 0 if failed == 0 else 1


async def main_async(args: argparse.Namespace, workdir: Path) -> int:
    cfg = sim.make_config(workdir, codec=args.codec)
    if args.live:
        env, result = await sim.run_live(cfg)
        return _report(env, [result.uid])

    rng = random.Random(args.seed)
    spacing = max(args.spacing, cfg.blackbox.post_trigger_s + 2)
    first = cfg.blackbox.pre_trigger_s + 10
    triggers = [
        sim.SimTrigger(at_s=first + i * spacing + rng.uniform(0, 1.9), dedup_key=f"t{i}")
        for i in range(args.count)
    ]
    total = triggers[-1].at_s + cfg.blackbox.post_trigger_s + 20
    print(f"Making {total:.0f} s of stand-in video ({args.codec}) ...")
    env = await sim.build_fast_env(cfg, total)
    print(f"Firing {len(triggers)} triggers ...")
    await sim.run_fast(env, triggers, total)
    uids = sorted({r.uid for r in env.results})
    created = sum(1 for r in env.results if r.action == "created")
    print(f"{created} incidents created from {len(env.results)} triggers. Checking clips ...")
    return _report(env, uids)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=100, help="number of triggers (fast mode)")
    ap.add_argument("--spacing", type=float, default=20.0, help="seconds between triggers (min 17)")
    ap.add_argument("--codec", choices=["h264", "mjpeg"], default="h264")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--live", action="store_true", help="real time, one trigger")
    ap.add_argument("--keep", type=Path, help="write to this folder and keep it")
    args = ap.parse_args()

    workdir = args.keep or Path(tempfile.mkdtemp(prefix="guardian-sim-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        return asyncio.run(main_async(args, workdir))
    except FFmpegMissing as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"Files kept in {workdir}")


if __name__ == "__main__":
    sys.exit(main())
