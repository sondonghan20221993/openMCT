"""
LoRa bridge — single process owning the serial port.

  WebSocket ws://127.0.0.1:8765  — downlink telemetry (FC / SH packets)
  HTTP      http://127.0.0.1:8082 — uplink commands (config / recovery)
"""
import argparse
import asyncio
import csv
import json
import os
import struct
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import serial
from serial.tools import list_ports
import websockets

# ---------------------------------------------------------------------------
# WebSocket state
# ---------------------------------------------------------------------------
WS_HOST = "127.0.0.1"
WS_PORT = 8765

clients: set = set()

# Link quality
# 주의: FC/SH는 lora_fc_downlink의 단일 LoRaTxCount를 번갈아 공유한다(통합 LoRa 프레임 seq).
# 따라서 손실률은 source별로 쪼개면 안 되고, 통합 seq gap으로 계산해야 한다.
# (source별로 나누면 교대 송신 때문에 gap=2가 되어 항상 ~50% 손실로 오판됨)
_heartbeat = 0
_last_seq = None
_total_expected = 0
_total_received = 0
_packet_loss = 0.0

# ---------------------------------------------------------------------------
# CSV 저장 설정
# ---------------------------------------------------------------------------
CSV_DIR = "telemetry_logs"
_csv_file = None
_csv_writer = None
_csv_lock = threading.Lock()
_csv_fields = [
    'timestamp', 'source',
    'roll', 'pitch', 'yaw',
    'x', 'y', 'z', 'vx', 'vy', 'vz',
    'lat', 'lon', 'alt', 'fix',
    'seq', 'boot_ms', 'health_state', 'fault_code',
    'heartbeat', 'packet_loss'
]

def _init_csv():
    """CSV 파일 초기화 (매일 새 파일)"""
    global _csv_file, _csv_writer

    try:
        os.makedirs(CSV_DIR, exist_ok=True)

        now = datetime.now()
        filename = os.path.join(CSV_DIR, f"telemetry_{now.strftime('%Y%m%d_%H%M%S')}.csv")

        _csv_file = open(filename, 'w', newline='')
        _csv_writer = csv.DictWriter(_csv_file, fieldnames=_csv_fields)
        _csv_writer.writeheader()
        _csv_file.flush()

        print(f"[CSV] Created {filename}")
    except Exception as e:
        print(f"[CSV] Error initializing CSV: {e}")

def _save_telemetry_to_csv(data: dict) -> None:
    """텔레메트리 데이터를 CSV에 저장"""
    if not _csv_writer:
        return

    try:
        with _csv_lock:
            row = {field: '' for field in _csv_fields}
            row['timestamp'] = datetime.now().isoformat()
            row.update(data)
            _csv_writer.writerow(row)
            _csv_file.flush()
    except Exception as e:
        print(f"[CSV] Error saving to CSV: {e}")

# ---------------------------------------------------------------------------
# Shared serial port
# ---------------------------------------------------------------------------
_ser: serial.Serial | None = None
_serial_write_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Uplink protocol (mirrors uplink_command_server.py)
# ---------------------------------------------------------------------------
UPLINK_PROTOCOL_VERSION = 1
UPLINK_CLASS_CONFIG       = 1
UPLINK_CLASS_ROUTE_UPDATE = 2
UPLINK_CLASS_RECOVERY     = 4

SCOPE_CFS_CORE_APP   = 1
SCOPE_MAVLINK_BRIDGE = 2

# route update (spec §18.4.6.2) — 검증은 uplink_app이 권위 수행, 여기선 형식만 점검
ROUTE_VERSION             = 1
MAX_ROUTE_WAYPOINTS       = 16
ROUTE_TYPES = {
    "mission":           1,   # mission_extension
    "mission_extension": 1,
    "landing":           2,
}

CONFIG_VERSION    = 1
VALUE_TYPE_UINT32 = 0

CFS_CORE_PARAMS = {
    "attitude_timeout_ms": 0,
    "local_timeout_ms":    1,
    "gps_timeout_ms":      2,
    "ekf_timeout_ms":      3,
    "bridge_timeout_ms":   4,
    "publish_period_ms":   5,
}

