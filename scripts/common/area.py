"""Discover vessels and load tracks for area maps and incident analysis."""

import argparse
import configparser
import json
import math
import time
from datetime import timedelta
from pathlib import Path

import duckdb
import pandas as pd
from .download_cache import DownloadCache, request_key
from .tracks import (
    Config,
    api_session,
    authorize,
    clean_positions,
    is_mmsi,
    parse_date,
    read_api,
    read_config,
    read_files,
    request_json,
)
from .progress import Progress
from shapely.geometry import box, mapping, shape

AREA_URL = "https://historic.ais.barentswatch.no/v1/historic/mmsiinarea"
LONGITUDE_LIMIT = 180
LATITUDE_LIMIT = 90


def read_area(path: Path) -> dict:
    """Read a WGS84 rectangular GeoJSON Feature exported by the map button."""
    feature = json.loads(path.read_text(encoding="utf-8"))
    try:
        if feature["type"] != "Feature":
            raise ValueError("Area must be a GeoJSON Feature.")
        geometry = shape(feature["geometry"])
        west, south, east, north = geometry.bounds
        if (
            geometry.geom_type != "Polygon"
            or not geometry.is_valid
            or not geometry.equals(box(west, south, east, north))
            or not (
                -LONGITUDE_LIMIT <= west < east <= LONGITUDE_LIMIT
                and -LATITUDE_LIMIT <= south < north <= LATITUDE_LIMIT
            )
        ):
            raise ValueError("Area must be a non-empty WGS84 rectangle.")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(
            "Invalid area file. Use the map's Use current map area button."
        ) from error
    properties = feature.get("properties") or {}
    if not isinstance(properties, dict):
        raise TypeError("Area properties must be an object.")
    return {"type": "Feature", "properties": properties, "geometry": mapping(geometry)}


def area_tiles(area: dict) -> list[dict]:
    """Split the rectangle into roughly 19 km cells, below the API's 500 km² limit.

    Longitude spacing uses the latitude nearest the equator, where cells are
    widest. Coordinates stay in WGS84; no spatial extension is needed.
    """
    west, south, east, north = shape(area["geometry"]).bounds
    latitude_step = 19 / 111.32
    nearest_equator = max(south, min(0, north))
    longitude_step = latitude_step / math.cos(math.radians(nearest_equator))
    rows = math.ceil((north - south) / latitude_step)
    columns = math.ceil((east - west) / longitude_step)
    tiles = []
    for row in range(rows):
        bottom = south + (north - south) * row / rows
        top = south + (north - south) * (row + 1) / rows
        for column in range(columns):
            left = west + (east - west) * column / columns
            right = west + (east - west) * (column + 1) / columns
            tiles.append(mapping(box(left, bottom, right, top)))
    return tiles


def discover_api(
    config: Config, area: dict, *, download: Path | None = None, offline: bool = False
) -> list[int]:
    """Union valid MMSIs across tiles and seven-day intervals.

    The API can include malformed AIS identifiers, such as seven-digit numbers.
    Report and skip these without discarding the valid vessels in the response.
    Unexpected response structures still stop the run.
    """
    ships = set()
    ignored = set()
    token_deadline = 0
    tiles = area_tiles(area)
    chunks = math.ceil((config.end - config.start) / timedelta(days=7))
    print(f"Area search: {len(tiles)} tiles x {chunks} time intervals.")
    with (
        DownloadCache(download, offline=offline) as cache,
        api_session() as session,
        Progress("Tiles", len(tiles) * chunks) as progress,
    ):
        start = config.start
        while start < config.end:
            end = min(start + timedelta(days=7), config.end)
            for number, polygon in enumerate(tiles, 1):
                progress.describe(
                    f"tile {number}/{len(tiles)} | {start:%Y-%m-%d} to {end:%Y-%m-%d} "
                    f"| {len(ships)} boats found"
                )
                body = {
                    "polygon": polygon,
                    "msgTimeFrom": start.isoformat(),
                    "msgTimeTo": end.isoformat(),
                }
                key = request_key("POST", AREA_URL, body=body)
                payload = cache.load(key)
                saved = payload is not None
                if not saved:
                    if time.monotonic() >= token_deadline:
                        token_deadline = authorize(session, config)
                    payload = request_json(session, "POST", AREA_URL, json=body)
                if not isinstance(payload, list) or any(
                    not isinstance(mmsi, int) or isinstance(mmsi, bool)
                    for mmsi in payload
                ):
                    raise ValueError(
                        "Unexpected area-search response: expected a list of integer MMSIs."
                    )
                valid = {mmsi for mmsi in payload if is_mmsi(str(mmsi))}
                invalid = set(payload) - valid - ignored
                if invalid:
                    progress.message(
                        "Warning: ignoring invalid AIS identifiers (expected nine digits): "
                        + ", ".join(str(mmsi) for mmsi in sorted(invalid))
                    )
                    ignored.update(invalid)
                ships.update(valid)
                if not saved:
                    cache.save(key, payload)
                progress.describe(
                    f"tile {number}/{len(tiles)} | {len(ships)} boats found"
                )
                progress.advance()
            start = end
    return sorted(ships)


