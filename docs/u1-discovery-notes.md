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

## First real print (2026-11-08 to 11-09): a mid-print failure was recorded

Recorded with `tools/ws_recorder.py --camera-watch` for 3 hours (57,834 lines, kept local in `recordings/`).
Print file: `sheep10_test.gcode`, 501 layers, started from the printer's screen at 22:12:08 UTC.

Verified timeline on a real U1 (UTC):

- 22:12:08 `print_stats.state` goes `printing`, `machine_state_manager.main_state` = 1. Gcode response "Changed main state to PRINTING".
- 22:12:41 to 22:16:09 `machine_state_manager.action_code` changes 136, 132, 133, 0, ... (meanings UNVERIFIED, likely heating, filament load and tool prep).
  `filament_motion_sensor e0_filament` and `e1_filament` go `filament_detected: true` as tools load.
- 22:16:09 gcode response `!! extruder[2]: state: load_feeding, error: wheel_speed!raw msg:logic error!`
- 22:16:10 `notify_exception_notification` `{id: 525, index: 2, code: 32, level: 2}` and `print_stats.state` goes `paused`
  with `print_stats.exception` filled in. The print paused itself 4 minutes in. It was still paused 3 hours later.
  Guardian could trigger on `print_stats.state -> paused` plus `exception`, or on `notify_exception_notification`.

Camera, verified:

- `notify_camera_status_change` `{monitor_domain: "lan", monitoring: true/false}` fires when monitoring starts and stops.
- `monitor.jpg` changed on every poll while monitoring was true. When it went false (22:18:50) it never changed again for the
  next 2 h 50 min, even with a print paused with an error. So the snapshot only exists while a viewer asks for it.
  Guardian cannot get footage from this path on its own. UNVERIFIED: whether anything else (touchscreen, a LAN request)
  can turn monitoring on without the app.
- The detector wrote 6 frame pairs to `/userdata/.tmp_detect_result/{org,det}` in the first minute of the print (22:12:37 to
  22:13:01, log shows "first" and "last"). That looks like a start-of-print check. No `noodle` detection frames were seen,
  but the print paused before the main layers.


## How the Snapmaker app and Snapmaker Orca get the camera (2026-11-09)

Verified by reading logs only (printer `unisrv.log`, Moonraker log, and the log of Orca's helper program
`snapmaker_control` on the PC). Nothing was sent to the printer for this.

