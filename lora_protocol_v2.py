#!/usr/bin/env python3
"""지상국 LoRa 다운링크 디코더 — 프로토콜 v2 (notes/lora_protocol_v2_spec.md).

- DL2(0xD2) 바이너리 통합 프레임 디코드 (SysTime 확장 블록, saturation flag 포함)
- v1 텍스트 프레임(FC,/SH,/HB, ASCII) 패스스루 (spec §8 공존 규칙)
- 유효 DL2 수신 시 ACK2(0xA2) 회신 (spec §6, §7.2 — 수신 직후 즉시)
- 바이트 스트림 상태머신: 프레임이 여러 read에 걸쳐 와도 처리 (spec §7.1)
- CSV 로깅: 호스트 수신 시각 포함 (영상 타임스탬프 대조용)
"""
import argparse
import csv
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Union

try:
    import serial
except ModuleNotFoundError:  # pragma: no cover - 테스트 환경엔 pyserial 불필요
    serial = None

DEFAULT_SERIAL_PATH = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"
DEFAULT_BAUDRATE = 57600

DL2_MAGIC = 0xD2
UP2_MAGIC = 0xB2
ACK2_MAGIC = 0xA2

DL2_BASE_LEN = 45          # magic..linkstate (CRC 제외, SysTime 블록 제외) — sats 포함(2026-07-13)
DL2_SYSTIME_BLOCK_LEN = 8
DL2_TAIL_LEN = 3           # uplink_last_seq(u16)+uplink_boot_count(u8) — BL-03(2026-07-22), SysTime 뒤/CRC 앞
DL2_FLAG_SYSTIME = 0x01
DL2_FLAG_POS_SATURATED = 0x02
# waypoint readback(2026-07-23, spec §4.3) — 꼬리 필드 뒤/CRC 앞, SysTime과 독립 첨부 가능
DL2_FLAG_WAYPOINT = 0x04
DL2_WAYPOINT_BLOCK_LEN = 28  # route_type+page_index+total_pages+waypoints_in_page(1×4) + waypoint[2]×12
DL2_WAYPOINTS_PER_PAGE = 2

ANGLE_SCALE = 1.0e4        # i16 rad*1e4
CM = 100.0


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass
class Dl2Frame:
    seq: int
    flags: int
    ufb: int
    ts_ms: int
    roll_rad: float
    pitch_rad: float
    yaw_rad: float
    x_m: float
    y_m: float
    z_m: float
    vx_mps: float
    vy_mps: float
    vz_mps: float
    lat_e7: int
    lon_e7: int
    alt_mm: int
    fix: int
    sats: int
    health: int
    fault: int
    linkstate: int
    sys_time_unix_usec: Optional[int] = None
    uplink_last_seq: int = 0    # BL-03(2026-07-22): 기체가 마지막 수락한 uplink seq — 지상 자가복구용
    uplink_boot_count: int = 0  # BL-03: 기체 부팅 카운터(uint8 wrap) — 재부팅 감지용
    # waypoint readback(2026-07-23, spec §4.3) — 페이지 있으면 채워짐, 없으면 None
    wp_route_type: Optional[int] = None
    wp_page_index: Optional[int] = None
    wp_total_pages: Optional[int] = None
    wp_waypoints: Optional[List[tuple]] = None  # [(x,y,z), ...] 이 페이지에 담긴 것만(1~2개)

    @property
    def pos_saturated(self) -> bool:
        return bool(self.flags & DL2_FLAG_POS_SATURATED)

    @property
    def has_waypoint_page(self) -> bool:
        return bool(self.flags & DL2_FLAG_WAYPOINT)


@dataclass
class V1Line:
    text: str


@dataclass
class DecodeError:
    reason: str


Event = Union[Dl2Frame, V1Line, DecodeError]


