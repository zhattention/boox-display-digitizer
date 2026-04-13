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
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSMakeRect,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Quartz import (
    CGDisplayBounds,
    CGEventCreateMouseEvent,
    CGEventPost,
    CGMainDisplayID,
    CGPreflightScreenCaptureAccess,
    CGRequestScreenCaptureAccess,
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
    if not CGPreflightScreenCaptureAccess():
        log.error("grab_screenshot called but screen capture is NOT permitted")
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    if img.getbbox() is None:
        log.warning("captured image is completely blank — permission likely denied")
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
    gave_up = "gave up:true" in result.stdout.replace(" ", "")
    if "Deny" in result.stdout or gave_up or result.returncode != 0:
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

    udp_task = asyncio.create_task(udp_broadcast(PORT))

    try:
        async with websockets.serve(handle_client, HOST, PORT, max_size=2 ** 20):
            log.info("listening... ws://%s:%d", _get_local_ip(), PORT)
            await asyncio.Future()
    finally:
        udp_task.cancel()


def _run_server_thread() -> None:
    try:
        asyncio.run(server_main())
    except Exception as e:
        log.error("server thread crashed: %s", e)

# ---------- macOS menu bar app ---------------------------------------------

INTERVAL_OPTIONS = [0.5, 1, 2, 3, 5]
APP_VERSION = "0.3.0"


def _make_label(text: str, frame, size: float = 13,
                bold: bool = False, color=None) -> NSTextField:
    label = NSTextField.alloc().initWithFrame_(frame)
    label.setStringValue_(text)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    label.setFont_(font)
    if color:
        label.setTextColor_(color)
    return label


class InfoWindow:
    """Native macOS window showing server status. Hides on close instead of quitting."""

    def __init__(self, ip: str, port: int):
        self.ip = ip
        self.port = port
        w, h = 380, 230
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(200, 400, w, h), style, NSBackingStoreBuffered, False,
        )
        self.window.setTitle_("Boox Display Digitizer")
        self.window.setReleasedWhenClosed_(False)
        self.window.setLevel_(3)  # floating

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))

        gray = NSColor.secondaryLabelColor()

        content.addSubview_(_make_label(
            "Boox Display Digitizer", NSMakeRect(24, 180, 340, 24), size=18, bold=True))
        content.addSubview_(_make_label(
            f"v{APP_VERSION}", NSMakeRect(24, 160, 340, 18), size=11, color=gray))
        content.addSubview_(_make_label(
            "Server", NSMakeRect(24, 128, 80, 18), size=12, bold=True))
        content.addSubview_(_make_label(
            f"ws://{ip}:{port}", NSMakeRect(110, 128, 240, 18), size=12))
        content.addSubview_(_make_label(
            "Status", NSMakeRect(24, 104, 80, 18), size=12, bold=True))
        self._status_label = _make_label(
            "Running", NSMakeRect(110, 104, 240, 18), size=12,
            color=NSColor.systemGreenColor())
        content.addSubview_(self._status_label)

        content.addSubview_(_make_label(
            "Screen", NSMakeRect(24, 80, 80, 18), size=12, bold=True))
        self._screen_label = _make_label(
            "Checking...", NSMakeRect(110, 80, 240, 18), size=12, color=gray)
        content.addSubview_(self._screen_label)

        content.addSubview_(_make_label(
            "Close this window — the app continues in the menu bar",
            NSMakeRect(24, 28, 340, 32), size=11, color=gray))

        self.window.setContentView_(content)

        # Request screen capture permission on first launch.
        log.info("requesting screen capture access...")
        granted = CGRequestScreenCaptureAccess()
        log.info("CGRequestScreenCaptureAccess() returned %s", granted)
        self._update_screen_permission()

    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        self.window.center()
        NSApp.activateIgnoringOtherApps_(True)

    def hide(self):
        self.window.orderOut_(None)

    def _update_screen_permission(self):
        has_access = CGPreflightScreenCaptureAccess()
        log.debug("CGPreflightScreenCaptureAccess() = %s", has_access)
        if has_access:
            self._screen_label.setStringValue_("Recording Allowed")
            self._screen_label.setTextColor_(NSColor.systemGreenColor())
        else:
            log.warning("screen capture NOT permitted — user must grant in "
                        "System Settings > Privacy & Security > Screen Recording")
            self._screen_label.setStringValue_("No Permission — grant in System Settings")
            self._screen_label.setTextColor_(NSColor.systemRedColor())

    def update_clients(self, count: int):
        status = f"Running — {count} client(s)" if count > 0 else "Running — waiting for connection"
        self._status_label.setStringValue_(status)
        self._update_screen_permission()


class BooxBridgeApp(rumps.App):
    def __init__(self):
        ip = _get_local_ip()
        super().__init__(
            name="Boox Display Digitizer",
            title="\u270F\uFE0F",
            quit_button=None,
        )

        self._info_window = InfoWindow(ip, PORT)

        self.menu = [
            rumps.MenuItem(f"ws://{ip}:{PORT}"),
            rumps.MenuItem("Clients: 0"),
            rumps.MenuItem("Screen: Checking..."),
            None,
            rumps.MenuItem("Screenshot Interval"),
            None,
            rumps.MenuItem("Show Window", callback=self._show_window),
            rumps.MenuItem("Quit", callback=self._quit),
        ]

        # Make status items non-clickable.
        self.menu[f"ws://{ip}:{PORT}"].set_callback(None)
        self.menu["Clients: 0"].set_callback(None)
        self.menu["Screen: Checking..."].set_callback(None)

        # Screenshot interval submenu.
        interval_menu = self.menu["Screenshot Interval"]
        for val in INTERVAL_OPTIONS:
            item = rumps.MenuItem(f"{val}s", callback=self._set_interval)
            item._interval_value = val
            if val == screenshot_interval:
                item.state = True
            interval_menu.add(item)

        # Start server.
        self._server_thread = threading.Thread(target=_run_server_thread, daemon=True)
        self._server_thread.start()

        # Show startup window.
        self._info_window.show()

        # Periodic UI update.
        self._timer = rumps.Timer(self._update_ui, 2)
        self._timer.start()

    def _update_ui(self, _sender) -> None:
        self.menu["Clients: 0"].title = f"Clients: {connected_clients}"
        has_access = CGPreflightScreenCaptureAccess()
        self.menu["Screen: Checking..."].title = (
            "Screen: Allowed" if has_access else "Screen: No Permission"
        )
        self._info_window.update_clients(connected_clients)

    def _show_window(self, _sender) -> None:
        self._info_window.show()

    def _set_interval(self, sender) -> None:
        global screenshot_interval
        screenshot_interval = sender._interval_value
        for item in self.menu["Screenshot Interval"].values():
            if hasattr(item, '_interval_value'):
                item.state = (item._interval_value == screenshot_interval)
        log.info("screenshot interval changed to %ss", screenshot_interval)

    def _quit(self, _sender) -> None:
        if server_loop:
            server_loop.call_soon_threadsafe(server_loop.stop)
        rumps.quit_application()


if __name__ == "__main__":
    BooxBridgeApp().run()