MAVLINK_BRIDGE_PARAMS = {
    "attitude_interval_us":        0,
    "local_position_interval_us":  1,
    "global_position_interval_us": 2,
    "gps_raw_interval_us":         3,
    "ekf_status_interval_us":      4,
    "reconnect_interval_ms":       5,
    "heartbeat_interval_ms":       6,
}


class _SeqCounter:
    def __init__(self):
        self._v = 1
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            v = self._v
            self._v = (self._v % 0xFFFF) + 1
            return v


_seq_counter = _SeqCounter()


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _build_lora_frame(seq: int, payload: bytes, cmd_class: int, flags: int = 0) -> str:
    payload_hex = payload.hex().upper()
    canonical = f"UP,{UPLINK_PROTOCOL_VERSION},{cmd_class},{seq},{flags},{payload_hex}"
    crc = _crc16(canonical.encode("ascii"))
    return f"{canonical},{crc:04X}"


def _config_checksum(scope, version, param_id, value_type, value_len, value_bytes) -> int:
    s = (scope + version
         + (param_id & 0xFF) + ((param_id >> 8) & 0xFF)
         + value_type + value_len + sum(value_bytes))
    return s & 0xFFFF


def _build_config_payload(scope: int, param_id: int, value: int) -> bytes:
    value_bytes = struct.pack("<I", value)
    checksum = _config_checksum(scope, CONFIG_VERSION, param_id,
                                VALUE_TYPE_UINT32, len(value_bytes), value_bytes)
    hdr = struct.pack("<BBHBBH", scope, CONFIG_VERSION, param_id,
                      VALUE_TYPE_UINT32, len(value_bytes), checksum)
    return hdr + value_bytes


def _build_route_payload(route_type: int, route_version: int, waypoints: list) -> bytes:
    # layout: route_type:u8, route_version:u8, waypoint_count:u8, reserved:u8, then x,y,z f32 LE per wp
    payload = struct.pack("<BBBB", route_type, route_version, len(waypoints), 0)
    for x, y, z in waypoints:
        payload += struct.pack("<fff", x, y, z)
    return payload


# ---------------------------------------------------------------------------
# 시리얼 포트 자동 탐지 (LoRa USB = Silicon Labs CP210x, VID 0x10C4)
# 노트북마다 COM 번호가 달라 고정값(COM7)이 깨지는 문제 해결.
# ---------------------------------------------------------------------------
LORA_USB_VIDS     = {0x10C4}                              # Silicon Labs CP210x
LORA_USB_KEYWORDS = ("CP210", "Silicon Labs", "USB Serial", "USB-SERIAL")


def autodetect_serial_port() -> str:
    ports = list(list_ports.comports())
    # 1순위: VID 매칭 (CP210x)
    for p in ports:
        if p.vid in LORA_USB_VIDS:
            return p.device
    # 2순위: 설명/제조사 문자열 매칭
    for p in ports:
        text = f"{p.description} {p.manufacturer or ''}".lower()
        if any(k.lower() in text for k in LORA_USB_KEYWORDS):
            return p.device
    # 후보가 하나뿐이면 그것으로
    if len(ports) == 1:
        return ports[0].device
    avail = ", ".join(f"{p.device}({p.description})" for p in ports) or "(없음)"
    raise RuntimeError(f"LoRa 시리얼 포트 자동탐지 실패 — --port 로 지정하세요. 사용 가능: {avail}")


def _lora_send(frame: str) -> None:
    with _serial_write_lock:
        _ser.write((frame + "\n").encode("ascii"))
        _ser.flush()


