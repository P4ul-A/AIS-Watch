"""Load AIS tracks and render standalone maps for the visualisation scripts."""

import base64
import configparser
import gzip
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pandas as pd
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .download_cache import DownloadCache, request_key
from .progress import Progress

COLUMNS = ["mmsi", "timestamp", "longitude", "latitude"]
API_ROOT = "https://historic.ais.barentswatch.no/v1/historic/tracks"
TOKEN_URL = "https://id.barentswatch.no/connect/token"
MMSI_DIGITS = 9
CREDENTIALS_PATH = Path(__file__).with_name("credentials.ini")


@dataclass
class Config:
    """Validated input settings; timestamps are UTC and paths are config-relative."""

    source: str
    start: datetime
    end: datetime
    ships: list[int]
    data: str
    output: Path
    gap_minutes: float
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    ship_names: dict[int, str] = field(default_factory=dict)
    ship_colors: dict[int, str] = field(default_factory=dict)
    display_interval_seconds: float = 0
    fade_trails: bool = True


def parse_date(value: str) -> datetime:
    """Parse an ISO date or timestamp, interpreting a missing offset as UTC."""
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc)


def is_mmsi(value: str) -> bool:
    """Return whether a value is a nine-digit ASCII vessel identifier."""
    return len(value) == MMSI_DIGITS and value.isascii() and value.isdigit()


def read_credentials() -> dict[str, str]:
    """Read shared local credentials; environment variables take precedence."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(CREDENTIALS_PATH, encoding="utf-8")
    return {
        key: os.environ.get(f"BARENTSWATCH_{key.upper()}", "").strip()
        or parser.get("barentswatch", key, fallback="").strip()
        for key in ("client_id", "client_secret")
    }


def read_config(
    path: Path,
    *,
    require_ships: bool = True,
    default_output: str = "output/tracks.html",
) -> Config:
    """Read settings and select track vessels from active [vessels] entries.

    Area and incident workflows pass require_ships=False because they select
    vessels through area discovery or incident groups instead.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    with path.open(encoding="utf-8") as handle:
        parser.read_file(handle)
    if "tracks" not in parser:
        raise ValueError("Config must contain a [tracks] section.")
    section = parser["tracks"]
    if "ships" in section:
        raise ValueError(
            "Remove [tracks] ships; active [vessels] entries now select the boats. "
            "Comment out a vessel entry to disable it."
        )
    required = ("source", "start", "end")
    missing = [key for key in required if not section.get(key, "").strip()]
    if missing:
        raise ValueError(f"Missing config values: {', '.join(missing)}")
    source = section["source"].strip().lower()
    if source not in {"api", "files"}:
        raise ValueError("source must be api or files.")
    start, end = parse_date(section["start"]), parse_date(section["end"])
    if start >= end:
        raise ValueError("start must be earlier than end.")
    ships = []
    ship_names = {}
    ship_colors = {}
    if parser.has_section("vessels"):
        for boat, value in parser.items("vessels"):
            identifier, separator, color = value.partition("/")
            identifier, color = identifier.strip(), color.strip()
            if not is_mmsi(identifier):
                raise ValueError(
                    f"Invalid MMSI for {boat!r}. Use Boat name = 123456789 / #0072b2."
                )
            mmsi = int(identifier)
            if require_ships and mmsi not in ships:
                ships.append(mmsi)
            ship_names.setdefault(mmsi, boat)
            if separator:
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    raise ValueError(
                        f"Invalid color for {boat!r}: use a hex color such as #0072b2."
                    )
                ship_colors[int(identifier)] = color.lower()
    if require_ships and not ships:
        raise ValueError(
            "Add at least one active entry under [vessels]: "
            "Boat name = 123456789 / #0072b2."
        )
    gap = section.getfloat("gap_minutes", fallback=30)
    if not math.isfinite(gap) or gap <= 0:
        raise ValueError("gap_minutes must be a positive, finite number.")
    display_interval = section.getfloat("display_interval_seconds", fallback=0)
    if not math.isfinite(display_interval) or not 0 <= display_interval <= gap * 60:
        raise ValueError(
            "display_interval_seconds must be between 0 and gap_minutes * 60."
        )
    data = section.get("data", "").strip()
    if source == "files" and not data:
        raise ValueError("File mode requires data, a Parquet path or glob.")
    base = path.resolve().parent
    output = section.get("output", default_output).strip()
    if not output or Path(output).suffix.lower() != ".html":
        raise ValueError("output must be an HTML file path ending in .html.")
    credentials = read_credentials()
    return Config(
        source,
        start,
        end,
        ships,
        str(base / data),
        base / output,
        gap,
        client_id=credentials["client_id"],
        client_secret=credentials["client_secret"],
        ship_names=ship_names,
        ship_colors=ship_colors,
        display_interval_seconds=display_interval,
        fade_trails=section.getboolean("fade_trails", fallback=True),
    )


