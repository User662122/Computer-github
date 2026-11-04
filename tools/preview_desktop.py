#!/usr/bin/env python3
"""Run the remote-desktop viewer locally with a fake VM desktop.

A real `livestream` needs Windows, mss/cv2 and a cloudflared tunnel. This
harness stubs the screen-capture stack with a drawn "desktop" that reacts to
the same `/input` requests, so the touch gestures can be tried in a phone or
desktop browser (and the scroll fix can be seen working) without a VM.

    pip install flask pillow numpy
    python tools/preview_desktop.py            # http://localhost:8000
    python tools/preview_desktop.py --port 9000 --open

Swiping scrolls the fake page exactly like a mouse wheel, pinching zooms the
view, tapping clicks, and holding still right-clicks.
"""

import argparse
import importlib.util
import io
import sys
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".github"))

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - developer tool
    sys.exit("This preview needs numpy and pillow: pip install numpy pillow")

WIDTH, HEIGHT = 1920, 1080
PAGE_HEIGHT = 5200


class DesktopModel:
    """A tiny fake Windows desktop that the stubbed pyautogui can drive."""

    def __init__(self):
        self.lock = threading.Lock()
        self.scroll_y = 0.0
        self.cursor = (WIDTH // 2, HEIGHT // 2)
        self.clicks = []
        self.zoom = 100
        self.last_event = "ready"

    # --- wheel -----------------------------------------------------
    def scroll(self, notches):
        with self.lock:
            self.scroll_y = max(0.0, min(PAGE_HEIGHT - HEIGHT, self.scroll_y - notches * 90))
            self.last_event = f"wheel {notches:+d} -> offset {int(self.scroll_y)}"

    def hscroll(self, notches):
        with self.lock:
            self.last_event = f"hscroll {notches:+d}"

    # --- pointer ---------------------------------------------------
    def move_to(self, x, y):
        with self.lock:
            self.cursor = (int(x), int(y))

    def click(self, x=None, y=None, kind="click"):
        with self.lock:
            if x is not None:
                self.cursor = (int(x), int(y))
            self.clicks.append((self.cursor[0], self.cursor[1], time.time(), kind))
            self.last_event = f"{kind} at {self.cursor[0]},{self.cursor[1]}"

    def drag(self, x, y):
        with self.lock:
            self.last_event = f"drag to {int(x)},{int(y)}"

    # --- zoom ------------------------------------------------------
    def zoom_hotkey(self, key):
        with self.lock:
            if key in ("add", "+"):
                self.zoom = min(400, int(self.zoom * 1.25))
            elif key in ("subtract", "-"):
                self.zoom = max(25, int(self.zoom / 1.25))
            else:
                self.zoom = 100
            self.last_event = f"desktop zoom {self.zoom}%"

    # --- rendering -------------------------------------------------
    def render(self):
        with self.lock:
            scroll = int(self.scroll_y)
            zoom = self.zoom
            cursor = self.cursor
            clicks = list(self.clicks)[-6:]
            event = self.last_event

        page = Image.new("RGB", (WIDTH, PAGE_HEIGHT), (18, 20, 26))
        draw = ImageDraw.Draw(page)
        font_big = _font(64)
        font = _font(30)
        font_small = _font(22)

        draw.rectangle([0, 0, WIDTH, 150], fill=(24, 30, 44))
        draw.text((60, 40), "Preview desktop - swipe to scroll, pinch to zoom", fill=(150, 220, 255), font=font_big)
        draw.text((60, 170), "This page is 5200 px tall. A one finger swipe must move it like the wheel.",
                  fill=(220, 220, 230), font=font)

        y = 240
        for line in range(60):
            draw.text((60, y), f"Line {line + 1:02d}   The quick brown fox jumps over the lazy dog 0123456789",
                      fill=(200, 205, 225), font=font)
            y += 78
            if line % 6 == 5:
                draw.line([40, y - 20, WIDTH - 40, y - 20], fill=(60, 70, 100), width=2)

        draw.rectangle([0, page.height - 120, WIDTH, page.height], fill=(30, 40, 60))
        draw.text((60, page.height - 90), "End of the fake page", fill=(120, 200, 160), font=font)

        view = page.crop((0, scroll, WIDTH, scroll + HEIGHT))

        if zoom != 100:
            scale = zoom / 100.0
            size = (int(WIDTH * scale), int(HEIGHT * scale))
            scaled = view.resize(size, Image.BILINEAR)
            view = scaled.crop((0, 0, WIDTH, HEIGHT))

        overlay = ImageDraw.Draw(view)
        overlay.rectangle([0, 0, WIDTH, 44], fill=(10, 12, 16))
        overlay.text((16, 10), f"{event}   |   offset {scroll}px   |   desktop zoom {zoom}%",
                     fill=(120, 255, 160), font=font_small)

        for cx, cy, when, kind in clicks:
            age = time.time() - when
            if age < 1.2:
                radius = int(18 + age * 60)
                colour = (255, 90, 90) if kind != "click" else (120, 200, 255)
                overlay.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                                outline=colour, width=4)
                overlay.text((cx + radius + 6, cy - 10), kind, fill=colour, font=font_small)

        px, py = cursor
        overlay.polygon([(px, py), (px + 22, py + 26), (px + 9, py + 27), (px + 15, py + 38),
                         (px + 8, py + 40), (px + 2, py + 29), (px - 4, py + 34)],
                        fill=(255, 255, 255), outline=(0, 0, 0))
        return view


