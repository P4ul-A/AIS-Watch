// Same-origin bridge keeps standalone maps independent of the control website.
function tellParent(message) {
    window.parent.postMessage(message, window.location.origin);
}
const dayPicker = document.getElementById('replay-day');
const calendar = document.getElementById('replay-calendar');
const selectedDay = new URLSearchParams(window.location.search).get('day') || new Intl.DateTimeFormat('sv-SE', {timeZone: 'Europe/Oslo'}).format(new Date());
dayPicker.textContent = `Replay day · ${selectedDay}`;
let calendarMonth = new Date(`${selectedDay.slice(0, 7)}-01T12:00:00Z`);
let calendarRequest = 0;

/** Mark actual archive days; fetching only the visible month keeps queries small. */
async function renderCalendar() {
    const request = ++calendarRequest;
    const month = calendarMonth.toISOString().slice(0, 7);
    const year = calendarMonth.getUTCFullYear();
    const monthIndex = calendarMonth.getUTCMonth();
    document.getElementById('calendar-month').textContent = new Intl.DateTimeFormat('en-GB', {month: 'long', year: 'numeric', timeZone: 'UTC'}).format(calendarMonth);
    const grid = document.getElementById('calendar-days');
    const status = document.getElementById('calendar-status');
    grid.replaceChildren();
    status.textContent = 'Checking recorded days…';
    const offset = (calendarMonth.getUTCDay() + 6) % 7;
    for (let index = 0; index < offset; index++) grid.append(document.createElement('span'));
    const count = new Date(Date.UTC(year, monthIndex + 1, 0)).getUTCDate();
    for (let day = 1; day <= count; day++) {
        const date = `${month}-${String(day).padStart(2, '0')}`;
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = day;
        button.dataset.day = date;
        button.setAttribute('aria-label', date);
        button.setAttribute('aria-pressed', String(date === selectedDay));
        button.onclick = () => {
            calendar.open = false;
            if (date !== selectedDay) tellParent({type: 'replay-day', day: date});
        };
        grid.append(button);
    }
    try {
        const response = await fetch(`/api/recorded-days?month=${month}`);
        if (!response.ok) throw new Error('Could not check recorded days.');
        const result = await response.json();
        if (request !== calendarRequest) return;
        const days = new Set(result.days);
        grid.querySelectorAll('button').forEach(button => {
            const hasData = days.has(button.dataset.day);
            button.classList.toggle('has-data', hasData);
            const label = `${button.dataset.day}: ${hasData ? 'recorded positions' : 'no recorded positions'}`;
            button.setAttribute('aria-label', label);
            button.title = label;
        });
        status.textContent = days.size ? '● Days with recorded positions' : 'No recorded positions this month';
    } catch (error) {
        if (request === calendarRequest) status.textContent = 'Recorded days unavailable. You can still choose a date.';
    }
}
calendar.addEventListener('toggle', () => { if (calendar.open) renderCalendar(); });
for (const [id, direction] of [['calendar-previous', -1], ['calendar-next', 1]]) {
    document.getElementById(id).onclick = () => {
        calendarMonth.setUTCMonth(calendarMonth.getUTCMonth() + direction);
        renderCalendar();
    };
}
document.addEventListener('click', event => {
    if (calendar.open && !calendar.contains(event.target)) calendar.open = false;
});
calendar.addEventListener('keydown', event => {
    if (event.key === 'Escape') { calendar.open = false; dayPicker.focus(); }
});
colorPicker.addEventListener('change', () => {
    if (selectedVessel) tellParent({type: 'boat-color', mmsi: selectedVessel.mmsi, color: colorPicker.value});
});
const resetCategoryColor = document.createElement('button');
resetCategoryColor.textContent = 'Use category shade';
resetCategoryColor.addEventListener('click', () => {
    if (selectedVessel) tellParent({type: 'boat-color', mmsi: selectedVessel.mmsi, color: null});
});
selection.append(resetCategoryColor);
let drawing = false, corner = null, preview = null;
// The recording panel owns visibility of both saved and draft area geometry.
let areaVisible = false;
function updateAreaVisibility() {
    for (const layer of [areaOutline, preview]) {
        if (!layer) continue;
        if (areaVisible) layer.addTo(map);
        else map.removeLayer(layer);
    }
}
map.on('click', event => {
    if (!drawing) return;
    if (!corner) {
        corner = event.latlng;
        return;
    }
    drawing = false;
    map.getContainer().style.cursor = '';
    const rectangle = L.rectangle(L.latLngBounds(corner, event.latlng));
    const geometry = rectangle.toGeoJSON();
    corner = null;
    if (preview) map.removeLayer(preview);
    preview = rectangle.addTo(map);
    tellParent({type: 'area', geometry});
});
map.on('mousemove', event => {
    if (!drawing || !corner) return;
    if (preview) map.removeLayer(preview);
    preview = L.rectangle(L.latLngBounds(corner, event.latlng), {interactive: false}).addTo(map);
});
window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || event.source !== window.parent) return;
    const message = event.data;
    if (message.type === 'resize') map.invalidateSize({pan: false});
    if (message.type === 'area-visibility') {
        areaVisible = Boolean(message.visible);
        if (!areaVisible) {
            drawing = false;
            corner = null;
            map.getContainer().style.cursor = '';
        }
        updateAreaVisibility();
        map.invalidateSize({pan: false});
    }
    if (message.type === 'draw') {
        drawing = true;
        corner = null;
        map.getContainer().style.cursor = 'crosshair';
    }
    if (message.type === 'bounds') {
        tellParent({type: 'area', geometry: L.rectangle(map.getBounds()).toGeoJSON()});
    }
    if (message.type === 'show-area') {
        if (preview) map.removeLayer(preview);
        preview = L.geoJSON(message.geometry, {color: '#f59e0b', fillOpacity: 0.05}).addTo(map);
        updateAreaVisibility();
        map.fitBounds(preview.getBounds());
    }
    if (message.type === 'focus-incident') {
        pauseReplay();
        const incident = message.incident;
        const pair = [incident.small_mmsi, incident.big_mmsi];
        const bounds = L.latLngBounds([]);
        vessels.forEach(vessel => {
            if (pair.includes(vessel.mmsi)) {
                if (!map.hasLayer(vessel.layer)) vessel.layer.addTo(map);
                vessel.segments.forEach(segment => segment.points.forEach(point => {
                    if (Math.abs(Date.parse(point[2]) - Date.parse(incident.joining_utc)) <= 40 * 60000) bounds.extend(point.slice(0, 2));
                }));
            } else if (map.hasLayer(vessel.layer)) map.removeLayer(vessel.layer);
        });
        currentTime = Math.max(replayStart, Date.parse(incident.joining_utc) - 20 * 60000);
        renderReplay();
        if (bounds.isValid()) map.fitBounds(bounds, {padding: [24, 24], maxZoom: 15});
    }
});
tellParent({type: 'replay-ready'});
