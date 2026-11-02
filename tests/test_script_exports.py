"""Verify exported SQLite downloads with the actual offline script readers."""

import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from datetime import timedelta
from pathlib import Path

from scripts.common.area import discover_api, load_area_positions, read_area
from scripts.common.download_cache import DownloadCache
from scripts.common.tracks import clean_positions, read_api, read_config
from support import ArchiveCase, MMSI, SECOND, report


class ScriptExportTests(ArchiveCase):
    """Test exact cache compatibility rather than just checking SQLite file headers."""

    def bundle(self):
        payload = [report(self.start + timedelta(hours=1), mmsi=MMSI),
                   report(self.start + timedelta(hours=2), mmsi=SECOND),
                   report(self.end - timedelta(minutes=10), mmsi=MMSI),
                   report(self.end + timedelta(hours=1), mmsi=MMSI, longitude=22)]
        self.save(payload)
        response = self.export('scripts', end='2026-10-26')
        folder = Path(self.temporary.name) / 'bundle'
        folder.mkdir(exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(set(archive.namelist()), {'download.sqlite', 'config.ini', 'area.geojson', 'README.txt'})
            archive.extractall(folder)
        return folder, payload

    def test_sqlite_download_uses_real_gzip_response_cache_and_embedded_config(self):
        """The single-file export preserves original reports without credentials."""
        original = report(self.start + timedelta(hours=1))
        self.save([original])
        response = self.export('sqlite')
        path = Path(self.temporary.name) / 'download.sqlite'
        path.write_bytes(response.data)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            config = db.execute("SELECT value FROM metadata WHERE key='config.ini'").fetchone()[0]
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='source'").fetchone()[0], 'ais-watch-archive')
            self.assertGreater(db.execute('SELECT count(*) FROM responses').fetchone()[0], 0)
        config_path = Path(self.temporary.name) / 'config.ini'
        config_path.write_text(config)
        frame = read_api(read_config(config_path), download=path, offline=True)
        self.assertEqual(len(frame), 1)
        self.assertEqual(float(frame.iloc[0]['longitude']), original['longitude'])
        self.assertNotIn(b'private-test-secret', response.data)
        with self.assertRaisesRegex(ValueError, '--offline'):
            with DownloadCache(path):
                pass

    def test_bundle_handles_dst_chunks_empty_days_filters_and_area_discovery(self):
        """All original reports round-trip through track and area script readers."""
        folder, payload = self.bundle()
        config = read_config(folder / 'config.ini')
        frame = clean_positions(read_api(config, download=folder / 'download.sqlite', offline=True), config)
        self.assertEqual(len(frame), len(payload))
        area = read_area(folder / 'area.geojson')
        discovered = discover_api(config, area, download=folder / 'download.sqlite', offline=True)
        self.assertEqual(set(discovered), {MMSI, SECOND})
        area_frame = load_area_positions(config, area, download=folder / 'download.sqlite', offline=True)
        self.assertEqual(len(area_frame), len(payload))
        filtered = self.export('sqlite', end='2026-10-26', mmsis=str(SECOND))
        filtered_path = folder / 'filtered.sqlite'
        filtered_path.write_bytes(filtered.data)
        with sqlite3.connect(filtered_path) as db:
            config_text = db.execute("SELECT value FROM metadata WHERE key='config.ini'").fetchone()[0]
        filtered_config = folder / 'filtered.ini'
        filtered_config.write_text(config_text)
        frame = read_api(read_config(filtered_config), download=filtered_path, offline=True)
        self.assertEqual(set(frame['mmsi']), {SECOND})

    def test_bundle_runs_track_area_and_incident_commands_offline(self):
        """The unmodified public CLI entry points can use an extracted export bundle."""
        folder, _ = self.bundle()
        for script in ('tracks/plot_tracks.py', 'area/plot_area.py', 'incidents/find_incidents.py'):
            with self.subTest(script=script):
                command = [sys.executable, 'scripts/' + script, '--config', str(folder / 'config.ini'),
                           '--save-download', str(folder / 'download.sqlite'), '--offline']
                if not script.startswith('tracks/'):
                    command += ['--output', str(folder / (Path(script).stem + '.html'))]
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((folder / 'tracks.html').exists())
        self.assertTrue((folder / 'find_incidents.csv').exists())

    def test_empty_script_export_has_clear_error(self):
        """Never hand users a script bundle with an unusable empty vessel config."""
        for format_name in ('sqlite', 'scripts'):
            response = self.client.get('/api/export', query_string={'day': '2026-10-25', 'format': format_name})
            self.assertEqual(response.status_code, 400)
            self.assertIn('No saved reports', response.get_json()['error'])
