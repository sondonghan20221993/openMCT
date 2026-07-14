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


if __name__ == "__main__":
    unittest.main()
