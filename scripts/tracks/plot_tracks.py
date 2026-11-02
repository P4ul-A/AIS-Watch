"""Map selected vessels from AIS files or the historical API."""

import argparse
import configparser
import sys
from pathlib import Path

# Allow direct execution from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.tracks import (  # noqa: E402
    build_tracks, clean_positions, read_api, read_config, read_files, write_map,
)


def main() -> int:
    """Run the configured source and map workflow; leave output alone on input errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.ini"),
        help="INI config (default: config.ini beside this script)",
    )
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
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        if config.source == "files" and (args.save_download or args.offline):
            raise ValueError(
                "Downloaded Parquet files are already saved; cache options require source = api."
            )
        raw = (
            read_api(config, download=args.save_download, offline=args.offline)
            if config.source == "api"
            else read_files(config)
        )
        tracks = build_tracks(clean_positions(raw, config), config)
        write_map(tracks, config)
    except (ValueError, TypeError, OSError, configparser.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
