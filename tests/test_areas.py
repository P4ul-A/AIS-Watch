"""Non-destructive area removal and upgrades of existing local archives."""

import json

from ais_watch.storage import Store
from support import ArchiveCase, GEOMETRY, MMSI, report


class AreaTests(ArchiveCase):
    """Keep recorded data usable when watch controls are changed or removed."""

    def test_delete_active_area_hides_it_stops_watch_and_preserves_data(self):
        """Removal pauses collection and leaves track exports and history available."""
        self.save([report(self.start)])
        self.client.post('/api/settings', json={'enabled': True})
        response = self.client.delete(f"/api/areas/{self.area['id']}", json={})
        self.assertEqual(response.status_code, 200)
        state = self.client.get('/api/state').get_json()
        self.assertEqual(state['areas'], [])
        self.assertFalse(state['settings']['enabled'])
        self.assertEqual(len(self.csv_rows()), 1)
        self.assertEqual(state['deadlines'], {})
        replay = self.client.get('/replay?day=2026-10-25')
        self.assertIn(b'const searchArea = null;', replay.data)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM areas WHERE id=?', (self.area['id'],)).fetchone()
            self.assertIsNotNone(row['deleted_at'])
            self.assertIsNotNone(row['active_until'])
            self.assertEqual(db.execute('SELECT count(*) FROM watches').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.client.post(f"/api/areas/{self.area['id']}/activate", json={}).status_code, 400)

    def test_delete_unused_area_keeps_another_active_watch_running(self):
        """Removing a draft rectangle cannot pause the actual recording area."""
        self.client.post('/api/settings', json={'enabled': True})
        draft = self.client.post('/api/areas', json={'name': 'Draft', 'geometry': GEOMETRY}).get_json()['id']
        self.assertEqual(self.client.delete(f'/api/areas/{draft}', json={}).status_code, 200)
        state = self.client.get('/api/state').get_json()
        self.assertEqual([area['id'] for area in state['areas']], [self.area['id']])
        self.assertTrue(state['settings']['enabled'])

    def test_schema_one_archive_migrates_without_reseeding_or_losing_reports(self):
        """Existing deployed databases gain area deletion without a fresh volume."""
        self.save([report(self.start)])
        before = self.positions()
        self.client.post('/api/boats', json={'mmsis': [MMSI], 'name': 'Keep my name'})
        with self.store.connect() as db:
            db.execute('ALTER TABLE areas DROP COLUMN deleted_at')
            db.execute('PRAGMA user_version=1')
        Store(self.temporary.name).initialize()
        self.assertEqual(self.positions(), before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertIsNone(db.execute('SELECT deleted_at FROM areas WHERE id=?', (self.area['id'],)).fetchone()[0])
            self.assertEqual(db.execute('SELECT name FROM boats WHERE mmsi=?', (MMSI,)).fetchone()[0], 'Keep my name')
