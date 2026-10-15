# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-10-15

### Added

- **Per-station switches** — A `switch.station_N` entity is now created for each station. Turning the switch on starts irrigation using that station's configured duration; turning it off stops immediately. Switch state reflects live active/inactive status driven by the coordinator.

- **Per-station run duration numbers** — `number.station_N_duration` (0–240 min) replaces the previous single global "Irrigation Manual Duration" number. Each station has its own independent run time. Setting a station's duration to 0 sends a 60-minute command to the device, relying on the controller's own internal timer rather than a HA-side stop command.

- **Device-side stop** — The commanded duration is now embedded in the BLE start packet for every station. The BL-IP counts down internally and closes the valve itself; HA no longer needs to send a stop command at the end of a normal run. The coordinator countdown loop is still maintained for water consumption accounting and the remaining-time sensor.

### Changed

- **Per-station start buttons removed** — The `button.sprinkle_station_N` buttons are replaced by the new per-station switches. Existing automations using those buttons should be updated to use `switch.turn_on` on the corresponding station switch.
- **"Stop sprinkle" button renamed** — The entity name changes to "Stop All" to better reflect that it stops all active stations immediately. The entity ID is unchanged.
- **Persistent storage migration** — On first load after upgrade, the stored `irrigation_manual_duration` (single value) is automatically expanded into a per-station `station_durations` list, seeding every station with the previous global value.

## [1.2.2] - 2026-09-26

### Fixed

- **Latest Status sensor stuck on "Connecting..." during start sequence**
  `ble_connection_status` was set to `"idle"` only inside the `try` block of `sprinkle_station_x_for_y_minutes` (success path), with no immediate push. The UI therefore showed "Connecting..." through the full internal retry loop (up to 3 × 3 s), then the `STATUS_REFRESH_DELAY` sleep, then the post-start BLE status read — potentially 15+ seconds of no feedback.
  Fixed by moving `ble_connection_status = "idle"` and an `async_update_listeners()` push to after the try/except block, so "Connecting..." clears as soon as the BLE command phase finishes regardless of success or error.

- **Latest Status sensor showed "Station X active" after pressing Stop**
  `start_irrigation` and `stop_irrigation` run as concurrent `asyncio` tasks. When Stop was pressed while the start command was still in its retry loop, `stop_irrigation` would complete — clearing `active_stations` and publishing "off" — but `start_irrigation`'s post-start `async_request_status_ble(station_hint=X)` would then fire, find the device active, repopulate `active_stations`, and publish "Station X active", overwriting the stop result.
  Fixed by checking `station_stop_events[station - 1].is_set()` immediately after the BLE command phase, before the `STATUS_REFRESH_DELAY` sleep and post-start verify. If Stop was requested while the start was in flight, `start_irrigation` now exits immediately with state `"Stopped"` without touching `active_stations`.

## [1.2.1] - 2026-08-20

### Fixed

- **Silent write failures no longer reported as successful irrigation starts**
  The BL-IP controller exposes both a Write Request (acknowledged) and a Write Command (unacknowledged) characteristic path. All BLE writes — command frame, commit frame (`3b 00`), and status-poll frame — were previously issued as Write Command (`response=False`). Because Write Command is fire-and-forget, a write that was queued by the adapter but rejected or ignored by the controller returned successfully to the caller, causing HA to report a successful irrigation start while no valve actually opened.
  Fixed by switching every `write_gatt_char` call to `response=True` (Write Request). The controller must now send a GATT ATT acknowledgement before the write returns; a rejected or dropped write raises an exception that propagates as `APIConnectionError`. The existing `@retry` decorator on `_write_with_auth_retry` still retries transient NACKs up to three times automatically.

## [1.2.0] - 2026-07-14

### Added

- **Backward-compatible `solem_toolkit.*` services** — All nine services previously exposed by the `solem_toolkit` integration (`sprinkle_station_x_for_y_minutes`, `stop_manual_sprinkle`, `request_status`, `turn_on`, `turn_off_permanent`, `turn_off_x_days`, `run_program_x`, `sprinkle_all_stations_for_y_minutes`, `list_characteristics`) are now registered automatically when the integration loads. Automations and scripts that call these services continue to work without modification; `solem_toolkit:` no longer needs to be declared in `configuration.yaml`.

- **`solem_toolkit.sprinkle_station_x_for_y_minutes` routes through the coordinator** — When called for a MAC address managed by a loaded coordinator, the compat service now delegates to `coordinator.start_irrigation()` instead of creating a standalone BLE API instance. This gives identical behavior to pressing the station button: pre-stop of any active station, BLE start command with verify-and-retry loop, post-start status confirmation, timer setup, water consumption accounting, and HA sensor state updates. A standalone API fallback is retained for MAC addresses not currently managed by a coordinator.

