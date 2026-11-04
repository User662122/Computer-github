"""Tests for the dirty-tile screen streaming engine and the cursor overlay.

The point of the engine is bandwidth: a static screen must cost nothing, a
small change must cost a small update, and a reconnecting viewer must be able
to resume instead of downloading everything again.
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".github"))

try:
    import numpy as np
except ImportError:                       # pragma: no cover
    raise unittest.SkipTest("numpy is required")

try:
    import cv2
except ImportError:                       # pragma: no cover
    cv2 = None

import screen_delta


def desktop(width=640, height=360, shade=240):
    """A flat "document" frame (BGR)."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:] = (shade, shade, shade)
    return frame


def document():
    """A page with text lines, so changes are not a flat grey rectangle."""
    frame = desktop()
    for line in range(8):
        frame[40 + line * 30:52 + line * 30, 40:560] = (20, 20, 20)
    return frame


def with_text(frame, y=100, x=40, width=260):
    copy = frame.copy()
    copy[y:y + 12, x:x + width] = (10, 10, 10)
    return copy


@unittest.skipIf(cv2 is None, "opencv is required for the delta encoder")
class TileEncoderTests(unittest.TestCase):
    def setUp(self):
        self.encoder = screen_delta.TileEncoder(webp=False)

    def test_first_frame_is_sent_whole(self):
        rects = self.encoder.full_frame(desktop(), scale_to=640)
        self.assertTrue(rects)
        self.assertEqual(sum(r["h"] for r in rects), 360)
        self.assertTrue(all(r["w"] == 640 for r in rects))

    def test_static_screen_costs_nothing(self):
        base = desktop()
        self.encoder.full_frame(base, scale_to=640)
        self.assertEqual(self.encoder.encode(base, scale_to=640), [])

    def test_small_change_costs_a_small_update(self):
        base = document()
        full = sum(len(r["data"]) for r in self.encoder.full_frame(base, scale_to=640))
        rects = self.encoder.encode(with_text(base), scale_to=640)
        self.assertTrue(rects)
        changed = sum(len(r["data"]) for r in rects)
        self.assertLess(changed, full / 2,
                        f"one changed line should be much smaller than the frame ({changed} vs {full})")
        for rect in rects:                    # tight around the changed text
            self.assertLess(rect["h"], 360)
            self.assertIn(rect["mime"], ("image/jpeg", "image/webp"))

    def test_noise_below_threshold_is_ignored(self):
        base = desktop()
        self.encoder.full_frame(base, scale_to=640)
        noisy = base.copy()
        noisy[50:60, 50:60] = np.clip(noisy[50:60, 50:60].astype(int) + 3, 0, 255)
        self.assertEqual(self.encoder.encode(noisy, scale_to=640), [])

    def test_neighbouring_tiles_merge_into_one_rectangle(self):
        base = desktop()
        self.encoder.full_frame(base, scale_to=640)
        block = base.copy()
        block[100:180, 100:260] = (0, 0, 0)      # a solid rectangle of change
        rects = self.encoder.encode(block, scale_to=640)
        self.assertEqual(len(rects), 1, rects)

    def test_fragmented_change_coalesces_to_one_picture(self):
        base = desktop()
        self.encoder.full_frame(base, scale_to=640)
        messy = base.copy()
        for y in range(10, 350, 200):          # scattered single tiles
            for x in range(10, 630, 200):
                messy[y:y + 6, x:x + 6] = (0, 0, 200)
        rects = self.encoder.rectangles(
            self.encoder.changed_cells(cv2.cvtColor(messy, cv2.COLOR_BGR2GRAY)),
            640, 360, limit=2)
        self.assertEqual(len(rects), 1, "over the budget everything coalesces")

    def test_resolution_is_capped(self):
        rects = self.encoder.full_frame(desktop(width=1920, height=1080), scale_to=1920)
        self.assertTrue(rects)
        self.assertLessEqual(max(r["w"] for r in rects), screen_delta.MAX_TILE_WIDTH)

    def test_scale_to_is_honoured(self):
        rects = self.encoder.full_frame(desktop(width=1920, height=1080), scale_to=480)
        self.assertEqual(max(r["w"] for r in rects), 480)
        self.assertEqual(self.encoder.width, 480)

    def test_reset_starts_from_scratch(self):
        base = desktop()
        self.encoder.full_frame(base, scale_to=640)
        self.encoder.reset()
        rects = self.encoder.encode(base, scale_to=640)
        self.assertTrue(rects, "after a reset the encoder has no reference frame")
        self.assertEqual(self.encoder.encode(base, scale_to=640), [])


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@unittest.skipIf(cv2 is None, "opencv is required for the delta encoder")
class ScreenHubTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.frame = desktop(320, 240)
        self.lock = threading.Lock()
        self.hub = screen_delta.ScreenHub(self.capture, scale_to=320,
                                          encoder=screen_delta.TileEncoder(webp=False),
                                          clock=self.clock)
        self.addCleanup(self.hub.stop)

    def capture(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def set_frame(self, frame):
        with self.lock:
            self.frame = frame

    def poll(self, hub, since, timeout=2.0):
        return hub.frames_since(since, timeout=timeout)

    def test_first_viewer_gets_a_full_frame(self):
        self.hub.start()
        result = self.poll(self.hub, 0)
        self.assertTrue(result["full"])
        self.assertEqual(result["reason"], "initial")
        self.assertTrue(result["r"])
        self.assertEqual(result["w"], 320)
        self.assertEqual(result["h"], 240)

    def test_idle_screen_sends_no_pixels(self):
        self.hub.start()
        first = self.poll(self.hub, 0)
        second = self.poll(self.hub, first["seq"], timeout=0.6)
        self.assertEqual(second["r"], [])
        self.assertFalse(second["full"])
        # Full frames are counted separately from incremental updates.
        stats = self.hub.stats()
        self.assertEqual(stats["bytes_sent"], 0,
                         "an idle screen must not transfer anything")
        self.assertGreaterEqual(stats["bytes_full"], sum(r["n"] for r in first["r"]))

    def test_changed_region_is_delta_encoded(self):
        self.hub.start()
        first = self.poll(self.hub, 0)
        self.set_frame(with_text(self.frame, y=100, width=120))
        update = self.poll(self.hub, first["seq"], timeout=3)
        self.assertTrue(update["r"], "a change must reach the viewer")
        changed = sum(r["n"] for r in update["r"])
        full = sum(r["n"] for r in first["r"])
        self.assertLess(changed, full)

    def test_tiles_are_fetchable_and_expire(self):
        self.hub.start()
        first = self.poll(self.hub, 0)
        rect = first["r"][0]
        blob = self.hub.tile(rect["s"], rect["i"])
        self.assertIsNotNone(blob)
        self.assertEqual(blob[1], "image/jpeg")
        self.assertIsNone(self.hub.tile(first["seq"] + 500, 99))

    def test_reconnecting_viewer_resumes_from_the_last_full_frame(self):
        self.hub.start()
        first = self.poll(self.hub, 0)
        self.set_frame(with_text(self.frame))
        self.poll(self.hub, first["seq"], timeout=3)
        # A phone that was away asks for everything it knew plus nothing new.
        again = self.poll(self.hub, 0)
        self.assertTrue(again["full"])
        self.assertEqual(sum(r["n"] for r in again["r"]),
                         sum(r["n"] for r in first["r"]))

    def test_viewer_ahead_of_the_server_resyncs(self):
        self.hub.start()
        first = self.poll(self.hub, 0)
        far_future = first["seq"] + 999
        result = self.poll(self.hub, far_future, timeout=2)
        self.assertTrue(result["full"] or result["r"] == [])

    def test_resolution_change_requests_a_full_frame(self):
        self.hub.start()
        first = self.poll(self.hub, 0)
        applied = self.hub.set_scale(160)
        self.assertEqual(applied, 160)
        result = self.poll(self.hub, first["seq"], timeout=3)
        self.assertTrue(result["full"])
        self.assertEqual(result["w"], 160)

    def test_stats_are_reported(self):
        self.hub.start()
        self.poll(self.hub, 0)
        stats = self.hub.stats()
        self.assertGreaterEqual(stats["seq"], 1)
        self.assertEqual(stats["size"], [320, 240])
        self.assertIn("bytes_sent", stats)

    def test_stop_ends_the_writer(self):
        self.hub.start()
        self.poll(self.hub, 0)
        self.hub.stop()
        time.sleep(0.2)
        self.assertFalse(self.hub.thread and self.hub.thread.is_alive())


@unittest.skipIf(cv2 is None, "opencv is required")
class CursorTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(REPO / ".github"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "runner_for_cursor", REPO / ".github" / "runner.py")
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def test_cursor_is_painted_at_the_pointer_position(self):
        frame = np.zeros((200, 300, 3), dtype=np.uint8)
        self.runner.draw_cursor(frame, 40, 60)
        patch = frame[60:78, 40:52]
        self.assertTrue(patch.any(), "the cursor must be visible in the frame")
        self.assertEqual(frame[0:8, 0:8].any(), False)

    def test_cursor_is_clamped_at_the_screen_edge(self):
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        self.runner.draw_cursor(frame, 155, 115)
        self.assertTrue(frame.any())

    def test_multiple_frames_reuse_the_shape(self):
        first = np.zeros((80, 80, 3), dtype=np.uint8)
        second = np.zeros((80, 80, 3), dtype=np.uint8)
        self.runner.draw_cursor(first, 10, 10)
        self.runner.draw_cursor(second, 10, 10)
        self.assertTrue(np.array_equal(first, second))


if __name__ == "__main__":
    unittest.main()
