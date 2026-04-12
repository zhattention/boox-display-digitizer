"""
boox-bridge Mac server.

Runs as a macOS menu bar app. The WebSocket server, mDNS registration,
and UDP broadcast all run in a background thread; rumps drives the UI
on the main thread.
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
import threading

import mss
import rumps
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

# ---------- config ---------------------------------------------------------

HOST = os.environ.get("BOOX_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("BOOX_BRIDGE_PORT", "9999"))

PEN_DOWN, PEN_MOVE, PEN_UP = 0, 1, 2
ERASER_DOWN, ERASER_MOVE, ERASER_UP = 3, 4, 5

MSG_SCREENSHOT = 0x10

PACKET_STRUCT = struct.Struct("!Bff")
PACKET_SIZE = PACKET_STRUCT.size

SCREENSHOT_MAX_WIDTH = 2200
SCREENSHOT_MAX_HEIGHT = 1650
JPEG_QUALITY = 85

DISCOVERY_PORT = 9998
DISCOVERY_MAGIC = b"BOOX-BRIDGE"
DISCOVERY_INTERVAL = 2

SERVICE_TYPE = "_boox-bridge._tcp.local."

# ---------- shared state ---------------------------------------------------

screenshot_interval: float = float(os.environ.get("BOOX_BRIDGE_FPS_INTERVAL", "1"))
connected_clients: int = 0
server_loop: asyncio.AbstractEventLoop | None = None

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


async def auto_screenshot(ws: websockets.WebSocketServerProtocol) -> None:
    while True:
        await asyncio.sleep(screenshot_interval)
        try:
            jpeg = await asyncio.to_thread(grab_screenshot_jpeg)
            await ws.send(bytes([MSG_SCREENSHOT]) + jpeg)
        except websockets.ConnectionClosed:
            break
        except Exception as e:
            log.warning("auto screenshot error: %s", e)


async def handle_client(ws: websockets.WebSocketServerProtocol) -> None:
    global connected_clients
    peer = ws.remote_address
    log.info("client connected: %s", peer)
    connected_clients += 1
    injector = MouseInjector(SCREEN_W, SCREEN_H)

    # Ask user to allow or deny the connection.
    import subprocess
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "osascript", "-e",
            'display dialog "A Boox device is requesting to connect.'
            f'\\n\\nIP Address:  {peer[0]}'
            f'\\nPort:  {peer[1]}'
            '\\n\\nAllow this device to control your mouse and view your screen?" '
            'with title "Boox Bridge — New Connection" '
            'buttons {"Deny", "Allow"} default button "Allow" '
            'with icon caution '
            'giving up after 30',
        ],
        capture_output=True, text=True,
    )
    if "Deny" in result.stdout or result.returncode != 0:
        log.info("connection denied by user: %s", peer)
        await ws.close(1008, "denied")
        return

    log.info("connection allowed: %s", peer)

    screen_info = json.dumps({"type": "screen_info", "w": SCREEN_W, "h": SCREEN_H})
    await ws.send(screen_info)

    push_task = asyncio.create_task(auto_screenshot(ws))

    n_packets = 0
    try:
        async for message in ws:
            if isinstance(message, str):
                continue
            if len(message) == PACKET_SIZE:
                packet_type, bx, by = PACKET_STRUCT.unpack(message)
                injector.handle(packet_type, bx, by)
                n_packets += 1
            elif len(message) == 1 and message[0] == MSG_SCREENSHOT:
                jpeg = await asyncio.to_thread(grab_screenshot_jpeg)
                await ws.send(bytes([MSG_SCREENSHOT]) + jpeg)
    except websockets.ConnectionClosed:
        pass
    finally:
        push_task.cancel()
        injector.release_all()
        connected_clients -= 1
        log.info("client disconnected: %s  (%d packets)", peer, n_packets)


async def udp_broadcast(ws_port: int) -> None:
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


async def server_main() -> None:
    global server_loop
    server_loop = asyncio.get_event_loop()

    log.info("boox-bridge server starting on ws://%s:%d", HOST, PORT)
    log.info("screen: %dx%d points", SCREEN_W, SCREEN_H)

    svc = _make_service_info(PORT)
    azc = AsyncZeroconf()
    await azc.async_register_service(svc)
    log.info("mDNS registered @ %s:%d", _get_local_ip(), PORT)

    udp_task = asyncio.create_task(udp_broadcast(PORT))

    try:
        async with websockets.serve(handle_client, HOST, PORT, max_size=2 ** 20):
            log.info("listening... ws://%s:%d", _get_local_ip(), PORT)
            await asyncio.Future()
    finally:
        udp_task.cancel()
        await azc.async_unregister_service(svc)
        await azc.async_close()


def _run_server_thread() -> None:
    try:
        asyncio.run(server_main())
    except Exception as e:
        log.error("server thread crashed: %s", e)

# ---------- macOS menu bar app ---------------------------------------------

INTERVAL_OPTIONS = [0.5, 1, 2, 3, 5]

class BooxBridgeApp(rumps.App):
    def __init__(self):
        ip = _get_local_ip()
        super().__init__(
            name="Boox Bridge",
            title="\u270F\uFE0F",  # pencil emoji
            quit_button=None,
        )

        self.status_item = rumps.MenuItem(f"ws://{ip}:{PORT}", callback=None)
        self.status_item.set_callback(None)
        self.clients_item = rumps.MenuItem("Clients: 0", callback=None)
        self.clients_item.set_callback(None)

        self.interval_menu = rumps.MenuItem("Screenshot Interval")
        for val in INTERVAL_OPTIONS:
            label = f"{val}s"
            item = rumps.MenuItem(label, callback=self._set_interval)
            item._interval_value = val
            if val == screenshot_interval:
                item.state = True
            self.interval_menu.add(item)

        self.menu = [
            self.status_item,
            self.clients_item,
            None,  # separator
            self.interval_menu,
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ]

        # Start server in background thread.
        self._server_thread = threading.Thread(target=_run_server_thread, daemon=True)
        self._server_thread.start()

        # Periodic UI update.
        self._timer = rumps.Timer(self._update_ui, 2)
        self._timer.start()

    def _update_ui(self, _sender) -> None:
        self.clients_item.title = f"Clients: {connected_clients}"

    def _set_interval(self, sender) -> None:
        global screenshot_interval
        screenshot_interval = sender._interval_value
        for item in self.interval_menu.values():
            if hasattr(item, '_interval_value'):
                item.state = (item._interval_value == screenshot_interval)
        log.info("screenshot interval changed to %ss", screenshot_interval)

    def _quit(self, _sender) -> None:
        if server_loop:
            server_loop.call_soon_threadsafe(server_loop.stop)
        rumps.quit_application()


if __name__ == "__main__":
    BooxBridgeApp().run()
