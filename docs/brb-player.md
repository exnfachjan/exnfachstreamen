# BRB clips (optional)

While the BRB scene is on air, a browser source called **BRB Player** can play
your Twitch clips on top of the BRB media. Off by default. Set it in the
dashboard → **BRB screen → Clips**.

When there is nothing to play (no clips yet, everything filtered out), the
player goes transparent and the normal BRB media shows. The BRB media is
muted while clips are on so the two don't overlap.

| Setting | Effect |
|---|---|
| Top / Random | most viewed first, or any clip in range (5 swapped for new ones every 30 min) |
| Range | last 24 h, 7 days, 30 days, 6 months, year, or all time |
| Max length | skip longer clips, or play them and cut off at the limit |
| Skip titles containing | comma-separated words; matching clips are left out |
| Only clips by | comma-separated clipper names; empty = everyone |
| Show title, clipper & date | info card bottom left for the first 6 s of each clip |
| Countdown | ring with the seconds left, bottom right |

No Twitch app or token needed. The dashboard fetches up to 20 clips, converts
them once to H.264 (Twitch delivers clips as HEVC only, which OBS's browser
cannot decode), stores them in `data/media/brb/clips/` and refreshes the list
every 30 minutes. A first load of 20 clips takes about 3-4 minutes; the status
line in the dashboard shows the progress.

Not included: "prefer the current category", which needs the official Twitch
API and an app key.

## Why there is no YouTube playlist

It was built and removed again. YouTube refuses to play on server IPs
(Hetzner and other data centres: "Sign in to confirm you're not a bot"), and
the embedded player is exactly what gets blocked - the official YouTube API
only returns metadata, not the video. Workarounds (a signed-in Google account
inside OBS, or routing YouTube through a home connection) were judged not
worth it.
