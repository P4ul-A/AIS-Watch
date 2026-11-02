// Review controls reuse the original-report map and replay; circles use analysis samples.
function renderIncidentCircle(fullTracks) {
    if (map.hasLayer(incidentCircle)) map.removeLayer(incidentCircle);
    if (!selectedIncident || fullTracks) return;
    const points = review.circles[selectedIncident.small_mmsi];
    const next = reportCount(points.map(point => point[2]), currentTime);
    const before = points[next - 1];
    const after = points[next];
    if (!before) return;
    let position = before.slice(0, 2);
    if (currentTime !== before[2]) {
        // Missing one-minute samples interrupt the circle as well as detection.
        if (!after || after[2] - before[2] > 60000) return;
        const fraction = (currentTime - before[2]) / (after[2] - before[2]);
        const deltaLon = ((after[1] - before[1] + 540) % 360) - 180;
        position = [before[0] + fraction * (after[0] - before[0]),
            ((before[1] + fraction * deltaLon + 540) % 360) - 180];
    }
    incidentCircle.setLatLng(position).addTo(map);
}

function reviewIncident(incident, button) {
    pauseReplay();
    selectedVessel = null;
    selection.hidden = true;
    selectedIncident = incident;
    const pair = vessels.filter(vessel => [incident.small_mmsi, incident.big_mmsi].includes(vessel.mmsi));
    const joining = Date.parse(incident.joining_utc);
    const end = Date.parse(incident.end_utc);
    const contextStart = joining - 20 * 60000;
    const contextEnd = end + 20 * 60000;
    const contextBounds = L.latLngBounds([]);
    pair.forEach(vessel => {
        if (!map.hasLayer(vessel.layer)) vessel.layer.addTo(map);
        const points = vessel.segments.flatMap(segment => segment.points);
        points.filter(point => Date.parse(point[2]) >= contextStart && Date.parse(point[2]) <= contextEnd)
            .forEach(point => contextBounds.extend(point.slice(0, 2)));
    });
    currentTime = Math.max(replayStart, contextStart,
        ...pair.map(vessel => vessel.segments[0].times[0]));
    currentTime = Math.min(currentTime, joining);
    document.querySelectorAll('.incident-button').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    document.getElementById('review-detail').textContent =
        `Reviewing ${incident.big_name} → ${incident.small_name} · ${formatTime(incident.joining_utc)}.`;
    document.querySelectorAll('.incident-explanation').forEach(details => {
        details.open = details === button.nextElementSibling;
    });
    map.invalidateSize({pan: false});
    if (contextBounds.isValid()) map.fitBounds(contextBounds, {padding: [30, 30], maxZoom: 15, animate: false});
    renderReplay();
    button.scrollIntoView({block: 'nearest'});
}

if (review) {
    const list = document.getElementById('incident-list');
    review.incidents.forEach(incident => {
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'incident-button';
        button.setAttribute('aria-pressed', 'false');
        button.setAttribute('aria-controls', `incident-explanation-${incident.id}`);
        const title = document.createElement('strong');
        title.textContent = `${incident.big_name} → ${incident.small_name}`;
        const time = document.createElement('span');
        time.textContent = `${formatTime(incident.joining_utc)} · ${incident.duration_minutes} min together`;
        const details = document.createElement('small');
        details.textContent = `Closest ${incident.closest_distance_m.toLocaleString()} m · ` +
            `Prior activity ${incident.prior_activity_minutes} min · MMSI ${incident.big_mmsi} / ${incident.small_mmsi}`;
        button.append(title, time, details);
        button.addEventListener('click', () => reviewIncident(incident, button));
        const explanation = document.createElement('details');
        explanation.className = 'incident-explanation';
        explanation.id = `incident-explanation-${incident.id}`;
        const summary = document.createElement('summary');
        summary.textContent = 'Why flagged';
        const reason = document.createElement('p');
        reason.textContent = incident.reason;
        const ending = document.createElement('p');
        ending.textContent = `Qualifying window: ${formatTime(incident.joining_utc)} → ${formatTime(incident.end_utc)}. ` +
            `Observed departure: ${incident.departure_utc ? formatTime(incident.departure_utc) : 'not established by this window'}.`;
        explanation.append(summary, reason, ending);
        item.append(button, explanation);
        list.appendChild(item);
    });
    document.getElementById('download-incidents').addEventListener('click', () => {
        // Download the Python-generated CSV verbatim, including number formatting.
        const url = URL.createObjectURL(new Blob([review.csv], {type: 'text/csv;charset=utf-8'}));
        const link = document.createElement('a');
        link.href = url;
        link.download = 'incidents.csv';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
}
