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
import platform
import random
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import serial
from serial.tools import list_ports
import websockets

from lora_protocol_v2 import DownlinkStream, Dl2Frame, V1Line, DecodeError, build_ack2

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
    'lat', 'lon', 'alt', 'fix', 'sats',
    'seq', 'boot_ms', 'health_state', 'fault_code',
    'heartbeat', 'packet_loss',
    'uplink_fb', 'link_state',            # parse_lora_line이 채우지만 기존 목록에 누락돼 있었음
    '_ack_send_ms', '_rx_total_ms',       # Stage 1/2 실측용 (openmct_bridge_notes.md 참조)
    'sys_time_unix_usec',                 # GPS 기반 UTC (DL2 전용, §16.4 — notes/temp/gps_time_sync_164_implementation.md)
    'uplink_last_seq', 'uplink_boot_count', # BL-03(2026-07-22): 지상 자가복구/재부팅감지용 (DL2 전용)
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
UPLINK_CLASS_DIAGNOSTIC   = 6  # waypoint readback(2026-07-23) 계기로 신설 — 기존 LINK_STATUS 등도 이걸로 처음 송신 가능해짐
UPLINK_CLASS_COUNTER_MGMT = 7  # BL-CTR(2026-07-22), mission_app_runtime_spec.md §18.4.6.7

# §18.4.6.7 counter management scope — 기체 UPLINK_APP_CounterScope_t와 동일 값
COUNTER_SCOPES = {
    "mavlink_bridge": 1,
    "cfs_core": 2,
    "uplink": 3,
    "lora_tdm": 4,
}
COUNTER_ACTION_RESET = 0  # 현재 유일하게 허용되는 action

# DIAGNOSTIC_CMD_TLM_t.DiagTarget — lora_protocol_v2_spec.md §4.3.
# Payload[0]=DiagAction, Payload[1]=DiagTarget, Payload[2..5]=RequestToken(LE)
# (uplink_app_utils.c UPLINK_APP_ForwardDiagnosticCommand와 동일 레이아웃).
DIAG_TARGET_LORA_TDM = 0  # 기본값(하위호환) — LINK_STATUS/RX_STATS/TX_STATS
DIAG_TARGET_CFS_CORE = 1  # waypoint readback(2026-07-23)

DIAG_ACTIONS = {
    # (target, action_name): action_code — LORA_TDM_APP_DiagAction_t / CFS_CORE_APP_DiagAction_t
    (DIAG_TARGET_LORA_TDM, "link_status"): 0,
    (DIAG_TARGET_LORA_TDM, "rx_stats"):    1,
    (DIAG_TARGET_LORA_TDM, "tx_stats"):    2,
    (DIAG_TARGET_CFS_CORE, "route_readback"): 3,  # CFS_CORE_APP_DIAG_ACTION_ROUTE_READBACK_REQUEST
}

# mission_app_runtime_spec.md §18.10.2 — UP 프레임 flags 필드 비트0.
# 벤치 테스트 전용: health gate(§18.10.1)를 이 명령 하나만 우회.
UPLINK_FORCE_FLAG = 0x01

# §18.11.1 권한 검증 — flags 필드 비트[7:6]에 실리는 인증 레벨(0~3).
# 지상은 지금까지 이 비트를 전혀 안 채우고 있었음(발견: 2026-07-13) — CONFIG류
# 명령이 실제로는 한 번도 권한검증을 통과한 적이 없었을 가능성. 클래스별 필요
# 레벨은 uplink_app_cmds.c UPLINK_APP_GetClassRequiredLevel과 반드시 맞춰야 한다.
UPLINK_CLASS_REQUIRED_LEVEL = {
    UPLINK_CLASS_CONFIG: 2,
    UPLINK_CLASS_ROUTE_UPDATE: 2,
    UPLINK_CLASS_RECOVERY: 3,
    UPLINK_CLASS_COUNTER_MGMT: 3,  # §18.4.6.7 — Level 3 (request_token≠0 필수)
    UPLINK_CLASS_DIAGNOSTIC: 1,    # uplink_app_cmds.c GetClassRequiredLevel과 동일(진단 조회는 저권한)
}


def _auth_level_flag_bits(command_class: int) -> int:
    level = UPLINK_CLASS_REQUIRED_LEVEL.get(command_class, 0)
    return (level & 0x3) << 6