def decode_dl2(frame: bytes) -> Dl2Frame:
    """magic부터 CRC까지 완전한 DL2 프레임 1개를 디코드. CRC는 호출 전 검증 완료 가정."""
    (seq, flags, ufb, ts_ms) = struct.unpack_from("<HBBI", frame, 2)
    angles = struct.unpack_from("<hhh", frame, 10)
    pos = struct.unpack_from("<hhh", frame, 16)
    vel = struct.unpack_from("<hhh", frame, 22)
    (lat_e7, lon_e7, alt_mm) = struct.unpack_from("<iii", frame, 28)
    (fix, sats, health, fault, linkstate) = struct.unpack_from("<BBBBB", frame, 40)

    sys_time = None
    tail_offset = DL2_BASE_LEN
    if flags & DL2_FLAG_SYSTIME and len(frame) >= DL2_BASE_LEN + DL2_SYSTIME_BLOCK_LEN + DL2_TAIL_LEN + 2:
        (sys_time,) = struct.unpack_from("<Q", frame, DL2_BASE_LEN)
        tail_offset = DL2_BASE_LEN + DL2_SYSTIME_BLOCK_LEN

    uplink_last_seq = 0
    uplink_boot_count = 0
    if len(frame) >= tail_offset + DL2_TAIL_LEN + 2:
        (uplink_last_seq, uplink_boot_count) = struct.unpack_from("<HB", frame, tail_offset)

    wp_route_type = wp_page_index = wp_total_pages = None
    wp_waypoints: Optional[List[tuple]] = None
    wp_offset = tail_offset + DL2_TAIL_LEN
    if flags & DL2_FLAG_WAYPOINT and len(frame) >= wp_offset + DL2_WAYPOINT_BLOCK_LEN + 2:
        (wp_route_type, wp_page_index, wp_total_pages, wp_in_page) = struct.unpack_from(
            "<BBBB", frame, wp_offset)
        raw = struct.unpack_from("<ffffff", frame, wp_offset + 4)
        wp_waypoints = [(raw[0], raw[1], raw[2]), (raw[3], raw[4], raw[5])][:max(wp_in_page, 0)]

    return Dl2Frame(
        seq=seq, flags=flags, ufb=ufb, ts_ms=ts_ms,
        roll_rad=angles[0] / ANGLE_SCALE,
        pitch_rad=angles[1] / ANGLE_SCALE,
        yaw_rad=angles[2] / ANGLE_SCALE,
        x_m=pos[0] / CM, y_m=pos[1] / CM, z_m=pos[2] / CM,
        vx_mps=vel[0] / CM, vy_mps=vel[1] / CM, vz_mps=vel[2] / CM,
        lat_e7=lat_e7, lon_e7=lon_e7, alt_mm=alt_mm,
        fix=fix, sats=sats, health=health, fault=fault, linkstate=linkstate,
        sys_time_unix_usec=sys_time,
        uplink_last_seq=uplink_last_seq, uplink_boot_count=uplink_boot_count,
        wp_route_type=wp_route_type, wp_page_index=wp_page_index,
        wp_total_pages=wp_total_pages, wp_waypoints=wp_waypoints,
    )


