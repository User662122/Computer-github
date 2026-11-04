"""Dirty-tile screen streaming: send only what changed.

MJPEG (the previous transport) re-sends a whole JPEG frame several times per
second even when nothing on screen moves, which on a phone means several
Mbit/s of mobile data for a static desktop. Real screen-sharing protocols
(VNC, RDP, TeamViewer, Chrome Remote Desktop) instead keep one frame on the
client, detect which regions changed, and send only those regions.

This module implements that:

* the frame is split into a grid of small tiles,
* tiles whose pixels did not change are never encoded or sent,
* neighbouring changed tiles are merged into a few rectangles,
* each rectangle is encoded once (WebP when available, JPEG otherwise),
* clients receive a long-polled JSON "what changed" message and fetch only
  those rectangles, drawing them onto a locally kept canvas,
* a history of recent updates lets a reconnecting phone resume instead of
  downloading the whole screen again.

A completely idle desktop therefore costs almost no bandwidth, and typing or
moving the mouse costs a few hundred bytes per update.

The module is deliberately free of Flask and pyautogui imports so it can be
unit tested without a screen.
"""

import io
import threading
import time
from collections import deque

try:                                  # numpy is a hard dependency of the runner
    import numpy as np
except ImportError:                   # pragma: no cover - runner always has it
    np = None

try:
    import cv2
except ImportError:                   # pragma: no cover - stubbed in tests
    cv2 = None


DEFAULT_TILE = 32            # pixels; small enough to tighten around text
CHANGE_THRESHOLD = 6         # per-pixel grey delta that counts as a change
ACTIVE_INTERVAL = 0.08       # ~12 fps while the screen is busy
IDLE_INTERVAL = 0.45         # ~2 fps when nothing changed (cheap to poll)
HEARTBEAT_INTERVAL = 5.0     # clients get an empty message this often
FORCE_FULL_AFTER = 30.0      # rebuild a stale full frame for new viewers
MAX_RECTS = 24               # per update; more than this and the screen is busy
MERGE_MARGIN = 2             # pixels of padding so seams never show
HISTORY_LIMIT = 240          # update messages kept for reconnecting clients
TILE_CACHE_LIMIT = 4000      # encoded tile blobs kept for fetching
TILE_CACHE_BYTES = 24 * 1024 * 1024


def _webp_available():
    """WebP is ~30% smaller than JPEG for screen content; check once."""
    if cv2 is None or np is None:
        return False
    try:
        sample = np.zeros((16, 16, 3), dtype=np.uint8)
        ok, buffer = cv2.imencode(".webp", sample, [int(cv2.IMWRITE_WEBP_QUALITY), 50])
        return bool(ok) and len(buffer) > 0
    except Exception:
        return False


WEBP_OK = _webp_available()
WEBP_QUALITY = 62            # good text legibility, small payloads
JPEG_QUALITY = 68
SMALL_UPDATE_QUALITY_BONUS = 10      # small rects can afford nicer encoding
MAX_TILE_WIDTH = 960                 # never send more detail than this


