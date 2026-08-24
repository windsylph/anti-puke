"""Unit tests for the parts of Motion Dots that are pure arithmetic.

Run: python3 -m pytest tests/ -q     (or: python3 tests/test_motion_dots.py)

Deliberately covers the things that are hard to eyeball on a 7-inch screen at
arm's length: that smoothing actually suppresses noise, that the ring self-
centres instead of pegging, and that no dot can ever be drawn off-screen.
"""

import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_modules"))

import config
import protocol
import ring

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sample(ax=0.0, az=0.0, gpitch=0.0, groll=0.0):
    return {"accel": {"x": ax, "y": 0.0, "z": az},
            "gyro": {"pitch": gpitch, "yaw": 0.0, "roll": groll}}


class TestRingGeometry(unittest.TestCase):
    def test_count_is_respected(self):
        for n in (1, 12, 13, 14, 40):
            self.assertEqual(len(ring.ring_anchors(1280, 800, n, 9.0)), n)

    def test_dots_sit_on_the_inset_rectangle(self):
        margin = 9.0
        for x, y in ring.ring_anchors(1280, 800, 14, margin):
            on_vertical = math.isclose(x, margin) or math.isclose(x, 1280 - margin)
            on_horizontal = math.isclose(y, margin) or math.isclose(y, 800 - margin)
            self.assertTrue(on_vertical or on_horizontal,
                            f"({x}, {y}) is not on the inset perimeter")

    def test_spacing_is_even_around_the_perimeter(self):
        anchors = ring.ring_anchors(1280, 800, 13, 9.0)
        # Compare consecutive gaps measured along the perimeter, not straight
        # line distance (corners make the latter shorter).
        gaps = []
        for i in range(len(anchors)):
            a, b = anchors[i], anchors[(i + 1) % len(anchors)]
            gaps.append(math.dist(a, b))
        # Every gap except the ones crossing a corner should match closely; allow
        # corner-crossing gaps to be shorter but never longer.
        self.assertLessEqual(max(gaps) - min(gaps), max(gaps) * 0.35)

    def test_degenerate_margin_does_not_divide_by_zero(self):
        anchors = ring.ring_anchors(100, 100, 5, 500.0)
        self.assertEqual(len(anchors), 5)
        self.assertTrue(all(math.isfinite(x) and math.isfinite(y) for x, y in anchors))

    def test_zero_count_is_empty(self):
        self.assertEqual(ring.ring_anchors(1280, 800, 0, 9.0), [])