def encode_dl2(frame: Dl2Frame) -> bytes:
    """테스트/시뮬레이터용 DL2 인코더 (spec §4). 디코더와 왕복 검증에 사용."""
    flags = frame.flags
    if frame.sys_time_unix_usec is not None:
        flags |= DL2_FLAG_SYSTIME
    if frame.wp_waypoints is not None:
        flags |= DL2_FLAG_WAYPOINT
    body_len = DL2_BASE_LEN + (DL2_SYSTIME_BLOCK_LEN if frame.sys_time_unix_usec is not None else 0) + DL2_TAIL_LEN
    if frame.wp_waypoints is not None:
        body_len += DL2_WAYPOINT_BLOCK_LEN

    buf = bytearray()
    buf += struct.pack("<BBHBBI", DL2_MAGIC, body_len, frame.seq, flags, frame.ufb, frame.ts_ms)
    buf += struct.pack("<hhh",
                       int(round(frame.roll_rad * ANGLE_SCALE)),
                       int(round(frame.pitch_rad * ANGLE_SCALE)),
                       int(round(frame.yaw_rad * ANGLE_SCALE)))
    buf += struct.pack("<hhh",
                       int(round(frame.x_m * CM)),
                       int(round(frame.y_m * CM)),
                       int(round(frame.z_m * CM)))
    buf += struct.pack("<hhh",
                       int(round(frame.vx_mps * CM)),
                       int(round(frame.vy_mps * CM)),
                       int(round(frame.vz_mps * CM)))
    buf += struct.pack("<iii", frame.lat_e7, frame.lon_e7, frame.alt_mm)
    buf += struct.pack("<BBBBB", frame.fix, frame.sats, frame.health, frame.fault, frame.linkstate)
    if frame.sys_time_unix_usec is not None:
        buf += struct.pack("<Q", frame.sys_time_unix_usec)
    buf += struct.pack("<HB", frame.uplink_last_seq & 0xFFFF, frame.uplink_boot_count & 0xFF)
    if frame.wp_waypoints is not None:
        wps = list(frame.wp_waypoints) + [(0.0, 0.0, 0.0)] * (2 - len(frame.wp_waypoints))
        buf += struct.pack("<BBBB", frame.wp_route_type or 0, frame.wp_page_index or 0,
                           frame.wp_total_pages or 0, len(frame.wp_waypoints))
        buf += struct.pack("<ffffff", *wps[0], *wps[1])
    buf += struct.pack("<H", crc16_ccitt(bytes(buf)))
    return bytes(buf)


def build_ack2(seq_echo: int) -> bytes:
    head = struct.pack("<BH", ACK2_MAGIC, seq_echo & 0xFFFF)
    return head + struct.pack("<H", crc16_ccitt(head))


def build_up2(version: int, command_class: int, seq: int, payload: bytes = b"", flags: int = 0) -> bytes:
    """UP2(v2 바이너리 업링크 명령) 인코드 — spec §5. 지상(bridge) -> 기체.

    기체측 대응 디코더: lora_tdm_app_utils.c LORA_TDM_APP_ParseUp2Frame().
    """
    plen = len(payload)
    if plen > 255 - 9:  # magic+plen+ver+class+seq(2)+flags+crc(2)=9, 나머지가 payload 한도
        raise ValueError("UP2 payload too large: %d" % plen)
    head = struct.pack("<BBBBHB", UP2_MAGIC, plen, version, command_class, seq & 0xFFFF, flags)
    body = head + payload
    return body + struct.pack("<H", crc16_ccitt(body))


@dataclass
class Up2Frame:
    version: int
    command_class: int
    seq: int
    flags: int
    payload: bytes


def decode_up2(frame: bytes) -> Up2Frame:
    """완전한 UP2 프레임 1개를 디코드. CRC는 호출 전 검증 완료 가정 (테스트/왕복검증용 —
    실제 수신측은 기체 C ParseUp2Frame이며 지상은 이 프레임을 보내기만 한다)."""
    plen = frame[1]
    (version, command_class, seq, flags) = struct.unpack_from("<BBHB", frame, 2)
    payload = bytes(frame[7:7 + plen])
    return Up2Frame(version=version, command_class=command_class, seq=seq, flags=flags, payload=payload)


