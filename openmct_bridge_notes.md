# Open MCT Bridge Notes

## What Already Exists

- `mavlink_bridge_app` already converts raw MAVLink into structured cFS telemetry:
  - `FC_ATTITUDE_STATE_MID`
  - `FC_EKF_LOCAL_STATE_MID`
  - `FC_GPS_RAW_STATE_MID`
  - `FC_EKF_STATUS_MID`
- Those message payloads already contain Open MCT-friendly fields:
  - attitude: `RollRad`, `PitchRad`, `YawRad`
  - local state: `X_m`, `Y_m`, `Z_m`, `Vx_mps`, `Vy_mps`, `Vz_mps`
  - GPS raw: `LatE7`, `LonE7`, `AltMm`, `SatellitesVisible`
  - EKF status: `Flags`

## What Does Not Exist Yet

- No Open MCT app/plugin code in this repository.
- No existing HTTP/WebSocket bridge that Open MCT can query.
- No PC-side parser for the final `[OK] ...` receiver log inside this repository.

## Shortest Path

The shortest path in the current structure is:

1. Keep the current PC receiver exactly as-is.
2. Make that receiver write its decoded `[OK] ...` lines to a log file.
3. Run `openmct_telemetry_server.py` on the PC to tail that file.
4. Point an Open MCT telemetry provider at:
   - `GET /latest`
   - `GET /history?series=...`
   - `GET /schema`
   - `GET /events`

This avoids touching Raspberry Pi cFS bring-up and avoids decoding LoRa packets twice inside cFS.

## Expected Input Log Style

The bridge is intentionally simple. It expects lines that contain `[OK]` and `key=value` pairs, for example:

```text
[OK] seq=12 boot_ms=123456 roll=0.11 pitch=-0.04 yaw=1.57 z=-3.25
[OK] seq=13 boot_ms=123556 lat=36.3507 lon=127.3802 alt=105.2 sats=12
```

If your PC receiver already prints values in this style, no receiver code change is needed beyond logging to file.

## Open MCT Provider Connection Point

In an Open MCT app, the usual connection points are:

1. `openmct.types.addType(...)`
   - define `fc.attitude.roll`, `fc.gps.lat`, `fc.ekf.z` style telemetry objects
2. `openmct.objects.addRoot(...)`
   - add a root folder like `cFS`
3. `openmct.objects.addProvider(...)`
   - resolve object metadata for those telemetry items
4. `openmct.telemetry.addProvider(...)`
   - fetch latest/history from this bridge server

The key runtime hookup is `openmct.telemetry.addProvider(...)`.

## Recommended Series Names

Use these normalized names end-to-end if possible:

- `roll`
- `pitch`
- `yaw`
- `x`
- `y`
- `z`
- `vx`
- `vy`
- `vz`
- `lat`
- `lon`
- `alt`
- `sats`
- `fix`
- `flags`

This keeps the PC log, bridge server, and Open MCT object model aligned.
