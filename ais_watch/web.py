"""Small Flask API and website; request handlers never perform AIS downloads."""

import csv
import io
import json
import math
import re
import sqlite3
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path

import pandas as pd
import requests
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
from shapely.geometry import box, mapping, shape

from scripts.common.detection import FIELDS, IncidentSettings, explain_incident
from scripts.common.tracks import TOKEN_URL, Config, build_tracks, render_map
from .exports import geojson_stream
from .script_export import script_download
from .storage import OSLO, Store, colour, day_bounds, describe_boat, now, parse, stamp


def rectangle(feature):
    """Validate the geometry shared by drawing, imports and saved area revisions."""
    try:
        geometry = shape(feature["geometry"])
        west, south, east, north = geometry.bounds
        valid = (feature["type"] == "Feature" and geometry.is_valid
                 and geometry.geom_type == "Polygon" and geometry.equals(box(west, south, east, north))
                 and -180 <= west < east <= 180 and -85 <= south < north <= 85)
        if valid:
            return {"type": "Feature", "properties": {}, "geometry": mapping(geometry)}
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    raise ValueError("Choose a non-empty rectangular GeoJSON Feature in WGS84 coordinates.")


def create_app(store=None):
    """Construct the website without starting a worker or making network requests."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    store = store or Store()
    app.extensions["archive"] = store

    @app.before_request
    def same_origin():
        """Require JSON and same-origin browser writes on the private control API."""
        if request.method in {"POST", "PUT", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="Cross-origin writes are disabled."), 403
            if not request.is_json:
                return jsonify(error="Send application/json."), 415

    @app.errorhandler(ValueError)
    def bad_input(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict(error):
        return jsonify(error="A name already exists or a referenced item is missing."), 409

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        with store.connect() as db:
            db.execute("SELECT 1").fetchone()
        worker = app.extensions.get("worker")
        healthy = worker is None or worker.thread.is_alive()
        return jsonify(ok=healthy), 200 if healthy else 503

    @app.get("/api/state")
    def state():
        with store.connect() as db:
            categories = [dict(row) for row in db.execute("SELECT * FROM categories ORDER BY sort_order,id")]
            areas = [dict(row) for row in db.execute("SELECT * FROM areas WHERE deleted_at IS NULL ORDER BY id DESC")]
            jobs = [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 12")]
            gaps = [dict(row) for row in db.execute("SELECT * FROM gaps ORDER BY end DESC LIMIT 30")]
            deadlines = {row["mmsi"]: row["deadline"] for row in db.execute("SELECT w.mmsi,max(w.deadline) AS deadline FROM watches w JOIN areas a ON a.id=w.area_id WHERE a.deleted_at IS NULL GROUP BY w.mmsi")}
            first = db.execute("SELECT timestamp FROM positions ORDER BY timestamp LIMIT 1").fetchone()
            last = db.execute("SELECT timestamp FROM positions ORDER BY timestamp DESC LIMIT 1").fetchone()
        for area in areas:
            area["geometry"] = json.loads(area["geometry"])
        credentials = store.credentials()
        return jsonify(settings=store.settings(), categories=categories, boats=store.boats(), areas=areas,
                       jobs=jobs, gaps=gaps, deadlines=deadlines, today=now().astimezone(OSLO).date().isoformat(),
                       first=first[0] if first else None, last=last[0] if last else None,
                       credentials={"client_id": credentials["client_id"], "secret_configured": bool(credentials["client_secret"])})

    @app.get("/api/recorded-days")
    def recorded_days():
        """Return days with actual saved positions, using Oslo calendar boundaries.

        Indexed existence queries avoid scanning all reports or treating a
        completed collection with no positions as a day containing data.
        """
        month = request.args.get("month", "")
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
            raise ValueError("Choose a month in YYYY-MM format.")
        start, _ = day_bounds(month + "-01")
        day = start.astimezone(OSLO).date()
        days = []
        with store.connect() as db:
            while day.strftime("%Y-%m") == month:
                lower, upper = day_bounds(day.isoformat())
                if db.execute("SELECT 1 FROM positions WHERE timestamp>=? AND timestamp<? LIMIT 1",
                              (stamp(lower), stamp(upper))).fetchone():
                    days.append(day.isoformat())
                day += timedelta(days=1)
        return jsonify(days=days)

    @app.post("/api/settings")
    def save_settings():
        values = request.get_json()
        current = store.settings()
        starting = values.get("enabled") is True and not current["enabled"]
        if "include_history" in values and not isinstance(values["include_history"], bool):
            raise ValueError("include_history must be true or false.")
        for key in ("enabled", "auto_incidents"):
            if key in values:
                if not isinstance(values[key], bool):
                    raise ValueError(f"{key} must be true or false.")
                current[key] = values[key]
        limits = {"interval_minutes": (1, 10080), "gap_minutes": (1, 1440),
                  "display_interval_seconds": (0, 3600), "radius_m": (1, 100000),
                  "max_small_speed_knots": (0.01, 100), "prior_minutes": (1, 1440),
                  "together_minutes": (1, 1440), "max_gap_minutes": (1, 1440)}
        for key, (low, high) in limits.items():
            if key in values:
                number = float(values[key])
                if not math.isfinite(number) or not low <= number <= high:
                    raise ValueError(f"{key} must be between {low:g} and {high:g}.")
                current[key] = number
        if current["display_interval_seconds"] > current["gap_minutes"] * 60:
            raise ValueError("Display sampling cannot exceed the track gap limit.")
        if "start_date" in values:
            start, _ = day_bounds(values["start_date"])
            if start > now():
                raise ValueError("Recording start cannot be in the future.")
            current["start_date"] = values["start_date"]
        with store.connect() as db:
            if starting and values.get("include_history"):
                # Rewind the active area and its durable cursor together. Existing
                # reports deduplicate during collection; closed areas stay unchanged.
                area = db.execute("SELECT id FROM areas WHERE active_from IS NOT NULL AND active_until IS NULL AND deleted_at IS NULL").fetchone()
                if not area:
                    raise ValueError("Activate a recording area first.")
                if db.execute("SELECT 1 FROM jobs WHERE kind='collect' AND state='running'").fetchone():
                    raise ValueError("Wait for the current collection to finish before including history.")
                # Leave a small margin within the rolling history window.
                start = stamp(now() - timedelta(days=14) + timedelta(minutes=5))
                db.execute("UPDATE areas SET active_from=min(active_from,?),cursor=? WHERE id=?", (start, start, area["id"]))
            for key, value in current.items():
                store.put(db, key, value)
            if starting:
                # Queue immediately rather than waiting for the scheduled interval.
                db.execute("INSERT OR IGNORE INTO jobs(kind,payload,created) VALUES('collect','{}',?)", (stamp(now()),))
        if "client_id" in values or values.get("client_secret"):
            credentials = store.credentials()
            credentials["client_id"] = str(values.get("client_id", credentials["client_id"])).strip()
            if values.get("client_secret"):
                credentials["client_secret"] = str(values["client_secret"]).strip()
            store.save_credentials(credentials)
        return jsonify(ok=True)

    @app.post("/api/credentials/test")
    def test_credentials():
        """Check entered credentials without saving them or exposing the token."""
        values = request.get_json()
        client_id = str(values.get("client_id", "")).strip()
        secret = str(values.get("client_secret", "")).strip() or store.credentials()["client_secret"]
        if not client_id or not secret:
            raise ValueError("Enter a client ID and client secret first.")
        try:
            response = requests.post(TOKEN_URL, data={
                "client_id": client_id, "client_secret": secret,
                "scope": "ais", "grant_type": "client_credentials",
            }, timeout=15)
            if response.status_code in (400, 401, 403):
                return jsonify(error="BarentsWatch rejected these credentials. Check the ID, secret and AIS access."), 400
            response.raise_for_status()
            token = response.json()
        except (requests.RequestException, ValueError):
            return jsonify(error="Could not verify credentials with BarentsWatch. Try again shortly."), 502
        if not isinstance(token, dict) or not token.get("access_token"):
            return jsonify(error="BarentsWatch did not return an access token."), 502
        return jsonify(message="Credentials accepted for AIS access. Save Settings to keep any changes.")

    @app.post("/api/boats")
    def save_boats():
        data = request.get_json()
        identifiers = data.get("mmsis", [])
        if not identifiers:
            raise ValueError("Select at least one boat or provide a nine-digit MMSI.")
        if len(identifiers) > 10000:
            raise ValueError("Select fewer boats per update.")
        with store.connect() as db:
            for identifier in identifiers:
                if not re.fullmatch(r"[0-9]{9}", str(identifier)):
                    raise ValueError("MMSI must contain exactly nine digits.")
                mmsi = int(identifier)
                store.ensure_boat(db, mmsi)
                boat = db.execute("SELECT * FROM boats WHERE mmsi=?", (mmsi,)).fetchone()
                if "category_id" in data and int(data["category_id"]) != boat["category_id"]:
                    category = int(data["category_id"])
                    target = db.execute("SELECT next_slot FROM categories WHERE id=?", (category,)).fetchone()
                    if not target:
                        raise ValueError("Category does not exist.")
                    db.execute("UPDATE boats SET category_id=?,slot=? WHERE mmsi=?", (category, target[0], mmsi))
                    db.execute("UPDATE categories SET next_slot=next_slot+1 WHERE id=?", (category,))
                if "name" in data:
                    db.execute("UPDATE boats SET name=? WHERE mmsi=?", (str(data["name"]).strip()[:120], mmsi))
                if "color_override" in data:
                    override = colour(data["color_override"]) if data["color_override"] else None
                    db.execute("UPDATE boats SET color_override=? WHERE mmsi=?", (override, mmsi))
                if "role" in data:
                    if data["role"] not in ("", "small", "big"):
                        raise ValueError("Choose an incident role: none, small or big.")
                    db.execute("UPDATE boats SET role=? WHERE mmsi=?", (data["role"], mmsi))
        return jsonify(ok=True)

    @app.post("/api/categories")
    def save_category():
        data = request.get_json()
        name = str(data.get("name", "")).strip()[:120]
        if not name:
            raise ValueError("Category name is required.")
        base = colour(data.get("color"))
        order = int(data.get("sort_order", 0))
        with store.connect() as db:
            if data.get("id"):
                db.execute("UPDATE categories SET name=?,color=?,sort_order=? WHERE id=?", (name, base, order, int(data["id"])))
                if data.get("reset"):
                    db.execute("UPDATE boats SET color_override=NULL WHERE category_id=?", (int(data["id"]),))
            else:
                db.execute("INSERT INTO categories(name,color,sort_order) VALUES(?,?,?)", (name, base, order))
        return jsonify(ok=True)

    @app.delete("/api/categories/<int:identifier>")
    def delete_category(identifier):
        if identifier == 1:
            raise ValueError("The default category cannot be deleted.")
        with store.connect() as db:
            for row in db.execute("SELECT mmsi FROM boats WHERE category_id=?", (identifier,)).fetchall():
                slot = db.execute("SELECT next_slot FROM categories WHERE id=1").fetchone()[0]
                db.execute("UPDATE boats SET category_id=1,slot=? WHERE mmsi=?", (slot, row[0]))
                db.execute("UPDATE categories SET next_slot=next_slot+1 WHERE id=1")
            db.execute("DELETE FROM categories WHERE id=?", (identifier,))
        return jsonify(ok=True)

    @app.post("/api/areas")
    def save_area():
        data = request.get_json()
        feature = rectangle(data.get("geometry"))
        name = str(data.get("name", "")).strip()[:120]
        if not name:
            raise ValueError("Give the area a name.")
        with store.connect() as db:
            identifier = db.execute("INSERT INTO areas(name,geometry) VALUES(?,?)", (name, json.dumps(feature))).lastrowid
        return jsonify(id=identifier)

    @app.post("/api/areas/<int:identifier>/activate")
    def activate_area(identifier):
        with store.connect() as db:
            area = db.execute("SELECT * FROM areas WHERE id=? AND deleted_at IS NULL", (identifier,)).fetchone()
            if not area:
                raise ValueError("Area not found.")
            if area["active_from"] and not area["active_until"]:
                return jsonify(ok=True)
            current = stamp(now())
            had_active = db.execute("SELECT 1 FROM areas WHERE active_from IS NOT NULL").fetchone()
            db.execute("UPDATE areas SET active_until=? WHERE active_from IS NOT NULL AND active_until IS NULL", (current,))
            # Initial activation can backfill; subsequent switches start at the switch time.
            start = current if had_active else stamp(day_bounds(store.settings()["start_date"])[0])
            if area["active_from"]:
                db.execute("INSERT INTO areas(name,geometry,active_from) VALUES(?,?,?)", (area["name"], area["geometry"], start))
            else:
                db.execute("UPDATE areas SET active_from=? WHERE id=?", (start, identifier))
        return jsonify(ok=True)

    @app.delete("/api/areas/<int:identifier>")
    def delete_area(identifier):
        """Hide an area and stop its discovery while preserving its archive references."""
        with store.connect() as db:
            area = db.execute("SELECT * FROM areas WHERE id=? AND deleted_at IS NULL", (identifier,)).fetchone()
            if not area:
                raise ValueError("Area not found.")
            current = stamp(now())
            db.execute("UPDATE areas SET deleted_at=?,active_until=CASE WHEN active_from IS NOT NULL THEN coalesce(active_until,?) ELSE active_until END WHERE id=?",
                       (current, current, identifier))
            if area["active_from"] and not area["active_until"]:
                Store.put(db, "enabled", False)
        return jsonify(ok=True)

    @app.post("/api/jobs")
    def queue_job():
        data = request.get_json()
        kind = data.get("kind")
        if kind not in ("collect", "analyse"):
            raise ValueError("Unknown job kind.")
        if data.get("day"):
            day_bounds(data["day"])
        return jsonify(id=store.enqueue(kind, {"day": data.get("day")}))

    @app.get("/api/incidents")
    def incidents():
        day = request.args.get("day", now().astimezone(OSLO).date().isoformat())
        day_bounds(day)
        with store.connect() as db:
            rows = db.execute("SELECT i.*,a.name AS area_name,r.settings AS rule_settings FROM incidents i JOIN areas a ON a.id=i.area_id JOIN analysis_runs r ON r.area_id=i.area_id AND r.day=i.day WHERE i.day=? ORDER BY joining_utc", (day,)).fetchall()
            runs = [dict(row) for row in db.execute("SELECT r.*,a.name AS area_name FROM analysis_runs r JOIN areas a ON a.id=r.area_id WHERE day=?", (day,))]
        boats = {boat["mmsi"]: boat for boat in store.boats()}
        result = []
        for row in rows:
            item = json.loads(row["payload"])
            item.update(area_id=row["area_id"], area_name=row["area_name"])
            for role in ("small", "big"):
                boat = boats[item[role + "_mmsi"]]
                item[role + "_name"] = boat["display_name"]
                item[role + "_color"] = boat["color"]
            item["reason"] = explain_incident(item, IncidentSettings(**json.loads(row["rule_settings"])))
            result.append(item)
        if request.args.get("format") == "csv":
            fields = ["area_name", *FIELDS, "small_color", "big_color"]
            output = io.StringIO()
            writer = csv.DictWriter(output, fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(result)
            return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="incidents-{day}.csv"'})
        return jsonify(incidents=result, runs=runs)

    def selection():
        """Resolve inclusive local date controls to a half-open UTC query."""
        today = now().astimezone(OSLO).date().isoformat()
        start, _ = day_bounds(request.args.get("start", request.args.get("day", today)))
        _, end = day_bounds(request.args.get("end", request.args.get("day", today)))
        if start >= end:
            raise ValueError("End date must be on or after start date.")
        selected = request.args.get("mmsis", "")
        identifiers = []
        for identifier in selected.split(",") if selected else []:
            if not re.fullmatch(r"[0-9]{9}", identifier):
                raise ValueError("Invalid MMSI selection.")
            identifiers.append(int(identifier))
        return start, end, identifiers

    def reports(start, end, identifiers):
        """Read reports and boat metadata from one consistent, indexed snapshot."""
        query = """SELECT p.*,b.name,b.ais_name,b.slot,b.color_override,b.role,
                   c.name AS category,c.color AS base_color
                   FROM positions p JOIN boats b ON b.mmsi=p.mmsi
                   JOIN categories c ON c.id=b.category_id
                   WHERE p.timestamp>=? AND p.timestamp<?"""
        parameters = [stamp(start), stamp(end)]
        if identifiers:
            query += " AND p.mmsi IN (" + ",".join("?" for _ in identifiers) + ")"
            parameters.extend(identifiers)
        query += " ORDER BY p.mmsi,p.timestamp,p.longitude,p.latitude"
        with store.connect() as db:
            for row in db.execute(query, parameters):
                yield describe_boat(row)

    def map_html(web_mode=False):
        """Reuse the standalone replay renderer with persistent names and colours."""
        start, end, identifiers = selection()
        if request.args.get("context") == "1":
            start -= timedelta(days=1)
            end += timedelta(days=1)
        frame = pd.DataFrame(list(reports(start, end, identifiers)), columns=["mmsi", "timestamp", "longitude", "latitude", "payload"])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame["name"] = ""
        boats = {boat["mmsi"]: boat for boat in store.boats()}
        settings = store.settings()
        config = Config(source="archive", start=start, end=end, ships=sorted(frame["mmsi"].unique().tolist()),
                        data="", output=Path("unused.html"), gap_minutes=settings["gap_minutes"],
                        display_interval_seconds=0 if not web_mode else settings["display_interval_seconds"],
                        ship_names={key: boat["display_name"] for key, boat in boats.items()},
                        ship_colors={key: boat["color"] for key, boat in boats.items()})
        tracks = build_tracks(frame, config)
        for track in tracks:
            track["category_id"] = boats[track["mmsi"]]["category_id"]
            track["category"] = boats[track["mmsi"]]["category"]
            track["color"] = boats[track["mmsi"]]["color"]
        with store.connect() as db:
            if request.args.get("area_id"):
                area = db.execute("SELECT geometry FROM areas WHERE id=?", (int(request.args["area_id"]),)).fetchone()
            else:
                area = db.execute("SELECT geometry FROM areas WHERE deleted_at IS NULL AND active_from IS NOT NULL AND active_from<? AND (active_until IS NULL OR active_until>?) ORDER BY active_from DESC LIMIT 1", (stamp(end), stamp(start))).fetchone()
            if not area:
                area = db.execute("SELECT geometry FROM areas WHERE deleted_at IS NULL ORDER BY active_from DESC,id DESC LIMIT 1").fetchone()
        return render_map(tracks, config, area=json.loads(area[0]) if area else None,
                          web_mode=web_mode, time_zone="Europe/Oslo")

    @app.get("/replay")
    def replay():
        return Response(map_html(True), mimetype="text/html")

    @app.get("/api/export")
    def export():
        start, end, identifiers = selection()
        format_name = request.args.get("format", "csv")
        if format_name in ("sqlite", "scripts"):
            temporary = tempfile.TemporaryDirectory(prefix="ais-script-export-")
            try:
                folder = Path(temporary.name)
                with store.connect() as db:
                    area = db.execute("SELECT geometry FROM areas WHERE active_from IS NOT NULL AND active_from<? AND (active_until IS NULL OR active_until>?) ORDER BY active_from DESC LIMIT 1",
                                      (stamp(end), stamp(start))).fetchone()
                feature = json.loads(area[0]) if area else None
                database = folder / "download.sqlite"
                config = script_download(database, reports(start, end, identifiers), start, end, store.settings(), feature)
                if format_name == "sqlite":
                    return temporary_download(temporary, database, "download.sqlite")
                archive = folder / "ais-scripts.zip"
                with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
                    bundle.write(database, "download.sqlite")
                    bundle.writestr("config.ini", config)
                    if feature:
                        bundle.writestr("area.geojson", json.dumps(feature, indent=2))
                    bundle.writestr("README.txt", "Recorded AIS Watch reports only; gaps and follow cutoffs are retained.\nExtract these files together. From the AIS-Watch repository run:\npython scripts/tracks/plot_tracks.py --config /path/to/config.ini --save-download /path/to/download.sqlite --offline\nFor area/incident scripts use the same flags and bundled area.geojson. Incident analysis requires both vessel roles in the exported selection.\n")
                return temporary_download(temporary, archive, "ais-scripts.zip")
            except Exception:
                temporary.cleanup()
                raise
        if format_name == "html":
            return Response(map_html(), mimetype="text/html", headers={"Content-Disposition": 'attachment; filename="ais-replay.html"'})
        if format_name == "csv":
            @stream_with_context
            def stream_csv():
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["mmsi", "name", "category", "color", "timestamp_utc", "timestamp_oslo", "longitude", "latitude", "original_report_json"])
                yield output.getvalue()
                for row in reports(start, end, identifiers):
                    output.seek(0)
                    output.truncate()
                    writer.writerow([row["mmsi"], row["display_name"], row["category"], row["color"], row["timestamp"], parse(row["timestamp"]).astimezone(OSLO).isoformat(), row["longitude"], row["latitude"], row["payload"]])
                    yield output.getvalue()
            return Response(stream_csv(), mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="ais-positions.csv"'})
        if format_name == "geojson":
            rows = reports(start, end, identifiers)
            stream = geojson_stream(rows, store.settings()["gap_minutes"])
            return Response(stream_with_context(stream), mimetype="application/geo+json",
                            headers={"Content-Disposition": 'attachment; filename="ais-tracks.geojson"'})
        raise ValueError("Choose sqlite, scripts, csv, geojson or html export.")

    def temporary_download(temporary, path, name):
        """Stream a completed export and remove its files when the response closes."""
        response = send_file(path, as_attachment=True, download_name=name)
        response.direct_passthrough = False
        response.call_on_close(temporary.cleanup)
        return response

    @app.get("/api/backup")
    def backup():
        temporary = tempfile.TemporaryDirectory(prefix="ais-backup-")
        destination = Path(temporary.name) / "ais-watch.sqlite3"
        try:
            with store.connect() as source, sqlite3.connect(destination) as target:
                source.backup(target)
            return temporary_download(temporary, destination, "ais-watch.sqlite3")
        except Exception:
            temporary.cleanup()
            raise

    return app
