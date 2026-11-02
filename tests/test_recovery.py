"""Collection recovery with deterministic time and a simulated historic API."""

import time
from datetime import timedelta
from unittest.mock import patch

from ais_watch.storage import Store, parse, stamp
from ais_watch.worker import Worker
from support import ArchiveCase, MMSI, SECOND, report


class RecoveryTests(ArchiveCase):
    """Run the real collector with controlled reports, outages and process restarts."""

    def collect(self, current, payload, failure=None):
        """Simulate discovery and track HTTP responses, preserving collector control flow."""
        def discover(config, area):
            return sorted({item['mmsi'] for item in payload if config.start <= parse(item['msgtime']) < config.end
                           and 20 <= item['longitude'] <= 21})

        def tracks(session, method, url, params=None):
            mmsi = int(url.split('/tracks/')[1].split('/')[0])
            if failure is not None:
                failure(mmsi)
            return [item for item in payload if item['mmsi'] == mmsi]

        with patch('ais_watch.worker.now', return_value=current + timedelta(seconds=5)), \
             patch('ais_watch.worker.discover_api', side_effect=discover), \
             patch('ais_watch.worker.authorize', return_value=time.monotonic() + 300), \
             patch('ais_watch.worker.request_json', side_effect=tracks):
            return self.worker.collect(self.store.enqueue('collect'))

    def test_partial_vessel_failure_retries_without_data_loss_or_duplicates(self):
        """A failed day retains earlier boats and does not advance its checkpoint."""
        payload = [report(self.start + timedelta(hours=1), mmsi=mmsi) for mmsi in (MMSI, SECOND)]
        def fail_second(mmsi):
            if mmsi == SECOND:
                raise ValueError('Simulated outage')
        with self.assertRaisesRegex(ValueError, 'Simulated outage'):
            self.collect(self.start + timedelta(hours=2), payload, failure=fail_second)
        self.assertEqual(len(self.positions()), 1)
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT cursor FROM areas WHERE id=?', (self.area['id'],)).fetchone()[0])
        self.store = Store(self.temporary.name)
        self.store.initialize()
        self.worker = Worker(self.store)
        self.collect(self.start + timedelta(hours=2), payload)
        self.assertEqual(len(self.positions()), 2)
        self.collect(self.start + timedelta(hours=3), payload)
        self.assertEqual(len(self.positions()), 2)

    def test_recent_interval_refresh_adds_late_reports(self):
        """A completed request is refreshed when later runs receive additional fixes."""
        initial = report(self.start + timedelta(hours=1))
        late = report(self.start + timedelta(hours=1, minutes=30))
        self.collect(self.start + timedelta(hours=2), [initial])
        self.collect(self.start + timedelta(hours=3), [initial, late])
        self.assertEqual(len(self.positions()), 2)

    def test_completed_day_and_pending_analysis_are_one_atomic_checkpoint(self):
        """Failure to persist pending analysis must also roll back the day's cursor."""
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_pending_analysis BEFORE INSERT ON collected_days BEGIN SELECT RAISE(ABORT,'simulated checkpoint failure'); END")
        with self.assertRaisesRegex(Exception, 'simulated checkpoint failure'):
            self.collect(self.start + timedelta(hours=2), [report(self.start + timedelta(hours=1))])
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT cursor FROM areas WHERE id=?', (self.area['id'],)).fetchone()[0])
        self.assertEqual(len(self.positions()), 1)

    def test_missed_recent_days_are_recovered_and_older_gaps_are_reported(self):
        """A long outage preserves old data and records the portion beyond API history."""
        self.save([report(self.start)])
        current = self.start + timedelta(days=25)
        recent = report(current - timedelta(hours=1))
        self.collect(current, [recent])
        self.assertEqual(len(self.positions()), 2)
        with self.store.connect() as db:
            gaps = db.execute('SELECT * FROM gaps').fetchall()
            self.assertEqual(len(gaps), 1)
            self.assertEqual(parse(gaps[0]['start']), self.start)
            self.assertEqual(parse(gaps[0]['end']), current - timedelta(days=14) + timedelta(minutes=5))
            self.assertEqual(parse(db.execute('SELECT cursor FROM areas WHERE id=?', (self.area['id'],)).fetchone()[0]), current)

    def test_retired_area_finishes_grace_without_new_discovery(self):
        """An area switch cannot silently truncate the old area's follow window."""
        closed = self.start + timedelta(hours=3)
        with self.store.connect() as db:
            db.execute('UPDATE areas SET active_until=? WHERE id=?', (stamp(closed), self.area['id']))
        self.save([report(self.start + timedelta(hours=2))], closed=closed)
        outside = report(self.start + timedelta(hours=25), longitude=22)
        self.collect(self.start + timedelta(hours=27), [outside])
        self.assertEqual(len(self.positions()), 2)
        before = self.positions()
        self.collect(self.start + timedelta(days=30), [])
        self.assertEqual(self.positions(), before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM gaps').fetchone()[0], 0)

    def test_pending_analysis_survives_store_reinitialization(self):
        """A restart cannot lose the days whose reports were already committed."""
        self.collect(self.start + timedelta(hours=2), [report(self.start + timedelta(hours=1))])
        expected = self.worker.pending_analysis_targets()
        self.assertIn((self.area['id'], '2026-10-25'), expected)
        self.store = Store(self.temporary.name)
        self.store.initialize()
        self.assertEqual(Worker(self.store).pending_analysis_targets(), expected)

    def test_running_jobs_are_requeued_on_restart(self):
        """Worker startup recovers interrupted work before processing any job."""
        job = self.store.enqueue('collect')
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
        worker = Worker(self.store)
        with patch.object(worker.thread, 'start'):
            worker.start()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?', (job,)).fetchone()[0], 'queued')
