# Device control: what the phone can actually do

Audited and implemented on 11 November 2026 against the phone itself: Samsung
Galaxy A20e (`SM-A202F`, `a20e`), Android 11, SDK 30. Every value below was
read from the device (Camera2 characteristics, sensor list, audio and battery
state), not taken from a spec sheet.

## 1. Inventory

| Capability | Hardware / API | State |
| --- | --- | --- |
| Back camera | Camera2 id `0`, flash unit, JPEG up to 4128x3096 | working |
| Front camera | Camera2 id `1`, no flash, JPEG up to 2608x1956 | working |
| Third camera id | Camera2 id `2`, reported as front-facing | listed, not offered by default |
| Flashlight | camera `0` LED, `FLASH_INFO_AVAILABLE` | working |
| Microphone | `AudioRecord`, 16 kHz mono PCM | working |
| Video recording | Camera2 session + `MediaRecorder`, H.264 720p30 and AAC | implemented, shares the preview session |
| Screen rotation | `Settings.System` user rotation, needs `WRITE_SETTINGS` | implemented |
| Speaker | `MediaPlayer` + five audio streams | working |
| Vibration | `Vibrator`, one-shot effects | working |
| Screen brightness | `Settings.System`, needs the `WRITE_SETTINGS` app-op | working |
| Battery | `ACTION_BATTERY_CHANGED` (level, charger, temperature, voltage) | working |
| Storage | `StatFs` on the app data directory | working |
| Sensors | accelerometer, proximity, gyroscope, magnetic field, light | working (light sensor reports nothing) |
| Connectivity | `ConnectivityManager`, `WifiManager`, Bluetooth adapter, airplane mode | read only: transport, validated/metered, link speed, addresses, RSSI, radio states. Radios cannot be switched |

## 2. Architecture

The control features live in a separate Android app, `android/device-control`,
because Termux:API only exposes stills, file recording and vibration — it has no
camera preview, no live microphone and no brightness control.

```
browser ──HTTPS──> Cloudflare ──> nginx:8080 ──> status.py:8765 ──loopback──> app:8790
                    (session cookie + password re-entry)         (pairing token)
```

- The app binds `127.0.0.1:8790` only. It never listens on WiFi.
- `status.py` holds the pairing token in `run/device.json` (mode 0600) and adds
  it to every forwarded request. The browser never sees the token.
- Every device route is behind the console session, and mutating actions are
  recorded in the audit log as `device-<action>`.
- `serverctl` renders nginx with `proxy_buffering off` for `/api/`, so camera
  frames and microphone chunks are not delayed by a buffer.

Pairing: open the pairing window in the app (60 seconds), press **Pair now** in
the console. The dashboard generates a 48-character token, posts it once, and
stores it; the app answers `/pair` only while the window is open.

## 3. Features and endpoints

All console endpoints are session-checked; the parenthesised path is the app
endpoint used through `status.py`.

| Feature | Console endpoint | Notes |
| --- | --- | --- |
| Summary | `GET /api/device/summary` | health, capabilities, battery, storage, volume |
| Live preview | `GET /api/device/camera/stream?camera=&size=&fps=` (multipart MJPEG) | 1-15 fps, 5-minute cap per connection |
| Photo | `POST /api/device/control {"action":"snapshot"}` | JPEG at the current preview size, saved in app storage |
| Gallery | `GET /api/device/photos`, `GET /api/device/photo?file=`, `photo-delete` | served as image/jpeg |
| Video | `action: video-start` / `video-stop`, `GET /api/device/videos`, `video`, `video-delete` | 720p30 MP4 with AAC sound while the preview keeps streaming |
| Rotation | `action: rotation {degrees}` | 0/90/180/270, or `degrees: null` for automatic; locked without `WRITE_SETTINGS` |
| Flashlight | `action: torch` | through the open session when the preview holds camera 0, else `setTorchMode` |
| Vibration | `action: vibrate {ms}` | 1-5000 ms |
| Brightness | `action: brightness {value}` | 1-255, switches the panel to manual mode |
| Volume | `GET /api/device/volume`, `action: volume {stream,index}` | music, ring, alarm, notification, call |
| Microphone live | `GET /api/device/audio/stream` (WAV header + PCM) | decoded in the dashboard with Web Audio |
| Recording | `action: record-start` / `record-stop` | 16 kHz mono WAV, stored in app storage |
| Recordings | `GET /api/device/recordings`, `recording`, `recording-play`, `recording-delete` | play on the phone speaker |
| Media | `GET /api/device/media`, upload, `media-play`, `media-control`, `media-delete` | uploads up to 8 MB |
| Sensors | `GET /api/device/sensors` | one reading per sensor, 800 ms budget |
| Power/storage | `GET /api/device/battery`, `GET /api/device/storage` | |
| Connectivity | `GET /api/device/connectivity` | transport, metered/validated, addresses, link speed, RSSI, Bluetooth and airplane state |

