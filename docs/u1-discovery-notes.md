# U1 discovery notes (partial, Part A only)

Date: 2026-11-08. Read-only HTTP GET requests only. Nothing was changed on the printer.
Raw dumps stay local in `docs/discovery/` (git-ignored, they hold the serial number and MAC).

## Verified (seen on one real U1)

- Moonraker answers on port 80 (through nginx) and on port 7125. No login needed.
- Moonraker 1.4.1, API 1.4.0. Firmware and software version 1.4.1 (build 1.4.1.6_20260608141446).
- Klippy state: ready. Host: aarch64, 4 cores, about 985 MB RAM, Buildroot Linux.
- `/server/webcams/list` returns an empty list.
- The `octoprint_compat` section of `/server/config` lists `stream_url: /webcam/?action=stream`.
- Requesting `/webcam/?action=stream` returned `502 Bad Gateway` from nginx. The camera service was not answering at that moment. Cause not known yet.
- `/server/files/directory?path=camera` is an empty read-only root.

## Objects seen (193 total) that matter to Guardian

- `defect_detection` (has sections `noodle`, `clean_bed`, `residue`, `nozzle`, each with `enable` and `sensitivity`)
- `filament_motion_sensor e0..e3_filament`, `filament_entangle_detect e0..e3_filament`
- `machine_state_manager` (`main_state`, `action_code`, meanings unknown)
- `print_stats`, `print_task_config`, `webhooks`, `toolhead`, `extruder`, `extruder1..3` (`extruder` has `switch_count`, `state`)
- `timelapse`, `exception_manager`, `filament_feed left/right`

## Still UNVERIFIED

- Whether `defect_detection` fires any event we can see when it finds a problem. It currently only shows settings.
- What the `machine_state_manager` numbers mean.
- Camera format, resolution, frame rate, and how to get the stream.
- Which events appear on the WebSocket during prints and tool changes.
