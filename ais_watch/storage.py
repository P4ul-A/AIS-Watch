"""SQLite persistence and stable vessel colours; connections belong to one thread."""

import colorsys
import configparser
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OSLO = ZoneInfo("Europe/Oslo")
UTC = timezone.utc
DEFAULTS = {
    "enabled": False, "interval_minutes": 60, "gap_minutes": 30,
    "display_interval_seconds": 0, "auto_incidents": True,
    "radius_m": 1000, "max_small_speed_knots": 3, "prior_minutes": 2,
    "together_minutes": 2, "max_gap_minutes": 5,
}
CATEGORIES = [
    ("Other / uncategorised", "#64748b"),
    ("Tromsø day trips without snorkelling", "#dc2626"),
    ("Liveaboards without snorkelling", "#2563eb"),
    ("Liveaboards with snorkelling", "#a855b5"),
    ("Skjervøy and surrounding-area day trips", "#22a052"),
]


def now():
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


def stamp(value):
    """Canonical UTC text sorts chronologically in SQLite."""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse(value):
    """Parse a stored UTC timestamp."""
    return datetime.fromisoformat(value).astimezone(UTC)


def day_bounds(value):
    """Return UTC boundaries of an Oslo date, including 23/25-hour DST days."""
    day = datetime.fromisoformat(value).date()
    return (datetime.combine(day, time.min, OSLO).astimezone(UTC),
            datetime.combine(day + timedelta(days=1), time.min, OSLO).astimezone(UTC))


def colour(value):
    """Validate an HTML RGB colour before persisting it."""
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Choose a colour in #RRGGBB format.")
    return value.lower()


def shade(base, slot):
    """Produce a repeatable category shade from a permanently allocated slot."""
    rgb = tuple(int(base[i:i + 2], 16) / 255 for i in (1, 3, 5))
    hue, light, saturation = colorsys.rgb_to_hls(*rgb)
    fraction = (slot * 0.61803398875) % 1
    light = 0.28 + fraction * 0.40
    hue = (hue + (((slot * 0.38196601125) % 1) - 0.5) * 0.07) % 1
    return "#" + "".join(f"{round(c * 255):02x}" for c in colorsys.hls_to_rgb(hue, light, max(saturation, 0.15)))


def describe_boat(row):
    """Resolve the same display name and effective colour for UI and exports."""
    boat = dict(row)
    boat["display_name"] = boat["name"] or boat["ais_name"] or f"MMSI {boat['mmsi']}"
    boat["color"] = boat["color_override"] or shade(boat["base_color"], boat["slot"])
    return boat


