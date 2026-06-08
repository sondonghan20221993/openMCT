#!/usr/bin/env python3
"""WebSocket uplink bridge: receives JSON commands from openMCT, sends LoRa UP frames via serial."""
import argparse
import asyncio
import json
import struct
import time

try:
    import serial
except ImportError:
    serial = None

try:
    import websockets
    from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError
except ImportError:
    raise SystemExit("websockets not installed: pip install websockets")

WS_HOST = "127.0.0.1"
WS_PORT = 8766

_UPLINK_VERSION = 1
_MAX_PAYLOAD    = 196

_CLASS_MAP = {
    "CONFIG":       1,
    "ROUTE_UPDATE": 2,
    "VIEWPOINT":    3,
    "RECOVERY":     4,
    "MODE":         5,
    "DIAGNOSTIC":   6,
}

_seq = 0


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _build_lora_frame(seq: int, cmd_class: int, payload: bytes,
                      version: int = _UPLINK_VERSION, flags: int = 0) -> str:
    if len(payload) > _MAX_PAYLOAD:
        raise ValueError(f"payload {len(payload)} > {_MAX_PAYLOAD}")
    canon = f"UP,{version},{cmd_class},{seq},{flags},{payload.hex().upper()}"
    crc   = _crc16_ccitt(canon.encode("ascii"))
    return f"{canon},{crc:04X}"


def _build_route_payload(route_type: int, route_version: int, waypoints: list) -> bytes:
    buf = struct.pack("<BBBB", route_type, route_version, len(waypoints), 0)
    for wp in waypoints:
        buf += struct.pack("<fff", float(wp["x"]), float(wp["y"]), float(wp["z"]))
    return buf


def _write_serial(frame: str, port: str, baud: int) -> None:
    if serial is None:
        raise RuntimeError("pyserial not installed: pip install pyserial")
    with serial.Serial(port, baud, timeout=2.0) as s:
        time.sleep(0.05)
        s.write((frame + "\n").encode("ascii"))
        s.flush()


async def _handle_command(msg: dict, serial_port, baud: int) -> dict:
    cls_name = msg.get("class", "")
    if cls_name not in _CLASS_MAP:
        return {"ok": False, "error": f"unknown class: {cls_name!r}"}

    seq = _next_seq()

    try:
        if cls_name == "ROUTE_UPDATE":
            route_type = int(msg.get("route_type", 1))
            route_ver  = int(msg.get("route_version", 1))
            wps        = msg.get("waypoints", [])
            if not wps:
                return {"ok": False, "error": "waypoints required", "seq": seq}
            payload = _build_route_payload(route_type, route_ver, wps)
        else:
            hex_str = msg.get("payload_hex", "").replace(" ", "")
            payload = bytes.fromhex(hex_str) if hex_str else b""

        frame = _build_lora_frame(seq, _CLASS_MAP[cls_name], payload)

        if serial_port:
            await asyncio.to_thread(_write_serial, frame, serial_port, baud)

        return {"ok": True, "seq": seq, "frame": frame, "class": cls_name}

    except Exception as exc:
        return {"ok": False, "error": str(exc), "seq": seq}


def _make_handler(serial_port, baud: int):
    async def handler(websocket):
        addr = websocket.remote_address
        print(f"[WS] connected: {addr}")
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as exc:
                    await websocket.send(json.dumps({"ok": False, "error": f"JSON parse error: {exc}"}))
                    continue

                result = await _handle_command(msg, serial_port, baud)
                print(f"[CMD] {result}")
                await websocket.send(json.dumps(result))
        except (ConnectionClosedOK, ConnectionClosedError):
            pass
        finally:
            print(f"[WS] disconnected: {addr}")

    return handler


async def _run(serial_port, baud: int) -> None:
    handler = _make_handler(serial_port, baud)
    async with websockets.serve(handler, WS_HOST, WS_PORT, ping_interval=None):
        transport = f"{serial_port} @ {baud}" if serial_port else "dry-run (no serial)"
        print(f"[WS] ws://{WS_HOST}:{WS_PORT}  transport={transport}")
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(description="Uplink WebSocket server (port 8766)")
    parser.add_argument("--serial-port", default=None, metavar="PORT",
                        help="LoRa serial port path, e.g. /dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, default=57600)
    args = parser.parse_args()
    asyncio.run(_run(args.serial_port, args.baudrate))


if __name__ == "__main__":
    main()