# ---------------------------------------------------------------------------
# TDM slot-aligned uplink
# ---------------------------------------------------------------------------
# 드론(lora_fc_downlink_app)은 반이중 TDM이라 downlink TX 직후 300ms만 RX 윈도우를 연다.
# 따라서 UP 프레임을 아무 때나 쏘면 윈도우를 놓쳐 버려진다(충돌/유실).
# 해결: HTTP 핸들러는 프레임을 큐에 적재만 하고, serial_reader가 downlink 라인을
# 수신한 직후(= Pi RX 윈도우가 막 열린 슬롯)에 큐를 flush 해서 전송한다.
# SH 패킷이 FC 없이도 ~1Hz로 downlink되므로 슬롯은 항상 열린다(uplink 지연 최대 ~1초).
#
# 단발 전송은 한 슬롯만 노려 타이밍 지터/RF 손실로 자주 빗나간다(실측: 1번=무응답,
# 여러 번 붙여넣으면 적중). → 동일 프레임을 연속 _UPLINK_RETX개 슬롯에 자동 재전송한다.
# uplink_app은 sequence(IsSequenceAccepted)로 중복을 무시하므로 1발만 적용되고 나머지는
# replay로 거부(무해)된다. 즉 한 번의 명령으로도 안정적으로 도달한다.
_UPLINK_RETX = 4
_pending_lock = threading.Lock()
_pending_uplink: list = []   # [[frame, remaining_retx], ...]


def _queue_uplink(frame: str) -> None:
    with _pending_lock:
        _pending_uplink.append([frame, _UPLINK_RETX])


def _flush_pending_uplink() -> None:
    with _pending_lock:
        if not _pending_uplink:
            return
        for item in _pending_uplink:
            _lora_send(item[0])
            n = _UPLINK_RETX - item[1] + 1
            print(f"[UP->slot] ({n}/{_UPLINK_RETX}) {item[0]}")
            item[1] -= 1
        _pending_uplink[:] = [it for it in _pending_uplink if it[1] > 0]

# ---------------------------------------------------------------------------
# HTTP uplink server
# ---------------------------------------------------------------------------

class UplinkHandler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json({"ok": True, "service": "lora-bridge", "transport": "lora"})
        elif self.path == "/api/uplink/meta":
            self._json({
                "scopes": {
                    "cfs_core": sorted(CFS_CORE_PARAMS),
                    "mavlink_bridge": sorted(MAVLINK_BRIDGE_PARAMS),
                },
                "transport": "lora",
            })
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        body = self._read_body()
        if body is None:
            return
        if self.path == "/api/uplink/config":
            self._handle_config(body)
        elif self.path == "/api/uplink/route":
            self._handle_route(body)
        elif self.path == "/api/uplink/recovery":
            self._handle_recovery(body)
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_config(self, body: dict):
        scope_name = body.get("scope", "")
        param      = body.get("param", "")
        raw_value  = body.get("value")

        if scope_name == "cfs_core":
            scope, params = SCOPE_CFS_CORE_APP, CFS_CORE_PARAMS
        elif scope_name == "mavlink_bridge":
            scope, params = SCOPE_MAVLINK_BRIDGE, MAVLINK_BRIDGE_PARAMS
        else:
            self._json({"error": f"unknown scope '{scope_name}'"}, HTTPStatus.BAD_REQUEST)
            return

        if param not in params:
            self._json({"error": f"unknown param '{param}'",
                        "available": sorted(params)}, HTTPStatus.BAD_REQUEST)
            return

        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            self._json({"error": "value must be integer"}, HTTPStatus.BAD_REQUEST)
            return

        if not (0 <= value <= 0xFFFFFFFF):
            self._json({"error": "value must be uint32"}, HTTPStatus.BAD_REQUEST)
            return

        seq = _seq_counter.next()
        try:
            payload = _build_config_payload(scope, params[param], value)
            frame   = _build_lora_frame(seq, payload, UPLINK_CLASS_CONFIG)
            _queue_uplink(frame)
        except Exception as e:
            self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        print(f"[UP] CONFIG seq={seq} {scope_name}.{param}={value}  queued  frame={frame}")
        self._json({"ok": True, "seq": seq, "scope": scope_name, "param": param,
                    "value": value, "transport": "lora", "queued": True})

    def _handle_route(self, body: dict):
        route_name = str(body.get("route_type", "")).lower()
        raw_wps    = body.get("waypoints")

        route_type = ROUTE_TYPES.get(route_name)
        if route_type is None:
            self._json({"error": f"unknown route_type '{route_name}'",
                        "available": ["mission", "landing"]}, HTTPStatus.BAD_REQUEST)
            return

        if not isinstance(raw_wps, list) or not (1 <= len(raw_wps) <= MAX_ROUTE_WAYPOINTS):
            self._json({"error": f"waypoints must be a list of 1..{MAX_ROUTE_WAYPOINTS} [x,y,z]"},
                       HTTPStatus.BAD_REQUEST)
            return

        waypoints = []
        for i, wp in enumerate(raw_wps):
            try:
                x, y, z = (float(v) for v in wp)
            except (TypeError, ValueError):
                self._json({"error": f"waypoint[{i}] must be [x,y,z] numbers"},
                           HTTPStatus.BAD_REQUEST)
                return
            waypoints.append((x, y, z))

        seq = _seq_counter.next()
        try:
            payload = _build_route_payload(route_type, ROUTE_VERSION, waypoints)
            frame   = _build_lora_frame(seq, payload, UPLINK_CLASS_ROUTE_UPDATE)
            _queue_uplink(frame)
        except Exception as e:
            self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        print(f"[UP] ROUTE seq={seq} type={route_name} wps={len(waypoints)}  queued  frame={frame}")
        self._json({"ok": True, "seq": seq, "route_type": route_name,
                    "waypoint_count": len(waypoints), "transport": "lora", "queued": True})

    def _handle_recovery(self, body: dict):
        payload_hex = body.get("payload_hex", "")
        if payload_hex:
            try:
                payload = bytes.fromhex(payload_hex)
            except ValueError:
                self._json({"error": "invalid payload_hex"}, HTTPStatus.BAD_REQUEST)
                return
        else:
            payload = b""

        seq = _seq_counter.next()
        try:
            frame = _build_lora_frame(seq, payload, UPLINK_CLASS_RECOVERY)
            _queue_uplink(frame)
        except Exception as e:
            self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        print(f"[UP] RECOVERY seq={seq}  queued  frame={frame}")
        self._json({"ok": True, "seq": seq, "transport": "lora", "queued": True})

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return None

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK):
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self._cors()
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_):
        pass

