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

## Part B: SSH (read-only), 2026-11-08

Only read commands were used (ps, ls, cat, grep, find, ss, df, free). Nothing was written or restarted.

Verified:

- Firmware: Buildroot 2024.02, Linux 6.1.99 aarch64 (Rockchip RK3562), 4 cores, 961 MB RAM, no swap. Idle load about 0.4 to 0.7.
- Storage: `/` is a small overlay, `/userdata` is 27 GB (about 26 GB free), `/tmp` is tmpfs (about 480 MB).
- Services: Klipper and Moonraker (init scripts S60/S61), nginx, mosquitto (MQTT), dropbear (SSH), `unisrv`, `gui` (the touchscreen), `rkaiq_3A_server` (camera ISP).
- nginx proxies `/webcam/` to `127.0.0.1:8080` (and 8081 to 8083 for webcam2 to 4). Nothing listens there and no mjpg-streamer exists on this firmware. That is why `/webcam/?action=stream` gives 502. The `octoprint_compat` stream_url is a dead route.
- The camera is owned by Snapmaker's `unisrv` service. It is not a standard streamer.
- While the Snapmaker app camera view is on, `unisrv` captures a JPEG about every 1.1 s into `/tmp/.monitor.jpg`. Moonraker serves it as `GET /server/files/camera/monitor.jpg`. Four fetches about one second apart gave four different 1920x1080 MJPEG-codec JPEGs (about 100 KB each). It shows the real camera view.
- The unisrv log shows the app camera mode uploading each frame to Snapmaker's cloud ("domain is wan", "Upload ..._monitor.enc"). It also shows a LAN flag. What turns on LAN-only monitoring is UNVERIFIED.
- Detection models are loaded inside `unisrv` (`noodle`, `noodle_verify`, `bed_check`, `print_check`), with results written under `/userdata/.tmp_detect_result/`. How a detection reaches Moonraker or MQTT is UNVERIFIED.

Not verified yet:

- Whether the snapshot file updates when the app camera view is OFF (the 502 earlier and the `notify_camera_monitoring_to_stop` log lines suggest it does not).
- Whether the frame is a true 1 fps or can go faster. A real video stream was not found.
- Whether detections appear on the Moonraker WebSocket or MQTT during a print.

## First print attempt (2026-11-08): print refused. Real events seen on the WebSocket

Recorded by `tools/ws_recorder.py` (file stays local in `recordings/`). The print was sent from the Snapmaker app
(`server.files.start_cloud_print`). The printer refused to start it, twice (two files).

Verified Moonraker WebSocket notifications, seen on a real U1:

- `notify_gcode_response`: `"!! [print_task_config] nozzle diameter mismatch:f_0 != e_0.4"`
- `notify_exception_notification` (twice per failure): `{id: 531, code: 14, level: 3, message: ...}` then
  `{id: 531, code: 7, level: 1, message: "Failed to start print: ..."}`. The user saw the screen error `0001 0531 0000 0007`,
  which matches id 531 and code 7.
- `notify_file_pull_progress` with `state: "downloading"` (percent) and then `state: "error"`.
- `notify_filelist_changed` for the downloaded gcode and thumbnails.
- `notify_camera_status_change` with `{"monitor_domain": "lan", "monitoring": true}` when the camera monitoring went on.
  This is a Moonraker notification Guardian can listen for.

Cause (from Klipper's `print_task_config.py` on the printer): the print job's nozzle diameter was 0 (never set),
the installed nozzle is 0.4, so the check fails. Likely the sliced file or the app did not provide a nozzle diameter.
Exact reason is UNVERIFIED. Nothing was changed on the printer. `print_stats.state` stayed `standby`.

This is a good example of an event Guardian should record, but it is a pre-print failure, not a mid-print one.
