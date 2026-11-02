"""Find vessels in a map rectangle and map their available tracks."""

import argparse
import configparser
import sys
from pathlib import Path

# Allow direct execution from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.area import area_config, load_area_positions  # noqa: E402
from common.tracks import build_tracks, write_map  # noqa: E402


def run(args: argparse.Namespace) -> None:
    """Discover ships and write the usual separate area map."""
    config, area = area_config(args, "area_tracks.html")
    frame = load_area_positions(
        config, area, download=args.save_download, offline=args.offline
    )
    if not config.ships:
        raise ValueError("No ships found in the area and interval. No map written.")
    tracks = build_tracks(frame, config)
    write_map(tracks, config, area=area)


def main() -> int:
    """Parse arguments and report input or retrieval errors without a traceback."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--area", type=Path, help="Override [area] file with an exported area.geojson"
    )
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.ini")
    )
    parser.add_argument("--start", help="Override start (UTC, included)")
    parser.add_argument("--end", help="Override end (UTC, excluded)")
    parser.add_argument(
        "--save-download",
        type=Path,
        help="SQLite file to save and reuse API responses (relative to working directory)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use only --save-download data; fail if a required response is missing",
    )
    parser.add_argument(
        "--output", type=Path, help="Output HTML path (relative to working directory)"
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
