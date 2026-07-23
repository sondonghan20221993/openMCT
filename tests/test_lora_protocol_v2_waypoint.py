import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lora_protocol_v2 import (
    Dl2Frame, DownlinkStream, RouteReadbackAssembler,
    decode_dl2, encode_dl2, build_ack2,
    DL2_FLAG_WAYPOINT, DL2_WAYPOINTS_PER_PAGE,
)


def make_base_frame(**overrides):
    base = dict(
        seq=1, flags=0, ufb=0, ts_ms=1000,
        roll_rad=0.0, pitch_rad=0.0, yaw_rad=0.0,
        x_m=0.0, y_m=0.0, z_m=0.0,
        vx_mps=0.0, vy_mps=0.0, vz_mps=0.0,
        lat_e7=0, lon_e7=0, alt_mm=0,
        fix=3, sats=8, health=0, fault=0, linkstate=1,
    )
    base.update(overrides)
    return Dl2Frame(**base)


class WaypointPageRoundTripTest(unittest.TestCase):
    def test_encode_decode_full_page(self):
        frame = make_base_frame(
            wp_route_type=1, wp_page_index=0, wp_total_pages=8,
            wp_waypoints=[(1.5, 2.5, 3.5), (4.5, 5.5, 6.5)],
        )
        wire = encode_dl2(frame)
        decoded = decode_dl2(wire)

        self.assertTrue(decoded.has_waypoint_page)
        self.assertEqual(decoded.wp_route_type, 1)
        self.assertEqual(decoded.wp_page_index, 0)
        self.assertEqual(decoded.wp_total_pages, 8)
        self.assertEqual(len(decoded.wp_waypoints), 2)
        self.assertAlmostEqual(decoded.wp_waypoints[0][0], 1.5, places=5)
        self.assertAlmostEqual(decoded.wp_waypoints[1][2], 6.5, places=5)

    def test_encode_decode_last_odd_page(self):
        frame = make_base_frame(
            wp_route_type=1, wp_page_index=7, wp_total_pages=8,
            wp_waypoints=[(9.0, 9.0, 9.0)],
        )
        wire = encode_dl2(frame)
        decoded = decode_dl2(wire)

        self.assertEqual(len(decoded.wp_waypoints), 1)
        self.assertAlmostEqual(decoded.wp_waypoints[0][0], 9.0, places=5)

    def test_no_waypoint_flag_when_absent(self):
        frame = make_base_frame()
        wire = encode_dl2(frame)
        decoded = decode_dl2(wire)

        self.assertFalse(decoded.has_waypoint_page)
        self.assertIsNone(decoded.wp_waypoints)

    def test_downlink_stream_parses_waypoint_frame(self):
        frame = make_base_frame(
            wp_route_type=1, wp_page_index=0, wp_total_pages=1,
            wp_waypoints=[(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)],
        )
        wire = encode_dl2(frame)
        stream = DownlinkStream()
        events = stream.feed(wire)

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].has_waypoint_page)


class RouteReadbackAssemblerTest(unittest.TestCase):
    def test_assembles_full_mission_16wp_8pages(self):
        asm = RouteReadbackAssembler()
        result = None
        for page in range(8):
            wps = [(float(page * 2), 0.0, 0.0), (float(page * 2 + 1), 0.0, 0.0)]
            event = make_base_frame(
                wp_route_type=1, wp_page_index=page, wp_total_pages=8, wp_waypoints=wps,
            )
            event.flags |= DL2_FLAG_WAYPOINT
            result = asm.feed(event)

        self.assertIsNotNone(result)
        self.assertEqual(len(result), 16)
        self.assertEqual(result[0], (0.0, 0.0, 0.0))
        self.assertEqual(result[15], (15.0, 0.0, 0.0))

    def test_incomplete_until_all_pages_received(self):
        asm = RouteReadbackAssembler()
        event = make_base_frame(
            wp_route_type=1, wp_page_index=0, wp_total_pages=2, wp_waypoints=[(1.0, 1.0, 1.0)],
        )
        event.flags |= DL2_FLAG_WAYPOINT
        result = asm.feed(event)
        self.assertIsNone(result)
        self.assertEqual(asm.progress, "1/2")

    def test_new_session_discards_previous_progress(self):
        asm = RouteReadbackAssembler()
        e1 = make_base_frame(wp_route_type=1, wp_page_index=0, wp_total_pages=2,
                              wp_waypoints=[(1.0, 1.0, 1.0)])
        e1.flags |= DL2_FLAG_WAYPOINT
        asm.feed(e1)
        self.assertEqual(asm.progress, "1/2")

        # 새 readback 세션(total_pages 다름) 시작 — 이전 진행분 폐기되어야 함
        e2 = make_base_frame(wp_route_type=1, wp_page_index=0, wp_total_pages=1,
                              wp_waypoints=[(2.0, 2.0, 2.0), (3.0, 3.0, 3.0)])
        e2.flags |= DL2_FLAG_WAYPOINT
        result = asm.feed(e2)
        self.assertEqual(result, [(2.0, 2.0, 2.0), (3.0, 3.0, 3.0)])

    def test_ignores_non_waypoint_frame(self):
        asm = RouteReadbackAssembler()
        event = make_base_frame()  # flags=0, no waypoint block
        result = asm.feed(event)
        self.assertIsNone(result)
        self.assertEqual(asm.progress, "0/0")


if __name__ == "__main__":
    unittest.main()
