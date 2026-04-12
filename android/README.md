# boox-bridge — Android (Boox Tab X)

Kotlin app that uses the **Onyx Pen SDK** to capture raw pen input on a
Boox device and stream it over WebSocket to the Mac server.

## Requirements

- **Android Studio** (Koala / Iguana or newer, any 2024+ release)
- Boox Tab X (or any Onyx device with the Pen SDK)
- Tab X and Mac on the **same Wi-Fi / LAN**

The Onyx SDK jars come from the Boox Maven repo (declared in
`settings.gradle.kts`) — Android Studio will download them on first sync.

## Build & install

1. **Open the project**: Android Studio → *Open* → pick the
   `android/` directory (not the repo root).
2. **First sync**: Android Studio will download Gradle, the Android Gradle
   Plugin, and the Onyx SDK jars. If sync fails complaining about the HTTP
   Boox repo, verify `settings.gradle.kts` still has
   `isAllowInsecureProtocol = true` for `http://repo.boox.com/...`.
3. **Enable USB debugging on the Boox**:
   - Settings → System → About → tap *Build number* 7 times to unlock
     developer options.
   - Settings → System → Developer options → enable *USB debugging*.
4. **Connect the Boox via USB** to your Mac. Accept the RSA fingerprint
   dialog on the Boox.
5. **Run**: in Android Studio, press ▶ *Run 'app'*. Pick the Boox from the
   device list.

Alternatively, from the command line (requires `adb` in your PATH):

```bash
cd android
./gradlew installDebug
adb shell am start -n com.boox.bridge/.MainActivity
```

If you don't have `./gradlew` yet, open the project once in Android Studio
(it will generate `gradle/wrapper/*` automatically) or run
`gradle wrapper --gradle-version 8.5` in `android/`.

## Use it

1. Start the Mac server first (see `../server/README.md`).
2. On the Boox, launch **Boox Bridge**. You'll see a full-screen white
   canvas with a small overlay at the top-left:
   - a text field for the server URL
   - a *Connect* button
   - a status indicator
3. Enter `ws://<mac-ip>:9999` (find your Mac's IP in System Settings → Wi-Fi
   → click *Details* next to your network).
4. Tap *Connect*. Status should change to **connected**.
5. Write anywhere on the canvas (except the overlay rectangle). Each stroke
   will:
   - show as ink on the Boox (SDK-rendered, very low latency)
   - be injected as mouse drag events on the Mac → ClassIn whiteboard (or
     whatever app is focused).

## Known limitations (Phase 1)

- No screen mirroring from the Mac yet — you need to glance at the Mac to
  see where you are in the target rectangle. Phase 2 fixes this.
- The target rectangle on the Mac is hardcoded in `server/server.py`
  (`TARGET_RECT`). You must edit it to match your ClassIn whiteboard
  position. Phase 3 adds an interactive calibration picker.
- If the Boox goes to sleep or you background the app, you may need to
  tap *Connect* again.
- If you see no ink on the Boox while writing, make sure the app is in
  the foreground — the raw drawing mode is paused in `onPause()`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Status stuck on **connecting…** | Mac IP wrong, firewall blocking port 9999, or Mac server not running |
| Status = **connection failed** | Tap Connect again after starting the server; check Mac's firewall (System Settings → Network → Firewall) |
| Pen writes on Boox but cursor doesn't move on Mac | Mac did not grant Accessibility permission to Python — see `server/README.md` |
| Cursor moves but ClassIn doesn't draw | ClassIn's pen tool isn't selected; or the target rectangle is outside the whiteboard area |
| Pen ink appears on overlay area too | Overlay exclude rect not computed yet (surface hadn't been laid out). Rotate or restart the app |
