import http.client
import json
import os
import sys
import unittest
from http.server import ThreadingHTTPServer
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fc_serial_ws_server as srv


class UplinkHandlerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), srv.UplinkHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        with srv._pending_lock:
            srv._pending_uplink.clear()
        srv._seq_counter._v = 1

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def _post(self, path, body):
        conn = self._conn()
        payload = json.dumps(body).encode("utf-8")
        conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def _get(self, path):
        conn = self._conn()
        conn.request("GET", path)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data


class CommonEndpointsTest(UplinkHandlerTestBase):
    def test_health(self):
        status, data = self._get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_meta_lists_scopes(self):
        status, data = self._get("/api/uplink/meta")
        self.assertEqual(status, 200)
        self.assertIn("attitude_timeout_ms", data["scopes"]["cfs_core"])
        self.assertIn("downlink_protocol", data["scopes"]["lora_tdm"])

    def test_unknown_get_path_404(self):
        status, _ = self._get("/no/such/path")
        self.assertEqual(status, 404)

    def test_unknown_post_path_404(self):
        status, _ = self._post("/no/such/path", {})
        self.assertEqual(status, 404)

    def test_invalid_json_body_400(self):
        conn = self._conn()
        conn.request("POST", "/api/uplink/config", body=b"{not json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertIn("error", data)

    def test_options_preflight(self):
        conn = self._conn()
        conn.request("OPTIONS", "/api/uplink/config")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "*")


class ConfigEndpointTest(UplinkHandlerTestBase):
    def test_valid_config_returns_200_and_queues(self):
        status, data = self._post("/api/uplink/config",
                                   {"scope": "cfs_core", "param": "attitude_timeout_ms", "value": 500})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["queued"])
        self.assertFalse(data["force"])
        with srv._pending_lock:
            self.assertEqual(len(srv._pending_uplink), 1)

    def test_force_flag_reflected_in_response(self):
        status, data = self._post("/api/uplink/config",
                                   {"scope": "cfs_core", "param": "attitude_timeout_ms",
                                    "value": 500, "force": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["force"])

    def test_unknown_scope_400(self):
        status, data = self._post("/api/uplink/config",
                                   {"scope": "bogus", "param": "x", "value": 1})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_unknown_param_400(self):
        status, data = self._post("/api/uplink/config",
                                   {"scope": "cfs_core", "param": "bogus", "value": 1})
        self.assertEqual(status, 400)
        self.assertIn("available", data)

    def test_non_integer_value_400(self):
        status, data = self._post("/api/uplink/config",
                                   {"scope": "cfs_core", "param": "attitude_timeout_ms",
                                    "value": "not-a-number"})
        self.assertEqual(status, 400)

    def test_value_out_of_uint32_range_400(self):
        status, data = self._post("/api/uplink/config",
                                   {"scope": "cfs_core", "param": "attitude_timeout_ms",
                                    "value": 0x100000000})
        self.assertEqual(status, 400)


class RouteEndpointTest(UplinkHandlerTestBase):
    def test_valid_route_returns_200_and_queues(self):
        status, data = self._post("/api/uplink/route",
                                   {"route_type": "mission", "waypoints": [[1, 2, 3], [4, 5, 6]]})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["waypoint_count"], 2)
        with srv._pending_lock:
            self.assertEqual(len(srv._pending_uplink), 1)

    def test_unknown_route_type_400(self):
        status, data = self._post("/api/uplink/route",
                                   {"route_type": "bogus", "waypoints": [[1, 2, 3]]})
        self.assertEqual(status, 400)

    def test_too_many_waypoints_400(self):
        wps = [[float(i), 0.0, 0.0] for i in range(srv.MAX_ROUTE_WAYPOINTS + 1)]
        status, data = self._post("/api/uplink/route", {"route_type": "mission", "waypoints": wps})
        self.assertEqual(status, 400)

    def test_empty_waypoints_400(self):
        status, data = self._post("/api/uplink/route", {"route_type": "mission", "waypoints": []})
        self.assertEqual(status, 400)

    def test_malformed_waypoint_400(self):
        status, data = self._post("/api/uplink/route",
                                   {"route_type": "mission", "waypoints": [["a", "b", "c"]]})
        self.assertEqual(status, 400)


