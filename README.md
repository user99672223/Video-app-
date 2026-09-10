# atvcast

Remux MKVs on a Debian laptop, play them on an Apple TV 4K over the LAN.

## Server (Debian)

Needs Python 3.9+ and `ffmpeg`/`ffprobe` on `PATH`. No pip packages.

Install it as a desktop app once:

```sh
sudo apt install -y ffmpeg
python3 server/atvcast.py --install-desktop
```

Then launch **atvcast** from your applications menu — it starts the server,
opens the UI in your browser, and shows the address to type into the Apple TV.
The media folder is set in the UI and remembered in
`~/.config/atvcast/config.json`; a busy port rolls forward automatically.

Still works headless: `python3 server/atvcast.py --media ~/Videos --port 8080`.
`--no-browser` suppresses the browser, `--uninstall-desktop` removes the entry.

Pick a file, choose audio/subtitle tracks, hit Convert. Output MP4s go to
`~/.cache/atvcast` (`--cache` to change) with an `index.json` alongside them.

Remux only — video is always `-c:v copy`. HEVC is retagged `hvc1` (`dvh1` with
a Dolby Vision RPU) because ffmpeg's default `hev1` will not decode on an Apple
TV. h264 High 10 files are rejected outright. Audio is decided per track (ac3/eac3/aac copied, anything else
re-encoded to E-AC-3 768k, max 6 channels). Text subtitles become `mov_text`,
bitmap ones (PGS/VobSub) are dropped and reported. HDR is never tone mapped;
color tags are verified after muxing and re-applied if the MP4 lost them.

Endpoints: `GET /library` (JSON), `GET /media/{id}.mp4` (Range/206 supported).
Binds `0.0.0.0`.

Decision-logic sanity check: `python3 server/atvcast.py --selftest`
(add `--sample /path/to/file.mkv` to also probe a real file).

## tvOS app

Xcode project is generated, not committed: `xcodegen generate`.

CI builds it unsigned on every push. The IPA lands in the **`TVApp-unsigned-ipa`**
artifact of the `build` workflow run (Actions → run → Artifacts) as
`TVApp-unsigned.ipa`. Sideload it, open Settings, enter `laptop-ip:8080`, then
Library.
