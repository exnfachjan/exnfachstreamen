# Third-party components in `ingest/`

| Component | Origin | License |
|---|---|---|
| `sls` (srt-live-server), `srtla_rec`, `libsrt` | [srtla-server-docker](https://github.com/AlexanderWagnerDev/srtla-server-docker), built on [OpenIRL/srt-live-server](https://github.com/OpenIRL/srt-live-server), [OpenIRL/srtla](https://github.com/OpenIRL/srtla), [OpenIRL/srt](https://github.com/OpenIRL/srt) | GPL-3.0 |
| nginx, nginx-mod-rtmp, ffmpeg, supervisor, SQLite, Alpine base | Alpine Linux | BSD-2-Clause / LGPL-2.1+ / GPL-2.0+ / Public Domain |
| `apikey.py` bootstrap logic | forked from [streamserver](https://github.com/exnfachjan/streamserver) | GPL-3.0-or-later |

Because this image ships GPL-3.0 binaries, `ingest/` (and the project as a
whole, see the repo-root `LICENSE`) is GPL-3.0-or-later. Anyone redistributing
this image must offer the GPL-covered source, available at the links above.
