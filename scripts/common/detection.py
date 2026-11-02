"""Gap-aware incident rules shared by the CLI and web application."""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from shapely.geometry import shape

@dataclass
class IncidentSettings:
    """Exploratory thresholds, with distances in metres and durations in minutes."""

    radius_m: float = 500
    max_small_speed_knots: float = 3
    prior_minutes: float = 5
    together_minutes: float = 5
    max_gap_minutes: float = 5


def distance_m(lat1, lon1, lat2, lon2):
    """Return great-circle distances in metres, accepting scalars or arrays."""
    lat1, lat2 = np.radians(lat1), np.radians(lat2)
    delta_lat = lat2 - lat1
    delta_lon = np.radians(lon2 - lon1)
    value = (
        np.sin(delta_lat / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin(delta_lon / 2) ** 2
    )
    return 6_371_000 * 2 * np.arcsin(np.sqrt(np.clip(value, 0, 1)))


def sample_track(
    ship: pd.DataFrame, timeline: pd.DatetimeIndex, settings: IncidentSettings
) -> pd.DataFrame:
    """Interpolate within short report intervals; never extrapolate or bridge gaps.

    Speed is the displacement between the bracketing original reports divided
    by elapsed time. At an exact report, use its incoming interval. Conflicting
    fixes at one timestamp invalidate adjacent intervals rather than choosing
    one arbitrarily. Invalid samples contain NaN, breaking detection windows.
    """
    result = pd.DataFrame(
        np.nan, index=timeline, columns=["latitude", "longitude", "speed"]
    )
    if len(ship) <= 1:
        return result
    ship = ship.sort_values("timestamp")
    conflicts = ship["timestamp"].duplicated(keep=False).to_numpy()
    times = ship["timestamp"].array.as_unit("ns").asi8 / 1e9
    target = timeline.as_unit("ns").asi8 / 1e9
    right = np.searchsorted(times, target, side="left")
    right = np.clip(right, 1, len(times) - 1)
    left = right - 1
    seconds = times[right] - times[left]
    valid = (target >= times[left]) & (target <= times[right]) & (seconds > 0)
    valid &= (
        (seconds <= settings.max_gap_minutes * 60)
        & ~conflicts[left]
        & ~conflicts[right]
    )
    latitude = ship["latitude"].to_numpy()
    longitude = ship["longitude"].to_numpy()
    fraction = np.divide(
        target - times[left], seconds, out=np.zeros_like(target), where=seconds > 0
    )
    # Interpolate the shorter longitude arc, including tracks near the dateline.
    delta_lon = (longitude[right] - longitude[left] + 180) % 360 - 180
    speed = np.divide(
        distance_m(latitude[left], longitude[left], latitude[right], longitude[right]),
        seconds,
        out=np.full_like(seconds, np.nan),
        where=seconds > 0,
    ) / (1852 / 3600)
    result.loc[valid, "latitude"] = latitude[left][valid] + fraction[valid] * (
        latitude[right][valid] - latitude[left][valid]
    )
    result.loc[valid, "longitude"] = (
        longitude[left][valid] + fraction[valid] * delta_lon[valid] + 180
    ) % 360 - 180
    result.loc[valid, "speed"] = speed[valid]
    return result


FIELDS = [
    "id",
    "small_name",
    "small_mmsi",
    "big_name",
    "big_mmsi",
    "joining_utc",
    "end_utc",
    "departure_utc",
    "duration_minutes",
    "closest_distance_m",
    "prior_activity_minutes",
    "approach_distance_change_m",
    "end_reason",
    "reason",
]


def ending_details(
    timeline: pd.DatetimeIndex, distances: np.ndarray, outside: np.ndarray, end: int
) -> tuple[str, str]:
    """Return an observed departure time, if any, and why a visit ended."""
    following = end + 1
    if following >= len(timeline):
        return "", "Analysis interval ended"
    if not np.isfinite(distances[following]):
        return "", "AIS coverage interrupted"
    if outside[following]:
        return timeline[following].isoformat(), "Observed departure"
    return "", "Small boat exceeded speed threshold"


def explain_incident(incident: dict, settings: IncidentSettings) -> str:
    """Explain the observed sequence, thresholds, and stopping rule for a flag."""
    small, big = incident["small_name"], incident["big_name"]
    ending = {
        "Analysis interval ended": "the selected analysis interval ended",
        "AIS coverage interrupted": "AIS coverage was interrupted",
        "Observed departure": f"{big} was next observed outside the circle",
        "Small boat exceeded speed threshold": (
            f"{small} next exceeded {settings.max_small_speed_knots:g} knots"
        ),
    }[incident["end_reason"]]
    return (
        f"{big} entered the {settings.radius_m:g} m circle around {small}. "
        f"For the preceding {incident['prior_activity_minutes']:g} minutes, "
        f"{small} stayed at or below {settings.max_small_speed_knots:g} knots and "
        f"within {settings.radius_m:g} m of its starting position, while {big} "
        f"remained outside the moving circle. Their separation decreased by "
        f"{incident['approach_distance_change_m']:g} m over that window. "
        f"They then met the proximity and small-boat speed rules for "
        f"{incident['duration_minutes']:g} minutes "
        f"(required: {settings.together_minutes:g} minutes). "
        f"Closest sampled distance: {incident['closest_distance_m']:g} m. "
        f"The qualifying window stopped because {ending}."
    )


def find_incidents(
    samples: dict[int, pd.DataFrame],
    small: list[int],
    big: list[int],
    settings: IncidentSettings,
    area: dict,
    names: dict[int, str],
) -> list[dict]:
    """Find continuous pair visits with fully observed prior and staying windows.

    Arrival is the first inside sample after a complete outside window. Duration
    ends at the last inside, slow sample. A departure is reported only when the
    next sample actually places the big boat outside. All times have one-minute
    analysis resolution; this does not establish a whale sighting or intent.
    """
    incidents = []
    prior_steps = math.ceil(settings.prior_minutes)
    west, south, east, north = shape(area["geometry"]).bounds
    for small_mmsi in set(small).intersection(samples):
        target = samples[small_mmsi]
        slow = (target["speed"] <= settings.max_small_speed_knots).to_numpy()
        lat, lon = target["latitude"].to_numpy(), target["longitude"].to_numpy()
        # A full preceding window must remain slow and local to its first point.
        local = (
            pd.Series(slow)
            .rolling(prior_steps + 1)
            .sum()
            .eq(prior_steps + 1)
            .to_numpy(copy=True)
        )
        # Check every point against the window's first position, not just its end.
        anchor_lat = target["latitude"].shift(prior_steps).to_numpy()
        anchor_lon = target["longitude"].shift(prior_steps).to_numpy()
        for offset in range(prior_steps):
            local &= (
                distance_m(
                    anchor_lat,
                    anchor_lon,
                    target["latitude"].shift(offset).to_numpy(),
                    target["longitude"].shift(offset).to_numpy(),
                )
                <= settings.radius_m
            )
        for big_mmsi in set(big).intersection(samples):
            other = samples[big_mmsi]
            distances = distance_m(
                lat, lon, other["latitude"].to_numpy(), other["longitude"].to_numpy()
            )
            inside = distances <= settings.radius_m
            outside = distances > settings.radius_m
            prior_outside = (
                pd.Series(outside)
                .shift(1)
                .rolling(prior_steps)
                .sum()
                .eq(prior_steps)
                .to_numpy()
            )
            starts = np.flatnonzero(inside & ~np.r_[False, inside[:-1]])
            ends = np.flatnonzero(inside & ~np.r_[inside[1:], False])
            for start, visit_end in zip(starts, ends, strict=True):
                if not local[start] or not prior_outside[start]:
                    continue
                if not (west <= lon[start] <= east and south <= lat[start] <= north):
                    continue
                fast = np.flatnonzero(~slow[start : visit_end + 1])
                end = start + int(fast[0]) - 1 if len(fast) else int(visit_end)
                duration = (
                    target.index[end] - target.index[start]
                ).total_seconds() / 60
                if duration < settings.together_minutes:
                    continue
                departure, end_reason = ending_details(
                    target.index, distances, outside, end
                )
                incidents.append(
                    {
                        "small_name": names[small_mmsi],
                        "small_mmsi": small_mmsi,
                        "big_name": names[big_mmsi],
                        "big_mmsi": big_mmsi,
                        "joining_utc": target.index[start].isoformat(),
                        "end_utc": target.index[end].isoformat(),
                        "departure_utc": departure,
                        "duration_minutes": round(duration, 2),
                        "closest_distance_m": round(
                            float(np.min(distances[start : end + 1])), 1
                        ),
                        "prior_activity_minutes": prior_steps,
                        "approach_distance_change_m": round(
                            float(distances[start - prior_steps] - distances[start]), 1
                        ),
                        "end_reason": end_reason,
                    }
                )
    incidents.sort(
        key=lambda item: (item["joining_utc"], item["small_mmsi"], item["big_mmsi"])
    )
    for number, incident in enumerate(incidents, 1):
        incident["id"] = number
        incident["reason"] = explain_incident(incident, settings)
    return incidents