### Changed

- **`solem_toolkit` merged into `solem_bluetooth_watering_controller`** — The integration no longer depends on `solem_toolkit` as a separate HA integration. The BLE API layer (`bleak`, `bleak_retry_connector`, `habluetooth`) is now implemented directly in `api.py`. The `solem_toolkit` dependency has been removed from `manifest.json`. The `solem_toolkit:` entry in `configuration.yaml` can be removed (it is harmless if left in place since the compat services still register under that domain name).

### Removed

- **Monthly irrigation scheduling** — The schedule system (`initialize_schedule`, `async_set_schedule`, `check_and_schedule_watering`, `run_watering_cycle`, `get_next_watering_date`, `calculate_sprinkle_target_amounts`, `needs_watering_today`, `calculate_forecasted_sprinkle_today`) has been removed along with its sensors (`Next Schedule`, `Forecasted Sprinkle Today`) and configuration fields (soil moisture sensor entity, soil moisture threshold, max sprinkles per day).

- **`sprinkle_total_amount_today` tracking** — Per-session mm accumulation has been removed from the irrigation loop.

### Fixed

- **Entity IDs preserved after sensor removal** — Removing the `Next Schedule` (counter slot 9) and `Rain Time Today` (slot 12) sensors would have shifted the `unique_id` counter for all subsequent sensors, causing `Total Water Consumption`, `Total Amount of Rain Today`, and `Total Forecasted Rain Today` to map to wrong entity IDs in the HA registry. Fixed by inserting counter-skip stubs at the removed sensors' original slots, preserving the original entity IDs for all remaining sensors.

## [1.1.4] - 2026-06-25

### Fixed

- **Stations 2+ always showed "BLE error" and station status/timer were never updated**
  The Solem BLE status packet byte 9 encodes the *active station number* (0x00 = none,
  0x01 = station 1, 0x02 = station 2, …), not a plain boolean. The toolkit's
  `parse_status_packet` compared `data[9] == 0x01`, which was only `True` when station 1
  was running. For stations 2 and above the field evaluated to `False`, so
  `station_active` was never set and the coordinator's post-start status check always
  found `active_stations = []`, triggering "BLE error".
  Fixed in two places:
  (1) `solem_toolkit/api.py` — `parse_status_packet` now uses `data[9] != 0x00` so
  `station_active` is correctly `True` for any active station. This also makes the
  toolkit's own start-verification loop succeed for stations 2+ via the `station_active`
  path (previously it could only succeed via the `status.status == "active_single"` path,
  which required the device to have fully transitioned modes within the 5-second
  notification window).
  (2) `coordinator.py` — `_update_station_timers_from_status` now mirrors the toolkit's
  own verification guard: `if station_active or mode_status in ("active_single",
  "active_all"):`. This ensures the coordinator recognises a running station even when
  the toolkit's `station_active` boolean was `False` (i.e. before the toolkit fix was
  applied or on older toolkit versions), using `mode_status` as a fallback.

- **Countdown timer stuck at 0 for stations 2+**
  The Solem status packet bytes 13–14 (remaining seconds) appear to be zero for
  stations 2+ on the tested firmware, even when those stations are actively irrigating
  (bytes 13–14 are only populated for station 1). Because `_update_station_timers_from_status`
  skips setting `station_timer_end` when `remaining_seconds` is zero, the countdown timer
  stayed at 0 for the full irrigation duration even though water was flowing.
  Fixed by adding a fallback in `start_irrigation`: after BLE confirms the station is
  active, if `station_timer_end` was not populated by the status read, the coordinator
  now sets it using the commanded duration. The countdown is approximate (it starts from
  the moment the status confirmation returns, not from the exact BLE write), but is
  functionally correct and tracks the actual irrigation window.

## [1.1.3] - 2026-06-19

### Fixed

- **"BLE error" shown even when the start command reached the device**
  When `active_stations` was non-empty (e.g. populated by a prior `request_status`
  action call), `start_irrigation` invoked the full `stop_irrigation()` as a
  pre-stop. `stop_irrigation` includes a post-stop BLE status read (via the
  solem_toolkit `request_status` service), which opened a new BLE connection
  immediately before `sprinkle_station_x_for_y_minutes` opened yet another one for
  its own internal start-verification loop. The rapid back-to-back BLE connections
  caused the toolkit's notification-based status read to time out, which caused the
  toolkit to raise `APIConnectionError` even though the start write had already been
  delivered and the device had begun irrigating.
  Fixed by replacing the full `stop_irrigation()` call in the pre-stop path with a
  lightweight inline sequence: signal all HA-side stop events, reset coordinator
  state, and send a single BLE stop write — no post-stop status refresh or sensor
  update. This reduces the number of BLE connections before the start command from
  three (stop write + status read + start write) to two (stop write + start write).

