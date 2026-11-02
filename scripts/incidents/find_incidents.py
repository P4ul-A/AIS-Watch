"""Flag possible whale-watching joining incidents for manual review."""

import argparse
import configparser
import csv
import io
import math
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

# Allow direct execution from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.detection import IncidentSettings, FIELDS, sample_track, find_incidents  # noqa: E402
from common.area import area_config, load_area_positions  # noqa: E402
from common.tracks import Config, build_tracks, is_mmsi, write_map  # noqa: E402


def resolve_group(
    key: str,
    section: configparser.SectionProxy,
    registry: dict[str, str],
    config: Config,
) -> list[int]:
    """Resolve one name or nine-digit MMSI per line through the vessel registry."""
    boats = []
    for line in section.get(key, "").splitlines():
        value = line.strip()
        if not value:
            continue
        identifier = (
            value
            if is_mmsi(value)
            else registry.get(value.lower(), "").partition("/")[0].strip()
        )
        if not is_mmsi(identifier):
            raise ValueError(
                f"Unknown boat {value!r} in {key}; add it under [vessels] or use its MMSI."
            )
        mmsi = int(identifier)
        if mmsi not in boats:
            boats.append(mmsi)
        if not is_mmsi(value):
            config.ship_names.setdefault(mmsi, value)
    if not boats:
        raise ValueError(f"[incidents] requires a non-empty {key} list.")
    return boats


def read_incident_config(
    path: Path, config: Config
) -> tuple[list[int], list[int], IncidentSettings]:
    """Resolve explicit boat groups and validate thresholds before any download."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    if not parser.has_section("incidents"):
        raise ValueError("Config must contain an [incidents] section.")
    registry = dict(parser.items("vessels")) if parser.has_section("vessels") else {}
    section = parser["incidents"]

    small = resolve_group("small_boats", section, registry, config)
    big = resolve_group("big_boats", section, registry, config)
    if set(small) & set(big):
        raise ValueError("small_boats and big_boats must not overlap.")
    settings = IncidentSettings()
    for key in asdict(settings):
        value = section.getfloat(key, fallback=getattr(settings, key))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be positive and finite.")
        setattr(settings, key, value)
    return small, big, settings


def run(args: argparse.Namespace) -> None:
    """Reuse area data, analyse configured pairs, and write HTML plus CSV."""
    config, area = area_config(args, "incidents.html")
    small, big, settings = read_incident_config(args.config, config)
    frame = load_area_positions(
        config,
        area,
        download=args.save_download,
        offline=args.offline,
        selected=small + big,
    )
    config.gap_minutes = settings.max_gap_minutes
    tracks = build_tracks(frame, config)
    names = {mmsi: config.ship_names.get(mmsi, f"MMSI {mmsi}") for mmsi in small + big}
    names.update({track["mmsi"]: track["name"] for track in tracks})
    timeline = pd.date_range(config.start, config.end, freq="1min", inclusive="left")
    samples = {
        int(mmsi): sample_track(ship, timeline, settings)
        for mmsi, ship in frame.groupby("mmsi")
    }
    incidents = find_incidents(samples, small, big, settings, area, names)
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(incidents)
    # Review circles use analysis positions; original AIS reports remain the map tracks.
    circles = {}
    for mmsi in {item["small_mmsi"] for item in incidents}:
        valid = samples[mmsi].dropna()
        circles[mmsi] = [
            [row.latitude, row.longitude, int(timestamp.timestamp() * 1000)]
            for timestamp, row in valid.iterrows()
        ]
    review = {
        "csv": csv_buffer.getvalue(),
        "incidents": incidents,
        "settings": asdict(settings),
        "circles": circles,
        "missing": [names[mmsi] for mmsi in small + big if mmsi not in samples],
        "small_boats": [names[mmsi] for mmsi in small],
        "big_boats": [names[mmsi] for mmsi in big],
    }
    write_map(tracks, config, area=area, review=review)
    csv_path = config.output.with_suffix(".csv")
    csv_path.write_text(review["csv"], encoding="utf-8", newline="")
    print(
        f"Flagged {len(incidents)} possible joining incidents. CSV saved to {csv_path.resolve()}"
    )


def main() -> int:
    """Parse options and report input/download errors without a traceback."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.ini")
    )
    parser.add_argument(
        "--area", type=Path, help="Override [area] file with an exported area.geojson"
    )
    parser.add_argument("--start", help="Override start (UTC, included)")
    parser.add_argument("--end", help="Override end (UTC, excluded)")
    parser.add_argument(
        "--save-download", type=Path, help="Reuse/save the area download SQLite file"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Read saved responses only; requires --save-download",
    )
    parser.add_argument(
        "--output", type=Path, help="Review HTML path; CSV uses the same stem"
    )
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, TypeError, OSError, configparser.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