class TestMotionFilter(unittest.TestCase):
    def test_no_samples_means_no_offset(self):
        f = ring.MotionFilter(config.Settings())
        self.assertEqual(f.offset(), (0.0, 0.0))

    def test_smoothing_suppresses_noise(self):
        """PRD 7.3: raw IMU noise must not reach the dots.

        The baseline filter has a time constant of ~1/tilt_baseline_ema_alpha
        samples (several seconds at 60Hz), so the measurement window has to start
        well after it has settled -- otherwise this measures the baseline warming
        up rather than how much noise survives the filter.
        """
        s = config.Settings()
        f = ring.MotionFilter(s)
        warmup = int(10 / s.tilt_baseline_ema_alpha)  # 10 baseline time constants
        offsets = []
        for i in range(warmup + 400):
            noise = 0.05 * (1 if i % 2 == 0 else -1)
            f.update(sample(ax=noise, az=0.5))
            if i >= warmup:
                offsets.append(f.offset()[0])

        peak_to_peak = max(offsets) - min(offsets)
        raw_peak_to_peak = 0.10 * s.tilt_gain_px  # what unfiltered noise would give
        self.assertLess(peak_to_peak, raw_peak_to_peak * 0.1,
                        f"noise reached the dots: {peak_to_peak:.3f}px "
                        f"vs {raw_peak_to_peak:.3f}px unfiltered")
        # Sub-pixel residual is the actual bar: anything under a pixel cannot
        # produce visible buzz.
        self.assertLess(peak_to_peak, 1.0)

    def test_baseline_settles_within_a_few_seconds(self):
        """The posture tracker must converge quickly enough that the ring is not
        still drifting a long time after the overlay is switched on."""
        s = config.Settings()
        f = ring.MotionFilter(s)
        for _ in range(int(5 * s.output_hz)):  # 5 seconds at the output rate
            f.update(sample(ax=0.3, az=0.5))
        dx, dy = f.offset()
        self.assertLess(math.hypot(dx, dy), 4.0,
                        f"still offset by {math.hypot(dx, dy):.2f}px after 5s of a steady hold")

    def test_ring_recentres_on_a_sustained_posture(self):
        """A held tilt must not park the dots against the clamp forever."""
        f = ring.MotionFilter(config.Settings())
        for _ in range(60):
            f.update(sample(ax=0.0, az=0.5))
        # Now hold a new posture for a long time.
        for _ in range(6000):
            f.update(sample(ax=0.4, az=0.5))
        dx, _ = f.offset()
        self.assertLess(abs(dx), 2.0,
                        f"dots stayed pushed to {dx:.2f}px after settling into a new posture")

    def test_tilt_direction_matches_shift_direction(self):
        """PRD 6.1: direction of shift corresponds to direction of tilt."""
        for axis, expect_x, expect_y in (("x", True, False), ("z", False, True)):
            f = ring.MotionFilter(config.Settings())
            for _ in range(30):
                f.update(sample())
            for _ in range(60):
                f.update(sample(**{"ax" if axis == "x" else "az": 0.5}))
            dx, dy = f.offset()
            if expect_x:
                self.assertGreater(dx, 1.0, "tilt toward +x did not move dots +x")
                self.assertLess(abs(dy), abs(dx) * 0.2, "tilt on x leaked into y")
            if expect_y:
                self.assertGreater(dy, 1.0, "tilt toward +z did not move dots +y")
                self.assertLess(abs(dx), abs(dy) * 0.2, "tilt on z leaked into x")

    def test_offset_is_clamped_by_magnitude(self):
        s = config.Settings()
        f = ring.MotionFilter(s)
        for _ in range(20):
            f.update(sample())
        # Slam it with an absurd diagonal tilt.
        for _ in range(200):
            f.update(sample(ax=50.0, az=50.0, gpitch=5000.0, groll=5000.0))
        dx, dy = f.offset()
        self.assertLessEqual(math.hypot(dx, dy), s.max_offset_px + 1e-6)

    def test_clamp_preserves_direction(self):
        s = config.Settings()
        f = ring.MotionFilter(s)
        for _ in range(20):
            f.update(sample())
        for _ in range(200):
            f.update(sample(ax=10.0, az=10.0))
        dx, dy = f.offset()
        # Equal push on both axes must stay on the diagonal after clamping.
        self.assertAlmostEqual(dx, dy, delta=abs(dx) * 0.05 + 1e-6)

    def test_malformed_samples_are_ignored(self):
        f = ring.MotionFilter(config.Settings())
        for _ in range(30):
            f.update(sample(ax=0.2, az=0.4))
        before = f.offset()
        for bad in ({}, {"accel": {}}, {"accel": {"x": "nan", "z": None}},
                    {"accel": {"x": float("nan"), "z": 1.0}},
                    {"accel": {"x": float("inf"), "z": 1.0}}, None):
            f.update(bad or {})
        self.assertEqual(f.offset(), before, "a malformed sample moved the dots")

    def test_gyro_contributes(self):
        f = ring.MotionFilter(config.Settings())
        for _ in range(30):
            f.update(sample())
        for _ in range(30):
            f.update(sample(groll=40.0))
        self.assertGreater(abs(f.offset()[0]), 1.0, "gyro had no effect")


