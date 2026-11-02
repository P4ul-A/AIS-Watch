# AIS Watch · Norwegian Orca Survey

Record and review a Norwegian AIS season from your private LAN/VPN.

## Start

```bash
docker compose up -d --build
```

Open **http://localhost:8080**. Set credentials, collection interval and initial
date in **Settings**. Draw/import a rectangle on **Map**, save and activate it,
then click **Start recording**. Choose whether to include the last 14 days
of available history for the active area or keep its current start/progress.
The history request leaves a five-minute margin within the rolling API window.
Existing local credentials are prefilled.

New checkout: copy `scripts/common/credentials.example.ini` to
`scripts/common/credentials.ini` before starting.

## Features

- **Recording:** discover visiting boats, save their full entry-day tracks, and
  follow for 24 hours after their latest in-area report. Returns renew tracking.
- **Boats:** compact category board with draggable cards showing name, MMSI and colour.
  Use **Move / edit** for category selection, names and incident roles; category
  settings sit within each column. Uncategorized tracks are faded to 15% of
  normal opacity; assigning a category restores normal visibility. Selecting
  a faded boat highlights it at normal opacity.
- **Replay:** choose an Oslo calendar day beside the timeline, scrub and adjust speed.
- **Incidents:** configurable automatic flags, explanations and replay for review.
- **Export tab:** select dates/boats for SQLite script downloads, CSV, GeoJSON or HTML;
  the script bundle includes matching config/area files for `--offline` use.
  Video export is available in replay. Background maps require internet.

## Storage

The `ais-data` Docker volume retains history and preferences indefinitely.
Interrupted collection retries; automatic recovery reaches back 14 days (with a five-minute margin).
Older missed intervals appear as gaps; saved history remains accessible.
**Delete area** stops its watch and removes it from the list; recorded data stays.

Download backups in **Settings**, especially before upgrades. Credentials are
stored separately. `docker compose down -v` deletes the archive.

## Tests

Temporary archives, simulated seasons and crashes; no network or real credentials:

```bash
docker compose build
docker run --rm --network none \
  --mount type=bind,source="$PWD/tests",target=/app/tests,readonly \
  --entrypoint python ais-watch-ais-watch -m unittest discover -s tests -v
```

AIS data: **Kystverket via BarentsWatch**.