class TileEncoder:
    """Turns successive frames into changed rectangles with encoded pixels."""

    def __init__(self, tile=DEFAULT_TILE, webp=WEBP_OK):
        self.tile = tile
        self.use_webp = webp
        self.prev_gray = None
        self.width = 0
        self.height = 0
        self.last_sent = {}          # (row, col) -> monotonic time

    # -- geometry ---------------------------------------------------
    def _grid(self, height, width):
        tile = self.tile
        cols = (width + tile - 1) // tile
        rows = (height + tile - 1) // tile
        return rows, cols

    def reset(self):
        self.prev_gray = None
        self.last_sent = {}

    # -- change detection -------------------------------------------
    def changed_cells(self, gray):
        """Boolean grid of tiles whose pixels moved by more than the threshold."""
        rows, cols = self._grid(gray.shape[0], gray.shape[1])
        tile = self.tile
        padded = np.zeros((rows * tile, cols * tile), dtype=gray.dtype)
        padded[:gray.shape[0], :gray.shape[1]] = gray
        cells = padded.reshape(rows, tile, cols, tile).astype(np.int16)

        if self.prev_gray is None or self.prev_gray.shape != gray.shape:
            changed = np.ones((rows, cols), dtype=bool)
        else:
            prev_padded = np.zeros_like(padded)
            prev_padded[: gray.shape[0], : gray.shape[1]] = self.prev_gray
            prev_cells = prev_padded.reshape(rows, tile, cols, tile).astype(np.int16)
            diff = np.abs(cells - prev_cells).max(axis=(1, 3))
            changed = diff > CHANGE_THRESHOLD
        self.prev_gray = gray
        return changed

    # -- rectangle merging ------------------------------------------
    def rectangles(self, changed, width, height, limit=MAX_RECTS):
        """Merge changed tiles into as few rectangles as the budget allows.

        A row run is extended downwards while the same columns stay changed,
        which collapses a moving window or a line of typed text into one
        rectangle instead of dozens of tiles.
        """
        rows, cols = changed.shape
        taken = np.zeros_like(changed)
        rects = []
        for row in range(rows):
            col = 0
            while col < cols:
                if not changed[row, col] or taken[row, col]:
                    col += 1
                    continue
                start = col
                while col < cols and changed[row, col] and not taken[row, col]:
                    col += 1
                end = col - 1
                bottom = row
                while bottom + 1 < rows:
                    nxt = changed[bottom + 1, start:end + 1]
                    busy = taken[bottom + 1, start:end + 1]
                    if nxt.all() and not busy.any():
                        bottom += 1
                    else:
                        break
                taken[row:bottom + 1, start:end + 1] = True
                x0 = max(0, start * self.tile - MERGE_MARGIN)
                y0 = max(0, row * self.tile - MERGE_MARGIN)
                x1 = min(width, (end + 1) * self.tile + MERGE_MARGIN)
                y1 = min(height, (bottom + 1) * self.tile + MERGE_MARGIN)
                rects.append((x0, y0, x1 - x0, y1 - y0))
        if len(rects) > limit:
            # Too fragmented: coalesce everything into one bounding box. It
            # costs one bigger image but far less per-rect overhead.
            x0 = min(r[0] for r in rects)
            y0 = min(r[1] for r in rects)
            x1 = max(r[0] + r[2] for r in rects)
            y1 = max(r[1] + r[3] for r in rects)
            rects = [(x0, y0, x1 - x0, y1 - y0)]
        return rects

    # -- encoding ---------------------------------------------------
    def _encode(self, bgr, quality):
        if self.use_webp:
            try:
                ok, buffer = cv2.imencode(
                    ".webp", bgr, [int(cv2.IMWRITE_WEBP_QUALITY), int(quality)])
                if ok:
                    return buffer.tobytes(), "image/webp"
            except Exception:
                self.use_webp = False
        ok, buffer = cv2.imencode(
            ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return None, None
        return buffer.tobytes(), "image/jpeg"

    def encode(self, bgr, quality=None, scale_to=None, force_full_tiles=()):
        """Encode one frame.

        Returns ``(rectangles, sent_cells)`` where each rectangle is a dict
        ``{"x", "y", "w", "h", "data", "mime"}`` covering a changed region of
        the frame after it was scaled to ``scale_to`` pixels wide.
        """
        if bgr is None or np is None or cv2 is None:
            return []
        height, width = bgr.shape[:2]
        if scale_to and width > scale_to:
            new_h = max(1, int(round(height * scale_to / float(width))))
            bgr = cv2.resize(bgr, (scale_to, new_h), interpolation=cv2.INTER_AREA)
            width, height = scale_to, new_h
        if width > MAX_TILE_WIDTH:
            new_h = max(1, int(round(height * MAX_TILE_WIDTH / float(width))))
            bgr = cv2.resize(bgr, (MAX_TILE_WIDTH, new_h), interpolation=cv2.INTER_AREA)
            width, height = MAX_TILE_WIDTH, new_h

        if cv2 is None:                       # pragma: no cover
            return []
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        if (width, height) != (self.width, self.height):
            # A resolution change invalidates the previous frame: a viewer
            # would otherwise get a patch that does not line up with its
            # canvas. Start clean so the caller asks for a full picture.
            self.prev_gray = None
        self.width, self.height = width, height

        changed = self.changed_cells(gray)
        for row, col in force_full_tiles:
            if row < changed.shape[0] and col < changed.shape[1]:
                changed[row, col] = True
        if not changed.any():
            return []

        rects = self.rectangles(changed, width, height)
        now = time.monotonic()
        quality = quality or (WEBP_QUALITY if self.use_webp else JPEG_QUALITY)
        small = max(1, int(0.05 * width * height))
        out = []
        for x, y, w, h in rects:
            crop = bgr[y:y + h, x:x + w]
            tile_quality = quality
            if w * h <= small:
                tile_quality = min(90, quality + SMALL_UPDATE_QUALITY_BONUS)
            data, mime = self._encode(crop, tile_quality)
            if not data:
                continue
            out.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h),
                        "data": data, "mime": mime})
        for row in range(changed.shape[0]):
            for col in range(changed.shape[1]):
                if changed[row, col]:
                    self.last_sent[(row, col)] = now
        return out

    def full_frame(self, bgr, scale_to=None, quality=None):
        """Encode the whole visible frame (first paint / re-sync)."""
        self.reset()
        if bgr is None:
            return [], 0, 0
        height, width = bgr.shape[:2]
        if scale_to and width > scale_to:
            new_h = max(1, int(round(height * scale_to / float(width))))
            bgr = cv2.resize(bgr, (scale_to, new_h), interpolation=cv2.INTER_AREA)
            width, height = scale_to, new_h
        if width > MAX_TILE_WIDTH:
            new_h = max(1, int(round(height * MAX_TILE_WIDTH / float(width))))
            bgr = cv2.resize(bgr, (MAX_TILE_WIDTH, new_h), interpolation=cv2.INTER_AREA)
            width, height = MAX_TILE_WIDTH, new_h
        if cv2 is not None:
            self.prev_gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        self.width, self.height = width, height
        q = quality or (WEBP_QUALITY if self.use_webp else JPEG_QUALITY)
        # Split into a few big strips so the client can fetch them in parallel
        # and a stalled request never blocks the whole picture.
        strips = 4 if height > 240 else 1
        out = []
        for index in range(strips):
            y = int(round(index * height / strips))
            bottom = int(round((index + 1) * height / strips))
            if bottom <= y:
                continue
            data, mime = self._encode(bgr[y:bottom, :], q)
            if data:
                out.append({"x": 0, "y": y, "w": width, "h": bottom - y,
                            "data": data, "mime": mime})
        return out


