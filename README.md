# Boox Display Digitizer

Turn your Boox e-ink tablet into a wireless pen digitizer.

Write on your Boox with the stylus and it appears on your Mac in real time — in any app that accepts mouse input. The Mac screen is mirrored back to the Boox so you can see exactly where you're drawing.

## Features

- **Pen → mouse injection** — stylus strokes on the Boox become mouse events on the Mac (~200 Hz, low latency)
- **Screen mirroring** — Mac screen streamed to Boox as grayscale JPEG (configurable 0.5–5 fps)
- **Pan & zoom** — two-finger gestures to navigate the mirrored screen on the Boox
- **Auto-discovery** — server broadcasts on the LAN, the Boox app finds it automatically
- **Connection auth** — Mac shows an Allow/Deny dialog for each new connection
- **Menu bar app** — server runs as a macOS status bar app with configuration menu
- **Eraser support** — pen eraser maps to right-click

## Architecture

```
┌──────── Boox ─────────┐        ┌──────────── Mac ──────────────┐
│                       │        │                               │
│  Onyx Pen SDK         │ ─────► │ WebSocket server              │
│   (raw stylus input)  │  pen   │   ↓                           │
│                       │ events │ CoreGraphics mouse injection  │
│  SurfaceView          │        │                               │
│   ├─ background JPEG  │ ◄───── │ mss screen capture            │
│   └─ ink layer        │ screen │   → grayscale → JPEG          │
│                       │        │                               │
│  UDP listener         │ ◄───── │ UDP broadcast (discovery)     │
│  NSD (mDNS) listener  │ ◄───── │ Bonjour/zeroconf             │
└───────────────────────┘        └───────────────────────────────┘
```

Single WebSocket connection, full-duplex, over Wi-Fi.

## Quick start

### Mac (server)

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

A ✏️ icon appears in the menu bar. First run will prompt for **Accessibility** permission (System Settings → Privacy & Security → Accessibility).

### Boox (Android app)

Build and install via Android Studio, or from the command line:

```bash
source env.sh
cd android
./gradlew installDebug
```

Open **Boox Display Digitizer** on the Boox. The server is discovered automatically — tap **Connect**.

> First install requires one-time ADB setup for Onyx SDK access:
> ```bash
> adb shell pm grant com.boox.bridge android.permission.WRITE_SECURE_SETTINGS
> ```

### Pre-built releases

Download the latest DMG (Mac) and APK (Boox) from [Releases](https://github.com/zhattention/boox-display-digitizer/releases).

## Repository layout

```
├── server/       # Mac — Python, menu bar app, WebSocket server
├── android/      # Boox — Kotlin + Onyx Pen SDK
├── .github/      # CI: builds macOS DMG (x86_64 + arm64) + APK
└── env.sh        # Android toolchain setup
```

## Configuration

### Server (environment variables)

| Variable | Default | Description |
|---|---|---|
| `BOOX_BRIDGE_PORT` | `9999` | WebSocket port |
| `BOOX_BRIDGE_FPS_INTERVAL` | `1` | Screenshot push interval (seconds) |

Screenshot interval can also be changed at runtime from the menu bar icon.

## Compatibility

- **Mac**: macOS 12+ (Intel and Apple Silicon)
- **Boox**: Tab X and other Onyx devices with Pen SDK (Android 9+)
- Works with any Mac app — ClassIn, Freeform, Excalidraw, Photoshop, etc.

## License

MIT
