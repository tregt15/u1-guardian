# U1 Guardian

A diagnostic tool for the Snapmaker U1 3D printer. It is open source (MIT).

Guardian keeps a rolling camera buffer. When something goes wrong, it saves the video from before and after the event. The goal is to show what happened right before a print failed.

Guardian only watches. It never sends a command that changes the printer.

## Status

Early work. Nothing here has been tested on a real U1 yet. The printer has not arrived.

What exists so far (Milestone 0, no printer needed):

- Config, event bus, clock, SQLite storage
- Camera capture prototype using a stand-in test video (in progress)

Everything about the real U1 (camera format, Moonraker objects, detection alerts) is **unverified**.

## Platforms

| Platform | Tested |
|---|---|
| Windows 11 | in progress |
| Raspberry Pi 3B+ | not yet |
| Linux, macOS | not yet |

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

## License

MIT. See LICENSE.