def discover_files(config: Config, area: dict) -> list[int]:
    """Find distinct ships with recorded fixes in the rectangle and time interval."""
    west, south, east, north = shape(area["geometry"]).bounds
    query = """
        SELECT DISTINCT mmsi
        FROM read_parquet(?)
        WHERE date_time_utc >= ? AND date_time_utc < ?
          AND longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?
        ORDER BY mmsi
    """
    try:
        with duckdb.connect() as connection:
            connection.execute("SET TimeZone = 'UTC'")
            rows = connection.execute(
                query, [config.data, config.start, config.end, west, east, south, north]
            ).fetchall()
    except duckdb.Error as error:
        raise ValueError(f"Cannot search Kystverket Parquet files: {error}") from error
    return [int(row[0]) for row in rows if is_mmsi(str(row[0]))]


def area_config(args: argparse.Namespace, output_name: str) -> tuple[Config, dict]:
    """Resolve geometry and config dates, allowing explicit CLI date overrides."""
    config = read_config(
        args.config, require_ships=False, default_output=f"output/{output_name}"
    )
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(args.config, encoding="utf-8")
    configured_area = parser.get("area", "file", fallback="").strip()
    area_path = args.area
    if area_path is None and configured_area:
        area_path = args.config.resolve().parent / configured_area
    if area_path is None:
        raise ValueError("Choose an area with --area or [area] file in the config.")
    area = read_area(area_path)
    if args.start:
        config.start = parse_date(args.start)
    if args.end:
        config.end = parse_date(args.end)
    if config.start >= config.end:
        raise ValueError("start must be earlier than end.")
    config.output = args.output or config.output
    if config.output.suffix.lower() != ".html":
        raise ValueError("output must end in .html.")
    if config.source == "files" and (args.save_download or args.offline):
        raise ValueError(
            "Downloaded Parquet files are already saved; cache options require source = api."
        )
    return config, area


def load_area_positions(
    config: Config,
    area: dict,
    *,
    download: Path | None = None,
    offline: bool = False,
    selected: list[int] | None = None,
) -> pd.DataFrame:
    """Discover area vessels and load full-interval tracks, optionally for a subset.

    Reuse the exact area and daily-track request keys used by the area map.
    Only vessels with recorded in-area fixes survive discovery verification.
    An empty result is returned so callers can choose their empty-output policy.
    """
    discovered = (
        discover_api(config, area, download=download, offline=offline)
        if config.source == "api"
        else discover_files(config, area)
    )
    config.ships = [mmsi for mmsi in discovered if selected is None or mmsi in selected]
    if not config.ships:
        return pd.DataFrame(
            columns=["mmsi", "timestamp", "longitude", "latitude", "name"]
        )
    print(f"Found {len(config.ships)} ships; loading their available tracks.")
    raw = (
        read_api(config, download=download, offline=offline)
        if config.source == "api"
        else read_files(config)
    )
    frame = clean_positions(raw, config)
    # Area search can include an endpoint at the exclusive end. Keep only
    # ships with an actual in-area fix during our half-open time interval.
    west, south, east, north = shape(area["geometry"]).bounds
    inside = frame["longitude"].between(west, east) & frame["latitude"].between(
        south, north
    )
    config.ships = sorted(int(mmsi) for mmsi in frame.loc[inside, "mmsi"].unique())
    return frame.loc[frame["mmsi"].isin(config.ships)].copy()