def read_files(config: Config) -> pd.DataFrame:
    """Filter Kystverket Parquet at the source, without reading its geometry column."""
    placeholders = ", ".join("?" for _ in config.ships)
    query = f"""
        SELECT mmsi, date_time_utc AS timestamp, longitude, latitude
        FROM read_parquet(?)
        WHERE mmsi IN ({placeholders})
          AND date_time_utc >= ? AND date_time_utc < ?
    """
    try:
        with duckdb.connect() as connection:
            connection.execute("SET TimeZone = 'UTC'")
            return connection.execute(
                query, [config.data, *config.ships, config.start, config.end]
            ).df()
    except duckdb.Error as error:
        raise ValueError(
            "Cannot read historical files. Check the data path and that these are "
            "Kystverket position Parquet files with mmsi, date_time_utc, longitude, "
            f"and latitude columns. DuckDB: {error}"
        ) from error


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    data: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json: dict | None = None,
):
    """Make a timed HTTP request, reporting errors without exposing response secrets."""
    try:
        response = session.request(
            method, url, timeout=(10, 60), data=data, params=params, json=json
        )
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as error:
        status = error.response.status_code
        raise ValueError(
            f"BarentsWatch returned HTTP {status}. Check credentials, API access, "
            "and requested dates. No map was written."
        ) from error
    except (requests.RequestException, ValueError) as error:
        raise ValueError(
            "BarentsWatch request failed or returned invalid JSON. "
            "Check your connection and try again. No map was written."
        ) from error


def validate_api_config(config: Config) -> None:
    """Check dates and credentials only when a network request is needed."""
    now = datetime.now(timezone.utc)
    if config.start < now - timedelta(days=14) or config.end > now:
        raise ValueError(
            "API dates must be within the past 14 days and cannot be in the future. "
            "Use source = files for older history."
        )
    client_id = config.client_id or os.environ.get("BARENTSWATCH_CLIENT_ID")
    client_secret = config.client_secret or os.environ.get("BARENTSWATCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError(
            "API mode requires client_id and client_secret in "
            "visualisations/common/credentials.ini under [barentswatch], "
            "or BARENTSWATCH_CLIENT_ID and BARENTSWATCH_CLIENT_SECRET "
            "environment variables."
        )


def api_session() -> requests.Session:
    """Create a retrying session; cached requests need no live API access."""
    retry = Retry(
        total=3,
        backoff_factor=1,
        backoff_max=8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def authorize(session: requests.Session, config: Config) -> float:
    """Set the bearer token and return its monotonic refresh deadline."""
    validate_api_config(config)
    token = request_json(
        session,
        "POST",
        TOKEN_URL,
        data={
            "client_id": config.client_id or os.environ["BARENTSWATCH_CLIENT_ID"],
            "client_secret": config.client_secret
            or os.environ["BARENTSWATCH_CLIENT_SECRET"],
            "scope": "ais",
            "grant_type": "client_credentials",
        },
    )
    if not isinstance(token, dict) or not token.get("access_token"):
        raise ValueError("BarentsWatch did not return an access token.")
    session.headers["Authorization"] = f"Bearer {token['access_token']}"
    return time.monotonic() + max(0, float(token.get("expires_in", 300)) - 30)


def read_api(
    config: Config, *, download: Path | None = None, offline: bool = False
) -> pd.DataFrame:
    """Read saved daily responses first, fetching and committing only missing ones."""
    frames = []
    token_deadline = 0
    days = math.ceil((config.end - config.start) / timedelta(days=1))
    print(
        f"Fetching {len(config.ships):,} boats over {days} days "
        f"({len(config.ships) * days:,} daily requests).",
        flush=True,
    )
    with (
        DownloadCache(download, offline=offline) as cache,
        api_session() as session,
        Progress("Boats", len(config.ships), steps_per_item=days) as progress,
    ):
        for mmsi in config.ships:
            start = config.start
            day = 0
            while start < config.end:
                day += 1
                end = min(start + timedelta(days=1), config.end)
                progress.describe(f"MMSI {mmsi} | day {day}/{days}")
                url = f"{API_ROOT}/{mmsi}/{start.isoformat()}/{end.isoformat()}"
                params = {"modelFormat": "Json", "filterSatellitePositions": "false"}
                key = request_key("GET", url, params=params)
                payload = cache.load(key)
                saved = payload is not None
                if not saved:
                    if time.monotonic() >= token_deadline:
                        token_deadline = authorize(session, config)
                    payload = request_json(session, "GET", url, params=params)
                if not isinstance(payload, list):
                    raise TypeError(
                        f"Unexpected API response for {mmsi}: expected a list."
                    )
                if payload:
                    frame = pd.DataFrame(payload).rename(
                        columns={"msgtime": "timestamp"}
                    )
                    if not set(COLUMNS).issubset(frame.columns):
                        raise ValueError(
                            f"API positions for {mmsi} lack required columns."
                        )
                    frame["name"] = frame.get("name", "")
                    frames.append(frame[[*COLUMNS, "name"]])
                if not saved:
                    cache.save(key, payload)
                start = end
                progress.advance()
    return (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=COLUMNS)
    )


