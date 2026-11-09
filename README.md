# U1 Guardian

A diagnostic tool for the Snapmaker U1 3D printer. It is open source (MIT).

Guardian keeps a rolling camera buffer. When something goes wrong, it saves the video from before and after the event. The goal is to show what happened right before a print failed.

Guardian only watches. It never sends a command that changes the printer.

## Status

Early work. The capture and incident code has only been tested with a stand-in test video. The camera on a real U1 has not been read yet.

What exists so far (Milestone 0, no printer needed):

- Config, event bus, clock, SQLite storage
- Camera capture with a rolling 2 second segment buffer (stand-in test video for now)
- Incident manager: saves 30 s before and 15 s after a trigger, merges repeats, and reports honest recording status
- `tools/simulate_trigger.py` to fire fake triggers and check the clips
- Read-only Moonraker WebSocket client, event detectors, and `tools/ws_recorder.py`
- `python -m guardian.main` runs it all (see config.example.yaml)
- HTTP API and a browser dashboard (incident list, incident page with video, trigger marker and timeline)
- Camera on the U1: snapshot polling only. Footage exists only while the Snapmaker app or the printer screen has the camera on (about 1 frame per second)

Everything about the real U1 (camera format, Moonraker objects, detection alerts) is **unverified**.

## Platforms

| Platform | Tested |
|---|---|
| Windows 11 | capture, incidents and tests run here |
| Raspberry Pi 3B+ | not yet |
| Linux, macOS | not yet |

## Run it

```
copy config.example.yaml config.yaml
python -m guardian.main --config config.yaml
```

Then open http://127.0.0.1:8080/ in a browser. The API only listens on this computer unless you change `api.bind`. If you do, set a token (`api.token` or the `GUARDIAN_API_TOKEN` environment variable). Recordings can show the inside of your home or workshop, so treat them as private.

## Requirements

- Python 3.11 or newer
- FFmpeg and ffprobe on your PATH (not bundled)

## Dev setup (Windows cmd)

```
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
pytest
```

Run the simulation (100 fake triggers, checks each clip, about 1 to 2 minutes):

```
python tools/simulate_trigger.py
python tools/simulate_trigger.py --live
```

## License

MIT. See LICENSE.
