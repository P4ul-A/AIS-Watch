"""One serial background worker for collection and reproducible incident analysis."""

import json
import logging
import threading
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pandas as pd
from shapely.geometry import shape

from scripts.common.area import discover_api
from scripts.common.detection import IncidentSettings, find_incidents, sample_track
from scripts.common.tracks import API_ROOT, Config, api_session, authorize, request_json
from .storage import OSLO, day_bounds, now, parse, stamp

logger = logging.getLogger(__name__)


class Cancelled(Exception):
    """Stop work at a safe checkpoint during container shutdown."""


class Worker:
    """Poll persisted jobs; only this thread performs collection or analysis."""

    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="ais-collector", daemon=True)

    def start(self):
        """Recover jobs interrupted by a previous container stop."""
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET state='queued',detail='Resuming after restart' WHERE state='running'")
        self.thread.start()

    def checkpoint(self):
        """Allow graceful cancellation between bounded network requests."""
        if self.stop.is_set():
            raise Cancelled()

    def detail(self, job, message):
        """Publish progress independently of durable per-interval checkpoints."""
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET detail=? WHERE id=?", (message, job))

    def run(self):
        """Schedule collections, claim queued jobs, and contain recoverable failures."""
        while not self.stop.is_set():
            try:
                settings = self.store.settings()
                with self.store.connect() as db:
                    last = db.execute("SELECT created FROM jobs WHERE kind='collect' ORDER BY id DESC LIMIT 1").fetchone()
                    active = db.execute("SELECT 1 FROM areas WHERE active_from IS NOT NULL AND active_until IS NULL").fetchone()
                    job = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
                due = not last or (now() - parse(last[0])).total_seconds() >= settings["interval_minutes"] * 60
                if not job and active and settings["enabled"] and due:
                    self.store.enqueue("collect")
                    continue
                if not job:
                    self.stop.wait(2)
                    continue
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET state='running',error=NULL WHERE id=?", (job["id"],))
                try:
                    if job["kind"] == "collect":
                        affected = self.collect(job["id"])
                        if settings["auto_incidents"]:
                            self.analyse(job["id"], self.pending_analysis_targets())
                    else:
                        payload = json.loads(job["payload"])
                        self.analyse(job["id"], self.analysis_targets(payload))
                    with self.store.connect() as db:
                        db.execute("UPDATE jobs SET state='done',finished=?,detail='Complete' WHERE id=?", (stamp(now()), job["id"]))
                except Cancelled:
                    with self.store.connect() as db:
                        db.execute("UPDATE jobs SET state='queued',detail='Interrupted; will resume' WHERE id=?", (job["id"],))
                except Exception as error:
                    # Do not log exception chains or HTTP response bodies containing credentials.
                    message = str(error) if isinstance(error, ValueError) else "Unexpected worker error; job can be retried."
                    logger.error("%s job failed (%s)", job["kind"], type(error).__name__)
                    with self.store.connect() as db:
                        db.execute("UPDATE jobs SET state='failed',finished=?,error=? WHERE id=?", (stamp(now()), message, job["id"]))
            except Exception:
                logger.error("Worker could not access its queue; retrying shortly")
                self.stop.wait(5)

    def config(self, start, end, ships=None):
        """Build the shared client's configuration without reading CLI config files."""
        credentials = self.store.credentials()
        return Config(source="api", start=start, end=end, ships=ships or [], data="",
                      output=Path("unused.html"), gap_minutes=30, **credentials)

    def collect(self, job):
        """Process local days atomically by area, retaining full entry days and grace tracks.

        Every run refreshes the preceding 48 hours. Failed days do not advance
        the area's cursor; already committed vessel reports deduplicate on retry.
        """
        current = now() - timedelta(seconds=5)
        # Keep requests just inside the rolling 14-day history window.
        floor = current - timedelta(days=14) + timedelta(minutes=5)
        affected = set()
        with self.store.connect() as db:
            areas = [dict(row) for row in db.execute("SELECT * FROM areas WHERE active_from IS NOT NULL AND deleted_at IS NULL")]
        if not areas:
            raise ValueError("Activate a recording area first.")
        with api_session() as session:
            token_deadline = 0
            for area in areas:
                self.checkpoint()
                activated = parse(area["active_from"])
                closed = parse(area["active_until"]) if area["active_until"] else current
                finish = min(current, closed + timedelta(hours=24))
                cursor = parse(area["cursor"]) if area["cursor"] else activated
                if area["active_until"] and cursor >= closed + timedelta(hours=24) and current >= closed + timedelta(hours=72):
                    continue
                start = max(activated, cursor - timedelta(hours=48))
                if start >= finish:
                    continue
                if start < floor:
                    if cursor < min(floor, finish):
                        with self.store.connect() as db:
                            db.execute("INSERT OR IGNORE INTO gaps VALUES(?,?,?,?)", (area["id"], stamp(max(activated, cursor)), stamp(min(floor, finish)), "Outside automatic API recovery window"))
                    start = floor
                geometry = json.loads(area["geometry"])
                bounds = shape(geometry["geometry"]).bounds
                while start < finish:
                    self.checkpoint()
                    with self.store.connect() as db:
                        deleted = db.execute("SELECT deleted_at FROM areas WHERE id=?", (area["id"],)).fetchone()[0]
                    if deleted:
                        break
                    day = start.astimezone(OSLO).date().isoformat()
                    midnight, next_day = day_bounds(day)
                    end = min(next_day, finish)
                    fetch_start = max(midnight, floor)
                    config = self.config(fetch_start, end)
                    self.detail(job, f"Discovering {area['name']} · {day}")
                    discovery_end = min(end, closed)
                    discovered = set()
                    if start < discovery_end:
                        search = self.config(start, discovery_end)
                        discovered.update(discover_api(search, geometry))
                    with self.store.connect() as db:
                        discovered.update(row[0] for row in db.execute(
                            "SELECT DISTINCT mmsi FROM watches WHERE area_id=? AND start<? AND deadline>=?",
                            (area["id"], stamp(end), stamp(fetch_start))))
                    for index, mmsi in enumerate(sorted(discovered), 1):
                        self.checkpoint()
                        with self.store.connect() as db:
                            deleted = db.execute("SELECT deleted_at FROM areas WHERE id=?", (area["id"],)).fetchone()[0]
                        if deleted:
                            break
                        self.detail(job, f"{area['name']} · {day} · boat {index}/{len(discovered)} · {mmsi}")
                        if time.monotonic() >= token_deadline:
                            token_deadline = authorize(session, config)
                        url = f"{API_ROOT}/{mmsi}/{config.start.isoformat()}/{end.isoformat()}"
                        payload = request_json(session, "GET", url,
                                               params={"modelFormat": "Json", "filterSatellitePositions": "false"})
                        rows = self.normalize(payload, mmsi, fetch_start, end)
                        self.save_reports(area, rows, mmsi, bounds, activated, closed)
                    previous_day = (midnight.astimezone(OSLO).date() - timedelta(days=1)).isoformat()
                    with self.store.connect() as db:
                        # Progress and pending analysis must survive or roll back together.
                        db.execute("UPDATE areas SET cursor=? WHERE id=?", (stamp(end), area["id"]))
                        for changed_day in (day, previous_day):
                            db.execute("INSERT OR REPLACE INTO collected_days VALUES(?,?,?)", (area["id"], changed_day, stamp(now())))
                    affected.update(((area["id"], day), (area["id"], previous_day)))
                    start = end
        return affected

    @staticmethod
    def normalize(payload, mmsi, start, end):
        """Retain original JSON fields while validating report identity and coordinates."""
        if not isinstance(payload, list):
            raise ValueError("Unexpected track response: expected a list.")
        result = []
        for item in payload:
            if not isinstance(item, dict) or not {"mmsi", "msgtime", "longitude", "latitude"} <= item.keys():
                raise ValueError("Track response lacks required fields.")
            try:
                timestamp = pd.to_datetime(item["msgtime"], utc=True).to_pydatetime()
                lon, lat = float(item["longitude"]), float(item["latitude"])
                valid = (int(item["mmsi"]) == mmsi and start <= timestamp < end
                         and -180 <= lon <= 180 and -90 <= lat <= 90)
                if valid:
                    result.append((timestamp, lon, lat, item))
            except (ValueError, TypeError, OverflowError):
                continue
        return sorted(result, key=lambda row: row[0])

    def save_reports(self, area, rows, mmsi, bounds, activated, closed):
        """Extend visit windows from actual in-area fixes, then save eligible reports."""
        west, south, east, north = bounds
        with self.store.connect() as db:
            self.store.ensure_boat(db, mmsi)
            for timestamp, lon, lat, _ in rows:
                if not (activated <= timestamp < closed and west <= lon <= east and south <= lat <= north):
                    continue
                entry_start = day_bounds(timestamp.astimezone(OSLO).date().isoformat())[0]
                deadline = timestamp + timedelta(hours=24)
                window = db.execute("SELECT * FROM watches WHERE area_id=? AND mmsi=? AND start<=? AND deadline>=? ORDER BY deadline DESC LIMIT 1",
                                    (area["id"], mmsi, stamp(timestamp), stamp(entry_start))).fetchone()
                if window:
                    db.execute("UPDATE watches SET start=min(start,?),last_inside=max(last_inside,?),deadline=max(deadline,?) WHERE id=?",
                               (stamp(entry_start), stamp(timestamp), stamp(deadline), window["id"]))
                else:
                    db.execute("INSERT INTO watches(area_id,mmsi,start,last_inside,deadline) VALUES(?,?,?,?,?)",
                               (area["id"], mmsi, stamp(entry_start), stamp(timestamp), stamp(deadline)))
            windows = db.execute("SELECT start,deadline FROM watches WHERE area_id=? AND mmsi=?", (area["id"], mmsi)).fetchall()
            saved_name = ""
            for timestamp, lon, lat, raw in rows:
                text = stamp(timestamp)
                if any(window["start"] <= text <= window["deadline"] for window in windows):
                    db.execute("INSERT INTO positions VALUES(?,?,?,?,?) ON CONFLICT DO UPDATE SET payload=excluded.payload",
                               (mmsi, text, lon, lat, json.dumps(raw)))
                    saved_name = str(raw.get("name") or "").strip(" @") or saved_name
            if saved_name:
                db.execute("UPDATE boats SET ais_name=? WHERE mmsi=?", (saved_name, mmsi))

    def pending_analysis_targets(self):
        """Find durable analysis work, including days from a previously failed run."""
        with self.store.connect() as db:
            return {(row["area_id"], row["day"]) for row in db.execute(
                "SELECT c.* FROM collected_days c LEFT JOIN analysis_runs r ON r.area_id=c.area_id AND r.day=c.day WHERE r.updated IS NULL OR c.updated>r.updated")}

    def analysis_targets(self, payload):
        """Resolve a requested day or every recorded local day without API access."""
        with self.store.connect() as db:
            areas = [row[0] for row in db.execute("SELECT id FROM areas WHERE active_from IS NOT NULL")]
            timestamps = [row[0] for row in db.execute("SELECT min(timestamp) FROM positions UNION ALL SELECT max(timestamp) FROM positions")]
        if payload.get("day"):
            days = [payload["day"]]
        elif all(timestamps):
            first = parse(timestamps[0]).astimezone(OSLO).date()
            last = parse(timestamps[1]).astimezone(OSLO).date()
            days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
        else:
            days = []
        return {(area, day) for area in areas for day in days}

    def analyse(self, job, targets):
        """Recompute affected days with surrounding context and preserve rule settings.

        Context starts one full local day earlier and extends through available
        next-day reports, so observed encounters can cross midnight unchanged.
        Results belong to the local day of joining and remain manual-review flags.
        """
        settings = self.store.settings()
        thresholds = IncidentSettings(**{key: settings[key] for key in asdict(IncidentSettings())})
        boats = self.store.boats()
        names = {boat["mmsi"]: boat["display_name"] for boat in boats}
        small = [boat["mmsi"] for boat in boats if boat["role"] == "small"]
        big = [boat["mmsi"] for boat in boats if boat["role"] == "big"]
        for area_id, day in sorted(targets):
            self.checkpoint()
            start, end = day_bounds(day)
            context_start = day_bounds((start.astimezone(OSLO).date() - timedelta(days=1)).isoformat())[0]
            context_end = min(now(), day_bounds((end.astimezone(OSLO).date() + timedelta(days=1)).isoformat())[0])
            if context_start >= context_end:
                continue
            self.detail(job, f"Analysing {day} · area {area_id}")
            with self.store.connect() as db:
                area = db.execute("SELECT geometry FROM areas WHERE id=?", (area_id,)).fetchone()
                rows = db.execute("SELECT p.mmsi,p.timestamp,p.longitude,p.latitude FROM positions p JOIN boats b ON b.mmsi=p.mmsi WHERE p.timestamp>=? AND p.timestamp<? AND b.role IN ('small','big') ORDER BY p.timestamp",
                                  (stamp(context_start), stamp(context_end))).fetchall()
            frame = pd.DataFrame([dict(row) for row in rows], columns=["mmsi", "timestamp", "longitude", "latitude"])
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
            timeline = pd.date_range(context_start, context_end, freq="1min", inclusive="left")
            samples = {int(mmsi): sample_track(ship, timeline, thresholds)
                       for mmsi, ship in frame.groupby("mmsi") if mmsi in small + big}
            incidents = find_incidents(samples, small, big, thresholds, json.loads(area[0]), names)
            incidents = [item for item in incidents if start <= parse(item["joining_utc"]) < end]
            missing = [names[mmsi] for mmsi in small + big if mmsi not in samples or
                       samples[mmsi].loc[(samples[mmsi].index >= start) & (samples[mmsi].index < end)].dropna().empty]
            with self.store.connect() as db:
                db.execute("DELETE FROM incidents WHERE area_id=? AND day=?", (area_id, day))
                for item in incidents:
                    db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?)", (area_id, day, item["small_mmsi"], item["big_mmsi"], item["joining_utc"], json.dumps(item)))
                db.execute("INSERT OR REPLACE INTO analysis_runs VALUES(?,?,?,?,?)",
                           (area_id, day, stamp(now()), json.dumps(asdict(thresholds)), json.dumps(missing)))
