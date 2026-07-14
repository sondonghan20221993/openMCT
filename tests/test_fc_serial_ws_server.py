import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fc_serial_ws_server as srv


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