def _generate_request_token() -> int:
    """RECOVERY(level3) 인증에 필요한 0이 아닌 request_token 자동 생성.

    notes/temp/recovery_request_token_missing.md 참조 — 호출자가 직접
    Payload[4:8]을 채우지 않아도 되도록 서버가 매번 생성한다.
    """
    token = random.getrandbits(32)
    return token if token != 0 else 1


def _assemble_recovery_payload(payload_hex: str, request_token: int) -> bytes:
    """RECOVERY 페이로드 조립: [0:4]=action/target/reason(입력 유지, 부족분 0패딩),
    [4:8]=request_token(u32 LE, 입력 무시하고 항상 덮어씀).

    uplink_app_utils.c::UPLINK_APP_ForwardRecoveryCommand 레이아웃과 일치해야 함.
    """
    payload = bytearray.fromhex(payload_hex) if payload_hex else bytearray()
    if len(payload) < 4:
        payload.extend(b"\x00" * (4 - len(payload)))
    return bytes(payload[:4]) + struct.pack("<I", request_token)

SCOPE_CFS_CORE_APP   = 1
SCOPE_MAVLINK_BRIDGE = 2
SCOPE_LORA_TDM       = 3

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

# lora_protocol_v2_spec.md §8 — v1(text)/v2(DL2) 런타임 전환.
# value: 0=v1, 1=v2. 기체측 대응: lora_tdm_app LORA_TDM_APP_PARAM_DOWNLINK_PROTOCOL.
LORA_TDM_PARAMS = {
    "downlink_protocol": 0,
}

# 항목별 값 제한 — cFS 기체측 코드와 반드시 동기화 유지.
# cfs_core_app: default_cfs_core_app_internal_cfg_values.h (PARAM_MIN_MS/MAX_MS)
# mavlink_bridge_app: default_mavlink_bridge_app_internal_cfg_values.h
#   (PARAM_INTERVAL_MIN_US/MAX_US, PARAM_MS_MIN/MAX)
# lora_tdm_app: 기체측도 0/1만 수락하도록 엄격화됨(BL-16, 2026-07-21,
#   lora_tdm_app_cmds.c/lora_tdm_app_utils.c) — 지상 (0,1) 제한과 대칭 일치.
_CFS_CORE_MS_BOUNDS       = (100, 60000)
_MAVLINK_BRIDGE_US_BOUNDS = (10000, 10000000)
_MAVLINK_BRIDGE_MS_BOUNDS = (100, 60000)