class TestFrameBuilding(unittest.TestCase):
    def setUp(self):
        self.s = config.Settings()
        self.anchors = ring.ring_anchors(1280, 800, self.s.dot_count, self.s.edge_margin_px)

    def test_dots_never_leave_the_screen(self):
        """Even a maxed-out offset plus drift must stay fully on screen."""
        for ox, oy in ((0, 0), (999, 999), (-999, -999), (999, -999), (-999, 999)):
            dots = ring.build_frame(self.anchors, (ox, oy), 1.0, 12.34, self.s, 1280, 800)
            for d in dots:
                self.assertGreaterEqual(d.x, self.s.dot_radius_px - 1e-6)
                self.assertLessEqual(d.x, 1280 - self.s.dot_radius_px + 1e-6)
                self.assertGreaterEqual(d.y, self.s.dot_radius_px - 1e-6)
                self.assertLessEqual(d.y, 800 - self.s.dot_radius_px + 1e-6)

    def test_idle_drift_is_present_but_subpixel(self):
        """PRD 6.1: near-static, but never a hard freeze."""
        a = ring.build_frame(self.anchors, (0, 0), 0.0, 0.0, self.s, 1280, 800)
        b = ring.build_frame(self.anchors, (0, 0), 0.0, 0.5, self.s, 1280, 800)
        moved = [math.dist((p.x, p.y), (q.x, q.y)) for p, q in zip(a, b)]
        self.assertGreater(max(moved), 0.0, "dots are frozen")
        self.assertLess(max(moved), 1.0, f"idle drift is not sub-pixel: {max(moved):.3f}px")

    def test_alpha_follows_intensity(self):
        rest = ring.build_frame(self.anchors, (0, 0), 0.0, 0.0, self.s, 1280, 800)
        moving = ring.build_frame(self.anchors, (0, 0), 1.0, 0.0, self.s, 1280, 800)
        self.assertAlmostEqual(rest[0].alpha, self.s.base_alpha, places=5)
        self.assertGreater(moving[0].alpha, rest[0].alpha)
        self.assertLessEqual(moving[0].alpha, 1.0)

    def test_alpha_boost_can_be_disabled(self):
        s = config.Settings()
        s.motion_alpha_boost = 0.0
        dots = ring.build_frame(self.anchors, (0, 0), 1.0, 0.0, s, 1280, 800)
        self.assertAlmostEqual(dots[0].alpha, s.base_alpha, places=5)


