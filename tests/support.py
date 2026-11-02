"""Temporary archives and deterministic AIS reports; tests never use real credentials."""

import base64
import csv
import gzip
import io
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ais_watch.storage import Store, day_bounds, stamp
from ais_watch.web import create_app
from ais_watch.worker import Worker

MMSI = 257085820
SECOND = 259032110
GEOMETRY = {"type": "Feature", "properties": {}, "geometry": {
    "type": "Polygon", "coordinates": [[[20, 69], [21, 69], [21, 71], [20, 71], [20, 69]]]}}


def report(timestamp, mmsi=MMSI, longitude=20.5, latitude=70, **extra):
    """Make an original report with fields that must survive archival and export."""
    return {"mmsi": mmsi, "msgtime": stamp(timestamp), "longitude": longitude,
            "latitude": latitude, "name": 'Båt, "Ørn"\nResearch',
            "speedOverGround": 3.125, "heading": 90,
            "extra": {"preserve": [1, None, "ø"]}, **extra}


def embedded_tracks(html):
    """Decode the actual compressed replay payload, rather than matching HTML text."""
    match = re.search(r'atob\(("[A-Za-z0-9+/=]+")\)', html)
    if not match:
        raise AssertionError("Replay lacks its compressed positions")
    return json.loads(gzip.decompress(base64.b64decode(json.loads(match[1]))))


class ArchiveCase(unittest.TestCase):
    """Give every test its own on-disk WAL archive and a network prohibition."""

    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "AIS_CREDENTIALS_FILE": "/missing-test-credentials.ini",
            "BARENTSWATCH_CLIENT_ID": "test-client",
            "BARENTSWATCH_CLIENT_SECRET": "private-test-secret"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network request"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)
        self.store.initialize()
        self.start, self.end = day_bounds("2026-10-25")
        self.area = self.add_area(self.start)
        self.worker = Worker(self.store)
        self.client = self.make_client()

    def make_client(self):
        """Construct a fresh application to exercise persisted state after restart."""
        app = create_app(self.store)
        app.testing = True
        return app.test_client()

    def add_area(self, start, closed=None, name="Test area"):
        """Create an immutable area revision, active or already retired."""
        with self.store.connect() as db:
            identifier = db.execute("INSERT INTO areas(name,geometry,active_from,active_until) VALUES(?,?,?,?)",
                                    (name, json.dumps(GEOMETRY), stamp(start), stamp(closed) if closed else None)).lastrowid
            return dict(db.execute("SELECT * FROM areas WHERE id=?", (identifier,)).fetchone())

    def save(self, payload, area=None, closed=None):
        """Pass original reports through the production normalizer and visit policy."""
        area = area or self.area
        for mmsi in sorted({row["mmsi"] for row in payload}):
            rows = self.worker.normalize([row for row in payload if row["mmsi"] == mmsi], mmsi,
                                         self.start - timedelta(days=1000), self.end + timedelta(days=1000))
            self.worker.save_reports(area, rows, mmsi, (20, 69, 21, 71),
                                     datetime.fromisoformat(area["active_from"]),
                                     closed or self.end + timedelta(days=1000))

    def positions(self):
        """Return a stable complete archive snapshot for losslessness comparisons."""
        with self.store.connect() as db:
            return [tuple(row) for row in db.execute("SELECT * FROM positions ORDER BY mmsi,timestamp,longitude,latitude")]

    def export(self, format_name="csv", **parameters):
        """Request an actual HTTP export using inclusive local calendar dates."""
        query = {"start": "2026-10-25", "end": "2026-10-25", "format": format_name, **parameters}
        response = self.client.get("/api/export", query_string=query)
        self.assertEqual(response.status_code, 200, response.data[:300])
        self.addCleanup(response.close)
        return response

    def csv_rows(self, **parameters):
        """Parse quoting, Unicode, and embedded JSON with a real CSV reader."""
        return list(csv.DictReader(io.StringIO(self.export(**parameters).data.decode())))