PARAM_BOUNDS = {
    "cfs_core": {p: _CFS_CORE_MS_BOUNDS for p in CFS_CORE_PARAMS},
    "mavlink_bridge": {
        "attitude_interval_us":        _MAVLINK_BRIDGE_US_BOUNDS,
        "local_position_interval_us":  _MAVLINK_BRIDGE_US_BOUNDS,
        "global_position_interval_us": _MAVLINK_BRIDGE_US_BOUNDS,
        "gps_raw_interval_us":         _MAVLINK_BRIDGE_US_BOUNDS,
        "ekf_status_interval_us":      _MAVLINK_BRIDGE_US_BOUNDS,
        "reconnect_interval_ms":       _MAVLINK_BRIDGE_MS_BOUNDS,
        "heartbeat_interval_ms":       _MAVLINK_BRIDGE_MS_BOUNDS,
    },
    "lora_tdm": {
        "downlink_protocol": (0, 1),
    },
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

    def resync_from_device(self, device_last_accepted_seq: int) -> None:
        """BL-03(2026-07-22): 기체가 다운링크로 보고하는
        uplink_last_seq(=마지막 수락 seq)를 보고 앞으로만 당김.
        지상 프로세스 재시작으로 카운터가 기체보다 뒤처졌을 때(문제 2)
        자동 복구 — 절대 뒤로는 안 당김(오래된/지연 프레임이 카운터를
        되돌리는 사고 방지)."""
        with self._lock:
            candidate = (device_last_accepted_seq % 0xFFFF) + 1
            if candidate > self._v:
                self._v = candidate


_seq_counter = _SeqCounter()


class _BootCountTracker:
    """BL-03/BL-12(2026-07-22): 기체 boot_count(uint8 wrap) 추이를 보고
    재부팅 감지 + 비정상 감소(상태파일 손상/위조 의심) 플래그를 노출한다.
    "감소=자동거부"는 절대 하지 않는다 — 상태파일 정상 유실(전원차단)로도
    감소처럼 보일 수 있어(§AcceptedCount=0 폴백) 오탐 시 명령권을 영구
    상실시킬 위험이 있음. 대신 anomaly 플래그만 세워 운영자 확인을 유도."""

    def __init__(self):
        self._lock = threading.Lock()
        self.last_seen = None
        self.anomaly = False

    def observe(self, boot_count: int) -> None:
        with self._lock:
            if self.last_seen is None:
                self.last_seen = boot_count
                return
            if boot_count == self.last_seen:
                return
            diff = (boot_count - self.last_seen) % 256
            if diff < 128:
                # 정상 전진(재부팅 포함, wrap 포함) — anomaly 해제
                self.last_seen = boot_count
                self.anomaly = False
            else:
                # 감소로 보임 — 자동 거부는 하지 않고 플래그만
                self.anomaly = True
                self.last_seen = boot_count


_boot_count_tracker = _BootCountTracker()


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


def autodetect_serial_port(with_retry: bool = False, retry_interval: int = 5) -> str:
    """
    자동 탐지로 LoRa 시리얼 포트 찾기.
    with_retry=True 시 모듈을 찾을 때까지 재시도 (retry_interval초마다).
    """
    attempt = 0
    while True:
        attempt += 1
        ports = list(list_ports.comports())
        # 1순위: VID 매칭 (CP210x)
        for p in ports:
            if p.vid in LORA_USB_VIDS:
                if attempt > 1:
                    print(f"[SERIAL] ✅ LoRa 모듈 감지됨 (시도 #{attempt})")
                return p.device
        # 2순위: 설명/제조사 문자열 매칭
        for p in ports:
            text = f"{p.description} {p.manufacturer or ''}".lower()
            if any(k.lower() in text for k in LORA_USB_KEYWORDS):
                if attempt > 1:
                    print(f"[SERIAL] ✅ LoRa 모듈 감지됨 (시도 #{attempt})")
                return p.device
        # 후보가 하나뿐이면 그것으로
        if len(ports) == 1:
            if attempt > 1:
                print(f"[SERIAL] ✅ LoRa 모듈 감지됨 (시도 #{attempt})")
            return ports[0].device

        # 실패한 경우
        avail = ", ".join(f"{p.device}({p.description})" for p in ports) or "(없음)"
        if not with_retry:
            raise RuntimeError(f"LoRa 시리얼 포트 자동탐지 실패 — --port 로 지정하세요. 사용 가능: {avail}")

        # 재시도 모드: 계속 기다리기
        print(f"[SERIAL] ⏳ LoRa 모듈 미감지 (시도 #{attempt}). {retry_interval}초 후 재시도... 사용 가능: {avail}")
        time.sleep(retry_interval)


def kill_stale_server_processes() -> int:
    """이 스크립트(fc_serial_ws_server.py)의 이전 인스턴스가 좀비로 남아
    COM 포트를 점유하는 경우를 위한 정리 — --kill-stale 플래그
    (2026-07-23, 사용자 지시). Windows 전용(PowerShell Win32_Process 조회);
    다른 OS에서는 아무 것도 하지 않고 0 반환.

    psutil 등 외부 의존성 추가 없이 PowerShell만으로 구현 — 이 프로젝트
    다른 곳(Pi SSH 명령 등)에서도 이미 subprocess로 powershell.exe를
    호출하는 관례와 동일.
    """
    if platform.system() != "Windows":
        print("[KILL-STALE] Windows 전용 기능 — 건너뜀")
        return 0

    self_pid = os.getpid()
    script_name = os.path.basename(__file__)
    # Name -match 'python'로 먼저 걸러야 함 — CommandLine만으로 매칭하면
    # 이 조회 자체를 실행하는 powershell.exe 하위 프로세스의 CommandLine에도
    # 검색 패턴 문자열이 그대로 포함돼 있어 자기 자신을 오탐하는 문제가 있었음
    # (2026-07-23, 실측으로 발견).
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | "
        f"Where-Object {{ $_.CommandLine -match '{script_name}' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[KILL-STALE] 프로세스 조회 실패: {exc}")
        return 0

    killed = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        pid = int(line)
        if pid == self_pid:
            continue
        try:
            subprocess.run(["taskkill.exe", "/PID", str(pid), "/F"],
                          capture_output=True, timeout=10, check=False)
            print(f"[KILL-STALE] 이전 인스턴스 종료: PID {pid}")
            killed += 1
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[KILL-STALE] PID {pid} 종료 실패: {exc}")

    if killed == 0:
        print("[KILL-STALE] 종료할 이전 인스턴스 없음")
    return killed