def clean_positions(frame: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Normalize UTC dates, filter the interval, discard invalid fixes, and deduplicate."""
    frame = frame.copy()
    frame["name"] = frame.get("name", "")
    frame["name"] = frame["name"].fillna("").astype(str).str.strip(" @")
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"], utc=True, errors="coerce", format="mixed"
    )
    for column in ("mmsi", "longitude", "latitude"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    valid = (
        frame["mmsi"].isin(config.ships)
        & frame["timestamp"].ge(config.start)
        & frame["timestamp"].lt(config.end)
        & frame["longitude"].between(-180, 180)
        & frame["latitude"].between(-90, 90)
    )
    cleaned = frame.loc[valid, [*COLUMNS, "name"]].drop_duplicates(
        subset=COLUMNS, keep="last"
    )
    print(f"Kept {len(cleaned):,} positions from {len(frame):,} received rows.")
    return cleaned.sort_values(["mmsi", "timestamp", "longitude", "latitude"])


def build_tracks(frame: pd.DataFrame, config: Config) -> list[dict]:
    """Build browser-ready segments; keep single fixes instead of inventing a path."""
    tracks = []
    with Progress("Tracks", len(config.ships)) as progress:
        for mmsi in config.ships:
            progress.describe(f"MMSI {mmsi}")
            ship = frame.loc[frame["mmsi"] == mmsi]
            if ship.empty:
                progress.advance()
                continue
            names = ship.loc[ship["name"] != "", "name"]
            name = (
                names.iloc[-1]
                if not names.empty
                else config.ship_names.get(mmsi, f"MMSI {mmsi}")
            )
            segments = []
            groups = (
                ship["timestamp"]
                .diff()
                .gt(pd.Timedelta(minutes=config.gap_minutes))
                .cumsum()
            )
            for _, segment in ship.groupby(groups):
                display_segment = segment
                if config.display_interval_seconds:
                    # Sample only after splitting gaps, preserving both ends of each segment.
                    buckets = segment["timestamp"].dt.floor(
                        pd.Timedelta(seconds=config.display_interval_seconds)
                    )
                    keep = ~buckets.duplicated()
                    keep.iloc[-1] = True
                    display_segment = segment.loc[keep]
                segments.append(
                    [
                        [row.latitude, row.longitude, row.timestamp.isoformat()]
                        for row in display_segment.itertuples(index=False)
                    ]
                )
            tracks.append(
                {
                    "mmsi": mmsi,
                    "name": name,
                    "count": len(ship),
                    "displayed_count": sum(len(segment) for segment in segments),
                    "segments": segments,
                }
            )
            progress.advance()
    print(f"Built tracks for {len(tracks):,} boats.")
    return tracks


def render_map(
    tracks: list[dict],
    config: Config,
    *,
    area: dict | None = None,
    review: dict | None = None,
    web_mode: bool = False,
    time_zone: str = "UTC",
) -> str:
    """Embed tracks in a standalone map, optionally with an incident review list.

    Empty archives can still show an area for initial website setup. The CLI
    writer retains its empty-data guard. Timezone changes affect labels only.
    """
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parent),
        autoescape=select_autoescape(["html"]),
    )
    packed_tracks = base64.b64encode(
        gzip.compress(
            json.dumps(tracks, separators=(",", ":")).encode(), compresslevel=1
        )
    ).decode("ascii")
    logo = Path(__file__).resolve().parents[2] / "ais_watch/static/norwegian-orca-survey.png"
    brand_logo = "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode() if logo.exists() else None
    html = environment.get_template("map.html").render(
        brand_logo=brand_logo,
        web_mode=web_mode,
        time_zone=time_zone,
        packed_tracks=packed_tracks,
        colors=config.ship_colors,
        area=area,
        review=review,
        start_ms=int(config.start.timestamp() * 1000),
        end_ms=int(config.end.timestamp() * 1000) - 1,
        start=config.start.strftime("%Y-%m-%d %H:%M"),
        end=config.end.strftime("%Y-%m-%d %H:%M"),
        source=config.source,
        gap_minutes=config.gap_minutes,
        display_interval_seconds=config.display_interval_seconds,
        fade_trails=config.fade_trails,
        missing=[
            config.ship_names.get(mmsi, mmsi)
            for mmsi in config.ships
            if mmsi not in {t["mmsi"] for t in tracks}
        ],
    )
    return html


def write_map(
    tracks: list[dict], config: Config, *, area: dict | None = None, review: dict | None = None
) -> None:
    """Write a standalone map after retrieval, retaining the CLI empty-data guard."""
    if not tracks and review is None:
        raise ValueError("No valid positions match the selected ships and dates. No map written.")
    html = render_map(tracks, config, area=area, review=review)
    config.output.parent.mkdir(parents=True, exist_ok=True)
    config.output.write_text(html, encoding="utf-8")
    print(f"Map saved to {config.output.resolve()}")
