# PC Drop

Send anything from the iPhone to the PC, from the share sheet — links, text, photos, videos,
any file, folders (zipped), several items at once. Nothing goes through a cloud: the phone talks
straight to `server/server.py` on the PC (home network or NordVPN Meshnet).

Every file type works (.ipa, .zip, .jar, .iso, ...), any size: files are streamed, never loaded
into memory. On the PC, files are saved like browser downloads — original name (`name (2).ext` on a clash),
in your **Downloads** folder. Change the folder on the dashboard (only the PC itself can change it).
Nothing is ever put on the clipboard. HEIC photos can be sent as JPG (toggle in the app).

## Parts

| Path | What |
|---|---|
| `App/` | SwiftUI app: server address, test connection, send files / clipboard |
| `ShareExtension/` | Share extension (accepts everything). Streams files to the PC with progress |
| `Shared/` | Uploader + file helpers used by both |
| `server/server.py` | PC side. Python 3.8+, no dependencies. Dashboard at `http://localhost:8765` |

## Build (no Mac needed)

`project.yml` is the Xcode project ([XcodeGen](https://github.com/yonaskolb/XcodeGen)).
Every push to `main` builds an **unsigned** IPA:

- https://github.com/theunknownblackhathacker/send-to-pc/releases/download/latest/PCDrop.ipa
- build summary on the `build-logs` branch

Sign and install it yourself (e.g. Feather). Keep the bundle IDs and the share extension (PlugIns).
No App Group and no entitlements are needed.

## IDs

- App: `com.kevin.pcdrop` · Share extension: `com.kevin.pcdrop.share`

## Server API

- `POST /send` with `X-Filename: <percent-encoded name>` → raw body saved as a file (streamed, any size)
- `POST /send` with `text/plain` body → saved as text/link
- `POST /send` multipart / urlencoded / JSON → also accepted (iOS Shortcuts, browser)
- `GET /items` → JSON list · `GET /file/<name>` → download
- Optional auth: set `PCDROP_TOKEN` on the PC; the app sends it as `X-Token`
