# Historical ship-track maps

Create an interactive HTML map of selected boats and dates, using either recent
BarentsWatch API positions or historical Parquet files from Kystverket. This uses
the repo's existing dependencies and Leaflet approach. No Docker, Flask server,
database, or spatial extensions are needed. Existing notebooks are not involved.

## Run

From the repository root, install the project's dependencies with Python 3.13+
and [uv](https://docs.astral.sh/uv/), then edit the config beside the script you want to run:

```bash
uv sync
uv run python visualisations/tracks/plot_tracks.py
uv run python visualisations/area/plot_area.py
uv run python visualisations/incidents/find_incidents.py
```

Each script reads its own `config.ini` beside the script by default. Use
`--config path/to/other.ini` to choose another file.

| Script folder | Example config | Purpose |
| --- | --- | --- |
| `tracks/` | `tracks/config.ini` | Map an explicit selection of boats. |
| `area/` | `area/config.ini` | Find and map boats in a rectangle. |
| `incidents/` | `incidents/config.ini` | Flag possible joining incidents for review. |

The examples use the saved BRIM / WHALE2SEA3 encounter in
`output/joining_example/`. Replace `data`, dates, boats, and output paths for your
own analysis. That fixture contains only two vessels; it cannot reveal others.
The incident example lists every flagging threshold and uses a two-minute stay
to reproduce that encounter; the detector's default when omitted remains five.
Shared loading, caching, map assets, and rendering live in `common/`.

Your previous personal configs are preserved as `tracks/config.txt` and
`tracks/position.ini`, with their relative paths adjusted to keep pointing at
the same data and outputs. They remain usable with `--config`.

Open the printed HTML path in a browser (for the track example,
`visualisations/output/selected_tracks.html`). Positions are embedded in the file, so it
can be shared and opened without a local server. Internet access is needed to
load Leaflet and background tiles. No credentials are embedded.

The default background uses [Esri's worldwide topographic tiles](https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer),
so offshore tracks remain covered beyond Norway. The initial view automatically
fits every position across all selected vessels, with padding around the tracks.
The map control also offers the repo's existing
[Kartverket WMTS service](https://cache.kartverket.no/) for Norway detail; its
topographic coverage ends near 71.6°N. OpenStreetMap's standard
tile service requires a web referrer and can block HTML opened directly from a
local `file://` URL, so it is not used here.

The map shows all selected vessels with coloured tracks, vessel toggles, first
and last recorded positions, and timestamps on markers and track tooltips.
Displayed timestamps use `YYYY-MM-DD HH:MM` in UTC; full timestamp precision is
retained in the embedded data and when deciding where to split tracks at gaps.
Click a boat name in the legend, any track segment, or a marker to highlight that vessel's entire track and
show its name and MMSI above the map. The selected vessel's colour picker updates
all its segments, markers, and legend swatch. Click the map background or Clear
selection to clear the highlight. Colour changes last until the page is reloaded.
Clicking a legend name also shows that vessel if it was hidden; the checkbox
continues to control visibility separately.
API maps use the latest non-empty vessel name returned with the selected
positions for labels and popups. If no name is available (including position-only
historical files), the label uses the selected config name, then falls back to MMSI.
Long gaps are left disconnected. Isolated positions remain visible.

## Track replay

Use the time slider below the map to reveal reports up to a selected UTC time.
Press Play to replay the configured interval, and Pause to stop at the current
time. Scrubbing the slider pauses playback. Speed ranges from real time to one
day of recorded time per second; the default is one hour per second. Playback
stops at the end and pauses when the browser tab is hidden. Pressing Play at the
end restarts from the beginning. Show full tracks restores the complete overview.

During replay, older track sections gradually fade toward 20% opacity, so recent
movement stands out while the travelled route stays visible. Fading follows
recorded time, with a six-hour half-life and roughly 15-minute sections; it also
applies when scrubbing and exporting recordings. Current-position markers stay
clear. Selecting a vessel further dims the other vessels as before.
Show full tracks restores uniform opacity across the routes.

Each boat has a replay marker at its latest recorded position, with its name on
hover and the report time on click. Positions advance only when reports arrive;
there is no interpolation or connection across data gaps. If the latest report
is older than `gap_minutes`, the popup says No recent AIS report. Boats without
reports yet remain absent. Selection, colours, and vessel toggles also work
during replay. This replays the embedded historical data without new API calls.

## Export a recording

Choose the replay Speed, then click Export recording · Full HD. The video is
1920 × 1080 at 30 frames per second, with only ticked boats, their colours,
selection highlight, UTC clock, and attribution. **Names** controls
the name legend; turn it off to omit names and give the map more space.
**Tracks** controls the trail lines; turn it off to show only moving ship markers.
Vessel selection and both options are captured when you click Export;
later changes to vessel checkboxes apply to the next export. Tick at least one
ship to export. The recording keeps the
current map zoom and centre and always covers the entire configured interval,
regardless of the slider position. Pan and zoom
before exporting to choose the recording area; tracks outside that area are
clipped. At one day per second, a twelve-day interval produces a twelve-second
video. Faster speeds produce shorter videos. Export leaves the interactive
replay position unchanged.

Frames are rendered and encoded as fast as the browser allows, without waiting
for playback. Rendering time depends on your device and the number of frames;
the selected speed sets video duration rather than export processing speed.
Progress is shown during rendering. Cancel export discards the incomplete video.
The completed file downloads automatically as H.264 MP4, with WebM as a fallback
when H.264 encoding is unavailable. Both formats include duration and seeking
metadata; the export status shows which format is used. Playback and speed controls are locked during export. Colours and
selection are captured when export starts. The expected video length is shown
while preparing the export.

Export loads background tiles at the recording resolution and needs internet
access. If tiles fail to load or the browser lacks a supported video encoder,
an explanation appears beside the button. Fast export requires browser WebCodecs
video encoding support. The video is created locally in the
browser; no track data is uploaded and no screen-sharing permission is needed.

## Configuration

Each `config.ini` is an INI file read with Python's standard `configparser`:

```ini
[tracks]
source = files
start = 2025-06-01
end = 2025-07-01
data = ../data/position/*.parquet
output = output/tracks.html
gap_minutes = 30

[vessels]
BARD = 259019260 / #0072b2
BRIM = 257085820 / #d55e00
```

- `source`: `files` or `api`. Each run uses exactly one source.
- `start` and `end`: ISO dates or timestamps; start included, end excluded.
  For example, June 1–30 is `start = 2025-06-01`, `end = 2025-07-01`.
  Dates without times mean midnight UTC. Timestamps without offsets are UTC;
  explicit offsets are converted to UTC.
- `[vessels]`: the single boat selection for track maps, with one
  `Boat name = 123456789 / #0072b2` entry per boat. Every uncommented entry
  is selected; comment out the whole entry with `#` to disable it. Names,
  MMSIs, and colours are maintained here only, in display order. Duplicate
  MMSIs are loaded once. This works in both API and file mode; no online name
  search is performed. The former `[tracks] ships` option must be removed.
- `data`: a single Parquet path or glob; required only for `files` mode.
- Colours are optional in `[vessels]`: use `Boat name = 123456789 / #RRGGBB`,
  for example `WHALE2SEA 5 = 259045050 / #009e73`. Omit the slash and colour to
  use the automatic palette. Colours apply to tracks,
  markers, and legend swatches. The map's colour picker can override them for
  the current browser session; it does not edit the config. Regenerate the map
  after changing config colours. Invalid colours stop the run
  before fetching positions or replacing the map.
- `output`: HTML path, default `output/tracks.html`. An existing map at this
  path is replaced only after data retrieval and processing succeed.
- `gap_minutes`: positive maximum time between connected reports, default 30.
- API credentials are shared across scripts through
  `visualisations/common/credentials.ini` under `[barentswatch]`.
  `BARENTSWATCH_CLIENT_ID` and `BARENTSWATCH_CLIENT_SECRET` environment variables
  override the corresponding file values. Do not put credentials in run configs.

All relative paths resolve against the config file's directory, regardless of
the directory where you run the command. Keep downloaded data out of Git;
generated maps in `visualisations/output/` are ignored.

## Find all ships in a map area

1. Open your track map and pan/zoom to the area you want to search.
2. Click **Use current map area**. Your browser downloads `area.geojson`, containing
   only the exact visible rectangle. Dates come from the config used for the
   next run. Replay slider position and vessel visibility do not change the area.
3. Run the separate area script from the repository root:

   ```bash
   uv run python visualisations/area/plot_area.py \
       --config visualisations/area/config.ini \
       --area ~/Downloads/area.geojson
   ```

4. Open `visualisations/output/area_tracks.html`. The search rectangle is outlined
   and the initial view fits it. The map includes the available full tracks of
   ships with at least one recorded position inside the rectangle during the
   interval, including their positions outside it. Replay, colours, vessel
   toggles, and video export work as on the original map. The original map is
   preserved with the default output path.

The script reads `source`, `data`, colours, and `gap_minutes` from
its own config. Area discovery selects the boats; `[vessels]` supplies names
and colours without restricting discovery.
Set `[area] file` to a config-relative GeoJSON path, or override it with
`--area`. Dates come from `[tracks] start` and `end`; dates in older GeoJSON files
are ignored. Use `--start` and `--end` to override config dates, for example
`--start 2026-10-25 --end 2026-10-30`. As elsewhere, start is
included and end is excluded. `--output path/to/map.html` overrides the output
path, relative to your working directory. Otherwise `[tracks] output` in its
own config selects the path (the example uses `output/area_tracks.html`). No new Python dependencies are needed.

In **API mode**, the script uses BarentsWatch's
[`POST /v1/historic/mmsiinarea`](https://historic.ais.barentswatch.no/index.html)
to discover vessels, then downloads their tracks. Recent dates must fit within
the rolling 14-day history window. The area endpoint accepts at most 500 km²
and seven days per request. Larger rectangles are split into roughly 19 km
cells, and longer intervals into chunks of at most seven days; MMSIs are
deduplicated across requests. Large searches can therefore take a while, followed
by one track request per ship per day. Credentials stay in Python and are not
exported to the GeoJSON or HTML.

Two terminal progress bars replace per-request output: **Tiles** counts all
tile/time-interval requests, then **Boats** counts vessels whose tracks have been
fully fetched. Both show a percentage, elapsed time, and estimated time remaining.
The boat bar also shows the current MMSI and day; its percentage and ETA advance
after each daily request, even before a whole boat finishes. ETA uses the average
elapsed time per completed request and is unknown until the first finishes.
Response sizes, retries, and API speed can change the estimate. Each ETA covers
its current stage; rendering the map happens afterward. Redirected logs get an
update every 30 seconds plus stage start/end, rather than terminal control codes.
Existing running processes keep their old output until the next launch.
Both scripts also show a **Tracks** bar while preparing vessel tracks for the
map, with completed vessel counts, elapsed time, and ETA. This stage works with
API responses, saved downloads, and Parquet files; vessels with no matching
positions still count as processed.

The API can contain malformed AIS identifiers. Non-nine-digit integer identifiers
are reported and skipped; valid vessels in the same response are retained. These
values are never padded or guessed. Unexpected response structures stop the run.

In **file mode**, DuckDB searches the configured Parquet files for vessels with
positions in the rectangle and interval, then loads those vessels' available
tracks. This can only find ships present in the supplied files. A download
limited to BRIM cannot reveal other vessels: use an area-based download from
[Kystverket](https://hais.kystverket.no/) with no ship filter for that purpose.
An area-limited download may contain only the in-area portions of each track.
Public data coverage and vessel exclusions still apply in either mode. Ships
crossing between reports without a recorded in-area position are not selected.

An old generated HTML needs to be regenerated once to gain the button; later
zoom changes need no Python rerun before exporting another area.

## Save, reuse, and resume API downloads

Add `--save-download` to keep API responses in a compressed SQLite file:

```bash
.venv/bin/python visualisations/area/plot_area.py \
    --area ~/Downloads/area.geojson \
    --save-download visualisations/output/area_download.sqlite
```

Run the same command again to reuse saved responses and download only missing
ones. Each successfully validated tile response and daily ship response is
committed immediately, including empty results. If a run is interrupted, rerun
with the same file, area, and dates to resume completed requests. There is no
need to wait for the HTML map to finish before the download becomes durable.
The usual tile and boat bars also count reused responses; each stage reports
how many requests were reused and newly saved.

For a guaranteed offline rebuild, append `--offline`:

```bash
.venv/bin/python visualisations/area/plot_area.py \
    --area ~/Downloads/area.geojson \
    --save-download visualisations/output/area_download.sqlite \
    --offline
```

Offline mode reads the file without modifying it, needs no API credentials, and
works after the saved interval leaves the API's rolling 14-day window. If any
required request is missing, it stops with an error rather than making a network
request or producing a map with silently missing data. Rebuilds still need time
to process positions and write HTML; displaying the map's tiles uses internet
as before.

Both flags also work with `plot_tracks.py`. These are API options: existing
Parquet file mode already reads saved data. The SQLite file contains position
data and vessel names but no client secrets or access tokens. It is a separate
format from Kystverket's Parquet files; continue using `source = api` with these
flags. Paths passed on the command line resolve against the working directory.

Responses are matched by their exact request coordinates, dates, MMSI, and API
parameters. Colours, gap settings, output paths, and the map template can change
without downloading those responses again. Changing the area or date boundaries
can require additional requests. Reused responses remain the version originally
downloaded; use a new SQLite path for a fresh download. Newly missing API requests
still require valid credentials and recent dates.

Without `--save-download`, the command behaves as before and does not persist
responses. An already running process cannot adopt this option midway through
its download. No additional dependencies are needed, and no notebooks are edited.

## Older history: downloaded files

1. Open [Kystverket's historical download service](https://hais.kystverket.no/).
2. Select ships by MMSI and your dates, choose Parquet, and request the data.
   The service delivers a download link by email and currently allows up to one
   year per request. Check available historical dates in the service.
3. Download and extract the ZIP. Point `data` at the extracted `position/*.parquet`
   files, and set `source = files`. For multiple downloads, use a glob covering
   their position files, for example `../data/**/position/*.parquet`.
4. Set `[vessels]` entries and dates in the config, then run the command above.

The script reads the [documented Kystverket columns](https://hais.kystverket.no/about):
`mmsi`, `date_time_utc`, `longitude`, `latitude`. It deliberately does not read
the geometry column. DuckDB filters ships and dates before loading the selected
positions into memory. The repo's older notebook exports have a different column
schema; this command expects the current Kystverket download format.

## Recent history: API

The [BarentsWatch historical API](https://developer.barentswatch.no/docs/AIS/historic-ais-api/)
provides only the past 14 days. Register a BarentsWatch account and create an API
client on My Page with access to the `ais` scope. Copy
`visualisations/common/credentials.example.ini` to
`visualisations/common/credentials.ini` and add your credentials there:

```ini
[barentswatch]
client_id = your-client-id
client_secret = your-client-secret
```

Use the values directly, without surrounding quotes. The shared credentials file
is ignored by Git and used by tracks, area, and incident scripts. Credentials
are never embedded in the generated map. Alternatively, set shell environment
variables, which take precedence over the shared file:

```bash
export BARENTSWATCH_CLIENT_ID='your-client-id'
export BARENTSWATCH_CLIENT_SECRET='your-client-secret'
```

Set `source = api` in the chosen script's `config.ini` and choose dates entirely within the past 14
days. `data` is ignored. For example, set the start to yesterday's UTC date and
the end to today's UTC date to plot the previous complete UTC day. The checked-in
historical example dates must be changed before running API mode.

Authentication uses client credentials and scope `ais`. Requests are sequential,
with at most one day per vessel per request. Both satellite and terrestrial
positions are requested. Tokens refresh before expiry. HTTP requests have a
10-second connection timeout and a 60-second read timeout, with up to three
retries and bounded exponential backoff for transient failures and rate limits.
The command fails rather than writing a partial map if any retrieval fails.

## Interpretation and troubleshooting

- Console output lists each vessel's usable point count; ships with no positions
  also appear in the map header. An entirely empty selection exits with an error.
- Positions are sorted, exact duplicate fixes removed, and missing/invalid
  timestamps and coordinates discarded. Coordinates are longitude/latitude in
  WGS84; geographic boundary filtering and correction of plausible but erroneous
  AIS fixes are not performed. Connected lines are observations joined in time,
  not a reconstructed route between observations.
- Coverage can be incomplete. Open data excludes certain small fishing and
  recreational vessels; see the source documentation. Credit Kystverket when
  sharing the visualisations.
- Missing files or incompatible columns: check the extracted `position/` path
  and download format. API errors: check credentials, API access, connectivity,
  and the rolling 14-day limit. Use file mode for older dates.
- On retrieval/validation failure, any existing output file is left in place;
  it belongs to the previous successful run. Check the command's exit status.

The map bundles Mediabunny 1.62.1 in `common/vendor/mediabunny.cjs` for frame-by-frame
encoding and MP4/WebM output. Its MPL-2.0 license is included alongside the bundle;
the unmodified source is available at
https://www.npmjs.com/package/mediabunny/v/1.62.1.
No additional Python dependencies or tests are introduced.

## Flag possible whale-watching joining incidents

The separate incident command produces a chronological list of **possible joining
incidents for manual review**, beside the existing track map and replay. It also
writes a CSV with the same rows. AIS movement does not establish that whales were
present, or whether a boat was searching independently.

Edit `[incidents]` in `incidents/config.ini`, which contains a commented example
of every flagging rule. Put one name or nine-digit
MMSI per line; names resolve through the existing `[vessels]` registry. Replace
the example identifiers with your own. Groups must be non-empty and must not
overlap. Incident groups select the boats; `[vessels]` supplies their names,
MMSIs, and colours, without automatically adding them to either group.

```ini
[incidents]
small_boats =
    123456789
big_boats =
    987654321
radius_m = 500
max_small_speed_knots = 3
prior_minutes = 5
together_minutes = 5
max_gap_minutes = 5
```

All five thresholds are optional, positive finite numbers, with the defaults
shown. They are exploratory settings, not established definitions of watching
or joining. Try different radii and review the resulting encounters.

Use the same exported rectangle, interval, and saved download as your area
map, with the separate incident config set to `source = api`:

```bash
.venv/bin/python visualisations/incidents/find_incidents.py \
    --config visualisations/incidents/config.ini \
    --area ~/Downloads/area.geojson \
    --save-download visualisations/output/area_download.sqlite
```

The config's `[tracks] output` selects the review HTML path; the companion CSV
uses the same stem. The example writes `output/incidents.html` and
`output/incidents.csv`. Use `--output path/to/review.html` to choose another location;
the CSV will be `path/to/review.csv`. Existing incident outputs are replaced on a
successful run. The original area-map output is not changed unless explicitly
chosen as the output path.

Area discovery uses the same tile/time requests and cache keys as `plot_area.py`.
Only configured vessels discovered in the rectangle have their full-interval
tracks loaded. Files mode uses the configured local Parquet files instead.
Dates come from the config; `--start` and `--end` override config dates.
An incident's joining location (the small boat's analysis position) must be
inside the rectangle; surrounding tracks can extend outside it.

To analyse an existing area download without any network requests, use the exact
same area and interval and append `--offline`:

```bash
.venv/bin/python visualisations/incidents/find_incidents.py \
    --config visualisations/incidents/config.ini \
    --area ~/Downloads/area.geojson \
    --save-download visualisations/output/area_download.sqlite \
    --offline
```

Changing detector thresholds or the two boat lists does not change area request
keys. A saved area-map download already includes tracks for the vessels it found.
A download made by this incident command includes only its selected vessels;
adding boats later may require more downloads. Missing cache requests fail
explicitly in offline mode. Configured vessels absent from the area data are
reported in the review's settings. An empty analysis still produces a review
page and CSV header.

### What gets flagged

Analysis uses a one-minute timeline starting at the selected interval's start.
Between consecutive recorded fixes, positions are linearly interpolated only
when the report interval is at most `max_gap_minutes`; no extrapolation is used.
Speed is calculated from the original fixes' great-circle displacement and
elapsed time, rather than relying on reported AIS speed. Conflicting positions
at the same timestamp invalidate adjacent intervals for analysis. Original
reports remain unchanged in the map and replay.

A flag requires all of the following:

- The small boat has remained at or below `max_small_speed_knots` throughout the
  preceding `prior_minutes`, with every analysis position within `radius_m` of
  its position at that window's beginning.
- The big boat was outside the moving circle throughout that preceding window,
  then enters it. Both boats must have coverage throughout the window.
- The big boat remains inside for at least `together_minutes`, while the small
  boat stays below the speed threshold. A course change is not required.

Prior windows round up to whole minutes; staying duration must reach the
configured minimum on the one-minute timeline. Boundary distances count as
inside. Missing coverage interrupts a visit. Already-together boats and visits
without an observed outside window cannot establish arrival order and are not
flagged. Each continuous visit produces at most one row per pair. A departure
and qualifying return can produce another row.

Rows include both names/MMSIs, joining time, last qualifying inside time,
observed departure time where available, duration, closest distance, verified
prior-activity window, distance closed over that window, end reason, and flag
reason. All timestamps are UTC. Ending because of missing coverage, the end of
the interval, or the small boat speeding up is distinguished from an observed
departure. These measurements have one-minute analysis resolution and inherit
the uncertainty of interpolated AIS positions.

Each row has a **Why flagged** explanation covering the arrival sequence,
thresholds, measured approach, qualifying duration, closest distance, and why
the qualifying window ended. Selecting a row opens its explanation; the same
text is included in the CSV's `reason` column.

Select a row to highlight both boats, fit their tracks around the encounter, and
pause replay up to 20 minutes before joining, limited by available reports.
During replay, the dashed circle follows the small boat's analysis position;
it disappears where analysis coverage is missing and in full-track overview.
Boat markers continue to show their latest original reports, so they may differ
slightly from the interpolated circle. The on-page CSV download contains the same
incident fields as the companion CSV. Review annotations are not saved.

## Large-area map performance

The area example uses these display settings under `[tracks]`:

```ini
display_interval_seconds = 300
fade_trails = false
```

This keeps every boat but shows the first recorded position in each five-minute
bucket, plus both endpoints of each gap-separated segment. The map labels this
as a sampled overview. Set `display_interval_seconds = 0` to display every report,
or use a shorter interval for more detail. Sampling affects map/replay/video
positions only; the SQLite download and incident calculations retain all reports.
The interval must not exceed `gap_minutes * 60`.

Disabling trail fading avoids constructing separate paths for every fifteen
minutes of every boat's history. Replay and vessel selection remain available.
For small maps, defaults remain full-resolution positions with fading enabled.
Maps with more than fifty boats start with the vessel legend collapsed.

Track data is gzip-compressed inside the HTML and unpacked locally by the browser.
The map remains a single file with no local server or new data download required.
The browser must support gzip `DecompressionStream`; a loading message explains
unpacking failures. Internet is still required for Leaflet and background tiles.
Regenerate older HTML maps to apply these changes.