def _font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


MODEL = DesktopModel()


class FakePyAutoGUI:
    """Stand-in for pyautogui that drives the drawn desktop."""

    FAILSAFE = False
    PAUSE = 0.0

    def scroll(self, amount):
        MODEL.scroll(int(amount))

    def hscroll(self, amount):
        MODEL.hscroll(int(amount))

    def moveTo(self, x, y, duration=0):
        MODEL.move_to(x, y)

    def click(self, x=None, y=None, clicks=1, interval=0.0, button="left", **kwargs):
        MODEL.click(x, y)

    def rightClick(self, x=None, y=None, **kwargs):
        MODEL.click(x, y, kind="right-click")

    def doubleClick(self, x=None, y=None, **kwargs):
        MODEL.click(x, y, kind="double-click")

    def dragTo(self, x, y, duration=0.0, button="left", **kwargs):
        MODEL.drag(x, y)

    def hotkey(self, *keys):
        keys = [str(k).lower() for k in keys]
        if "ctrl" in keys:
            for key in keys:
                if key in ("add", "+", "subtract", "-", "0"):
                    MODEL.zoom_hotkey(key)
                    return
        MODEL.last_event = "hotkey " + "+".join(keys)

    def press(self, key):
        MODEL.last_event = f"press {key}"

    def write(self, text, interval=0.0):
        MODEL.last_event = f"typed {len(text)} chars"


def install_stubs():
    cv2 = types.ModuleType("cv2")
    cv2.COLOR_BGRA2BGR = 0
    cv2.IMWRITE_JPEG_QUALITY = 1

    def cvt_color(frame, code):
        return np.asarray(frame)[:, :, :3]

    def resize(frame, size):
        width, height = size
        image = Image.fromarray(np.asarray(frame))
        return np.asarray(image.resize((width, height), Image.BILINEAR))

    class _Buffer:
        def __init__(self, data):
            self._data = data

        def tobytes(self):
            return self._data

    def imencode(ext, frame, params):
        buffer = io.BytesIO()
        Image.fromarray(np.asarray(frame)).save(buffer, format="JPEG", quality=70)
        return True, _Buffer(buffer.getvalue())

    cv2.cvtColor = cvt_color
    cv2.resize = resize
    cv2.imencode = imencode
    sys.modules["cv2"] = cv2

    mss = types.ModuleType("mss")

    class _Sct:
        monitors = [{}, {"width": WIDTH, "height": HEIGHT}]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def grab(self, monitor):
            frame = np.asarray(MODEL.render().convert("RGB"))[:, :, ::-1]
            alpha = np.full((HEIGHT, WIDTH, 1), 255, dtype=np.uint8)
            return np.concatenate([frame, alpha], axis=2)

    mss.mss = lambda: _Sct()
    sys.modules["mss"] = mss


def load_runner():
    spec = importlib.util.spec_from_file_location("telegram_runner", REPO / ".github" / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._ensure_pyautogui = lambda: FakePyAutoGUI()
    module.SCREEN_W, module.SCREEN_H = WIDTH, HEIGHT
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    install_stubs()
    runner = load_runner()
    app = runner.create_desktop_app()
    # The preview has no tunnels, so let the browser stop button be harmless.
    runner.livestream_stop = lambda *a, **k: False

    from werkzeug.serving import make_server

    server = make_server(args.host, args.port, app, threaded=True)
    print(f"Preview desktop on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
