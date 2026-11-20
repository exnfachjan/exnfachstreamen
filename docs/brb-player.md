# BRB playlist (optional)

While the BRB scene is on air, a browser source called **BRB Player** can play
your recent Twitch clips and/or a YouTube playlist on top of the BRB media.
Off by default. Set it in the dashboard → **BRB screen → Playlist**.

| Mode | Plays |
|---|---|
| Off | just the uploaded BRB video/image |
| YouTube playlist | the playlist, looped (shuffle optional) |
| My Twitch clips | the channel's top clips of the last 24 h / 7 d / 30 d / all time |
| Both | every clip once, then one YouTube video, repeat |

When nothing can play (no clips yet, YouTube blocked), the player goes
transparent and the normal BRB media shows. The BRB media is muted while the
playlist is on so the two don't overlap.

## Twitch clips

No Twitch app or token needed. The dashboard fetches up to 20 clips, converts
them once to H.264 (Twitch delivers clips as HEVC only, which OBS's browser
cannot decode), stores them in `data/media/brb/clips/` and refreshes the list
every 30 minutes. A first load of 20 clips takes about 3-4 minutes; the status
line in the dashboard shows the progress.

## YouTube: sign in once inside OBS

YouTube treats server IPs (Hetzner and other data centres) as bots and
refuses to play there - the status line then says
`youtube blocked - sign in to YouTube inside OBS`. A signed-in session gets
through. Use a **separate Google account**, not your main one: it lives on the
server.

1. Open OBS in the browser: `https://<panel>/obs/vnc.html?path=obs/websockify&autoconnect=true`
2. OBS menu **Docks → Custom Browser Docks** → name `YouTube`, URL
   `https://accounts.google.com/ServiceLogin?continue=https://www.youtube.com` → Apply
3. Sign in in that dock, then close it. The login is stored in OBS's browser
   profile, which the BRB Player shares.
4. Dashboard → Playlist → **Save** (reloads the player).

## Rights

Music and videos from YouTube on a Twitch stream can get a DMCA strike or
muted VODs. Stick to playlists you are allowed to broadcast (your own videos,
licence-free music such as NCS).