# ---------------------------------------------------------------------------
# Downlink parser
# ---------------------------------------------------------------------------

def parse_int(v):
    try: return int(v)
    except: return None

def parse_float(v):
    try: return float(v)
    except: return None


def _update_link(seq: int):
    """통합 LoRa 프레임 seq gap으로 손실률 집계 (FC+SH 단일 카운터 공유)."""
    global _heartbeat, _last_seq, _total_expected, _total_received, _packet_loss
    _heartbeat += 1
    if _last_seq is not None:
        gap = (seq - _last_seq) & 0xFFFFFF
        if 1 <= gap <= 1000:
            _total_expected += gap
            _total_received += 1
    else:
        _total_expected += 1
        _total_received += 1
    _last_seq = seq
    _packet_loss = round(
        (_total_expected - _total_received) / _total_expected * 100.0, 1
    ) if _total_expected > 0 else 0.0
    return _packet_loss


def parse_lora_line(line: str):
    line = line.strip()
    parts = line.split(",")
    if not parts:
        return None

    ts     = int(time.time() * 1000)
    source = parts[0]

    if source == "FC" and len(parts) >= 17:
        seq        = parse_int(parts[1])
        ts_ms      = parse_int(parts[2])
        roll       = parse_float(parts[3])
        pitch      = parse_float(parts[4])
        yaw        = parse_float(parts[5])
        x          = parse_float(parts[6])
        y          = parse_float(parts[7])
        z          = parse_float(parts[8])
        vx         = parse_float(parts[9])
        vy         = parse_float(parts[10])
        vz         = parse_float(parts[11])
        lat_e7     = parse_int(parts[12])
        lon_e7     = parse_int(parts[13])
        alt_mm     = parse_int(parts[14])
        fix        = parse_int(parts[15])
        uplink_fb  = parse_int(parts[16])
        rollspeed  = parse_float(parts[17]) if len(parts) >= 20 else None
        pitchspeed = parse_float(parts[18]) if len(parts) >= 20 else None
        yawspeed   = parse_float(parts[19]) if len(parts) >= 20 else None

        if any(v is None for v in [seq, ts_ms, roll, pitch, yaw, x, y, z, vx, vy, vz]):
            return None

        _update_link(seq)
        data = {
            "timestamp": ts, "source": "FC",
            "seq": seq, "boot_ms": ts_ms,
            "roll": roll, "pitch": pitch, "yaw": yaw,
            "x": x, "y": y, "z": z,
            "vx": vx, "vy": vy, "vz": vz,
            "heartbeat": _heartbeat, "packet_loss": _packet_loss,
        }
        if lat_e7     is not None: data["lat"]        = lat_e7 / 1e7
        if lon_e7     is not None: data["lon"]        = lon_e7 / 1e7
        if alt_mm     is not None: data["alt"]        = alt_mm / 1000.0
        if fix        is not None: data["fix"]        = fix
        if uplink_fb  is not None: data["uplink_fb"]  = uplink_fb
        if rollspeed  is not None: data["rollspeed"]  = rollspeed
        if pitchspeed is not None: data["pitchspeed"] = pitchspeed
        if yawspeed   is not None: data["yawspeed"]   = yawspeed
        return data

    if source == "SH" and len(parts) >= 7:
        seq          = parse_int(parts[1])
        ts_ms        = parse_int(parts[2])
        health_state = parse_int(parts[3])
        fault_code   = parse_int(parts[4])
        link_state   = parse_int(parts[5])
        uplink_fb    = parse_int(parts[6])

        if any(v is None for v in [seq, ts_ms, health_state, fault_code]):
            return None

        _update_link(seq)
        return {
            "timestamp": ts, "source": "SH",
            "seq": seq, "boot_ms": ts_ms,
            "health_state": health_state, "fault_code": fault_code,
            "link_state": link_state, "uplink_fb": uplink_fb,
            "heartbeat": _heartbeat, "packet_loss": _packet_loss,
        }

    return None

