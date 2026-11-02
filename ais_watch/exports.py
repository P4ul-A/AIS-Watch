"""Stream continuous GeoJSON tracks without retaining the entire track in memory."""

import json
import tempfile
from itertools import chain, groupby

from .storage import parse


def geojson_stream(rows, gap_minutes):
    """Yield gap-split features with matching coordinates and UTC timestamp arrays.

    Rows and their boat metadata come from one SQLite query snapshot. Long
    coordinate arrays stream directly; timestamps spill to a temporary file
    after 64 KiB so a season-long continuous track cannot exhaust memory.
    Closing an abandoned download also closes its underlying database reader.
    """
    previous = None
    segment = 0

    def segment_key(row):
        nonlocal previous, segment
        timestamp = parse(row['timestamp'])
        if previous and (row['mmsi'] != previous[0] or (timestamp - previous[1]).total_seconds() > gap_minutes * 60):
            segment += 1
        previous = (row['mmsi'], timestamp)
        return segment

    try:
        yield '{"type":"FeatureCollection","features":['
        for index, (_, points) in enumerate(groupby(rows, key=segment_key)):
            first = next(points)
            second = next(points, None)
            properties = {'mmsi': first['mmsi'], 'name': first['display_name'],
                          'category': first['category'], 'color': first['color']}
            if index:
                yield ','
            if second is None:
                properties['timestamps_utc'] = [first['timestamp']]
                yield json.dumps({'type': 'Feature', 'geometry': {
                    'type': 'Point', 'coordinates': [first['longitude'], first['latitude']]},
                    'properties': properties})
                continue
            with tempfile.SpooledTemporaryFile(max_size=64 * 1024, mode='w+', encoding='utf-8') as timestamps:
                yield '{"type":"Feature","geometry":{"type":"LineString","coordinates":['
                for position, row in enumerate(chain((first, second), points)):
                    separator = ',' if position else ''
                    timestamps.write(separator + json.dumps(row['timestamp']))
                    yield separator + json.dumps([row['longitude'], row['latitude']])
                yield ']},"properties":' + json.dumps(properties)[:-1] + ',"timestamps_utc":['
                timestamps.seek(0)
                while chunk := timestamps.read(64 * 1024):
                    yield chunk
                yield ']}}'
        yield ']}'
    finally:
        if hasattr(rows, 'close'):
            rows.close()
