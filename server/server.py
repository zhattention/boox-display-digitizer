"""
boox-bridge Mac server.

Listens for WebSocket connections from the Boox app and injects system
mouse events so writing on the Boox moves the cursor inside ClassIn
(or any other Mac app).

Advertises itself via mDNS (Bonjour) so the Boox app can auto-discover
the server on the local network.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import socket
import struct
import sys

import mss
import websockets
from PIL import Image
from Quartz import (
    CGDisplayBounds,
    CGEventCreateMouseEvent,
    CGEventPost,
    CGMainDisplayID,
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
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

# ---------- UDP discovery --------------------------------------------------

DISCOVERY_PORT = 9998
DISCOVERY_MAGIC = b"BOOX-BRIDGE"
DISCOVERY_INTERVAL = 2  # seconds

# ---------- config ---------------------------------------------------------

HOST = os.environ.get("BOOX_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("BOOX_BRIDGE_PORT", "9999"))

PEN_DOWN, PEN_MOVE, PEN_UP = 0, 1, 2
ERASER_DOWN, ERASER_MOVE, ERASER_UP = 3, 4, 5

MSG_SCREENSHOT = 0x10

PACKET_STRUCT = struct.Struct("!Bff")  # type, x, y
PACKET_SIZE = PACKET_STRUCT.size  # 9

SCREENSHOT_MAX_WIDTH = 1600
SCREENSHOT_MAX_HEIGHT = 1200
JPEG_QUALITY = 65

SERVICE_TYPE = "_boox-bridge._tcp.local."

# ---------- screen info ----------------------------------------------------

def _get_screen_size() -> tuple[int, int]:
    bounds = CGDisplayBounds(CGMainDisplayID())
    return int(bounds.size.width), int(bounds.size.height)

SCREEN_W, SCREEN_H = _get_screen_size()

# ---------- logging --------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("boox-bridge")

# ---------- mDNS / Bonjour ------------------------------------------------

def _get_local_ip() -> str:
    """Best-effort LAN IP address of this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _make_service_info(port: int) -> ServiceInfo:
    ip = _get_local_ip()
    hostname = socket.gethostname()
    return ServiceInfo(
        SERVICE_TYPE,
        f"Boox Bridge ({hostname}).{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={"version": "1"},
    )

# ---------- core logic -----------------------------------------------------


def _post(event_type: int, x: float, y: float, button: int) -> None:
    ev = CGEventCreateMouseEvent(None, event_type, (x, y), button)
    CGEventPost(kCGHIDEventTap, ev)


class MouseInjector:
    def __init__(self, screen_w: int, screen_h: int) -> None:
        self.sw = screen_w
        self.sh = screen_h
        self.button_down = False
        self.eraser_down = False

    def map_point(self, bx: float, by: float) -> tuple[float, float]:
        bx = max(0.0, min(1.0, bx))
        by = max(0.0, min(1.0, by))
        return (bx * self.sw, by * self.sh)

    def handle(self, packet_type: int, bx: float, by: float) -> None:
        x, y = self.map_point(bx, by)

        if packet_type == PEN_DOWN:
            _post(kCGEventMouseMoved, x, y, kCGMouseButtonLeft)
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
            _post(kCGEventMouseMoved, x, y, kCGMouseButtonRight)
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
        if self.button_down:
            _post(kCGEventLeftMouseUp, 0, 0, kCGMouseButtonLeft)
            self.button_down = False
        if self.eraser_down:
            _post(kCGEventRightMouseUp, 0, 0, kCGMouseButtonRight)
            self.eraser_down = False


def grab_screenshot_jpeg() -> bytes:
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    img = img.convert("L")
    img.thumbnail((SCREENSHOT_MAX_WIDTH, SCREENSHOT_MAX_HEIGHT), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


async def handle_client(ws: websockets.WebSocketServerProtocol) -> None:
    peer = ws.remote_address
    log.info("client connected: %s", peer)
    injector = MouseInjector(SCREEN_W, SCREEN_H)

    screen_info = json.dumps({"type": "screen_info", "w": SCREEN_W, "h": SCREEN_H})
    await ws.send(screen_info)
    log.info("sent screen_info: %dx%d points", SCREEN_W, SCREEN_H)

    n_packets = 0
    try:
        async for message in ws:
            if isinstance(message, str):
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


async def udp_broadcast(ws_port: int) -> None:
    """Periodically broadcast server presence on UDP so the Boox app can
    auto-discover without manual IP entry."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    payload = DISCOVERY_MAGIC + struct.pack("!H", ws_port)
    log.info("UDP discovery broadcasting on port %d", DISCOVERY_PORT)
    try:
        while True:
            try:
                sock.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
            except OSError:
                pass
            await asyncio.sleep(DISCOVERY_INTERVAL)
    finally:
        sock.close()


async def main() -> None:
    log.info("boox-bridge server starting on ws://%s:%d", HOST, PORT)
    log.info("screen: %dx%d points", SCREEN_W, SCREEN_H)

    # mDNS (Bonjour)
    svc = _make_service_info(PORT)
    azc = AsyncZeroconf()
    await azc.async_register_service(svc)
    log.info("mDNS registered: %s @ %s:%d", svc.name, _get_local_ip(), PORT)

    # UDP broadcast discovery (fallback for devices where NSD doesn't work)
    udp_task = asyncio.create_task(udp_broadcast(PORT))

    try:
        async with websockets.serve(handle_client, HOST, PORT, max_size=2 ** 20):
            log.info("listening... Ctrl-C to stop")
            await asyncio.Future()
    finally:
        udp_task.cancel()
        await azc.async_unregister_service(svc)
        await azc.async_close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
        sys.exit(0)
