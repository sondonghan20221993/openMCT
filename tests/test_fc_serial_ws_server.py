import os
import re
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fc_serial_ws_server as srv
from lora_protocol_v2 import Dl2Frame, DL2_BASE_LEN


class Dl2FrameToDataTest(unittest.TestCase):
    def _make_frame(self, **overrides):
        base = dict(
            seq=42, flags=0, ufb=0, ts_ms=12345,
            roll_rad=0.1, pitch_rad=-0.2, yaw_rad=0.3,
            x_m=1.0, y_m=-2.0, z_m=3.0,
            vx_mps=0.5, vy_mps=-0.5, vz_mps=0.0,
            lat_e7=374530000, lon_e7=1269850000, alt_mm=50000,
            fix=3, sats=9, health=1, fault=0, linkstate=1,
        )
        base.update(overrides)
        return Dl2Frame(**base)

    def setUp(self):
        srv._heartbeat = 0
        srv._last_seq = None
        srv._total_expected = 0
        srv._total_received = 0
        srv._packet_loss = 0.0

    def test_field_mapping_and_scaling(self):
        frame = self._make_frame()
        data = srv.dl2_frame_to_data(frame)

        self.assertEqual(data["source"], "DL2")
        self.assertEqual(data["seq"], 42)
        self.assertEqual(data["boot_ms"], 12345)
        self.assertEqual(data["roll"], 0.1)
        self.assertEqual(data["pitch"], -0.2)
        self.assertEqual(data["yaw"], 0.3)
        self.assertEqual(data["x"], 1.0)
        self.assertEqual(data["y"], -2.0)
        self.assertEqual(data["z"], 3.0)
        self.assertAlmostEqual(data["lat"], 37.453)
        self.assertAlmostEqual(data["lon"], 126.985)
        self.assertAlmostEqual(data["alt"], 50.0)
        self.assertEqual(data["fix"], 3)
        self.assertEqual(data["sats"], 9)
        self.assertEqual(data["health_state"], 1)
        self.assertEqual(data["fault_code"], 0)
        self.assertEqual(data["link_state"], 1)
        self.assertEqual(data["uplink_fb"], 0)

    def test_updates_link_quality_seq_tracking(self):
        srv.dl2_frame_to_data(self._make_frame(seq=1))
        srv.dl2_frame_to_data(self._make_frame(seq=2))
        self.assertEqual(srv._heartbeat, 2)
        self.assertEqual(srv._packet_loss, 0.0)

    def test_sys_time_included_when_present(self):
        frame = self._make_frame(sys_time_unix_usec=1752480000123456)
        data = srv.dl2_frame_to_data(frame)
        self.assertEqual(data["sys_time_unix_usec"], 1752480000123456)

    def test_sys_time_absent_when_not_present(self):
        frame = self._make_frame()  # sys_time_unix_usec 기본값 None
        data = srv.dl2_frame_to_data(frame)
        self.assertNotIn("sys_time_unix_usec", data)

    def test_csv_writer_accepts_dl2_row_without_extra_keys(self):
        """dl2_frame_to_data 결과가 _csv_fields에 없는 키를 안 담아야
        csv.DictWriter(extrasaction 기본값 'raise')가 안 터진다."""
        data = srv.dl2_frame_to_data(self._make_frame())
        extra = set(data.keys()) - set(srv._csv_fields)
        self.assertEqual(extra, set(), f"_csv_fields에 없는 키: {extra}")


