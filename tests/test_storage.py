"""Durability, visit eligibility, retention, and backup/restore regression tests."""

import json
import sqlite3
import subprocess
import sys
import threading
import tempfile
from datetime import timedelta
from pathlib import Path

from ais_watch.storage import Store, day_bounds, parse, stamp
from ais_watch.worker import Worker
from scripts.common.tracks import read_api, read_config
from support import ArchiveCase, MMSI, SECOND, embedded_tracks, report


class StorageTests(ArchiveCase):
    """Verify data on disk across independent connections and process lifetimes."""

    def test_restart_preserves_positions_preferences_and_original_fields(self):
        """Seeding and fresh application instances never reset archived user data."""
        original = report(self.start + timedelta(hours=8))
        self.save([original])
        response = self.client.post('/api/boats', json={"mmsis": [MMSI], "name": "Custom boat", "color_override": "#123456", "category_id": 1})
        self.assertEqual(response.status_code, 200)
        before = self.positions()
        self.store = Store(self.temporary.name)
        self.store.initialize()
        self.client = self.make_client()
        self.assertEqual(self.positions(), before)
        boat = next(boat for boat in self.store.boats() if boat['mmsi'] == MMSI)
        self.assertEqual((boat['display_name'], boat['color'], boat['category_id']), ("Custom boat", "#123456", 1))
        self.assertEqual(json.loads(before[0][-1]), original)

    def test_entry_backfill_expiry_and_return(self):
        """Backfill entry day, include the deadline, exclude later reports until return."""
        hours = (1, 8, 20, 32, 33)
        payload = [report(self.start + timedelta(hours=hour), longitude=20.5 if hour == 8 else 22) for hour in hours]
        self.save(payload)
        self.assertEqual([parse(row[1]) for row in self.positions()], [self.start + timedelta(hours=hour) for hour in hours[:-1]])
        self.save(payload + [report(self.start + timedelta(hours=34))])
        self.assertEqual(len(self.positions()), 6)
        with self.store.connect() as db:
            self.assertEqual(parse(db.execute('SELECT max(deadline) FROM watches').fetchone()[0]), self.start + timedelta(hours=58))

    def test_expiry_never_deletes_old_positions(self):
        """A later expired-watch refresh leaves earlier reports untouched."""
        self.save([report(self.start + timedelta(hours=1))])
        before = self.positions()
        self.save([report(self.start + timedelta(days=400), longitude=22)])
        self.assertEqual(self.positions(), before)

    def test_exact_duplicates_update_raw_fields_but_keep_conflicting_coordinates(self):
        """One fix is idempotent; conflicting positions remain distinct for analysis."""
        timestamp = self.start + timedelta(hours=3)
        first = report(timestamp)
        self.save([first] * 3)
        self.save([{**first, "heading": 123}, report(timestamp, longitude=20.6)])
        self.assertEqual(len(self.positions()), 2)
        self.assertEqual(json.loads(self.positions()[0][-1])['heading'], 123)

    def test_failed_transaction_rolls_back_visit_and_all_positions(self):
        """A serialization failure cannot leave a partial vessel transaction."""
        self.save([report(self.start)])
        before = self.positions()
        bad = report(self.start + timedelta(hours=1), extra=object())
        with self.assertRaises(TypeError):
            self.save([report(self.start + timedelta(minutes=30)), bad])
        self.assertEqual(self.positions(), before)
        with self.store.connect() as db:
            self.assertEqual(parse(db.execute('SELECT max(last_inside) FROM watches').fetchone()[0]), self.start)

    def test_committed_data_survives_hard_process_exit_uncommitted_data_does_not(self):
        """Exercise a genuine process death with an open SQLite write transaction."""
        self.save([report(self.start)])
        before = self.positions()
        script = '''
import os,sys
from ais_watch.storage import Store
store=Store(sys.argv[1])
with store.connect() as db:
    db.execute("UPDATE boats SET name='Committed before crash' WHERE mmsi=257085820")
connection=store.connect()
db=connection.__enter__()
db.execute("DELETE FROM positions")
db.execute("UPDATE boats SET name='Uncommitted' WHERE mmsi=257085820")
os._exit(23)
'''
        result = subprocess.run([sys.executable, '-c', script, self.temporary.name], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.store = Store(self.temporary.name)
        self.store.initialize()
        self.assertEqual(self.positions(), before)
        self.assertEqual(next(boat for boat in self.store.boats() if boat['mmsi'] == MMSI)['name'], 'Committed before crash')
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_backup_restores_every_table_and_exports_without_credentials(self):
        """A downloaded snapshot alone restores tracks, preferences, and progress."""
        self.save([report(self.start), report(self.start, mmsi=SECOND)])
        self.client.post('/api/boats', json={'mmsis': [MMSI], 'name': 'Restored name', 'color_override': '#123456'})
        self.store.enqueue('collect')
        with self.store.connect() as db:
            db.execute('UPDATE areas SET cursor=? WHERE id=?', (stamp(self.end), self.area['id']))
            Store.put(db, 'interval_minutes', 17)
            expected = {row[0]: [tuple(item) for item in db.execute('SELECT * FROM ' + row[0])]
                        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()}
        response = self.client.get('/api/backup')
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'private-test-secret', response.data)
        with tempfile.TemporaryDirectory() as restored:
            path = Path(restored) / 'ais-watch.sqlite3'
            path.write_bytes(response.data)
            recovered = Store(restored)
            recovered.initialize()
            with recovered.connect() as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                for table, rows in expected.items():
                    self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM ' + table)], rows, table)
            self.store = recovered
            self.client = self.make_client()
            self.assertEqual(len(self.csv_rows()), 2)
            self.assertEqual(self.store.settings()['interval_minutes'], 17)

    def test_backup_excludes_open_uncommitted_writes(self):
        """WAL backup remains readable while a writer holds an uncommitted change."""
        self.save([report(self.start)])
        with self.store.connect() as writer:
            writer.execute('UPDATE boats SET name=? WHERE mmsi=?', ('Uncommitted name', MMSI))
            response = self.client.get('/api/backup')
            self.addCleanup(response.close)
            path = Path(self.temporary.name) / 'snapshot.sqlite3'
            path.write_bytes(response.data)
            with sqlite3.connect(path) as backup:
                self.assertEqual(backup.execute('SELECT count(*) FROM positions').fetchone()[0], 1)
                self.assertNotEqual(backup.execute('SELECT name FROM boats WHERE mmsi=?', (MMSI,)).fetchone()[0], 'Uncommitted name')

    def test_reader_snapshot_does_not_block_a_committing_writer(self):
        """A season export can read its snapshot while collection keeps saving."""
        self.save([report(self.start)])
        with self.store.connect() as reader:
            reader.execute('BEGIN')
            self.assertEqual(reader.execute('SELECT count(*) FROM positions').fetchone()[0], 1)
            self.save([report(self.start + timedelta(seconds=1))])
            self.assertEqual(reader.execute('SELECT count(*) FROM positions').fetchone()[0], 1)
        self.assertEqual(len(self.positions()), 2)

    def test_future_schema_refused_without_changing_archive(self):
        """An older application must leave a newer database untouched."""
        self.save([report(self.start)])
        with self.store.connect() as db:
            db.execute('PRAGMA user_version=999')
        with self.assertRaises(RuntimeError):
            Store(self.temporary.name).initialize()
        self.assertEqual(len(self.positions()), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 999)

    def test_full_six_month_season_remains_exportable_after_restart(self):
        """Archive 180 local days for three vessels, then export months-old reports."""
        first = day_bounds('2025-10-01')[0]
        boats = (MMSI, SECOND, 259019260)
        self.area = self.add_area(first, closed=first + timedelta(days=200), name='Previous season')
        expected = 0
        for day in range(180):
            timestamp = first + timedelta(days=day, hours=12)
            payload = [report(timestamp + timedelta(minutes=minute), mmsi=mmsi)
                       for mmsi in boats for minute in (0, 1, 2)]
            self.save(payload, area=self.area, closed=first + timedelta(days=200))
            expected += len(payload)
            if day % 30 == 0:
                self.store = Store(self.temporary.name)
                self.store.initialize()
                self.worker = Worker(self.store)
        self.client = self.make_client()
        rows = self.csv_rows(start='2025-10-01', end='2026-03-30')
        self.assertEqual(len(rows), expected)
        self.assertEqual({int(row['mmsi']) for row in rows}, set(boats))
        self.assertEqual(len({(row['mmsi'], row['timestamp_utc']) for row in rows}), expected)
        self.assertEqual(json.loads(rows[0]['original_report_json'])['extra'], {'preserve': [1, None, 'ø']})
        features = self.export('geojson', start='2025-10-01', end='2026-03-30').get_json()['features']
        self.assertEqual(sum(len(feature['properties']['timestamps_utc']) for feature in features), expected)
        tracks = embedded_tracks(self.export('html', start='2025-10-01', end='2026-03-30').data.decode())
        self.assertEqual(sum(len(segment) for track in tracks for segment in track['segments']), expected)
        download = self.export('sqlite', start='2025-10-01', end='2026-03-30')
        cache_path = Path(self.temporary.name) / 'season.sqlite'
        cache_path.write_bytes(download.data)
        with sqlite3.connect(cache_path) as cache:
            config_text = cache.execute("SELECT value FROM metadata WHERE key='config.ini'").fetchone()[0]
        config_path = Path(self.temporary.name) / 'season.ini'
        config_path.write_text(config_text)
        offline = read_api(read_config(config_path), download=cache_path, offline=True)
        self.assertEqual(len(offline), expected)



    def test_backup_includes_committed_reports_still_in_wal(self):
        """A backup must include WAL commits that are not yet in the main file."""
        with self.store.connect() as keeper:
            keeper.execute('BEGIN')
            keeper.execute('SELECT count(*) FROM positions').fetchone()
            self.save([report(self.start + timedelta(seconds=index)) for index in range(50)])
            wal = Path(str(self.store.path) + '-wal')
            self.assertTrue(wal.exists())
            self.assertGreater(wal.stat().st_size, 32)
            response = self.client.get('/api/backup')
            self.addCleanup(response.close)
            destination = Path(self.temporary.name) / 'wal-backup.sqlite3'
            destination.write_bytes(response.data)
            with sqlite3.connect(destination) as backup:
                self.assertEqual(backup.execute('SELECT count(*) FROM positions').fetchone()[0], 50)
                self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_backups_during_collection_are_transactionally_consistent(self):
        """Each live snapshot sees its position count and paired setting together."""
        stop = threading.Event()
        ready = threading.Event()
        failures = []

        def write_reports():
            try:
                for index in range(1000):
                    if stop.is_set():
                        break
                    timestamp = self.start + timedelta(seconds=index)
                    with self.store.connect() as db:
                        db.execute('INSERT INTO positions VALUES(?,?,?,?,?)',
                                   (MMSI, stamp(timestamp), 20.5, 70, json.dumps(report(timestamp))))
                        Store.put(db, 'paired_position_count', index + 1)
                    ready.set()
                    stop.wait(.002)
            except Exception as error:
                failures.append(error)
                ready.set()

        thread = threading.Thread(target=write_reports)
        thread.start()
        try:
            self.assertTrue(ready.wait(5), 'Background writer failed to start')
            for index in range(3):
                response = self.client.get('/api/backup')
                self.addCleanup(response.close)
                destination = Path(self.temporary.name) / f'live-backup-{index}.sqlite3'
                destination.write_bytes(response.data)
                with sqlite3.connect(destination) as backup:
                    count = backup.execute('SELECT count(*) FROM positions').fetchone()[0]
                    paired = json.loads(backup.execute("SELECT value FROM settings WHERE key='paired_position_count'").fetchone()[0])
                    self.assertEqual(count, paired)
                    self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
