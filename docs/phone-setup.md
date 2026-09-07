# Phone setup - bonded SRTLA ingest

You don't build anything on the phone: use an existing IRL streaming app that
speaks SRTLA (BELABOX's bonding protocol). Recommended:

| Platform | App | Notes |
|---|---|---|
| iOS | **Moblin** (free, open source) | First-class SRTLA + multi-link bonding |
| Android | **IRL Pro** | SRTLA supported, popular with IRL streamers |
| Android | **BELABOX** (with hardware encoder rig) | The reference SRTLA implementation |

One key, all three protocols - copy the exact URLs from the dashboard's
"Connect your encoder" card once the stack is running (it fills in your real
`PUBLISH_KEY` and domain). The patterns below use `<KEY>` as a placeholder.

## Connection settings

- **URL / server**: `srtla://<server-ip>:5000`
  (in apps that ask for host+port separately: host `<server-ip>`, port `5000`,
  protocol SRTLA)
- **Stream ID**: `<KEY>` (just the key itself - sls, not MediaMTX, is the
  ingest server now, so no `publish:live:user:pass` prefix).
- **Bonding**: enable both links (e.g. cellular + wifi). In Moblin:
  Settings → Streams → your stream → SRT(LA) → enable bonding/second connection.
- **Latency**: 2000 ms is a good starting point for cellular.
- **Bitrate**: start at 3500–5000 kbps adaptive.

## Fallbacks (no SRTLA app handy)

- **Plain SRT** (single link): `srt://<server-ip>:4001?streamid=<KEY>`
  Works from Larix Broadcaster, Moblin, OBS on a laptop, ffmpeg, etc.
- **RTMP** (last resort, least resilient): server `rtmp://<server-ip>:1935/live`,
  stream key `<KEY>`. Needs **H.264 + AAC** - HEVC/H.265 breaks the internal
  RTMP→SRT bridge silently (use SRT or SRTLA instead if your encoder only
  does HEVC).

## How the failsafe behaves

- Your phone feed drops → within ~1.5 s the server switches viewers to your BRB
  screen. The Twitch stream itself never stops.
- Feed comes back → after ~3 s of stable data the server snaps back to LIVE.
- If your connection is bouncing rapidly, the watchdog parks on BRB and waits
  for a longer stable window before returning, so viewers don't see strobing.
