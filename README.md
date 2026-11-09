# U1 Guardian

A diagnostic tool for the Snapmaker U1 3D printer. It is open source (MIT).

Guardian keeps a rolling camera buffer. When something goes wrong, it saves the video from before and after the event. The goal is to show what happened right before a print failed.

Guardian only watches. It never sends a command that changes the printer.

## Status

Early work. It has run on one real U1 on Windows. It has not run on a Raspberry Pi yet.

What exists so far (Milestone 0, no printer needed):

- Config, event bus, clock, SQLite storage
- Camera capture with a rolling 2 second segment buffer (stand-in test video for now)
- Incident manager: saves 30 s before and 15 s after a trigger, merges repeats, and reports honest recording status
- `tools/simulate_trigger.py` to fire fake triggers and check the clips
- Read-only Moonraker WebSocket client, event detectors, and `tools/ws_recorder.py`
- `python -m guardian.main` runs it all (see config.example.yaml)
- HTTP API and a browser dashboard (incident list, incident page with video, trigger marker and timeline)
- Tool change log: every tool change with its time and result. Alerts when a change gets stuck, ends on the wrong tool,
  or ends with odd tool states. Checked against 407 real tool changes (no false alarms). No real failed change has been seen yet
- Alert when the filament feeder reports an error (seen once on a real U1)
- Camera on the U1: snapshot polling only. Footage exists only while the Snapmaker app has the camera view open (the printer's touchscreen is UNVERIFIED) (about 1 frame per second)

What was seen on a real U1 is in `docs/u1-discovery-notes.md`. Anything not listed there, like Snapmaker's own
spaghetti and bed alerts, is **unverified**. Guardian does not pretend to detect those.

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

## Email alerts (optional, bring your own key)

Guardian can email you when an incident starts and again when the clip is ready. It uses
[Resend](https://resend.com). Guardian does not include a key and never pays for your email.
You make your own free Resend account and key, so any cost is between you and Resend.

1. Create a Resend account and an API key.
2. Put the key in the environment variable `GUARDIAN_RESEND_KEY` (`RESEND_API_KEY` also works).
   An easy way is a file named `.env` next to your `config.yaml`, with one line:
   `GUARDIAN_RESEND_KEY=your_key_here`. The `.env` file is ignored by git. Never put the key in
   `config.yaml` and never commit it.
3. In `config.yaml`, set `notifications.email.enabled: true` and list your address in `to_addrs`.
4. Open the dashboard and press "Send test email".

Resend's test sender (`onboarding@resend.dev`) only delivers to the email address of your own Resend
account. To send to other people, verify your own domain in Resend and change `from_addr`.
Messages are plain text. Clips are never attached, so video never leaves your network.

## Raspberry Pi

See `docs/pi-setup.md`. The install script and service file are untested on a real Pi so far.

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
