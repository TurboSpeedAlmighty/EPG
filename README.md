# EPG

Daily XMLTV programme guides for the [iptv-org](https://github.com/iptv-org/iptv) United States and Mexico playlists. Channel ids match each playlist's `tvg-id`, including feed tags such as `@SD`.

| Country | Playlist | Guide |
| --- | --- | --- |
| United States | https://iptv-org.github.io/iptv/countries/us.m3u | https://raw.githubusercontent.com/TurboSpeedAlmighty/EPG/guides/us.xml.gz |
| Mexico | https://iptv-org.github.io/iptv/countries/mx.m3u | https://raw.githubusercontent.com/TurboSpeedAlmighty/EPG/guides/mx.xml.gz |

Paste a guide URL into an IPTV player as the XMLTV / EPG source for that playlist. Each file holds 3 days of listings.

The guides are rebuilt every day at 06:00 UTC. GitHub Actions clones [iptv-org/epg](https://github.com/iptv-org/epg), maps playlist channels onto that grabber's `sites/*/*.channels.xml` sources, and runs `npm run grab` with `--gzip`, `--days`, and `--maxConnections`. Listings are merged into one file per country and published by force-pushing a single commit to the orphan `guides` branch (the files are not stored on `main`). If a country build produces no channels, the previous file on `guides` is left in place and that workflow run fails.

## Run a build manually

Scheduled runs start only after this workflow is on `main`. To build immediately: open the **Actions** tab, choose **Build EPG guides**, and click **Run workflow**.

The two URLs above start working after that run succeeds. If Actions is turned off for the repository, enable it under Settings → Actions. If the publish step is denied permission to push, set Settings → Actions → General → Workflow permissions to **Read and write**.