def _open_serial_with_retry(serial_port: str, baudrate: int, with_retry: bool = True,
                             retry_interval: int = 5) -> "serial.Serial":
    """포트를 찾았어도 다른 프로세스가 점유 중이면(PermissionError 등) 즉시
    죽지 않고 계속 재탐색하도록 — autodetect_serial_port의 재시도 정책과
    동일 스타일(2026-07-23, 사용자 지시로 open() 단계에도 재시도 확대)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            ser = serial.Serial(serial_port, baudrate, timeout=1)
            if attempt > 1:
                print(f"[SERIAL] ✅ 포트 열기 성공 (시도 #{attempt})")
            return ser
        except serial.SerialException as exc:
            if not with_retry:
                raise
            print(f"[SERIAL] ⏳ 포트 열기 실패 (시도 #{attempt}): {exc}. "
                  f"{retry_interval}초 후 재시도... (다른 프로그램이 점유 중일 수 있음)")
            time.sleep(retry_interval)


def _lora_send_bytes(data: bytes) -> None:
    with _serial_write_lock:
        _ser.write(data)
        _ser.flush()


def _lora_send(frame: str) -> None:
    _lora_send_bytes((frame + "\n").encode("ascii"))


def _lora_send_ack2(seq_echo: int) -> None:
    """DL2(v2) 프레임 수신 ACK — 바이너리 ACK2, 개행 없음(길이기반 프레이밍)."""
    if seq_echo is None:
        return
    _lora_send_bytes(build_ack2(seq_echo))


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
_pending_uplink: list = []   # [[seq, payload, cmd_class, base_flags, remaining_retx], ...]

# BL-14(2026-07-22): Flags bits[2:1]=RETX_IDX(0~3 = 슬롯번호-1). 사본마다
# flags가 달라 프레임을 슬롯마다 재조립(CRC도 사본별 재계산). 기체는 이
# 값으로 동작을 바꾸지 않고 EVS 로그에만 표기 — "몇 번째 슬롯에서
# 통과했는지"로 RF 링크 마진을 진단(mission_app_runtime_spec.md §18.4.3.1).
_RETX_IDX_SHIFT = 1
_RETX_IDX_MASK = 0x3

# BL-23(2026-07-22): flush는 지금처럼 다운링크 수신 시에만 실제 전송한다
# (반이중 TDM 슬롯 정렬을 깨지 않기 위해 타이머 기반 강제 송신은 넣지
# 않음, ambiguity_audit_by_task_2026-07-21.md 우려 반영). 대신 다운링크가
# 끊긴 채로 새 명령이 계속 큐에 쌓이는 것을 막기 위해 큐 크기 상한을
# 둔다 — 평시엔 매 다운링크(~150~200ms)마다 비워져 1~2개 이상 쌓일 일이
# 없으므로, 16은 정상 동작을 방해하지 않는 넉넉한 안전장치일 뿐이다
# (사용자 결정, 2026-07-22). 초과 시 HTTP 에러가 아니라 가장 오래된
# 항목을 버리고 경고 로그만 남긴다 — 새 명령은 그대로 accept.
_UPLINK_QUEUE_MAX_SIZE = 16

# BL-24(2026-07-22): UFB=1(CRC_FAIL) 자동 재전송이 새 seq를 발급하면,
# 큐에 남아있던 원본 seq 사본이 뒤늦게 수락될 경우 같은 명령이 두 번
# 실행되는 경합이 있다(새 seq는 기체에 정상 신규 명령으로 보임).
# 같은 seq 재전송이면 원본이 이미 수락됐어도 DUPLICATE(BL-01)로 무시돼
# 이중 실행이 구조적으로 불가능 — 최근 전송 명령을 seq별로 캐시해두고
# /api/uplink/resend가 같은 seq 그대로 재큐잉한다(A안, 사용자 결정).
_SENT_CACHE_MAX = 32
_sent_commands: dict = {}    # seq → (payload, cmd_class, flags)


def _queue_uplink(seq: int, payload: bytes, cmd_class: int, flags: int = 0) -> None:
    with _pending_lock:
        if len(_pending_uplink) >= _UPLINK_QUEUE_MAX_SIZE:
            dropped_seq, _payload, dropped_class, _flags, dropped_remaining = _pending_uplink.pop(0)
            print(f"[UP QUEUE FULL] dropped oldest seq={dropped_seq} class={dropped_class} "
                  f"remaining_retx={dropped_remaining} (max={_UPLINK_QUEUE_MAX_SIZE}) — "
                  f"다운링크 단절 추정, 기체 미도달")
        _pending_uplink.append([seq, payload, cmd_class, flags, _UPLINK_RETX])
        _sent_commands[seq] = (payload, cmd_class, flags)
        while len(_sent_commands) > _SENT_CACHE_MAX:
            _sent_commands.pop(next(iter(_sent_commands)))  # 삽입순 → 가장 오래된 것부터


def _flush_pending_uplink() -> None:
    with _pending_lock:
        if not _pending_uplink:
            return
        for item in _pending_uplink:
            seq, payload, cmd_class, base_flags, remaining = item
            retx_idx = _UPLINK_RETX - remaining          # 0=첫 전송, 1~3=재전송
            flags = base_flags | ((retx_idx & _RETX_IDX_MASK) << _RETX_IDX_SHIFT)
            frame = _build_lora_frame(seq, payload, cmd_class, flags)
            _lora_send(frame)
            print(f"[UP->slot] ({retx_idx + 1}/{_UPLINK_RETX}) {frame}")
            item[4] -= 1
        _pending_uplink[:] = [it for it in _pending_uplink if it[4] > 0]

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
                    "lora_tdm": sorted(LORA_TDM_PARAMS),
                },
                "bounds": PARAM_BOUNDS,
                "transport": "lora",
                "boot_count_anomaly": _boot_count_tracker.anomaly,  # BL-03: 감소 감지 시 운영자 확인 필요
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
        elif self.path == "/api/uplink/resend":
            self._handle_resend(body)
        elif self.path == "/api/uplink/counter":
            self._handle_counter(body)
        elif self.path == "/api/uplink/diagnostic":
            self._handle_diagnostic(body)
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
        elif scope_name == "lora_tdm":
            scope, params = SCOPE_LORA_TDM, LORA_TDM_PARAMS
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

        lo, hi = PARAM_BOUNDS[scope_name][param]
        if not (lo <= value <= hi):
            self._json({"error": f"value out of range for '{param}': "
                                  f"must be between {lo} and {hi}",
                        "min": lo, "max": hi}, HTTPStatus.BAD_REQUEST)
            return

        # mission_app_runtime_spec.md §18.10.2 — UPLINK_APP_FORCE_FLAG(0x01).
        # 벤치 테스트 전용: DEGRADED/FAILED에서도 이 명령 하나만 health gate 우회.
        force = bool(body.get("force", False))
        # §18.10.3 — 비트[7:6]은 자격증명이 아니라 명령 클래스의 등급 분류 코드
        # (§18.11.1, 기체측 GetClassRequiredLevel과 동일 값). 지상이 지금까지 이걸
        # 안 채워서 CONFIG류가 항상 등급 불일치로 막혔음 — 클래스에 맞는 값으로 채운다.
        flags = _auth_level_flag_bits(UPLINK_CLASS_CONFIG) | (UPLINK_FORCE_FLAG if force else 0)

        seq = _seq_counter.next()
        try:
            payload = _build_config_payload(scope, params[param], value)
            frame   = _build_lora_frame(seq, payload, UPLINK_CLASS_CONFIG, flags)  # 로그용(retx=0 사본)
            _queue_uplink(seq, payload, UPLINK_CLASS_CONFIG, flags)
        except Exception as e:
            self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        force_tag = " [FORCE]" if force else ""
        print(f"[UP] CONFIG seq={seq} {scope_name}.{param}={value}{force_tag}  queued  frame={frame}")
        self._json({"ok": True, "seq": seq, "scope": scope_name, "param": param,
                    "value": value, "force": force, "transport": "lora", "queued": True})

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
            flags   = _auth_level_flag_bits(UPLINK_CLASS_ROUTE_UPDATE)
            frame   = _build_lora_frame(seq, payload, UPLINK_CLASS_ROUTE_UPDATE, flags)  # 로그용(retx=0 사본)
            _queue_uplink(seq, payload, UPLINK_CLASS_ROUTE_UPDATE, flags)
        except Exception as e:
            self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        print(f"[UP] ROUTE seq={seq} type={route_name} wps={len(waypoints)}  queued  frame={frame}")
        self._json({"ok": True, "seq": seq, "route_type": route_name,
                    "waypoint_count": len(waypoints), "transport": "lora", "queued": True})

    def _handle_recovery(self, body: dict):
        payload_hex = body.get("payload_hex", "")
        try:
            bytearray.fromhex(payload_hex) if payload_hex else None
        except ValueError:
            self._json({"error": "invalid payload_hex"}, HTTPStatus.BAD_REQUEST)
            return

        # §18.11.1 레벨3 게이트는 토큰 0을 항상 거부하므로 호출자가 직접
        # 채우지 않아도 되게 서버가 매번 생성해 덮어쓴다.
        # (notes/temp/recovery_request_token_missing.md A안)
        request_token = _generate_request_token()
        payload = _assemble_recovery_payload(payload_hex, request_token)

        seq = _seq_counter.next()
        try:
            flags = _auth_level_flag_bits(UPLINK_CLASS_RECOVERY)
            frame = _build_lora_frame(seq, payload, UPLINK_CLASS_RECOVERY, flags)  # 로그용(retx=0 사본)
            _queue_uplink(seq, payload, UPLINK_CLASS_RECOVERY, flags)
        except Exception as e:
            self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        print(f"[UP] RECOVERY seq={seq} token={request_token:#010x}  queued  frame={frame}")
        self._json({"ok": True, "seq": seq, "request_token": request_token,
                    "transport": "lora", "queued": True})

    def _handle_counter(self, body: dict):
        # BL-CTR(2026-07-22, §18.4.6.7): counter management (class 7).
        # payload = counter_scope(1) + counter_action(1, RESET=0 고정) +
        #           request_token(4, LE — 기체는 Payload[2..5]에서 파싱).
        # 라우팅은 기체 uplink_app이 직접 수행(cfs_core_app 미경유).
        scope_name = body.get("scope")
        if scope_name not in COUNTER_SCOPES:
            self._json({"error": f"unknown scope '{scope_name}'",
                        "available": sorted(COUNTER_SCOPES)}, HTTPStatus.BAD_REQUEST)
            return

        scope = COUNTER_SCOPES[scope_name]
        request_token = _generate_request_token()
        payload = bytes([scope, COUNTER_ACTION_RESET]) + request_token.to_bytes(4, "little")

        seq = _seq_counter.next()
        try:
            flags = _auth_level_flag_bits(UPLINK_CLASS_COUNTER_MGMT)
            frame = _build_lora_frame(seq, payload, UPLINK_CLASS_COUNTER_MGMT, flags)  # 로그용(retx=0 사본)
            _queue_uplink(seq, payload, UPLINK_CLASS_COUNTER_MGMT, flags)
        except Exception as e:
            self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        print(f"[UP] COUNTER seq={seq} scope={scope_name}({scope}) token={request_token:#010x}  queued  frame={frame}")
        self._json({"ok": True, "seq": seq, "scope": scope_name, "action": "reset",
                    "request_token": request_token, "transport": "lora", "queued": True})

    def _handle_diagnostic(self, body: dict):
        # DIAGNOSTIC class(6) — 여태 지상에 송신 경로가 전혀 없었음(2026-07-23
        # waypoint readback 작업 중 발견). payload = DiagAction(1) + DiagTarget(1)
        # + RequestToken(4, LE) — uplink_app_utils.c UPLINK_APP_ForwardDiagnosticCommand
        # 파싱 순서(Payload[0]=Action, [1]=Target, [2..5]=Token)와 반드시 일치해야 함.
        target_name = body.get("target")
        action_name = body.get("action")

        target = {"lora_tdm": DIAG_TARGET_LORA_TDM, "cfs_core": DIAG_TARGET_CFS_CORE}.get(target_name)
        if target is None:
            self._json({"error": f"unknown target '{target_name}'",
                        "available": ["lora_tdm", "cfs_core"]}, HTTPStatus.BAD_REQUEST)
            return

        action = DIAG_ACTIONS.get((target, action_name))
        if action is None:
            available = sorted(a for (t, a) in DIAG_ACTIONS if t == target)
            self._json({"error": f"unknown action '{action_name}' for target '{target_name}'",
                        "available": available}, HTTPStatus.BAD_REQUEST)
            return

        request_token = _generate_request_token()
        payload = bytes([action, target]) + request_token.to_bytes(4, "little")

        seq = _seq_counter.next()
        try:
            flags = _auth_level_flag_bits(UPLINK_CLASS_DIAGNOSTIC)
            frame = _build_lora_frame(seq, payload, UPLINK_CLASS_DIAGNOSTIC, flags)  # 로그용(retx=0 사본)
            _queue_uplink(seq, payload, UPLINK_CLASS_DIAGNOSTIC, flags)
        except Exception as e:
            self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        print(f"[UP] DIAGNOSTIC seq={seq} target={target_name}({target}) action={action_name}({action}) "
              f"token={request_token:#010x}  queued  frame={frame}")
        self._json({"ok": True, "seq": seq, "target": target_name, "action": action_name,
                    "request_token": request_token, "transport": "lora", "queued": True})

    def _handle_resend(self, body: dict):
        # BL-24(2026-07-22): UFB=1 자동 재전송용 — 새 seq를 발급하지 않고
        # 캐시된 원본 명령을 같은 seq 그대로 재큐잉한다(진짜 재전송).
        # 원본이 이미 기체에 수락된 경우에도 DUPLICATE로 무시돼 무해.
        seq = body.get("seq")
        if not isinstance(seq, int):
            self._json({"error": "seq (int) required"}, HTTPStatus.BAD_REQUEST)
            return

        with _pending_lock:
            cached = _sent_commands.get(seq)
        if cached is None:
            self._json({"error": f"seq {seq} not in sent cache (max {_SENT_CACHE_MAX} entries)"},
                       HTTPStatus.NOT_FOUND)
            return

        payload, cmd_class, flags = cached
        _queue_uplink(seq, payload, cmd_class, flags)
        print(f"[UP] RESEND seq={seq} class={cmd_class}  requeued (same seq)")
        self._json({"ok": True, "seq": seq, "transport": "lora", "queued": True, "resend": True})

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
        # 필드 18: sats (SatellitesVisible) — lora_tdm_app 2026-07-13 추가.
        # 주의: 예전에 예약해뒀던 "len>=20이면 rollspeed/pitch/yawspeed가 idx 17~19"
        # 가정은 기체 인코더가 한 번도 구현한 적 없는 죽은 경로였음. sats가 idx 17을
        # 선점하므로, rollspeed 확장을 나중에 추가한다면 idx 19~21로 밀어야 한다.
        sats       = parse_int(parts[17]) if len(parts) >= 18 else None
        rollspeed  = parse_float(parts[19]) if len(parts) >= 22 else None
        pitchspeed = parse_float(parts[20]) if len(parts) >= 22 else None
        yawspeed   = parse_float(parts[21]) if len(parts) >= 22 else None

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
        if sats       is not None: data["sats"]       = sats
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


def dl2_frame_to_data(frame: Dl2Frame) -> dict:
    """DL2(v2 바이너리 통합 프레임) -> 기존 WS/CSV 스키마(v1 FC/SH와 동일 필드명).

    DL2는 자세/위치(구 FC)와 health/link(구 SH)를 한 프레임에 합쳐 보내므로
    source="DL2" 단일 레코드로 변환한다. pos_saturated는 현재 _csv_fields에
    없어 이번 범위에서는 반영하지 않음. sys_time_unix_usec(§16.4, GPS UTC —
    notes/temp/gps_time_sync_164_implementation.md)는 있을 때만 채움.
    """
    ts = int(time.time() * 1000)
    _update_link(frame.seq)
    _seq_counter.resync_from_device(frame.uplink_last_seq)
    _boot_count_tracker.observe(frame.uplink_boot_count)
    data = {
        "timestamp": ts, "source": "DL2",
        "seq": frame.seq, "boot_ms": frame.ts_ms,
        "roll": frame.roll_rad, "pitch": frame.pitch_rad, "yaw": frame.yaw_rad,
        "x": frame.x_m, "y": frame.y_m, "z": frame.z_m,
        "vx": frame.vx_mps, "vy": frame.vy_mps, "vz": frame.vz_mps,
        "lat": frame.lat_e7 / 1e7, "lon": frame.lon_e7 / 1e7, "alt": frame.alt_mm / 1000.0,
        "fix": frame.fix, "sats": frame.sats,
        "health_state": frame.health, "fault_code": frame.fault, "link_state": frame.linkstate,
        "uplink_fb": frame.ufb,
        "uplink_last_seq": frame.uplink_last_seq, "uplink_boot_count": frame.uplink_boot_count,
        "heartbeat": _heartbeat, "packet_loss": _packet_loss,
    }
    if frame.sys_time_unix_usec is not None:
        data["sys_time_unix_usec"] = frame.sys_time_unix_usec
    return data

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


def _send_ack(seq) -> None:
    """다운링크 seq에 대한 keepalive ACK 회신 (Stage 1, 2026-07-13 추가).

    lora_tdm_app_behavior_spec.md §11: 기체는 ACK 수신을 LinkState=CONNECTED
    keepalive로 사용한다. 지금까지 이 서버가 ACK를 보내지 않아 지상 화면(OK)과
    기체 판단(LinkState)이 어긋날 수 있었다 — 이 함수로 그 갭을 메운다.
    """
    if seq is None:
        return
    _lora_send(f"ACK,{seq}")


_downlink_stream = DownlinkStream()


async def serial_reader():
    while True:
        rx_start = time.monotonic()
        # v1(줄단위)/v2(DL2, 길이기반 바이너리)가 혼용될 수 있어 readline()이 아니라
        # 원시 청크를 읽어 DownlinkStream에 먹인다 — readline()은 DL2 바이트 안에
        # 우연히 0x0A가 섞이면 프레임을 중간에 끊어버릴 위험이 있어 쓸 수 없다.
        chunk = await asyncio.to_thread(_ser.read, 256)
        if not chunk:
            await asyncio.sleep(0.01)
            continue

        events = _downlink_stream.feed(chunk)

        for event in events:
            if isinstance(event, DecodeError):
                print("[BAD]", event.reason)
                continue

            if isinstance(event, Dl2Frame):
                data = dl2_frame_to_data(event)
                ack_start = time.monotonic()
                _lora_send_ack2(event.seq)
            elif isinstance(event, V1Line):
                data = parse_lora_line(event.text)
                if data is None:
                    print("[BAD]", event.text)
                    continue
                ack_start = time.monotonic()
                _send_ack(data.get("seq"))
            else:
                continue

            # downlink 수신 = Pi가 방금 TX함 = RX 윈도우가 지금 열림.
            # ACK를 먼저 보내 keepalive를 확보한 뒤, 남는 시간에 pending uplink를 flush한다.
            ack_elapsed_ms = (time.monotonic() - ack_start) * 1000.0

            _flush_pending_uplink()

            msg = json.dumps(data)
            rx_elapsed_ms = (time.monotonic() - rx_start) * 1000.0
            print(f"[OK] {msg}  (ack_send={ack_elapsed_ms:.1f}ms total={rx_elapsed_ms:.1f}ms)")

            # CSV에 저장 (계측값 포함 — Stage 1/2 실측 런북 참조)
            data["_ack_send_ms"] = round(ack_elapsed_ms, 2)
            data["_rx_total_ms"] = round(rx_elapsed_ms, 2)
            _save_telemetry_to_csv(data)

            await broadcast(msg)


async def main_async(serial_port: str, baudrate: int, http_port: int, enable_lora_retry: bool = True):
    global _ser, _csv_file
    if serial_port.lower() == "auto":
        serial_port = autodetect_serial_port(with_retry=enable_lora_retry, retry_interval=5)
    print(f"[SERIAL] opening {serial_port} @ {baudrate}")
    _ser = _open_serial_with_retry(serial_port, baudrate, with_retry=enable_lora_retry, retry_interval=5)

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
    p.add_argument("--no-lora-retry", action="store_true", help="LoRa 모듈 탐지 실패 시 재시도하지 않고 즉시 종료")
    p.add_argument("--kill-stale", action="store_true",
                    help="시작 전 이 스크립트의 이전 인스턴스를 종료(COM 포트 점유 정리, Windows 전용)")
    args = p.parse_args()

    if args.kill_stale:
        kill_stale_server_processes()

    enable_lora_retry = not args.no_lora_retry
    asyncio.run(main_async(args.port, args.baud, args.http_port, enable_lora_retry))