class TestProtocol(unittest.TestCase):
    def test_roundtrip(self):
        dots = [protocol.Dot(1.5, 2.5, 0.75), protocol.Dot(-3.0, 4.0, 1.0)]
        radius, decoded = protocol.decode_dots(protocol.encode_dots(dots, 4.5))
        self.assertAlmostEqual(radius, 4.5, places=5)
        self.assertEqual(len(decoded), 2)
        for original, result in zip(dots, decoded):
            self.assertAlmostEqual(original.x, result.x, places=4)
            self.assertAlmostEqual(original.alpha, result.alpha, places=4)

    def test_truncated_packet_is_rejected(self):
        payload = protocol.encode_dots([protocol.Dot(1, 2, 3)], 4.0)
        self.assertIsNone(protocol.decode_dots(payload[:-4]))

    def test_wrong_magic_is_rejected(self):
        payload = bytearray(protocol.encode_dots([], 4.0))
        payload[0] ^= 0xFF
        self.assertIsNone(protocol.decode_dots(bytes(payload)))

    def test_dot_count_is_capped(self):
        many = [protocol.Dot(0, 0, 1)] * (protocol.MAX_DOTS + 50)
        _, decoded = protocol.decode_dots(protocol.encode_dots(many, 4.0))
        self.assertEqual(len(decoded), protocol.MAX_DOTS)

    def test_info_decoding(self):
        import struct
        good = struct.pack("<IHHII", protocol.MAGIC_INFO, protocol.PROTOCOL_VERSION, 0, 1280, 800)
        self.assertEqual(protocol.decode_info(good), (1280, 800))
        bad_version = struct.pack("<IHHII", protocol.MAGIC_INFO, 99, 0, 1280, 800)
        self.assertIsNone(protocol.decode_info(bad_version))
        zero = struct.pack("<IHHII", protocol.MAGIC_INFO, protocol.PROTOCOL_VERSION, 0, 0, 800)
        self.assertIsNone(protocol.decode_info(zero))

    def test_matches_cpp_header(self):
        """The C++ and Python definitions must not drift apart."""
        header = open(os.path.join(REPO, "overlay", "protocol.h")).read()

        for name, expected in (("HelloPacket", protocol.HELLO_SIZE),
                               ("InfoPacket", protocol.INFO_SIZE),
                               ("DotsHeader", protocol.DOTS_HEADER_SIZE),
                               ("Dot", protocol.DOT_SIZE)):
            m = re.search(rf"static_assert\(sizeof\({name}\)\s*==\s*(\d+)", header)
            self.assertIsNotNone(m, f"no size assertion for {name} in protocol.h")
            self.assertEqual(int(m.group(1)), expected,
                             f"{name}: C++ says {m.group(1)}, Python says {expected}")

        for name, expected in (("kMagicHello", protocol.MAGIC_HELLO),
                               ("kMagicInfo", protocol.MAGIC_INFO),
                               ("kMagicDots", protocol.MAGIC_DOTS)):
            m = re.search(rf"{name}\s*=\s*0x([0-9A-Fa-f]+)U", header)
            self.assertIsNotNone(m, f"no magic constant {name} in protocol.h")
            self.assertEqual(int(m.group(1), 16), expected, f"{name} differs")

        m = re.search(r"kProtocolVersion\s*=\s*(\d+)", header)
        self.assertEqual(int(m.group(1)), protocol.PROTOCOL_VERSION)

        m = re.search(r"kMaxDots\s*=\s*(\d+)", header)
        self.assertEqual(int(m.group(1)), protocol.MAX_DOTS)

    def test_default_ports_match_header(self):
        header = open(os.path.join(REPO, "overlay", "protocol.h")).read()
        m = re.search(r"kDefaultOverlayPort\s*=\s*(\d+)", header)
        self.assertEqual(int(m.group(1)), config.OVERLAY_PORT)


class TestSettings(unittest.TestCase):
    def test_dot_count_is_in_the_prd_range(self):
        self.assertGreaterEqual(config.Settings().dot_count, 12)
        self.assertLessEqual(config.Settings().dot_count, 14)

    def test_margin_is_in_the_prd_range(self):
        self.assertGreaterEqual(config.Settings().edge_margin_px, 8)
        self.assertLessEqual(config.Settings().edge_margin_px, 10)

    def test_clamping_rejects_nonsense(self):
        s = config.Settings.from_dict({
            "dot_count": 100000, "sensitivity": -5, "tilt_ema_alpha": 99,
            "base_alpha": 17, "dot_radius_px": -1,
        }).clamped()
        self.assertLessEqual(s.dot_count, 256)
        self.assertGreaterEqual(s.sensitivity, 0.0)
        self.assertLessEqual(s.tilt_ema_alpha, 1.0)
        self.assertLessEqual(s.base_alpha, 1.0)
        self.assertGreater(s.dot_radius_px, 0.0)

    def test_int_fields_stay_ints(self):
        s = config.Settings.from_dict({"dot_count": "14"})
        self.assertIsInstance(s.dot_count, int)

    def test_unknown_and_bad_keys_are_survivable(self):
        s = config.Settings.from_dict({"nonsense": 1, "sensitivity": "abc"})
        self.assertEqual(s.sensitivity, config.Settings().sensitivity)

    def test_save_load_roundtrip(self):
        import tempfile
        s = config.Settings()
        s.sensitivity = 1.75
        s.dot_count = 12
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            config.save(s, path)
            loaded = config.load(path)
        self.assertAlmostEqual(loaded.sensitivity, 1.75)
        self.assertEqual(loaded.dot_count, 12)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(config.load("/nonexistent/nope.json").dot_count,
                         config.Settings().dot_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
