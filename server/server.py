"""
boox-bridge Mac server — Phase 1 minimum.

Listens for WebSocket connections from the Boox app and injects system
mouse events so writing on the Boox moves the cursor inside ClassIn
(or any other Mac app).

Wire format (upstream, Boox → Mac):
    1 byte  type    0=down  1=move  2=up  3=eraser_down  4=eraser_move  5=eraser_up
    4 bytes x       float big-endian, normalized 0.0..1.0
    4 bytes y       float big-endian, normalized 0.0..1.0
    total 9 bytes

Coordinate system:
    Boox sends normalized (0,0)=top-left  →  (1,1)=bottom-right of its screen.
    Server maps that into TARGET_RECT on the Mac.

Run:
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python server.py
First run will prompt for Accessibility permission — grant it to your
terminal (or Python) in System Settings → Privacy & Security → Accessibility.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import sys

import mss
import websockets
from PIL import Image
from Quartz import (
    CGEventCreateMouseEvent,
    CGEventPost,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseDragged,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGEventRightMouseDown,
    kCGEventRightMouseDragged,
    kCGEventRightMouseUp,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGMouseButtonRight,
)

# ---------- config ---------------------------------------------------------

HOST = os.environ.get("BOOX_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("BOOX_BRIDGE_PORT", "9999"))

# Rectangle on the Mac where the Boox maps to. Phase 1: hardcoded.
# Phase 3 will replace this with an on-screen calibration picker.
# Format: (x, y, width, height) in screen points.
TARGET_RECT = (100, 100, 1200, 900)

# Packet type constants (must match Android side)
PEN_DOWN, PEN_MOVE, PEN_UP = 0, 1, 2
ERASER_DOWN, ERASER_MOVE, ERASER_UP = 3, 4, 5

# Upstream: 1-byte packet, client asks for a fresh screenshot.
# Downstream: same byte prefix, followed by JPEG bytes of TARGET_RECT.
MSG_SCREENSHOT = 0x10

PACKET_STRUCT = struct.Struct("!Bff")  # type, x, y
PACKET_SIZE = PACKET_STRUCT.size  # 9

# Screenshot parameters. We downscale and grayscale the captured region
# so each frame stays well under 100 KB on the wire.
SCREENSHOT_MAX_WIDTH = 900
SCREENSHOT_MAX_HEIGHT = 1200
JPEG_QUALITY = 60

# ---------- logging --------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("boox-bridge")

# ---------- core logic -----------------------------------------------------


def _post(event_type: int, x: float, y: float, button: int) -> None:
    """Post a CoreGraphics mouse event directly via the HID event tap.

    We bypass pynput here because pynput.mouse.Controller.position uses
    CGWarpMouseCursorPosition, which teleports the cursor without generating
    a LeftMouseDragged event. Drawing apps like Freeform, Excalidraw and
    ClassIn only extend a stroke when they see LeftMouseDragged events, so
    using pynput's warp leaves strokes broken and the button stuck "down".
    """
    ev = CGEventCreateMouseEvent(None, event_type, (x, y), button)
    CGEventPost(kCGHIDEventTap, ev)


class MouseInjector:
    """Maps normalized Boox coordinates to Mac screen points and injects
    system mouse events. All events go through CoreGraphics directly."""

    def __init__(self, target_rect: tuple[int, int, int, int]) -> None:
        self.tx, self.ty, self.tw, self.th = target_rect
        self.button_down = False
        self.eraser_down = False

    def map_point(self, bx: float, by: float) -> tuple[float, float]:
        bx = max(0.0, min(1.0, bx))
        by = max(0.0, min(1.0, by))
        return (self.tx + bx * self.tw, self.ty + by * self.th)

    def handle(self, packet_type: int, bx: float, by: float) -> None:
        x, y = self.map_point(bx, by)

        if packet_type == PEN_DOWN:
            _post(kCGEventLeftMouseDown, x, y, kCGMouseButtonLeft)
            self.button_down = True
        elif packet_type == PEN_MOVE:
            if self.button_down:
                _post(kCGEventLeftMouseDragged, x, y, kCGMouseButtonLeft)
            else:
                _post(kCGEventMouseMoved, x, y, kCGMouseButtonLeft)
        elif packet_type == PEN_UP:
            _post(kCGEventLeftMouseUp, x, y, kCGMouseButtonLeft)
            self.button_down = False
        elif packet_type == ERASER_DOWN:
            _post(kCGEventRightMouseDown, x, y, kCGMouseButtonRight)
            self.eraser_down = True
        elif packet_type == ERASER_MOVE:
            if self.eraser_down:
                _post(kCGEventRightMouseDragged, x, y, kCGMouseButtonRight)
            else:
                _post(kCGEventMouseMoved, x, y, kCGMouseButtonRight)
        elif packet_type == ERASER_UP:
            _post(kCGEventRightMouseUp, x, y, kCGMouseButtonRight)
            self.eraser_down = False
        else:
            log.warning("unknown packet type: %d", packet_type)

    def release_all(self) -> None:
        """Call on disconnect to avoid a 'stuck' pressed button."""
        if self.button_down:
            _post(kCGEventLeftMouseUp, 0, 0, kCGMouseButtonLeft)
            self.button_down = False
        if self.eraser_down:
            _post(kCGEventRightMouseUp, 0, 0, kCGMouseButtonRight)
            self.eraser_down = False


def grab_screenshot_jpeg() -> bytes:
    """Capture TARGET_RECT area of the Mac screen, convert to grayscale,
    downscale while preserving aspect ratio, JPEG-encode. Returns the raw
    JPEG bytes (no prefix byte). Runs in a worker thread so mss can create
    its own CGDisplay handle per call."""
    with mss.mss() as sct:
        region = {
            "top": TARGET_RECT[1],
            "left": TARGET_RECT[0],
            "width": TARGET_RECT[2],
            "height": TARGET_RECT[3],
        }
        shot = sct.grab(region)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    img = img.convert("L")
    img.thumbnail((SCREENSHOT_MAX_WIDTH, SCREENSHOT_MAX_HEIGHT), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


async def handle_client(ws: websockets.WebSocketServerProtocol) -> None:
    peer = ws.remote_address
    log.info("client connected: %s", peer)
    injector = MouseInjector(TARGET_RECT)

    n_packets = 0
    try:
        async for message in ws:
            if isinstance(message, str):
                # text messages are reserved for future control commands
                log.debug("text message: %s", message)
                continue
            if len(message) == PACKET_SIZE:
                packet_type, bx, by = PACKET_STRUCT.unpack(message)
                injector.handle(packet_type, bx, by)
                n_packets += 1
                if n_packets % 1000 == 0:
                    log.info("%d packets processed", n_packets)
            elif len(message) == 1 and message[0] == MSG_SCREENSHOT:
                log.info("screenshot requested by %s", peer)
                jpeg = await asyncio.to_thread(grab_screenshot_jpeg)
                await ws.send(bytes([MSG_SCREENSHOT]) + jpeg)
                log.info("screenshot sent: %d bytes", len(jpeg))
            else:
                log.warning("bad packet length: %d", len(message))
    except websockets.ConnectionClosed:
        pass
    finally:
        injector.release_all()
        log.info("client disconnected: %s  (%d packets)", peer, n_packets)


async def main() -> None:
    log.info("boox-bridge server starting on ws://%s:%d", HOST, PORT)
    log.info("target rectangle on Mac: x=%d y=%d w=%d h=%d", *TARGET_RECT)
    log.info("(set TARGET_RECT in server.py to match your ClassIn whiteboard)")

    async with websockets.serve(handle_client, HOST, PORT, max_size=2 ** 20):
        log.info("listening... Ctrl-C to stop")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
        sys.exit(0)
