"""Persist successful AIS responses so downloads can be reused or resumed."""

import gzip
import json
import sqlite3
import sys
from pathlib import Path
from types import TracebackType


def request_key(
    method: str, url: str, *, params: dict | None = None, body: dict | None = None
) -> str:
    """Identify an exact data request without credentials or bearer tokens."""
    return json.dumps(
        {"version": 1, "method": method, "url": url, "params": params, "body": body},
        sort_keys=True,
        separators=(",", ":"),
    )


class DownloadCache:
    """Store compressed JSON responses in a SQLite file, committing each request.

    A missing path disables persistence. Offline mode requires a path and raises
    on missing requests; it never silently replaces a missing response with no data.
    """

    def __init__(self, path: Path | None, *, offline: bool = False) -> None:
        if offline and path is None:
            raise ValueError(
                "--offline requires --save-download with a saved download path."
            )
        self.path = path
        self.offline = offline
        self.connection = None
        self.reused = 0
        self.saved = 0

    def __enter__(self) -> "DownloadCache":
        if self.path is not None:
            if self.offline and not self.path.is_file():
                raise ValueError(f"Saved download not found: {self.path}")
            try:
                if self.offline:
                    self.connection = sqlite3.connect(
                        self.path.resolve().as_uri() + "?mode=ro", uri=True
                    )
                else:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.connection = sqlite3.connect(self.path, isolation_level=None)
                    self.connection.execute(
                        "CREATE TABLE IF NOT EXISTS responses "
                        "(request_key TEXT PRIMARY KEY, payload BLOB NOT NULL)"
                    )
                has_metadata = self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
                if has_metadata:
                    origin = self.connection.execute("SELECT value FROM metadata WHERE key='source'").fetchone()
                    if origin and origin[0] == 'ais-watch-archive' and not self.offline:
                        self.connection.close()
                        self.connection = None
                        raise ValueError("AIS Watch exports contain recorded data only. Use --offline with the matching bundled config.")
            except sqlite3.Error as error:
                if self.connection is not None:
                    self.connection.close()
                raise ValueError(
                    f"Cannot open saved download {self.path}: {error}"
                ) from error
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.connection is not None:
            self.connection.close()
            print(
                f"Saved download: {self.reused:,} requests reused, {self.saved:,} saved "
                f"({self.path}).",
                file=sys.stderr,
                flush=True,
            )

    def load(self, key: str) -> list | None:
        """Return a saved response, distinguishing an empty list from a cache miss."""
        if self.connection is None:
            return None
        try:
            row = self.connection.execute(
                "SELECT payload FROM responses WHERE request_key = ?", (key,)
            ).fetchone()
            if row is not None:
                payload = json.loads(gzip.decompress(row[0]))
        except (sqlite3.Error, OSError, EOFError, ValueError) as error:
            raise ValueError(
                f"Cannot read saved download {self.path}: {error}"
            ) from error
        if row is not None:
            if not isinstance(payload, list):
                raise ValueError(
                    f"Invalid response in saved download {self.path}: expected a list."
                )
            self.reused += 1
            return payload
        if self.offline:
            raise ValueError(
                "Saved download is incomplete for this area, ship selection, or interval. "
                "Run without --offline to download missing requests."
            )
        return None

    def save(self, key: str, payload: list) -> None:
        """Atomically save one validated response, including an empty result."""
        if self.connection is None:
            return
        data = gzip.compress(
            json.dumps(payload, separators=(",", ":")).encode(), compresslevel=3
        )
        try:
            self.connection.execute(
                "INSERT OR REPLACE INTO responses VALUES (?, ?)", (key, data)
            )
        except sqlite3.Error as error:
            raise ValueError(f"Cannot save download {self.path}: {error}") from error
        self.saved += 1