class ScreenHub:
    """Capture thread + change log shared by every connected viewer."""

    def __init__(self, capture, scale_to=960, interval=ACTIVE_INTERVAL,
                 idle_interval=IDLE_INTERVAL, encoder=None, clock=time.monotonic):
        self.capture = capture                 # () -> BGR ndarray | None
        self.scale_to = scale_to
        self.interval = interval
        self.idle_interval = idle_interval
        self.encoder = encoder or TileEncoder()
        self.clock = clock

        self.cond = threading.Condition()
        self.seq = 0
        self.latest = []                        # rects of the newest update
        self.width = 0
        self.height = 0
        self.history = deque(maxlen=HISTORY_LIMIT)   # (seq, rects)
        self.last_full_seq = 0
        self.tiles = {}                         # (seq, index) -> (bytes, mime)
        self.tiles_order = deque()
        self.tiles_bytes = 0
        self.clients = 0
        # Grace period counted from construction, not from 0 (= "long ago"),
        # otherwise the writer would idle before the first viewer arrives.
        self.last_client = clock()
        self.force_full = False
        self.thread = None
        self.stopping = threading.Event()
        self.bytes_sent = 0
        self.bytes_full = 0
        self.full_frames = 0
        self.frames = 0
        self.last_change = 0.0

    # -- lifecycle --------------------------------------------------
    def start(self):
        with self.cond:
            self.start_locked()
            return self.thread

    def start_locked(self):
        """Start the writer thread (caller holds the lock)."""
        if self.thread and self.thread.is_alive():
            return self.thread
        self.stopping.clear()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="Screen delta writer")
        self.thread.start()
        return self.thread

    def stop(self):
        self.stopping.set()
        with self.cond:
            self.cond.notify_all()

    def request_full_frame(self):
        with self.cond:
            self.force_full = True

    def note_client(self):
        with self.cond:
            self.last_client = self.clock()

    # -- writer ------------------------------------------------------
    def _trim_tiles(self):
        while self.tiles_order and (len(self.tiles_order) > TILE_CACHE_LIMIT
                                    or self.tiles_bytes > TILE_CACHE_BYTES):
            key = self.tiles_order.popleft()
            data = self.tiles.pop(key, None)
            if data:
                self.tiles_bytes -= len(data[0])

    def _publish(self, rects, full=False):
        with self.cond:
            self.seq += 1
            seq = self.seq
            clean = []
            for index, rect in enumerate(rects):
                key = (seq, index)
                self.tiles[key] = (rect["data"], rect["mime"])
                self.tiles_order.append(key)
                self.tiles_bytes += len(rect["data"])
                clean.append({"x": rect["x"], "y": rect["y"], "w": rect["w"],
                              "h": rect["h"], "i": index, "s": seq,
                              "n": len(rect["data"])})
            self._trim_tiles()
            self.latest = clean
            self.history.append((seq, clean))
            if full:
                self.last_full_seq = seq
            self.width = self.encoder.width
            self.height = self.encoder.height
            self.cond.notify_all()
            return seq

    def _run(self):
        idle_since = self.clock()
        last_publish = self.clock()
        while not self.stopping.is_set():
            try:
                with self.cond:
                    want_full = self.force_full
                    self.force_full = False
                    idle_client = (self.clients == 0 and
                                   self.clock() - self.last_client > 30.0)
                if idle_client:
                    # Nobody is watching: do not capture at all.
                    time.sleep(0.25)
                    continue

                frame = self.capture()
                if frame is None:
                    time.sleep(0.2)
                    continue

                if want_full:
                    rects = self.encoder.full_frame(frame, scale_to=self.scale_to)
                    self.bytes_full += sum(len(r["data"]) for r in rects)
                    self.full_frames += 1
                    self._publish(rects, full=True)
                    last_publish = self.clock()
                    idle_since = self.clock()
                else:
                    rects = self.encoder.encode(frame, scale_to=self.scale_to)
                    now = self.clock()
                    if rects:
                        self.bytes_sent += sum(len(r["data"]) for r in rects)
                        self._publish(rects)
                        last_publish = now
                        idle_since = now
                        self.last_change = now
                    elif now - last_publish >= HEARTBEAT_INTERVAL:
                        self._publish([])          # keep clients' polls fresh
                        last_publish = now

                self.frames += 1
                busy = (self.clock() - idle_since) < 2.0
                delay = self.interval if busy else self.idle_interval
                if self.stopping.wait(delay):
                    return
            except Exception as exc:               # keep the stream alive
                print(f"[screen delta] {exc}")
                time.sleep(0.5)

    # -- clients -----------------------------------------------------
    def set_scale(self, width):
        """Change the resolution of the stream (a sharper or smaller picture)."""
        width = int(width)
        with self.cond:
            if width == self.scale_to:
                return self.scale_to
            self.scale_to = width
            self.encoder.reset()
            self.force_full = True
            self.start_locked()
            return self.scale_to

    def _full_payload_locked(self):
        """Rectangles of the newest full frame (caller holds the lock)."""
        if not self.last_full_seq:
            return None
        for seq, rects in reversed(self.history):
            if seq == self.last_full_seq:
                return seq, list(rects)
        return None

    def _full_result(self, reason):
        payload = self._full_payload_locked()
        if payload is None:
            return None
        seq, rects = payload
        return {"seq": seq, "r": rects, "full": True,
                "w": self.width, "h": self.height, "reason": reason}

    def frames_since(self, since, timeout=20.0):
        """Long-poll for updates newer than ``since``.

        Returns a dict:
          ``seq``    newest update sequence the client should remember,
          ``r``      rectangles to draw (``x``, ``y``, ``w``, ``h``, ``i``
                     index, ``s`` sequence of that rectangle, ``n`` bytes),
          ``full``   True when the rectangles replace the whole picture,
          ``w``/``h`` picture size, ``reason`` why this message was sent.

        A full frame always supersedes older partial updates, so a client is
        never told to clear its canvas and then handed a partial patch.
        """
        # Waiting is bounded by real time, not by the injectable clock: a
        # frozen or mocked clock must never turn a poll into an endless wait.
        deadline = time.monotonic() + timeout
        with self.cond:
            self.last_client = self.clock()
            self.clients = max(1, self.clients)

            if since <= 0:
                # First paint: hand over the newest full frame if we have one.
                result = self._full_result("initial")
                if result is None:
                    self.force_full = True
                    self.start_locked()
                    while not self.last_full_seq and time.monotonic() < deadline \
                            and not self.stopping.is_set():
                        self.cond.wait(timeout=max(0.05, deadline - time.monotonic()))
                    result = self._full_result("initial")
                return result or {"seq": 0, "r": [], "full": True,
                                  "w": self.width, "h": self.height,
                                  "reason": "initial"}

            resync = False
            if since > self.seq:
                since = self.seq                      # server restarted
                resync = True
            elif self.history and since < self.history[0][0] - 1:
                since = self.seq                      # fell behind the history
                resync = True
            if resync:
                self.force_full = True

            if self.last_full_seq and since < self.last_full_seq:
                result = self._full_result("resync" if resync else "full")
                if result is not None:
                    return result

            while self.seq <= since and time.monotonic() < deadline \
                    and not self.stopping.is_set():
                self.cond.wait(timeout=max(0.05, deadline - time.monotonic()))

            if self.last_full_seq and since < self.last_full_seq:
                result = self._full_result("full")
                if result is not None:
                    return result

            rects = []
            for seq, batch in self.history:
                if seq > since:
                    rects.extend(batch)
            if not rects:
                return {"seq": max(self.seq, since), "r": [], "full": False,
                        "w": self.width, "h": self.height, "reason": "empty"}
            return {"seq": self.seq, "r": rects, "full": False,
                    "w": self.width, "h": self.height, "reason": "update"}

    def tile(self, seq, index):
        """Encoded pixels for one rectangle of one update."""
        with self.cond:
            return self.tiles.get((seq, index))

    def stats(self):
        with self.cond:
            return {"seq": self.seq, "frames": self.frames,
                    "bytes_sent": self.bytes_sent,
                    "full_frames": self.full_frames,
                    "bytes_full": self.bytes_full,
                    "tiles_cached": len(self.tiles_order),
                    "tile_cache_bytes": self.tiles_bytes,
                    "size": [self.width, self.height]}