- Viewers on the LAN (the phone app, Orca's embedded browser) read the picture with
  `GET /server/files/camera/monitor.jpg?_nocache=...` on port 80. That is the same file Guardian polls.
- What turns the camera on is a request `camera.start_monitor` that reaches `unisrv` through the printer's cloud
  connection (an MQTT link to Snapmaker's cloud). The printer log says `domain is wan`, and the viewer renews it about every
  5 minutes. `camera.stop_monitor` ends it. The printer announces changes with `notify_camera_status_change`.
- Orca's helper `snapmaker_control` listens only on this PC's loopback port and sends every request through the cloud
  gateway ("wan"). Its log has no LAN route at all. So Orca is NOT a separate local path to the camera.
- While monitoring is on through this route, the printer uploads each frame to Snapmaker's cloud (seen earlier in
  `unisrv.log`: "Upload ... monitor.enc"). Guardian itself never uploads anything.
- UNVERIFIED: whether any local-only way to start monitoring exists. The printer's own web page (Fluidd) has no code
  for it. Guardian does not send `camera.start_monitor` and cannot start the camera on its own.
- The printer's web server access log is empty, so which client asked for what could only be read from Moonraker's
  log and the helper's log.


## The camera session lapses on its own (2026-11-09)

Seen in `unisrv.log` and in Guardian's frame times, with the camera view open on a viewer:

- The monitoring session is not continuous. At 06:18:32 UTC the pictures stopped. At 06:19:06 a new
  `camera.start_monitor` arrived and the log said "Current monitor state: lan is false, wan is false", so the session had
  lapsed before the renewal. That is a gap of about 30 seconds in the picture feed, even though a viewer was open.
- Guardian reported this honestly: the incident that spanned the gap was `partial` ("Post-roll is 1.3s short").
  Expect this kind of gap about every 5 to 10 minutes while a viewer keeps the camera on. UNVERIFIED how regular it is.
- The printer's own AI asks for a picture about every 45 seconds during a print (`camera.detect_capture` in the log).
  The `print_check` (noodle) model is loaded inside `unisrv`. This is separate from the viewer's snapshot file.


## Second print (2026-11-09): a full print with 399 tool changes

Recorded with `tools/ws_recorder.py` for 8 hours (151,736 lines, kept local in `recordings/print2.jsonl`). The recorder
joined about 1 hour in (layer 2 of 501). Same file `sheep10_test.gcode`. The print ended normally at 07:23:02 UTC
(`print_stats.state` -> `complete`, `notify_history_changed` action `finished`). No errors, no exceptions.

Tool changes, verified on a real U1:

- 399 tool changes. Pairs: T0->T1 152, T1->T0 149, T1->T2 49, T2->T1 47, T2->T0 2. Every one finished.
- Each change took 3 to 5 s (median 4 s), measured from the gcode line to `toolhead.extruder` changing.
  Times are whole seconds from the recorder, so they are only good to about 1 s.
- The order of messages for a change from tool A to tool B:
  1. `notify_gcode_response` `// A -> B`, then `// park A !!!`
  2. `B.activating_move = true`
  3. `A.state = "PARKED"`, `A.park_pin = true`, `A.active_pin = false`, `A.grab_valid_pin = false`
  4. `// pick B !!!`
  5. `B.state = "UNKNOWN"`, `B.active_pin = true`, `B.state = "ACTIVATE"`, `B.park_pin = false`, `B.grab_valid_pin = true`
  6. `// Activating extruder B`, then `toolhead.extruder = "B"`, `B.activating_move = false`, `B.switch_count` + 1
  Object names: tool 0 is `extruder`, tools 1 to 3 are `extruder1` to `extruder3`.
- The order inside steps 3 and 5 changes from one change to the next (6 different orders seen). Only the end state is
  the same every time. So Guardian checks the end state, not the order.
- Once, the first time T2 was used (03:10:52), `B.active_pin` went true, false, true and `B.state` went
  `UNKNOWN`, `PARKED`, `ACTIVATE` within one second. The change still finished normally. Right after it the printer set
  action code `PRINT_PREEXTRUDING` (`machine_state_manager.action_code` 134) for 14 s.
- In print 1 the next change's `// B -> C` line sometimes arrived before `toolhead.extruder = "B"` from the change before.
  So a change counts as done on `// Activating extruder B` or `toolhead.extruder = "B"`, whichever comes first.

Action codes now verified by name (from gcode responses "Set action code ..."): 0 `IDLE`, 132 `PRINT_SWITCH_CHECKING`,
133 `PRINT_AUTO_FEEDING`, 134 `PRINT_PREEXTRUDING`, 136 `PRINT_BED_DETECTING`.

Print 1 failure, looked at again: it was NOT a failed tool change. The change to T2 finished at 22:15:57. Then the
filament load on T2 failed 12 s later. The first sign is `filament_feed right.extruder2.channel_error = "wheel_speed"`
(22:16:09), then `channel_state = "load_fail"`, then the `!!` gcode line, then the exception (22:16:10). The
`filament_feed` object goes through `load_picking`, `load_feeding`, `load_heating`, `load_extruding`, `load_flushing`,
`load_finish` on a good load.

Camera, verified:

- `notify_camera_monitoring_to_stop` `{remain: N}` counts down about once a second from about 50 to 0, then
  `notify_camera_status_change` `monitoring: false` follows. This is a 50 s warning before the camera stops. Seen in
  both prints.
- In print 2 the camera was off from 02:01:43 to 05:45:05, then on until the end. The short lapse at 06:18 seen in
  `unisrv.log` did NOT show up as a `notify_camera_status_change` on the WebSocket. So the WebSocket does not report
  every gap. Guardian's own frame times are the only honest record.
- No detection (noodle, spaghetti) messages were seen on the WebSocket in either print.


## Starting the camera over the LAN (2026-11-09)

Source of the idea: SimplyPrint's U1 webcam guide ("stock firmware + external keepalive") and the community project
Dr-Hack-N-Sniff/Snapmaker-U1-Stock-Camera-Fluidd. Owner approved Guardian sending this one request.

Verified on the real U1 (one test, sent from the PC over the Moonraker WebSocket on port 80, no token needed):

- Request: `{"jsonrpc": "2.0", "id": 1, "method": "camera.start_monitor", "params": {"domain": "lan", "interval": 0}}`
- About 2 s later: `notify_camera_status_change` `{monitor_domain: "lan", monitoring: true}`. No JSON-RPC reply to the
  request itself came back on the WebSocket.
- `monitor.jpg` changed 56 times in the next 60 s. `unisrv.log`: "Start camera monitor, domain is lan, interval is 0",
  "New monitor state: lan is true, wan is false", then about 480 ms per picture (about 2 a second). The app's WAN
  sessions took about 980 ms per picture.
- `unisrv.log` showed no `Upload ... monitor.enc` line for the LAN session. Those lines appear only when a WAN (app)
  session starts. The per-picture log line says "capturing image and uploading" in both modes, so "nothing leaves the
  printer" is based on the log only. UNVERIFIED beyond that.
- How long a LAN session lasts without renewal: still running after 5 minutes. Guardian renews on the countdown or
  when pictures stop, so the exact limit does not matter much. Not measured yet.

Other findings over SSH (read-only commands):

- The 06:18 picture gap in print 2 was a crash of `unisrv`, not a normal session end. Its log stops at 06:18:24 and a
  core dump exists: `/userdata/.coredump/unisrv_S0_PID1218_20261109T061836Z.core`. No WebSocket message was sent.
- Logs live in `/oem/printer_data/logs/` (`unisrv.log`, `moonraker.log`, `klippy.log`, `gui.log`, `syslog`).
- `unisrv.log` says a firmware update is downloaded and waiting ("has new: true, has downloaded: true").

The printer's own build plate check (read from `/home/lava/klipper/klippy/extras/defect_detection.py`):

- Settings come from `defect_detection` (on this printer: `main_enable` true, `clean_bed` enabled, sensitivity low,
  `noodle` enabled, `residue` and `nozzle` off).
- At print start (action code 136 `PRINT_BED_DETECTING`) Klipper asks `unisrv` for `camera.detect_capture` over the
  printer's own MQTT (`camera/request`, `camera/response`) and runs the `bed_check` model. This takes its own picture,
  so it works with the camera view off.
- On a find it raises exception id 532, level 2, with code 1 "detected dirty bed", 2 "detected noodle",
  3 "detected residue", 4 "detected dirty nozzle", and then pauses the print (`PAUSE`).
- Read from source code only. A real 532 has not been seen yet.


## Printer logs over HTTP and the firmware version (2026-11-09)

- Moonraker's file roots include `logs` (`/oem/printer_data/logs`, read only). `GET /server/files/logs/klippy.log`
  works with HTTP range requests (`Range: bytes=-N`), so Guardian needs no SSH for logs.
- klippy.log lines start with the UTC time of day only (`22:16:10.905:`). moonraker.log and unisrv.log have full dates.
  The printer clock is UTC and was about 1 s behind the PC.
- klippy.log only logs `Stats` while printing. Each Stats line has all heater temperatures and targets.
- Print 1 failure, from klippy.log: during `[feed_loading] phase2` the filament wheel counters (`cnt_a_1`, `cnt_a_2`)
  stopped at 419 while the feed motor counter kept going, then "hanging neutral", "auto load error: logic error!",
  the `!!` line, `Pausing...` and "Raising exception: id:525". So the filament was not moving through the feeder.
- `GET /printer/info` gives `software_version: 1.4.1.6_20260608141446`. Guardian uses it to notice firmware updates.



## Firmware 2.1.0 (installed 2026-11-10)

The printer updated from 1.4.1.6_20260608141446 to 2.1.0.157_20261028045538 (Moonraker 2.1.0). Self-test: all
objects Guardian reads are still there. Seen so far, first print on 2.1.0:

- New line before a print: `Pausing toolhead to calculate extruder1 phase offset`, then the gcode response
  `// extruder1 -> extruder1` and `Extruder extruder1 already active`. Nothing is parked or picked. Guardian took it
  as a tool change, timed out and auto paused a good print. Fixed: `A -> A` is ignored.
- Tool changes still use the same lines (`A -> B`, `park A !!!`, `pick B !!!`, `Activating extruder B`). They looked
  faster in klippy.log (about 2.4 s from park to activating).
- klippy.log moved to `klippylogs/klippy.log` (the old `klippy.log` stays but is no longer written). Lines now start
  with month and day: `11-10 07:28:29.688:`.
- unisrv picture timing line changed tag (`MON:387`) and went from about 480 ms to about 30 ms per picture.
  Camera pictures are larger (about 184 KB).
- Moonraker logs Guardian's camera request as `Repeater camera.start_monitor`.
- Guardian's pause during `M109` (wait for nozzle heat) took effect about 20 s later, when the wait finished.
  So a pause is not instant while the printer waits for a heater.