class RecoveryEndpointTest(UplinkHandlerTestBase):
    def test_valid_recovery_returns_200_with_token(self):
        status, data = self._post("/api/uplink/recovery", {"payload_hex": "0102"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("request_token", data)
        self.assertNotEqual(data["request_token"], 0)
        with srv._pending_lock:
            self.assertEqual(len(srv._pending_uplink), 1)

    def test_missing_payload_hex_still_succeeds(self):
        status, data = self._post("/api/uplink/recovery", {})
        self.assertEqual(status, 200)
        self.assertIn("request_token", data)

    def test_invalid_payload_hex_400(self):
        status, data = self._post("/api/uplink/recovery", {"payload_hex": "not-hex"})
        self.assertEqual(status, 400)


class RetxIndexFlushTest(UplinkHandlerTestBase):
    """BL-14(2026-07-22): 4x 슬롯 재전송 사본마다 Flags bits[2:1]=RETX_IDX(0~3)가
    실리고, flags가 달라진 만큼 CRC도 사본별로 재계산돼야 한다
    (mission_app_runtime_spec.md §18.4.3.1)."""

    def _capture_flush_all(self):
        sent = []
        original = srv._lora_send
        srv._lora_send = sent.append
        try:
            for _ in range(srv._UPLINK_RETX):
                srv._flush_pending_uplink()
        finally:
            srv._lora_send = original
        return sent

    def test_each_slot_carries_incrementing_retx_idx_and_valid_crc(self):
        status, _ = self._post("/api/uplink/config",
                                {"scope": "cfs_core", "param": "attitude_timeout_ms", "value": 500})
        self.assertEqual(status, 200)

        sent = self._capture_flush_all()
        self.assertEqual(len(sent), srv._UPLINK_RETX)

        for i, frame in enumerate(sent):
            parts = frame.split(",")
            # UP,<ver>,<class>,<seq>,<flags>,<payload_hex>,<crc>
            flags = int(parts[4])
            self.assertEqual((flags >> srv._RETX_IDX_SHIFT) & srv._RETX_IDX_MASK, i,
                             f"slot {i + 1}: RETX_IDX mismatch in {frame}")
            canonical = ",".join(parts[:-1])
            self.assertEqual(int(parts[-1], 16), srv._crc16(canonical.encode("ascii")),
                             f"slot {i + 1}: CRC not recomputed for {frame}")

        # RETX_IDX 외 비트(auth level 등)는 사본 간 동일해야 함
        base_masks = {int(f.split(",")[4]) & ~(srv._RETX_IDX_MASK << srv._RETX_IDX_SHIFT)
                      for f in sent}
        self.assertEqual(len(base_masks), 1)

        with srv._pending_lock:
            self.assertEqual(len(srv._pending_uplink), 0)


class UplinkQueueCapTest(UplinkHandlerTestBase):
    """BL-23(2026-07-22): 다운링크 단절로 flush가 안 불려도 큐가 무한정
    쌓이지 않도록 상한(16)을 둔다 — 초과 시 가장 오래된 항목을 버리고
    새 명령은 그대로 accept(HTTP 에러 아님)."""

    def test_over_capacity_drops_oldest_and_still_accepts_new(self):
        seqs = []
        for i in range(srv._UPLINK_QUEUE_MAX_SIZE + 3):
            status, data = self._post("/api/uplink/config",
                                       {"scope": "cfs_core", "param": "attitude_timeout_ms",
                                        "value": 500 + i})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            seqs.append(data["seq"])

        with srv._pending_lock:
            self.assertEqual(len(srv._pending_uplink), srv._UPLINK_QUEUE_MAX_SIZE)
            remaining_seqs = [item[0] for item in srv._pending_uplink]

        # 가장 오래된 3개(첫 3개 seq)는 버려지고, 최신 항목들만 남아야 함
        self.assertEqual(remaining_seqs, seqs[3:])


class ResendEndpointTest(UplinkHandlerTestBase):
    """BL-24(2026-07-22): UFB=1 재전송은 새 seq 재조립이 아니라 캐시된
    원본을 같은 seq로 재큐잉(진짜 재전송) — 원본이 이미 수락됐어도
    기체 DUPLICATE 방어로 이중 실행이 불가능해진다."""

    def test_resend_requeues_same_seq_and_payload(self):
        status, data = self._post("/api/uplink/config",
                                   {"scope": "cfs_core", "param": "attitude_timeout_ms", "value": 500})
        self.assertEqual(status, 200)
        orig_seq = data["seq"]

        with srv._pending_lock:
            orig_item = [it[:4] for it in srv._pending_uplink if it[0] == orig_seq][0]

        status, data = self._post("/api/uplink/resend", {"seq": orig_seq})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["resend"])
        self.assertEqual(data["seq"], orig_seq)   # 새 seq 발급 없음

        with srv._pending_lock:
            matches = [it[:4] for it in srv._pending_uplink if it[0] == orig_seq]
        self.assertEqual(len(matches), 2)          # 원본 + 재전송본
        self.assertEqual(matches[0], matches[1])   # seq/payload/class/flags 완전 동일

    def test_resend_unknown_seq_404(self):
        status, data = self._post("/api/uplink/resend", {"seq": 60000})
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_resend_missing_seq_400(self):
        status, data = self._post("/api/uplink/resend", {})
        self.assertEqual(status, 400)


class CounterEndpointTest(UplinkHandlerTestBase):
    """BL-CTR(2026-07-22, §18.4.6.7): counter management(class 7) 전송 —
    payload = scope(1)+action(1,RESET=0)+request_token(4,LE), Level 3."""

    def test_valid_counter_reset_queues_class7_frame(self):
        status, data = self._post("/api/uplink/counter", {"scope": "lora_tdm"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["scope"], "lora_tdm")
        self.assertEqual(data["action"], "reset")
        self.assertNotEqual(data["request_token"], 0)

        with srv._pending_lock:
            self.assertEqual(len(srv._pending_uplink), 1)
            seq, payload, cmd_class, flags, _remaining = srv._pending_uplink[0]

        self.assertEqual(cmd_class, srv.UPLINK_CLASS_COUNTER_MGMT)
        self.assertEqual(len(payload), 6)
        self.assertEqual(payload[0], srv.COUNTER_SCOPES["lora_tdm"])
        self.assertEqual(payload[1], srv.COUNTER_ACTION_RESET)
        # request_token은 기체가 Payload[2..5] LE로 파싱 — 0이면 Level 3 게이트에서 거부됨
        self.assertEqual(int.from_bytes(payload[2:6], "little"), data["request_token"])
        # Level 3 → flags bits[7:6] = 3
        self.assertEqual((flags >> 6) & 0x3, 3)

    def test_all_four_scopes_accepted(self):
        for scope_name, scope_val in srv.COUNTER_SCOPES.items():
            status, data = self._post("/api/uplink/counter", {"scope": scope_name})
            self.assertEqual(status, 200, f"scope={scope_name}")
            self.assertTrue(data["ok"], f"scope={scope_name}")

    def test_unknown_scope_400(self):
        status, data = self._post("/api/uplink/counter", {"scope": "bogus"})
        self.assertEqual(status, 400)
        self.assertIn("available", data)


class FlightModeEndpointTest(UplinkHandlerTestBase):
    """BL-44(2026-07-24, §18.4.6.8): flight mode base 명령(class 8) 전송 —
    payload = flight_mode(1)+waypoint_start_index(1)+request_token(4,LE), Level 3."""

    def test_hover_queues_class8_frame(self):
        status, data = self._post("/api/uplink/flight_mode", {"mode": "hover"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "hover")
        self.assertEqual(data["waypoint_start_index"], 0)
        self.assertNotEqual(data["request_token"], 0)

        with srv._pending_lock:
            self.assertEqual(len(srv._pending_uplink), 1)
            seq, payload, cmd_class, flags, _remaining = srv._pending_uplink[0]

        self.assertEqual(cmd_class, srv.UPLINK_CLASS_FLIGHT_MODE)
        self.assertEqual(len(payload), 6)
        self.assertEqual(payload[0], srv.FLIGHT_MODES["hover"])
        self.assertEqual(payload[1], 0)
        self.assertEqual(int.from_bytes(payload[2:6], "little"), data["request_token"])
        # Level 3 → flags bits[7:6] = 3
        self.assertEqual((flags >> 6) & 0x3, 3)

    def test_waypoint_with_start_index(self):
        status, data = self._post("/api/uplink/flight_mode",
                                  {"mode": "waypoint", "waypoint_start_index": 7})
        self.assertEqual(status, 200)
        with srv._pending_lock:
            _seq, payload, _cls, _flags, _remaining = srv._pending_uplink[0]
        self.assertEqual(payload[0], srv.FLIGHT_MODES["waypoint"])
        self.assertEqual(payload[1], 7)

    def test_land_accepted(self):
        status, data = self._post("/api/uplink/flight_mode", {"mode": "land"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_unknown_mode_400(self):
        status, data = self._post("/api/uplink/flight_mode", {"mode": "bogus"})
        self.assertEqual(status, 400)
        self.assertIn("available", data)

    def test_nonzero_waypoint_index_rejected_for_hover(self):
        status, data = self._post("/api/uplink/flight_mode",
                                  {"mode": "hover", "waypoint_start_index": 3})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_nonzero_waypoint_index_rejected_for_land(self):
        status, data = self._post("/api/uplink/flight_mode",
                                  {"mode": "land", "waypoint_start_index": 1})
        self.assertEqual(status, 400)

    def test_waypoint_index_out_of_uint8_range_400(self):
        status, data = self._post("/api/uplink/flight_mode",
                                  {"mode": "waypoint", "waypoint_start_index": 256})
        self.assertEqual(status, 400)

    def test_force_flag_sets_force_bit(self):
        status, data = self._post("/api/uplink/flight_mode",
                                  {"mode": "waypoint", "waypoint_start_index": 1, "force": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["force"])
        with srv._pending_lock:
            _seq, _payload, _cls, flags, _remaining = srv._pending_uplink[0]
        self.assertEqual(flags & srv.UPLINK_FORCE_FLAG, srv.UPLINK_FORCE_FLAG)

    def test_default_force_is_false_and_bit_unset(self):
        status, data = self._post("/api/uplink/flight_mode", {"mode": "hover"})
        self.assertEqual(status, 200)
        self.assertFalse(data["force"])
        with srv._pending_lock:
            _seq, _payload, _cls, flags, _remaining = srv._pending_uplink[0]
        self.assertEqual(flags & srv.UPLINK_FORCE_FLAG, 0)


class RouteReadbackStatusEndpointTest(UplinkHandlerTestBase):
    """0x1913 waypoint readback 왕복 상태 조회(GET) — spec §4.3 "미완(후속 검토):
    ground 측 GUI 패널" 항목. RouteReadbackAssembler(lora_protocol_v2.py)가 이미
    파싱/재조립하지만 콘솔 출력만 하던 것을 모듈 상태로 노출."""

    def setUp(self):
        super().setUp()
        srv._route_readback = srv.RouteReadbackAssembler()
        srv._route_readback_state.update({
            "status": "idle", "route_type": None, "progress": "0/0",
            "waypoints": None, "updated_ms": None,
        })

    def _fake_frame(self, page_index, total_pages, waypoints, route_type=1):
        return srv.Dl2Frame(
            seq=0, flags=srv.DL2_FLAG_WAYPOINT, ufb=0, ts_ms=0,
            roll_rad=0.0, pitch_rad=0.0, yaw_rad=0.0,
            x_m=0.0, y_m=0.0, z_m=0.0, vx_mps=0.0, vy_mps=0.0, vz_mps=0.0,
            lat_e7=0, lon_e7=0, alt_mm=0, fix=0, sats=0,
            health=0, fault=0, linkstate=0,
            sys_time_unix_usec=None, uplink_last_seq=0, uplink_boot_count=0,
            wp_route_type=route_type, wp_page_index=page_index,
            wp_total_pages=total_pages, wp_waypoints=waypoints,
        )

    def test_idle_before_any_readback(self):
        status, data = self._get("/api/uplink/route_readback")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "idle")

    def test_pending_after_partial_pages(self):
        srv.dl2_frame_to_data(self._fake_frame(0, 2, [(0.0, -10.0, 3.0), (2.0, -10.0, 3.0)]))
        status, data = self._get("/api/uplink/route_readback")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["progress"], "1/2")

    def test_complete_after_all_pages(self):
        srv.dl2_frame_to_data(self._fake_frame(0, 2, [(0.0, -10.0, 3.0), (2.0, -10.0, 3.0)]))
        srv.dl2_frame_to_data(self._fake_frame(1, 2, [(4.0, -10.0, 3.0)]))
        status, data = self._get("/api/uplink/route_readback")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["route_type"], 1)
        self.assertEqual(len(data["waypoints"]), 3)
        self.assertEqual(data["waypoints"][2], [4.0, -10.0, 3.0])


if __name__ == "__main__":
    unittest.main()
