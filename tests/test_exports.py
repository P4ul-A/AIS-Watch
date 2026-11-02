"""Lossless formats, filtering, concurrency, and bounded-memory streaming exports."""

import csv
import io
import json
import tracemalloc
from datetime import timedelta

from ais_watch.storage import day_bounds, parse, stamp
from support import ArchiveCase, MMSI, SECOND, embedded_tracks, report


class ExportTests(ArchiveCase):
    """Compare exported reports and metadata against the saved source records."""

    def test_csv_preserves_unicode_quoting_precision_and_all_original_fields(self):
        """CSV remains round-trippable with nested JSON and multiline boat names."""
        payload = [report(self.start + timedelta(seconds=second, microseconds=123456), longitude=20.123456789)
                   for second in range(4)]
        self.save(payload)
        name = 'Ørn, "Research"\nSkjervøy'
        self.client.post('/api/boats', json={'mmsis': [MMSI], 'name': name, 'color_override': '#123456'})
        rows = self.csv_rows()
        self.assertEqual(len(rows), len(payload))
        for row, original in zip(rows, payload):
            self.assertEqual(json.loads(row['original_report_json']), original)
            self.assertEqual(row['name'], name)
            self.assertEqual(row['color'], '#123456')
            self.assertEqual(float(row['longitude']), original['longitude'])
            self.assertEqual(row['timestamp_utc'], original['msgtime'])
            self.assertEqual(parse(row['timestamp_oslo']), parse(original['msgtime']))

    def test_inclusive_dates_use_oslo_boundaries_and_exclude_following_midnight(self):
        """Filtering includes all 25 DST hours, with exclusive next-day midnight."""
        payload = [report(self.start - timedelta(microseconds=1)), report(self.start),
                   report(self.end - timedelta(microseconds=1)), report(self.end)]
        self.save(payload)
        rows = self.csv_rows()
        self.assertEqual([row['timestamp_utc'] for row in rows], [stamp(self.start), stamp(self.end - timedelta(microseconds=1))])

    def test_repeated_dst_hour_and_spring_transition_export_distinct_instants(self):
        """Two local 02:30 reports with different offsets never collapse together."""
        for day in ('2026-03-29', '2026-10-25'):
            with self.subTest(day=day):
                start, end = day_bounds(day)
                area = self.add_area(start, closed=end, name=day)
                self.save([report(start + timedelta(minutes=minute)) for minute in range(int((end-start).total_seconds()/60))], area=area, closed=end)
                rows = self.csv_rows(start=day, end=day)
                self.assertEqual(len(rows), int((end-start).total_seconds()/60))
                if day == '2026-10-25':
                    repeated = [row for row in rows if 'T02:30:' in row['timestamp_oslo']]
                    self.assertEqual(len(repeated), 2)
                    self.assertNotEqual(repeated[0]['timestamp_oslo'], repeated[1]['timestamp_oslo'])

    def test_vessel_filters_apply_to_all_three_formats(self):
        """CSV, GeoJSON and embedded replay select exactly the requested vessels."""
        self.save([report(self.start, mmsi=MMSI), report(self.start, mmsi=SECOND)])
        self.assertEqual({int(row['mmsi']) for row in self.csv_rows(mmsis=str(SECOND))}, {SECOND})
        features = self.export('geojson', mmsis=str(SECOND)).get_json()['features']
        self.assertEqual({item['properties']['mmsi'] for item in features}, {SECOND})
        tracks = embedded_tracks(self.export('html', mmsis=str(SECOND)).data.decode())
        self.assertEqual({item['mmsi'] for item in tracks}, {SECOND})

    def test_unknown_vessel_and_empty_archive_have_valid_empty_exports(self):
        """An empty selection still gives valid CSV, GeoJSON and standalone HTML."""
        for populated in (False, True):
            if populated:
                self.save([report(self.start)])
            with self.subTest(populated=populated):
                self.assertEqual(self.csv_rows(mmsis='999999999'), [])
                self.assertEqual(self.export('geojson', mmsis='999999999').get_json()['features'], [])
                self.assertEqual(embedded_tracks(self.export('html', mmsis='999999999').data.decode()), [])

    def test_invalid_export_parameters_are_rejected(self):
        """Bad dates, reverse intervals, MMSIs and formats fail before streaming."""
        for parameters in ({'start': 'bad'}, {'start': '2026-10-27', 'end': '2026-10-25'},
                           {'mmsis': 'abc'}, {'format': 'unsupported'}):
            with self.subTest(parameters=parameters):
                response = self.client.get('/api/export', query_string={'start': '2026-10-25', 'end': '2026-10-25', **parameters})
                self.assertEqual(response.status_code, 400)
                self.assertIn('error', response.get_json())

    def test_geojson_preserves_every_fix_and_splits_only_long_gaps(self):
        """A gap equal to the limit connects; longer gaps and different boats split."""
        payload = [report(self.start + timedelta(minutes=minute)) for minute in (0, 30, 61)]
        payload += [report(self.start, mmsi=SECOND)]
        self.save(payload)
        features = self.export('geojson').get_json()['features']
        self.assertEqual(len(features), 3)
        by_boat = [item for item in features if item['properties']['mmsi'] == MMSI]
        self.assertEqual([item['geometry']['type'] for item in by_boat], ['LineString', 'Point'])
        self.assertEqual([len(item['properties']['timestamps_utc']) for item in by_boat], [2, 1])
        self.assertEqual(sum(len(item['properties']['timestamps_utc']) for item in features), len(payload))

    def test_sampling_never_changes_saved_or_exported_resolution(self):
        """Website thinning cannot remove raw positions from any exported format."""
        self.save([report(self.start + timedelta(seconds=second)) for second in range(120)])
        self.client.post('/api/settings', json={'display_interval_seconds': 60})
        rows = self.csv_rows()
        self.assertEqual(len(rows), 120)
        features = self.export('geojson').get_json()['features']
        self.assertEqual(sum(len(item['properties']['timestamps_utc']) for item in features), 120)
        exported = embedded_tracks(self.export('html').data.decode())
        self.assertEqual(sum(len(segment) for track in exported for segment in track['segments']), 120)
        replay = self.client.get('/replay?day=2026-10-25')
        preview = embedded_tracks(replay.data.decode())
        self.assertLess(sum(len(segment) for track in preview for segment in track['segments']), 120)
        self.assertEqual(len(self.positions()), 120)

    def test_current_boat_metadata_matches_across_export_formats(self):
        """Names, category labels and effective colours survive export consistently."""
        self.save([report(self.start)])
        self.client.post('/api/boats', json={'mmsis': [MMSI], 'name': 'Export boat', 'color_override': '#123456', 'category_id': 1})
        csv_row = self.csv_rows()[0]
        feature = self.export('geojson').get_json()['features'][0]['properties']
        track = embedded_tracks(self.export('html').data.decode())[0]
        for exported in (csv_row, feature, track):
            self.assertEqual((exported['name'], exported['category'], exported['color']), ('Export boat', 'Other / uncategorised', '#123456'))

    def test_new_boat_arriving_before_stream_opens_does_not_break_export(self):
        """A concurrent collector cannot produce positions without export metadata."""
        for format_name in ('csv', 'geojson'):
            with self.subTest(format=format_name):
                response = self.client.get('/api/export', query_string={'day': '2026-10-25', 'format': format_name}, buffered=False)
                try:
                    self.save([report(self.start, mmsi=244000111 if format_name == 'csv' else 244000222)])
                    data = response.get_data()
                    if format_name == 'csv':
                        rows = list(csv.DictReader(io.StringIO(data.decode())))
                        self.assertTrue(rows)
                    else:
                        self.assertTrue(json.loads(data)['features'])
                finally:
                    response.close()

    def test_committed_changes_during_csv_stream_do_not_change_its_snapshot(self):
        """Later commits do not duplicate/remove rows or mix metadata mid-export."""
        self.save([report(self.start + timedelta(seconds=second)) for second in range(5)])
        response = self.client.get('/api/export?day=2026-10-25&format=csv', buffered=False)
        iterator = iter(response.response)
        chunks = [next(iterator), next(iterator)]
        self.save([report(self.start + timedelta(seconds=10))])
        self.client.post('/api/boats', json={'mmsis': [MMSI], 'name': 'Changed mid-export'})
        chunks.extend(iterator)
        response.close()
        rows = list(csv.DictReader(io.StringIO(b''.join(chunks).decode())))
        self.assertEqual(len(rows), 5)
        self.assertEqual(len({row['name'] for row in rows}), 1)
        self.assertEqual(len(self.csv_rows()), 6)

    def test_abandoned_stream_releases_sqlite_reader(self):
        """Cancelling a download cannot pin WAL files forever."""
        self.save([report(self.start + timedelta(seconds=second)) for second in range(5)])
        for format_name in ('csv', 'geojson'):
            response = self.client.get('/api/export', query_string={'day': '2026-10-25', 'format': format_name}, buffered=False)
            iterator = iter(response.response)
            next(iterator)
            next(iterator)
            response.close()
        with self.store.connect() as db:
            checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            self.assertEqual(checkpoint[0], 0)

    def test_long_continuous_track_streams_without_accumulating_entire_track(self):
        """Streaming 30,000 fixes stays bounded instead of buffering every raw JSON."""
        count = 30_000
        with self.store.connect() as db:
            db.executemany('INSERT INTO positions VALUES(?,?,?,?,?)',
                           ((MMSI, stamp(self.start + timedelta(seconds=index)), 20.5, 70,
                             json.dumps(report(self.start + timedelta(seconds=index), extra='x'*512))) for index in range(count)))
        for format_name in ('csv', 'geojson'):
            with self.subTest(format=format_name):
                tracemalloc.start()
                response = self.client.get('/api/export', query_string={'day': '2026-10-25', 'format': format_name}, buffered=False)
                try:
                    size = sum(len(chunk) for chunk in response.response)
                    _, peak = tracemalloc.get_traced_memory()
                    self.assertGreater(size, count * 20)
                    self.assertLess(peak, 8 * 1024 * 1024, f'{format_name} buffered {peak / 1024**2:.1f} MiB')
                finally:
                    response.close()
                    tracemalloc.stop()
