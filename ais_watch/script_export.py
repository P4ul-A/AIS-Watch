"""Export recorded reports in the standalone scripts' compressed SQLite format."""

import configparser
import gzip
import io
import json
import re
import sqlite3
from datetime import timedelta
from itertools import groupby

from scripts.common.area import AREA_URL, area_tiles
from scripts.common.download_cache import request_key
from scripts.common.tracks import API_ROOT
from shapely.geometry import shape

from .storage import parse, stamp


def script_download(path, rows, start, end, settings, area=None):
    """Write exact script request keys and gzip JSON payloads from an archive snapshot.

    Track requests use the scripts' 24-hour chunks, including empty chunks.
    Area requests describe only vessels in this exported archive, not complete
    historical API coverage. Metadata marks that distinction and embeds the
    matching config; the companion bundle exposes it as ordinary files.
    """
    boats = []
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE responses(request_key TEXT PRIMARY KEY,payload BLOB NOT NULL);
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE exported_positions(mmsi INTEGER,timestamp TEXT,longitude REAL,latitude REAL);
            CREATE INDEX exported_time ON exported_positions(timestamp);
        ''')

        def save(key, payload):
            db.execute('INSERT OR REPLACE INTO responses VALUES(?,?)',
                       (key, gzip.compress(json.dumps(payload, separators=(',', ':')).encode(), compresslevel=3)))

        for mmsi, reports in groupby(rows, key=lambda row: row['mmsi']):
            pending = next(reports)
            boats.append(pending)
            chunk_start = start
            while chunk_start < end:
                chunk_end = min(chunk_start + timedelta(days=1), end)
                payload = []
                while pending is not None and parse(pending['timestamp']) < chunk_end:
                    payload.append(json.loads(pending['payload']))
                    db.execute('INSERT INTO exported_positions VALUES(?,?,?,?)',
                               (mmsi, pending['timestamp'], pending['longitude'], pending['latitude']))
                    pending = next(reports, None)
                url = f'{API_ROOT}/{mmsi}/{chunk_start.isoformat()}/{chunk_end.isoformat()}'
                save(request_key('GET', url, params={'modelFormat': 'Json', 'filterSatellitePositions': 'false'}), payload)
                chunk_start = chunk_end
        if not boats:
            raise ValueError('No saved reports match this selection. Choose dates with recorded data.')
        if area:
            chunk_start = start
            while chunk_start < end:
                chunk_end = min(chunk_start + timedelta(days=7), end)
                for polygon in area_tiles(area):
                    west, south, east, north = shape(polygon).bounds
                    mmsis = [row[0] for row in db.execute('''SELECT DISTINCT mmsi FROM exported_positions
                        WHERE timestamp>=? AND timestamp<? AND longitude BETWEEN ? AND ?
                        AND latitude BETWEEN ? AND ? ORDER BY mmsi''',
                        (stamp(chunk_start), stamp(chunk_end), west, east, south, north))]
                    body = {'polygon': polygon, 'msgTimeFrom': chunk_start.isoformat(), 'msgTimeTo': chunk_end.isoformat()}
                    save(request_key('POST', AREA_URL, body=body), mmsis)
                chunk_start = chunk_end
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config['tracks'] = {'source': 'api', 'start': start.isoformat(), 'end': end.isoformat(),
                            'output': 'tracks.html', 'gap_minutes': str(settings['gap_minutes'])}
        config['vessels'] = {}
        for boat in boats:
            name = re.sub(r'[\r\n:=\[\]]', ' ', boat['display_name']).strip()
            config['vessels'][f"{name} ({boat['mmsi']})"] = f"{boat['mmsi']} / {boat['color']}"
        if area:
            config['area'] = {'file': 'area.geojson'}
        config['incidents'] = {
            'small_boats': '\n'.join(str(boat['mmsi']) for boat in boats if boat['role'] == 'small'),
            'big_boats': '\n'.join(str(boat['mmsi']) for boat in boats if boat['role'] == 'big'),
            **{key: str(settings[key]) for key in ('radius_m', 'max_small_speed_knots', 'prior_minutes', 'together_minutes', 'max_gap_minutes')}}
        text = io.StringIO()
        text.write('# AIS Watch recorded reports only; recording gaps and follow cutoffs are retained.\n# Use --offline with download.sqlite; this is not a complete API-history download.\n')
        config.write(text)
        config_text = text.getvalue()
        for key, value in {'source': 'ais-watch-archive', 'config.ini': config_text,
                           'area.geojson': json.dumps(area), 'recorded_only': 'true'}.items():
            db.execute('INSERT INTO metadata VALUES(?,?)', (key, value))
        db.execute('DROP TABLE exported_positions')
    return config_text