class Store:
    """Own the archive and migrations, without sharing SQLite connections."""

    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get("AIS_DATA_DIR", "/data"))
        self.path = self.directory / "ais-watch.sqlite3"

    @contextmanager
    def connect(self):
        """Commit successful short transactions and roll back exceptions."""
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        """Migrate the archive and seed once, never overwriting user edits."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("This archive needs a newer AIS-Watch version.")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS categories(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                    color TEXT NOT NULL, sort_order INTEGER NOT NULL, next_slot INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS boats(
                    mmsi INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', ais_name TEXT NOT NULL DEFAULT '',
                    category_id INTEGER NOT NULL REFERENCES categories(id), slot INTEGER NOT NULL,
                    color_override TEXT, role TEXT NOT NULL DEFAULT '' CHECK(role IN ('','small','big')));
                CREATE TABLE IF NOT EXISTS areas(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, geometry TEXT NOT NULL,
                    active_from TEXT, active_until TEXT, cursor TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_area ON areas((1))
                    WHERE active_from IS NOT NULL AND active_until IS NULL;
                CREATE TABLE IF NOT EXISTS watches(
                    id INTEGER PRIMARY KEY, area_id INTEGER NOT NULL REFERENCES areas(id),
                    mmsi INTEGER NOT NULL REFERENCES boats(mmsi), start TEXT NOT NULL,
                    last_inside TEXT NOT NULL, deadline TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS watch_lookup ON watches(area_id,mmsi,start,deadline);
                CREATE TABLE IF NOT EXISTS positions(
                    mmsi INTEGER NOT NULL REFERENCES boats(mmsi), timestamp TEXT NOT NULL,
                    longitude REAL NOT NULL, latitude REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(mmsi,timestamp,longitude,latitude)) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS positions_time ON positions(timestamp,mmsi);
                CREATE TABLE IF NOT EXISTS jobs(
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'queued', created TEXT NOT NULL, finished TEXT,
                    detail TEXT NOT NULL DEFAULT '', error TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS pending_jobs ON jobs(kind,payload)
                    WHERE state IN ('queued','running');
                CREATE TABLE IF NOT EXISTS gaps(
                    area_id INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                    reason TEXT NOT NULL, UNIQUE(area_id,start,end));
                CREATE TABLE IF NOT EXISTS collected_days(
                    area_id INTEGER NOT NULL REFERENCES areas(id), day TEXT NOT NULL,
                    updated TEXT NOT NULL, PRIMARY KEY(area_id,day));
                CREATE TABLE IF NOT EXISTS analysis_runs(
                    area_id INTEGER NOT NULL REFERENCES areas(id), day TEXT NOT NULL,
                    updated TEXT NOT NULL, settings TEXT NOT NULL, missing TEXT NOT NULL,
                    PRIMARY KEY(area_id,day));
                CREATE TABLE IF NOT EXISTS incidents(
                    area_id INTEGER NOT NULL REFERENCES areas(id), day TEXT NOT NULL,
                    small_mmsi INTEGER NOT NULL, big_mmsi INTEGER NOT NULL, joining_utc TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(area_id,day,small_mmsi,big_mmsi,joining_utc));
            ''')
            columns = {row[1] for row in db.execute("PRAGMA table_info(areas)")}
            if "deleted_at" not in columns:
                db.execute("ALTER TABLE areas ADD COLUMN deleted_at TEXT")
            if not db.execute("SELECT 1 FROM settings WHERE key='seeded'").fetchone():
                for index, (name, base) in enumerate(CATEGORIES):
                    db.execute("INSERT INTO categories(id,name,color,sort_order) VALUES(?,?,?,?)",
                               (index + 1, name, base, index))
                self.seed_boats(db)
                settings = dict(DEFAULTS, seeded=True, start_date=now().astimezone(OSLO).date().isoformat())
                for key, value in settings.items():
                    self.put(db, key, value)
            db.execute("PRAGMA user_version=2")
        self.initialize_credentials()

    @staticmethod
    def put(db, key, value):
        """Persist one JSON setting in the caller's transaction."""
        db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))

    def settings(self):
        """Return persisted configuration without credentials."""
        with self.connect() as db:
            return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM settings")}

    @staticmethod
    def ensure_boat(db, mmsi, name="", category=1, override=None):
        """Allocate a stable shade slot for a newly encountered boat."""
        existing = db.execute("SELECT 1 FROM boats WHERE mmsi=?", (mmsi,)).fetchone()
        if not existing:
            slot = db.execute("SELECT next_slot FROM categories WHERE id=?", (category,)).fetchone()[0]
            db.execute("UPDATE categories SET next_slot=next_slot+1 WHERE id=?", (category,))
            db.execute("INSERT INTO boats(mmsi,name,category_id,slot,color_override) VALUES(?,?,?,?,?)",
                       (mmsi, name, category, slot, override))

    def seed_boats(self, db):
        """Import only active seed entries; section comments define the four groups."""
        group = 1
        headings = {"# Day trips tromso no snokeling": 2,
                    "# Live aboard vessels non snorkeling": 3,
                    "# Live aboard vessels with snorkeling": 4,
                    "# Day trips skjervoy and area": 5, "# Other boats in the area": 1}
        source = ROOT / "scripts/tracks/skjervoy_2026-11-01.ini"
        for line in source.read_text().splitlines():
            line = line.strip()
            group = headings.get(line, group)
            if line.startswith(("#", "[")) or "=" not in line:
                continue
            name, value = line.split("=", 1)
            match = re.fullmatch(r"\s*(\d{9})\s*/\s*(#[0-9a-fA-F]{6})\s*", value)
            if match:
                self.ensure_boat(db, int(match[1]), name.strip(), group, colour(match[2]))
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(ROOT / "scripts/incidents/config.ini")
        for key, role in (("small_boats", "small"), ("big_boats", "big")):
            for name in parser.get("incidents", key, fallback="").splitlines():
                db.execute("UPDATE boats SET role=? WHERE lower(name)=?", (role, name.strip().lower()))

    def boats(self):
        """Resolve display names and colours consistently for every interface."""
        with self.connect() as db:
            rows = db.execute('''SELECT b.*,c.name AS category,c.color AS base_color,c.sort_order
                FROM boats b JOIN categories c ON c.id=b.category_id ORDER BY c.sort_order,b.name,b.mmsi''').fetchall()
        return [describe_boat(row) for row in rows]

    def credentials(self):
        """Read private credentials; never return them through the website API."""
        path = self.directory / "credentials.json"
        return json.loads(path.read_text()) if path.exists() else {"client_id": "", "client_secret": ""}

    def save_credentials(self, values):
        """Atomically replace a private credential file with owner-only permissions."""
        path = self.directory / "credentials.json"
        temporary = self.directory / "credentials.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(values, handle)
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def initialize_credentials(self):
        """Create empty Settings credentials once, preserving existing values."""
        if (self.directory / "credentials.json").exists():
            return
        self.save_credentials({"client_id": "", "client_secret": ""})

    def enqueue(self, kind, payload=None):
        """Deduplicate identical pending requests while allowing different day jobs."""
        encoded = json.dumps(payload or {}, sort_keys=True)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO jobs(kind,payload,created) VALUES(?,?,?)",
                       (kind, encoded, stamp(now())))
            return db.execute("SELECT id FROM jobs WHERE kind=? AND payload=? AND state IN ('queued','running') ORDER BY id LIMIT 1",
                              (kind, encoded)).fetchone()[0]
