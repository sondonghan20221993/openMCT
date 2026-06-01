import asyncio
import json
import time
import serial
import websockets

SERIAL_PORT = "COM7"
BAUD = 57600

WS_HOST = "127.0.0.1"
WS_PORT = 8765

clients = set()


def parse_int(value: str):
    try:
        return int(value)
    except ValueError:
        return None


def parse_float(value: str):
    try:
        return float(value)
    except ValueError:
        return None


def parse_fc_line(line: str):
    line = line.strip()

    parts = line.split(",")
    if not parts:
        return None

    timestamp = int(time.time() * 1000)
    source = parts[0]

    # FC + seq + boot_ms + roll,pitch,yaw,x,y,z,vx,vy,vz
    # Optional trailing fields: lat,lon,alt,sats,fix,flags
    if source == "FC" and len(parts) >= 12:
        data = {
            "timestamp": timestamp,
            "source": source,
            "seq": parse_int(parts[1]),
            "boot_ms": parse_int(parts[2]),
            "roll": parse_float(parts[3]),
            "pitch": parse_float(parts[4]),
            "yaw": parse_float(parts[5]),
            "x": parse_float(parts[6]),
            "y": parse_float(parts[7]),
            "z": parse_float(parts[8]),
            "vx": parse_float(parts[9]),
            "vy": parse_float(parts[10]),
            "vz": parse_float(parts[11]),
        }

        optional_fields = [
            ("lat", parse_float),
            ("lon", parse_float),
            ("alt", parse_float),
            ("sats", parse_int),
            ("fix", parse_int),
            ("flags", parse_int)
        ]

        for index, (key, parser) in enumerate(optional_fields, start=12):
            if len(parts) > index:
                data[key] = parser(parts[index])

        if any(value is None for value in data.values()):
            return None

        return data

    # GPS + seq + boot_ms + lat,lon,alt,sats[,fix]
    if source == "GPS" and len(parts) >= 7:
        data = {
            "timestamp": timestamp,
            "source": source,
            "seq": parse_int(parts[1]),
            "boot_ms": parse_int(parts[2]),
            "lat": parse_float(parts[3]),
            "lon": parse_float(parts[4]),
            "alt": parse_float(parts[5]),
            "sats": parse_int(parts[6])
        }

        if len(parts) > 7:
            data["fix"] = parse_int(parts[7])

        if any(value is None for value in data.values()):
            return None

        return data

    # EKF + seq + boot_ms + flags
    if source == "EKF" and len(parts) >= 4:
        data = {
            "timestamp": timestamp,
            "source": source,
            "seq": parse_int(parts[1]),
            "boot_ms": parse_int(parts[2]),
            "flags": parse_int(parts[3])
        }

        if any(value is None for value in data.values()):
            return None

        return data

    return None


async def ws_handler(websocket):
    clients.add(websocket)
    print("[WS] client connected")

    try:
        await websocket.wait_closed()
    finally:
        clients.discard(websocket)
        print("[WS] client disconnected")


async def broadcast(msg: str):
    if not clients:
        print("[WS] no clients, skipping broadcast")
        return

    # 핵심: set을 직접 순회하지 않고 복사본을 순회
    current_clients = list(clients)
    dead_clients = []
    print(f"[WS] broadcasting to {len(current_clients)} client(s)")

    for client in current_clients:
        try:
            await client.send(msg)
            print("[WS] sent message")
        except Exception:
            print("[WS] send failed, marking client dead")
            dead_clients.append(client)

    for client in dead_clients:
        clients.discard(client)


async def serial_reader():
    print(f"[SERIAL] opening {SERIAL_PORT} @ {BAUD}")
    ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)

    while True:
        raw = await asyncio.to_thread(ser.readline)

        if not raw:
            await asyncio.sleep(0.01)
            continue

        line = raw.decode(errors="ignore").strip()
        data = parse_fc_line(line)

        if data is None:
            print("[BAD]", line)
            continue

        msg = json.dumps(data)
        print("[OK]", msg)
        print(f"[WS] broadcast attempt, clients={len(clients)}")

        await broadcast(msg)
        print("[WS] broadcast complete")


async def main():
    server = await websockets.serve(
        ws_handler,
        WS_HOST,
        WS_PORT,
        ping_interval=None
    )

    print(f"[WS] ws://{WS_HOST}:{WS_PORT}")

    try:
        await serial_reader()
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