class RouteReadbackAssembler:
    """waypoint readback(2026-07-23, spec §4.3) 페이지 재조립.

    Dl2Frame.has_waypoint_page인 이벤트를 순서대로 feed()에 넣으면
    page_index로 재조립하고, total_pages만큼 모이면 완료. 페이지 누락
    (재시작 등)은 자동 재시도 없음 — 지상이 DIAGNOSTIC 요청을 재전송해야
    함(spec §4.3 "단순화" 결정).
    """

    def __init__(self) -> None:
        self.route_type: Optional[int] = None
        self.total_pages: Optional[int] = None
        self._pages: dict = {}

    def feed(self, event: Dl2Frame) -> Optional[List[tuple]]:
        """완료되면 waypoint (x,y,z) 리스트(순서대로) 반환, 아니면 None."""
        if not event.has_waypoint_page or event.wp_waypoints is None:
            return None

        if self.total_pages != event.wp_total_pages:
            # 새 readback 세션 시작(또는 첫 수신) — 이전 진행분 폐기
            self._pages = {}
            self.route_type = event.wp_route_type
            self.total_pages = event.wp_total_pages

        self._pages[event.wp_page_index] = event.wp_waypoints

        if self.total_pages and len(self._pages) >= self.total_pages:
            waypoints: List[tuple] = []
            for i in range(self.total_pages):
                if i not in self._pages:
                    return None  # 페이지 누락 — 아직 미완료
                waypoints.extend(self._pages[i])
            return waypoints
        return None

    @property
    def progress(self) -> str:
        if self.total_pages is None:
            return "0/0"
        return "%d/%d" % (len(self._pages), self.total_pages)


class DownlinkStream:
    """v1(ASCII 줄) / v2(DL2) 혼합 바이트 스트림 파서 (spec §8).

    feed()에 임의 크기 바이트 조각을 넣으면 완성된 이벤트 목록을 반환한다.
    프레임/줄이 조각 경계에 걸쳐도 내부 버퍼에 유지된다 (spec §7.1).
    """

    MAX_V1_LINE = 256

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> List[Event]:
        self._buf += chunk
        events: List[Event] = []
        while True:
            event, consumed = self._try_parse()
            if consumed == 0:
                break
            del self._buf[:consumed]
            if event is not None:
                events.append(event)
        return events

    def _try_parse(self):
        buf = self._buf
        if not buf:
            return None, 0

        first = buf[0]
        if first == DL2_MAGIC:
            return self._try_parse_dl2()
        if first in (UP2_MAGIC, ACK2_MAGIC):
            # 지상 수신 스트림에 나타날 수 없는 방향 — 1바이트 버리고 재동기화
            return DecodeError("unexpected magic 0x%02X" % first), 1
        if 0x20 <= first < 0x7F or first in (0x0A, 0x0D):
            return self._try_parse_v1_line()
        return DecodeError("garbage byte 0x%02X" % first), 1

    def _try_parse_dl2(self):
        buf = self._buf
        if len(buf) < 2:
            return None, 0
        body_len = buf[1]
        min_len = DL2_BASE_LEN + DL2_TAIL_LEN
        max_len = DL2_BASE_LEN + DL2_SYSTIME_BLOCK_LEN + DL2_TAIL_LEN + DL2_WAYPOINT_BLOCK_LEN
        if body_len < min_len or body_len > max_len:
            return DecodeError("bad DL2 len %d" % body_len), 1
        total = body_len + 2  # + CRC16
        if len(buf) < total:
            return None, 0  # 프레임 미완성 — 다음 feed 대기
        frame = bytes(buf[:total])
        (rx_crc,) = struct.unpack_from("<H", frame, body_len)
        if crc16_ccitt(frame[:body_len]) != rx_crc:
            # spec §3: CRC 실패 시 magic 바이트부터 재스캔
            return DecodeError("DL2 crc fail seq_area=%s" % frame[2:4].hex()), 1
        return decode_dl2(frame), total

    def _try_parse_v1_line(self):
        buf = self._buf
        nl = buf.find(b"\n")
        # 정상 v1 텍스트에는 0xD2가 등장할 수 없으므로(비ASCII), 줄 완성 전에
        # DL2 magic이 보이면 앞부분은 손상 잔여물 — 버리고 magic부터 재동기화
        magic = buf.find(bytes([DL2_MAGIC]))
        if magic >= 0 and (nl < 0 or magic < nl):
            return DecodeError("garbage before DL2 magic (%d bytes)" % magic), magic
        if nl < 0:
            if len(buf) > self.MAX_V1_LINE:
                return DecodeError("v1 line overflow"), len(buf)
            return None, 0
        line = bytes(buf[:nl]).decode("ascii", errors="replace").strip("\r")
        return V1Line(text=line), nl + 1