def _find_cfs_repo_root():
    """cfs-telemetry-app repo를 형제 디렉터리에서 탐색 (BL-26/27/28,
    2026-07-21). 못 찾으면 None — 크로스 repo 테스트는 이 머신에서만
    돈다는 걸 인정하고 skip 처리(다른 환경에서 깨지지 않게)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (
        os.path.join(here, "..", "cfs-telemetry-app"),
        os.path.join(here, "..", "..", "cfs-telemetry-app"),
    ):
        candidate = os.path.abspath(candidate)
        if os.path.isdir(os.path.join(candidate, "lora_tdm_app")):
            return candidate
    return None


def _parse_c_define(header_path, name):
    """`#define NAME VALUE`(정수, U/u/L 접미사·주석 허용)를 정규식으로
    추출 — 손으로 값을 복제하는 대신 C 헤더를 단일 진실로 교차검증."""
    pattern = re.compile(r"^\s*#define\s+" + re.escape(name) + r"\s+(-?\d+)")
    with open(header_path, "r", encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line)
            if m:
                return int(m.group(1))
    raise AssertionError(f"{name} not found in {header_path}")


_CFS_ROOT = _find_cfs_repo_root()


class LoraProtocolV2SyncTest(unittest.TestCase):
    def test_dl2_base_len_includes_sats_field(self):
        """lora_protocol_v2.py가 cfs-telemetry-app/bridge/lora_downlink_decoder.py
        최신본(45바이트, sats 포함)과 동기화된 상태인지 회귀 확인."""
        self.assertEqual(DL2_BASE_LEN, 45)

    @unittest.skipUnless(_CFS_ROOT, "cfs-telemetry-app repo가 형제 디렉터리에 없음")
    def test_dl2_base_len_matches_c_header(self):
        """BL-26(2026-07-21): DL2_BASE_LEN이 기체 C 헤더
        (LORA_TDM_APP_DL2_BASE_LEN)와 실제로 일치하는지 확인 —
        자기 자신과의 비교(위 테스트)만으로는 크로스 repo 드리프트를
        못 잡음.

        정정(2026-07-24): BL-03(2026-07-22)에서 C 헤더가 리팩터되며
        `LORA_TDM_APP_DL2_LEN_FIELD`가 "45바이트 기본 길이"에서
        "기본 + tail(uplink_last_seq/boot_count)"로 의미가 바뀌어
        `(BASE_LEN + TAIL_LEN)` 연산식이 됨 — 정수 리터럴만 파싱하는
        `_parse_c_define`이 매칭 실패해 `python3 -m unittest discover`로
        전체 스위트를 돌릴 때만 드러나는 조용한 실패 상태였음(개별
        `pytest tests/test_uplink_handler_integration.py` 실행으로는
        미노출). 45바이트 기본 길이를 그대로 갖고 있는
        `LORA_TDM_APP_DL2_BASE_LEN`으로 대상 정정."""
        header = os.path.join(
            _CFS_ROOT, "lora_tdm_app", "fsw", "inc", "lora_tdm_app_interface_cfg.h"
        )
        c_value = _parse_c_define(header, "LORA_TDM_APP_DL2_BASE_LEN")
        self.assertEqual(DL2_BASE_LEN, c_value)


class ParamBoundsSyncTest(unittest.TestCase):
    """BL-27(2026-07-21): fc_serial_ws_server.py의 PARAM_BOUNDS가 기체
    C 상수(손으로 복제한 값)와 실제로 일치하는지 교차검증."""

    @unittest.skipUnless(_CFS_ROOT, "cfs-telemetry-app repo가 형제 디렉터리에 없음")
    def test_cfs_core_bounds_match_c_header(self):
        header = os.path.join(
            _CFS_ROOT, "cfs_core_app", "config", "default_cfs_core_app_internal_cfg_values.h"
        )
        lo = _parse_c_define(header, "CFS_CORE_APP_PARAM_MIN_MS")
        hi = _parse_c_define(header, "CFS_CORE_APP_PARAM_MAX_MS")
        self.assertEqual(srv.PARAM_BOUNDS["cfs_core"]["attitude_timeout_ms"], (lo, hi))

    @unittest.skipUnless(_CFS_ROOT, "cfs-telemetry-app repo가 형제 디렉터리에 없음")
    def test_mavlink_bridge_bounds_match_c_header(self):
        header = os.path.join(
            _CFS_ROOT,
            "mavlink_bridge_app",
            "config",
            "default_mavlink_bridge_app_internal_cfg_values.h",
        )
        us_lo = _parse_c_define(header, "MAVLINK_BRIDGE_APP_PARAM_INTERVAL_MIN_US")
        us_hi = _parse_c_define(header, "MAVLINK_BRIDGE_APP_PARAM_INTERVAL_MAX_US")
        ms_lo = _parse_c_define(header, "MAVLINK_BRIDGE_APP_PARAM_MS_MIN")
        ms_hi = _parse_c_define(header, "MAVLINK_BRIDGE_APP_PARAM_MS_MAX")
        self.assertEqual(
            srv.PARAM_BOUNDS["mavlink_bridge"]["attitude_interval_us"], (us_lo, us_hi)
        )
        self.assertEqual(
            srv.PARAM_BOUNDS["mavlink_bridge"]["reconnect_interval_ms"], (ms_lo, ms_hi)
        )


class Crc16Test(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(srv._crc16(b""), 0xFFFF)

    def test_known_frame_crc_matches_build_lora_frame(self):
        canonical = "UP,1,1,1,128,"
        crc = srv._crc16(canonical.encode("ascii"))
        frame = srv._build_lora_frame(1, b"", srv.UPLINK_CLASS_CONFIG, 0x80)
        self.assertEqual(frame, f"{canonical},{crc:04X}")


class BuildLoraFrameTest(unittest.TestCase):
    def test_field_order_and_payload_hex(self):
        frame = srv._build_lora_frame(7, b"\x01\x02", srv.UPLINK_CLASS_ROUTE_UPDATE, 0x80)
        prefix, crc_field = frame.rsplit(",", 1)
        self.assertEqual(prefix, "UP,1,2,7,128,0102")
        self.assertEqual(len(crc_field), 4)
        int(crc_field, 16)  # must be valid hex

    def test_empty_payload_produces_empty_hex_field(self):
        frame = srv._build_lora_frame(1, b"", srv.UPLINK_CLASS_CONFIG, 0)
        self.assertIn(",1,1,1,0,,", frame)


class AuthLevelFlagBitsTest(unittest.TestCase):
    def test_config_level_2(self):
        self.assertEqual(srv._auth_level_flag_bits(srv.UPLINK_CLASS_CONFIG), 2 << 6)

    def test_route_update_level_2(self):
        self.assertEqual(srv._auth_level_flag_bits(srv.UPLINK_CLASS_ROUTE_UPDATE), 2 << 6)

    def test_recovery_level_3(self):
        self.assertEqual(srv._auth_level_flag_bits(srv.UPLINK_CLASS_RECOVERY), 3 << 6)

    def test_unknown_class_defaults_to_zero(self):
        self.assertEqual(srv._auth_level_flag_bits(999), 0)


class GenerateRequestTokenTest(unittest.TestCase):
    def test_never_zero_across_many_calls(self):
        for _ in range(2000):
            token = srv._generate_request_token()
            self.assertNotEqual(token, 0)
            self.assertTrue(0 < token <= 0xFFFFFFFF)


class ConfigPayloadTest(unittest.TestCase):
    def test_build_config_payload_layout(self):
        payload = srv._build_config_payload(srv.SCOPE_CFS_CORE_APP, 5, 12345)
        scope, version, param_id, value_type, value_len, checksum = struct.unpack_from(
            "<BBHBBH", payload)
        (value,) = struct.unpack_from("<I", payload, 8)
        self.assertEqual(scope, srv.SCOPE_CFS_CORE_APP)
        self.assertEqual(version, srv.CONFIG_VERSION)
        self.assertEqual(param_id, 5)
        self.assertEqual(value_type, srv.VALUE_TYPE_UINT32)
        self.assertEqual(value_len, 4)
        self.assertEqual(value, 12345)
        expected_checksum = srv._config_checksum(
            srv.SCOPE_CFS_CORE_APP, srv.CONFIG_VERSION, 5,
            srv.VALUE_TYPE_UINT32, 4, struct.pack("<I", 12345))
        self.assertEqual(checksum, expected_checksum)


class RoutePayloadTest(unittest.TestCase):
    def test_build_route_payload_layout(self):
        waypoints = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]
        payload = srv._build_route_payload(srv.ROUTE_TYPES["mission"], srv.ROUTE_VERSION, waypoints)
        route_type, route_version, count, reserved = struct.unpack_from("<BBBB", payload)
        self.assertEqual(route_type, srv.ROUTE_TYPES["mission"])
        self.assertEqual(route_version, srv.ROUTE_VERSION)
        self.assertEqual(count, 2)
        self.assertEqual(reserved, 0)
        x0, y0, z0 = struct.unpack_from("<fff", payload, 4)
        self.assertEqual((x0, y0, z0), (1.0, 2.0, 3.0))
        x1, y1, z1 = struct.unpack_from("<fff", payload, 16)
        self.assertEqual((x1, y1, z1), (4.0, 5.0, 6.0))


class AssembleRecoveryPayloadTest(unittest.TestCase):
    def test_pads_short_payload_hex_to_four_bytes(self):
        payload = srv._assemble_recovery_payload("0102", 0x11223344)
        self.assertEqual(len(payload), 8)
        self.assertEqual(payload[:4], b"\x01\x02\x00\x00")

    def test_empty_payload_hex_padded(self):
        payload = srv._assemble_recovery_payload("", 1)
        self.assertEqual(payload[:4], b"\x00\x00\x00\x00")

    def test_token_always_overwrites_trailing_bytes(self):
        # even if caller supplies 8+ bytes including a token-looking tail,
        # bytes [4:8] must reflect the server-generated token, not the input
        payload = srv._assemble_recovery_payload("01020304FFFFFFFF", 0x11223344)
        self.assertEqual(payload[:4], b"\x01\x02\x03\x04")
        self.assertEqual(payload[4:8], struct.pack("<I", 0x11223344))

    def test_token_little_endian_encoding(self):
        payload = srv._assemble_recovery_payload("01020304", 0x11223344)
        self.assertEqual(payload[4:8], b"\x44\x33\x22\x11")


class SeqCounterTest(unittest.TestCase):
    def test_starts_at_one_and_increments(self):
        c = srv._SeqCounter()
        self.assertEqual(c.next(), 1)
        self.assertEqual(c.next(), 2)

    def test_wraps_after_0xffff_back_to_one(self):
        c = srv._SeqCounter()
        c._v = 0xFFFF
        self.assertEqual(c.next(), 0xFFFF)
        self.assertEqual(c.next(), 1)


class ParseIntFloatTest(unittest.TestCase):
    def test_parse_int_valid_and_invalid(self):
        self.assertEqual(srv.parse_int("42"), 42)
        self.assertIsNone(srv.parse_int("abc"))
        self.assertIsNone(srv.parse_int(None))

    def test_parse_float_valid_and_invalid(self):
        self.assertEqual(srv.parse_float("1.5"), 1.5)
        self.assertIsNone(srv.parse_float("abc"))
        self.assertIsNone(srv.parse_float(None))


class ParseLoraLineTest(unittest.TestCase):
    def setUp(self):
        srv._heartbeat = 0
        srv._last_seq = None
        srv._total_expected = 0
        srv._total_received = 0
        srv._packet_loss = 0.0

    def _fc_fields(self, extra=""):
        base = "FC,1,1000,0.1,0.2,0.3,1,2,3,0.01,0.02,0.03,100000000,200000000,50000,1,0"
        return base + extra

    def test_fc_minimal_valid_line(self):
        data = srv.parse_lora_line(self._fc_fields())
        self.assertIsNotNone(data)
        self.assertEqual(data["source"], "FC")
        self.assertEqual(data["seq"], 1)
        self.assertNotIn("sats", data)

    def test_fc_too_few_fields_returns_none(self):
        data = srv.parse_lora_line("FC,1,1000,0.1")
        self.assertIsNone(data)

    def test_fc_sats_field_present_at_idx17(self):
        data = srv.parse_lora_line(self._fc_fields(",7"))
        self.assertEqual(data["sats"], 7)
        self.assertNotIn("rollspeed", data)

    def test_fc_rollspeed_block_requires_len_ge_22(self):
        # idx17=sats, idx18=reserved, idx19..21=rollspeed/pitch/yawspeed
        line = self._fc_fields(",7,0,0.5,0.6,0.7")
        data = srv.parse_lora_line(line)
        self.assertEqual(data["sats"], 7)
        self.assertEqual(data["rollspeed"], 0.5)
        self.assertEqual(data["pitchspeed"], 0.6)
        self.assertEqual(data["yawspeed"], 0.7)

    def test_sh_minimal_valid_line(self):
        data = srv.parse_lora_line("SH,2,2000,1,0,1,0")
        self.assertIsNotNone(data)
        self.assertEqual(data["source"], "SH")
        self.assertEqual(data["health_state"], 1)

    def test_sh_too_few_fields_returns_none(self):
        data = srv.parse_lora_line("SH,2,2000")
        self.assertIsNone(data)

    def test_unknown_source_returns_none(self):
        self.assertIsNone(srv.parse_lora_line("XX,1,2,3"))


if __name__ == "__main__":
    unittest.main()
