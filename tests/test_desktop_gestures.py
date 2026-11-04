"""Regression tests for the remote-desktop touch gestures.

A one-finger swipe used to be replayed as mouse-down / move / mouse-up, which
selected text or dragged windows instead of scrolling. These tests pin down
the fixed behaviour:

* swipes travel through the wheel pipeline and never press a mouse button,
* pixel deltas survive as real wheel notches (with a carried remainder),
* pinch zoom is handled in the viewer, and the desktop zoom path still works,
* the viewer itself is served from ``desktop_client.html`` without caching.
"""

import importlib.util
import math
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".github"))

spec = importlib.util.spec_from_file_location("telegram_runner", REPO / ".github" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def _stub(name, **attrs):
    """Install a tiny stand-in for an optional screen-capture dependency."""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules.setdefault(name, module)


def _install_capture_stubs():
    """cv2/mss/flask are not needed to exercise the routes or gesture maths."""
    _stub("cv2",
          COLOR_BGRA2BGR=0, IMWRITE_JPEG_QUALITY=1,
          cvtColor=lambda frame, code: frame,
          resize=lambda frame, size: frame,
          imencode=lambda ext, frame, params: (True, mock.Mock(tobytes=lambda: b"jpeg")))

    class _Sct:
        monitors = [{}, {"width": 1920, "height": 1080}]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def grab(self, monitor):
            return _frame()

    _stub("mss", mss=lambda: _Sct())


class _Frame:
    """Minimal numpy-like frame: shape (1080, 1920, 3)."""

    shape = (1080, 1920, 3)


class FakePyAutoGUI:
    """Records what the server asks the mouse and keyboard to do."""

    def __init__(self):
        self.calls = []

    def scroll(self, amount):
        self.calls.append(("scroll", amount))

    def hscroll(self, amount):
        self.calls.append(("hscroll", amount))

    def click(self, x=None, y=None):
        self.calls.append(("click", x, y))

    def rightClick(self, x=None, y=None):
        self.calls.append(("rightclick", x, y))

    def doubleClick(self, x=None, y=None):
        self.calls.append(("doubleclick", x, y))

    def moveTo(self, x, y, duration=0):
        self.calls.append(("moveTo", x, y))

    def dragTo(self, x, y, duration=0, button=None):
        self.calls.append(("dragTo", x, y))

    def hotkey(self, *keys):
        self.calls.append(("hotkey",) + keys)

    def press(self, key):
        self.calls.append(("press", key))

    def write(self, text, interval=0):
        self.calls.append(("write", text))

    def mouse_button_names(self):
        return [c for c in self.calls if c[0] in ("click", "rightclick", "doubleclick", "dragTo")]


class ViewerPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = runner._desktop_client_html()

    def test_viewer_is_served_from_its_own_file(self):
        self.assertIn("<!DOCTYPE html>", self.page)
        self.assertIn("pointerdown", self.page)
        self.assertIn("GitHub VM Remote Desktop", self.page)

    def test_one_finger_swipe_scrolls_instead_of_dragging(self):
        # The old viewer mapped touchend to a mouse drag; that is the bug.
        self.assertNotIn("action:'drag', x_ratio:startX", self.page)
        self.assertNotIn("postInput({action:'drag', x_ratio:startX", self.page)
        # ... and the new one sends scroll pixels from a pointer swipe.
        self.assertIn("queueScroll", self.page)
        self.assertIn("single.mode = (swipeMode === 'pan') ? 'pan' : 'scroll'", self.page)

    def test_pinch_zoom_is_implemented_client_side(self):
        self.assertIn("startPinch", self.page)
        self.assertIn("zoomTo(zoom * ratio", self.page)
        self.assertIn("minZoom()", self.page)
        self.assertIn("zoomIn", self.page)

    def test_browser_page_zoom_and_pull_to_refresh_are_blocked(self):
        self.assertIn("touch-action:none", self.page)
        self.assertIn("gesturestart", self.page)
        self.assertIn("overscroll-behavior:none", self.page)

    def test_long_press_and_tap_are_distinguished(self):
        self.assertIn("LONG_PRESS_MS", self.page)
        self.assertIn("action: 'rclick'", self.page)
        self.assertIn("action: 'click'", self.page)


class ScrollFlingerTests(unittest.TestCase):
    def setUp(self):
        self.flinger = runner._ScrollFlinger()

    def _notches(self, steps):
        return [n for step in steps for n in step]

    def test_swipe_pixels_become_wheel_notches(self):
        steps = self.flinger.feed(0, -300, reset=True)
        total = sum(ny for _, ny in steps)
        self.assertLess(total, 0, "swiping up must scroll the page up (down the content)")
        self.assertGreaterEqual(abs(total), 2)

    def test_slow_swipe_is_not_lost_to_rounding(self):
        total = 0
        for _ in range(30):                     # 30 x 10 px == one 300 px swipe
            for _, ny in self.flinger.feed(0, -10):
                total += ny
        self.assertLessEqual(total, -2, "smooth slow swipe should still scroll")

    def test_remainder_is_kept_between_moves(self):
        self.assertEqual(self.flinger.feed(0, -50), [])
        self.assertEqual(self.flinger.feed(0, -50), [(0, -1)])

    def test_reset_starts_a_clean_gesture(self):
        self.flinger.feed(0, -60)
        self.assertEqual(self.flinger.feed(0, -10, reset=True), [])
        # only the remainder of this gesture is carried over
        self.assertLess(self.flinger.carry_y, 0)
        self.assertGreater(self.flinger.carry_y, -0.3)

    def test_flick_is_capped_so_it_does_not_keep_scrolling(self):
        steps = self.flinger.feed(0, -50000, reset=True)
        self.assertLessEqual(max(abs(ny) for _, ny in steps), runner.WHEEL_MAX_NOTCHES)
        self.assertLess(abs(self.flinger.carry_y), 1.0)
        self.assertEqual(self.flinger.feed(0, 0), [])

    def test_diagonal_swipe_sends_both_directions(self):
        steps = self.flinger.feed(-200, 100, reset=True)
        self.assertIn((-2, 0), steps)
        self.assertIn((0, 1), steps)

    def test_bad_values_are_ignored(self):
        self.assertEqual(self.flinger.feed("nope", None), [])
        self.assertEqual(self.flinger.feed(float("nan"), 0), [])

    def test_queue_is_ordered_for_the_worker(self):
        self.flinger.feed(0, -300, reset=True)
        queued = self.flinger.take()
        self.assertTrue(queued)
        self.assertEqual(self.flinger.take(), [])


class ScaleTests(unittest.TestCase):
    def test_scale_is_clamped(self):
        self.assertEqual(runner._safe_scale(2), 2.0)
        self.assertEqual(runner._safe_scale(0), 1.0)
        self.assertEqual(runner._safe_scale(-5), 1.0)
        self.assertEqual(runner._safe_scale("abc"), 1.0)
        self.assertEqual(runner._safe_scale(None), 1.0)
        self.assertLessEqual(runner._safe_scale(99), 4.0)
        self.assertGreaterEqual(runner._safe_scale(0.01), 0.25)


class InputRouteTests(unittest.TestCase):
    """Exercise /input and / through Flask's test client."""

    @classmethod
    def setUpClass(cls):
        _install_capture_stubs()
        try:
            cls.app = runner.create_desktop_app()
        except ImportError as exc:          # flask not installed
            raise unittest.SkipTest(f"flask unavailable: {exc}")

    def setUp(self):
        self.gui = FakePyAutoGUI()
        patch = mock.patch.object(runner, "_ensure_pyautogui", return_value=self.gui)
        patch.start()
        self.addCleanup(patch.stop)
        runner._scroll_flinger.reset()
        self.client = self.app.test_client()

    def post(self, payload):
        return self.client.post("/input", json=payload)

    def test_index_page_is_not_cached(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertIn(b"pointerdown", response.data)

    def test_swipe_scrolls_with_the_wheel_and_never_Presses_a_button(self):
        response = self.post({"action": "scroll", "pixels": True,
                              "dx": 0, "dy": -250, "reset": True})
        self.assertTrue(response.get_json()["ok"])
        # let the single wheel worker drain the queue
        for _ in range(50):
            if not runner._scroll_flinger.pending and self.gui.calls:
                break
            import time
            time.sleep(0.02)
        scrolls = [c for c in self.gui.calls if c[0] in ("scroll", "hscroll")]
        self.assertTrue(scrolls, "a swipe must produce wheel events")
        self.assertLess(min(c[1] for c in scrolls), 0)
        self.assertEqual(self.gui.mouse_button_names(), [],
                         "scrolling must not press a mouse button")

    def test_horizontal_swipe_uses_hscroll_when_available(self):
        self.post({"action": "scroll", "pixels": True, "dx": -400, "dy": 0, "reset": True})
        import time
        for _ in range(50):
            if any(c[0] == "hscroll" for c in self.gui.calls):
                break
            time.sleep(0.02)
        self.assertTrue([c for c in self.gui.calls if c[0] == "hscroll"])

    def test_legacy_notch_scroll_still_works(self):
        response = self.post({"action": "scroll", "amount": -300})
        self.assertTrue(response.get_json()["ok"])
        self.assertIn(("scroll", -300), self.gui.calls)

    def test_mouse_drag_is_still_available_on_desktop(self):
        self.post({"action": "drag", "x_ratio": 0.1, "y_ratio": 0.1,
                   "x2_ratio": 0.5, "y2_ratio": 0.5})
        self.assertIn(("dragTo", 960, 540), self.gui.calls)

    def test_zoom_action_uses_ctrl_plus(self):
        response = self.post({"action": "zoom", "direction": "in"})
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["percent"], 100)
        hotkeys = [c for c in self.gui.calls if c[0] == "hotkey"]
        self.assertTrue(hotkeys, "desktop zoom should use the app's zoom shortcut")

    def test_stream_and_snapshot_widths_are_clamped(self):
        # The clamp runs inside the generators; check the helper the routes use.
        app_source = (REPO / ".github" / "runner.py").read_text(encoding="utf-8")
        self.assertIn('request.args.get("w")', app_source)
        self.assertIn("def _clamp_stream_width", app_source)
        self.assertIn("max(480, min(1920, width))", app_source)


class TelegramZoomCommandTests(unittest.TestCase):
    def setUp(self):
        self.send = mock.Mock()
        self.gui = FakePyAutoGUI()
        for name, value in (("send_message", self.send),
                            ("_ensure_pyautogui", mock.Mock(return_value=self.gui))):
            patch = mock.patch.object(runner, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        runner._zoom_reset_state()

    def test_zoom_in_and_out_change_the_percentage(self):
        runner.do_zoom(42, "in")
        self.assertEqual(runner._zoom_percent(), 125)
        runner.do_zoom(42, "out")
        self.assertEqual(runner._zoom_percent(), 100)
        self.assertTrue(self.send.called)

    def test_zoom_reset_returns_to_100(self):
        runner.do_zoom(42, "200")
        self.assertAlmostEqual(runner._zoom_percent(), 200)
        runner.do_zoom(42, "reset")
        self.assertAlmostEqual(runner._zoom_percent(), 100)
        self.assertIn(("hotkey", "ctrl", "0"), self.gui.calls)

    def test_zoom_usage_message_for_garbage(self):
        runner.do_zoom(42, "banana")
        self.assertIn("Usage", self.send.call_args.args[1])

    def test_zoom_without_pyautogui_is_reported(self):
        with mock.patch.object(runner, "_ensure_pyautogui", return_value=None):
            runner.do_zoom(42, "in")
        self.assertIn("Could not", self.send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