CSV_FIELDS = [
    "host_time_iso", "host_time_unix", "seq", "ufb", "ts_ms",
    "roll_rad", "pitch_rad", "yaw_rad", "x_m", "y_m", "z_m",
    "vx_mps", "vy_mps", "vz_mps", "lat_e7", "lon_e7", "alt_mm",
    "fix", "sats", "health", "fault", "linkstate", "pos_saturated", "sys_time_unix_usec",
]


def frame_to_csv_row(frame: Dl2Frame, host_time: float) -> dict:
    return {
        "host_time_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(host_time))
                         + (".%03dZ" % int((host_time % 1) * 1000)),
        "host_time_unix": "%.3f" % host_time,
        "seq": frame.seq, "ufb": frame.ufb, "ts_ms": frame.ts_ms,
        "roll_rad": "%.4f" % frame.roll_rad,
        "pitch_rad": "%.4f" % frame.pitch_rad,
        "yaw_rad": "%.4f" % frame.yaw_rad,
        "x_m": "%.2f" % frame.x_m, "y_m": "%.2f" % frame.y_m, "z_m": "%.2f" % frame.z_m,
        "vx_mps": "%.2f" % frame.vx_mps, "vy_mps": "%.2f" % frame.vy_mps, "vz_mps": "%.2f" % frame.vz_mps,
        "lat_e7": frame.lat_e7, "lon_e7": frame.lon_e7, "alt_mm": frame.alt_mm,
        "fix": frame.fix, "sats": frame.sats, "health": frame.health, "fault": frame.fault,
        "linkstate": frame.linkstate,
        "pos_saturated": int(frame.pos_saturated),
        "sys_time_unix_usec": frame.sys_time_unix_usec if frame.sys_time_unix_usec is not None else "",
    }


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LoRa downlink v2 decoder / ACK responder")
    parser.add_argument("--port", default=DEFAULT_SERIAL_PATH)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE)
    parser.add_argument("--csv", default=None, help="DL2 프레임 CSV 로그 경로")
    parser.add_argument("--no-ack", action="store_true", help="ACK2 회신 비활성 (수신 전용 모니터)")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list) -> int:  # pragma: no cover - 실기기 루프 (로직은 UT로 검증)
    args = parse_args(argv)
    if serial is None:
        print("pyserial 필요: pip install pyserial", file=sys.stderr)
        return 1

    ser = serial.Serial(args.port, args.baud, timeout=0.05)
    stream = DownlinkStream()
    csv_writer = None
    csv_file = None
    if args.csv:
        csv_file = open(args.csv, "a", newline="")
        csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        if csv_file.tell() == 0:
            csv_writer.writeheader()

    try:
        while True:
            chunk = ser.read(256)
            if not chunk:
                continue
            now = time.time()
            for event in stream.feed(chunk):
                if isinstance(event, Dl2Frame):
                    if not args.no_ack:
                        ser.write(build_ack2(event.seq))  # spec §7.2: 수신 직후 즉시
                    if csv_writer:
                        csv_writer.writerow(frame_to_csv_row(event, now))
                        csv_file.flush()
                    if not args.quiet:
                        print("DL2 seq=%u fix=%u hp=(%.1f,%.1f,%.1f) health=%u%s" % (
                            event.seq, event.fix, event.x_m, event.y_m, event.z_m,
                            event.health,
                            " utc_us=%d" % event.sys_time_unix_usec
                            if event.sys_time_unix_usec is not None else ""))
                elif isinstance(event, V1Line):
                    if not args.quiet:
                        print("V1 | " + event.text)
                elif isinstance(event, DecodeError) and not args.quiet:
                    print("ERR | " + event.reason, file=sys.stderr)
    except KeyboardInterrupt:
        return 0
    finally:
        if csv_file:
            csv_file.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