# ---------------------------------------------------------------------------
# WebSocket + serial async loop
# ---------------------------------------------------------------------------

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
        return
    dead = []
    for c in list(clients):
        try:
            await c.send(msg)
        except Exception:
            dead.append(c)
    for c in dead:
        clients.discard(c)


async def serial_reader():
    while True:
        raw = await asyncio.to_thread(_ser.readline)
        if not raw:
            await asyncio.sleep(0.01)
            continue
        line = raw.decode(errors="ignore").strip()
        # downlink 라인 수신 = Pi가 방금 TX함 = RX 윈도우(300ms)가 지금 열림.
        # 대기 중인 uplink 프레임을 이 슬롯에 즉시 전송(TDM 정렬).
        _flush_pending_uplink()
        data = parse_lora_line(line)
        if data is None:
            print("[BAD]", line)
            continue
        msg = json.dumps(data)
        print("[OK]", msg)

        # CSV에 저장
        _save_telemetry_to_csv(data)

        await broadcast(msg)


async def main_async(serial_port: str, baudrate: int, http_port: int):
    global _ser, _csv_file
    if serial_port.lower() == "auto":
        serial_port = autodetect_serial_port()
        print(f"[SERIAL] auto-detected {serial_port}")
    print(f"[SERIAL] opening {serial_port} @ {baudrate}")
    _ser = serial.Serial(serial_port, baudrate, timeout=1)

    # CSV 초기화
    _init_csv()

    # HTTP server in daemon thread
    http_server = ThreadingHTTPServer(("127.0.0.1", http_port), UplinkHandler)
    t = threading.Thread(target=http_server.serve_forever, daemon=True)
    t.start()
    print(f"[HTTP]  http://127.0.0.1:{http_port}  (uplink)")

    # WebSocket server
    ws_server = await websockets.serve(ws_handler, WS_HOST, WS_PORT, ping_interval=None)
    print(f"[WS]    ws://{WS_HOST}:{WS_PORT}  (telemetry)")

    try:
        await serial_reader()
    finally:
        # CSV 파일 닫기
        if _csv_file:
            _csv_file.close()
            print("[CSV] File closed")

        ws_server.close()
        await ws_server.wait_closed()
        http_server.shutdown()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="LoRa bridge: downlink WS + uplink HTTP on one serial port")
    p.add_argument("--port",      default="auto",  help="serial port, or 'auto' to detect LoRa CP210x (default: auto)")
    p.add_argument("--baud",      type=int, default=57600, help="baud rate (default: 57600)")
    p.add_argument("--http-port", type=int, default=8082,  help="uplink HTTP port (default: 8082)")
    args = p.parse_args()

    asyncio.run(main_async(args.port, args.baud, args.http_port))