- **Station status sensor and countdown timer not updated despite irrigation running**
  `start_irrigation` returned early on any `APIConnectionError` raised by
  `sprinkle_station_x_for_y_minutes`. The toolkit raises this error when its
  internal start-verification loop exhausts its attempts, which can happen if the
  BLE notification for the post-write status confirmation arrives after the 5-second
  timeout window even though the device did start irrigating. The early return
  prevented `stations[n].state`, `active_stations`, and `station_timer_end` from
  ever being set, leaving the status sensor on "BLE error" and the timer at zero
  while water was actually flowing.
  Fixed by making the `except APIConnectionError` block fall through instead of
  returning. The existing `async_request_status_ble` call immediately below is now
  the authoritative decision point: if it confirms `active_stations` is non-empty
  the start is treated as successful and the full post-start flow runs normally;
  only if the device genuinely did not become active is "BLE error" shown and the
  function aborted.

## [1.1.2] - 2026-06-18

### Added

- **"Connecting..." and "BLE error" states on the Latest Status sensor**
  The Latest Status sensor now reflects the full lifecycle of a BLE command, not just
  the confirmed active-station state. As soon as a start or stop button is pressed the
  sensor immediately switches to `Connecting...` — before the BLE round-trip completes —
  so the user gets instant visual feedback. If the BLE command fails with a connection
  error the sensor transitions to `BLE error` and stays there until the next successful
  periodic status poll resets it to `off`. Both states take priority over the
  `active_stations` logic so they always surface regardless of cached station state.
  Implemented via a new `ble_connection_status` field (`"idle"` / `"connecting"` /
  `"error"`) on `SolemCoordinator`. The value is pushed immediately using
  `async_set_updated_data(self.data)` (reusing existing coordinator data) so no
  expensive weather/sensor refresh is required for the status-change notification.
  `ble_connection_status` is also exposed as an attribute on the Latest Status sensor
  for easier debugging in the HA Developer Tools.

## [1.1.1] - 2026-06-18

### Fixed

- **Station sensors not updating for non-first stations (e.g. station 2, 3, 4)**
  When a station button was pressed while a previous station's background task was still
  running (each button press spawns a `create_task`), `_update_station_timers_from_status`
  scanned stations in order 1 → N and broke on the first one whose state was `"Sprinkling"`.
  If an earlier station was still in its sleep loop with `state = "Sprinkling"`, the active
  station, remaining-time countdown, and latest-status sensor were attributed to that earlier
  station instead of the one just started. Sensors for the intended station were never updated.
  Fixed by passing a `station_hint` from `start_irrigation` through `async_request_status_ble`
  to `_update_station_timers_from_status`, bypassing the linear scan when the caller already
  knows which station was just commanded to start.

- **Race condition: shared stop event could be cleared before a running station saw it**
  `start_irrigation` called `irrigation_stop_event.clear()` unconditionally at entry. If a
  new station was started before the previous station's `await sleep(1)` tick had a chance
  to check `is_set()`, the clear would race with the set issued by `stop_irrigation`, leaving
  the previous station's loop running undetected — silently over-counting water consumption
  and `sprinkle_total_amount_today` for the wrong station.
  Fixed by replacing the single shared `irrigation_stop_event: asyncio.Event` with a
  per-station list `station_stop_events: list[asyncio.Event]` (one event per station).
  Each station's loop checks and clears only its own event; `stop_irrigation` sets all of
  them. The list is also re-created in `update_config` when the station count changes.

- **Starting a new station while another was active left both tasks running concurrently**
  Because button presses use `asyncio.create_task`, a second press would spawn a second
  `start_irrigation` coroutine without stopping the first. Both tasks would race to update
  `active_stations`, `station_timer_end`, and sensor state, producing inconsistent readings
  and double water-consumption accounting.
  Fixed by adding an explicit guard at the top of `start_irrigation`: if `active_stations`
  is non-empty, `stop_irrigation()` is awaited before the new session begins. This sends the
  BLE stop command, signals all running tasks via their per-station stop events, and resets
  coordinator state cleanly before the new station's command and status read are issued.

### Changed

- `async_request_status_ble` now accepts an optional `station_hint: int | None` parameter
  that is forwarded to `_update_station_timers_from_status`. Callers that do not pass a hint
  (periodic polling, stop-after events) continue to use the existing scan-based fallback.
- `_update_station_timers_from_status` now accepts an optional `station_hint: int | None`
  parameter. When provided the hint is used directly to set `active_stations` and
  `station_timer_end`, avoiding false matches against stale `"Sprinkling"` state on other
  stations. The linear scan is retained as a fallback for hint-less calls.

## [1.1.0] - (initial tracked release)

- First version captured in this changelog. See repository history for earlier changes.
