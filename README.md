# atvcast

Remux MKVs on a Debian laptop, play them on an Apple TV 4K over the LAN.

## Server (Debian)

Needs Python 3.9+ and `ffmpeg`/`ffprobe` on `PATH`. No pip packages.

```sh
python3 server/atvcast.py --media ~/Videos --port 8080
```

Open <http://localhost:8080/>, pick a file, choose audio/subtitle tracks, hit
Convert. Output MP4s go to `~/.cache/atvcast` (`--cache` to change) with an
`index.json` alongside them.

Remux only — video is always `-c:v copy`. h264 High 10 files are rejected
outright. Audio is decided per track (ac3/eac3/aac copied, anything else
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
