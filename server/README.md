# boox-bridge — Mac server

## Install

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python server.py
```

First time running, macOS will prompt for **Accessibility** permission — this
is required so the server can inject system-level mouse events via
`CGEventPost`. Allow the terminal you ran `python` from (or Python itself) in
System Settings → Privacy & Security → Accessibility.

If the permission dialog does not appear and the mouse fails to move, manually
add Terminal/iTerm/Python to the Accessibility list and restart `server.py`.

## Configure

Edit `TARGET_RECT` at the top of `server.py`. It is the rectangle on your Mac
screen where the Boox maps to. Format: `(x, y, width, height)` in screen
points (not pixels on Retina).

Tip for finding coordinates: open Preview → take a screenshot
(`Shift-Cmd-5` → *Record Selected Portion*), drag a rectangle over ClassIn's
whiteboard, note the coordinates shown in the selection indicator.

In Phase 3 this will be replaced by an interactive calibration picker.

## Test without the Boox

You can verify the server works before building the Android app. Use any
WebSocket client that can send binary frames, for example `websocat`:

```bash
# install websocat via brew first: brew install websocat
# Then connect:
websocat ws://localhost:9999 --binary
```

Each binary frame must be exactly 9 bytes:
- 1 byte: packet type (0=down, 1=move, 2=up)
- 4 bytes: x as float32 big-endian, 0.0–1.0
- 4 bytes: y as float32 big-endian, 0.0–1.0

Or more easily, send test packets from another Python script:

```python
import asyncio, struct, websockets

async def main():
    async with websockets.connect("ws://localhost:9999") as ws:
        s = struct.Struct("!Bff")
        await ws.send(s.pack(0, 0.5, 0.5))  # pen down at center
        await asyncio.sleep(0.1)
        await ws.send(s.pack(1, 0.6, 0.5))  # move right
        await asyncio.sleep(0.1)
        await ws.send(s.pack(2, 0.6, 0.5))  # pen up

asyncio.run(main())
```

You should see the cursor jump to the middle of your target rectangle, drag
right, then release. If the cursor moves but no drawing happens in ClassIn,
check that ClassIn's pen tool is selected, not its selector.