## 4. Android restrictions, stated plainly

1. **Video and preview share one session.** Camera2 allows a JPEG stream and
   MediaRecorder's surface as two targets of the same session, so recording does
   not end the preview; `MediaRecorder`'s own camera binding, which does end it,
   is not used. Two consequences: the video surface caps resolution at 1280x720
   while the JPEG stream keeps its own size, and sound is dropped when the
   microphone is already streaming or recording, because only one capture can
   hold it.
2. **One camera user at a time.** While the preview holds camera `0`,
   `CameraManager.setTorchMode()` fails with `CAMERA_IN_USE`; the app sets
   `FLASH_MODE_TORCH` on the open capture session instead. The torch is released
   when the preview stops.
3. **One microphone owner.** Only one `AudioRecord` can hold the mic, so live
   listening and recording share one capture loop. Two dashboards listening at
   once share it as well.
4. **Two-way audio is half-live.** Phone to browser is a live 16 kHz stream.
   Browser to phone is a file upload played by the phone, not a live browser
   microphone stream: this console does not capture the browser microphone.
   That is a product choice to be made explicitly, not a platform limit.
5. **Brightness needs an app-op.** `WRITE_SETTINGS` is granted with
   `adb shell appops set ie.maurya.galaxy.device android:write_settings allow`;
   without it the endpoint returns 403 and the UI shows "brightness locked".
   Automatic brightness is disabled when a manual value is written.
6. **Rotation is a setting, not a lock.** A chosen angle disables the
   accelerometer rotation and stays until automatic is selected; an app that
   forces its own orientation (camera, video players) can still override it for
   its own window.
7. **The light sensor reports nothing** on this unit (no `TYPE_LIGHT`), so the
   table omits it rather than showing a zero.
8. **The SSID stays hidden.** Android 10+ returns `<unknown ssid>` unless the
   app also holds a location grant, which it deliberately does not request; the
   app reports `hidden by Android` rather than a misleading name. Radios are
   read-only here, so no toggle in this console can take the phone off WiFi.
9. **Streams are time-boxed.** Camera and audio connections close after five
   minutes; the dashboard reconnects by restarting the preview.
10. **Uploads are bounded.** 8 MB per file, and only audio/video container types
   the `MediaPlayer` can decode are worth uploading.
11. **The app must be armed.** The control service runs as a foreground service
   with `camera|microphone` types; disarming it or stopping the app closes the
   port. The console reports "control app not running" instead of failing
   silently.

## 5. Build and install

```sh
cd android/device-control && bash build.sh
adb install -r build/galaxy-device.apk
adb shell appops set ie.maurya.galaxy.device android:write_settings allow
```

The build uses only the installed SDK: `javac`, `d8`, `aapt2`, `zipalign`,
`apksigner`, no Gradle and no network downloads.

## 6. Verification performed on the device

| Check | Result |
| --- | --- |
| `GET /capabilities` | 3 camera ids, both JPEG size lists, permissions granted |
| Snapshot, back camera | `cam0-*.jpg`, 298,330 bytes at 1920x1080 |
| MJPEG preview, 4 fps for 6 s | 10 frames, 3.0 MB, boundary intact |
| Torch with preview open | `CAMERA_IN_USE` avoided; session path reported `via: session` |
| Torch idle | `setTorchMode` path, on and off |
| Brightness | write accepted, `automatic: false`, value read back |
| Vibration | 300 ms one-shot accepted |
| Recording, 4 s | `mic-*.wav`, 131,200 bytes, valid RIFF/WAVE header |
| Live audio, 4 s | 121,644 bytes of 16 kHz PCM |
| Media upload and play | `tone.wav` (32,044 bytes) played, position 777/1000 ms |
| Sensors | accelerometer, proximity, gyroscope values returned |
| Connectivity | WiFi transport, `wlan0`, `192.168.0.74`, 72 Mbps link, -49 dBm, Bluetooth and airplane mode off |
| Proxy | `test_device_proxy.py`, 5 tests: pairing, allowlist, streaming, upload, outages |
| Console | `test_console.cjs` device test: banner, cards, torch/vibrate/brightness requests, preview lifecycle |
