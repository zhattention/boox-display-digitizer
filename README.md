# boox-bridge

Turn a **Boox Tab X** into a wireless writing tablet for a Mac. Designed for
writing inside **ClassIn**'s whiteboard — but works with any Mac app that
accepts a normal mouse.

## Architecture

```
┌──────── Boox Tab X ────────┐        ┌──────── Mac ────────┐
│                            │        │                     │
│  Onyx TouchHelper          │ ─────► │ WebSocket server    │
│    (raw pen events)        │  pen   │   ↓                 │
│                            │ points │ pynput mouse inject │ ──► ClassIn
│  SurfaceView               │        │                     │
│    ├─ background (JPEG)    │ ◄───── │ mss screen capture  │
│    └─ ink layer (SDK 直绘)  │ screen │   → grayscale JPEG  │
│                            │  JPEG  │                     │
└────────────────────────────┘        └─────────────────────┘
```

- **Upstream** (Boox → Mac): high frequency, pen points, ~200 Hz, 9 bytes each.
- **Downstream** (Mac → Boox): low frequency, screen JPEGs, 2–5 fps.
- Single WebSocket, full-duplex.

## Repository layout

```
boox/
├── server/    # Mac side — Python, self-contained, independently testable
└── android/   # Boox side — Kotlin + Onyx Pen SDK, open in Android Studio
```

## Phased plan

- [x] Phase 1 — minimum viable: pen → mouse injection, no screen mirror
- [ ] Phase 2 — reverse screen stream (Mac → Boox background)
- [ ] Phase 3 — calibration UI (pick ClassIn whiteboard rectangle), point interpolation

## Quick start

See [`server/README.md`](server/README.md) and [`android/README.md`](android/README.md).
