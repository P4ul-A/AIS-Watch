/* Compact controls around the shared Leaflet replay. No credentials enter URLs. */
const $ = selector => document.querySelector(selector);
let state, pendingArea = null, pendingIncident = null;
let firstLoad = true;
let replayDay = '';
const oslo = new Intl.DateTimeFormat('sv-SE', {timeZone: 'Europe/Oslo', dateStyle: 'short', timeStyle: 'short'});
const timeText = value => value ? oslo.format(new Date(value)) : '—';
function notice(message) { $('#notice').textContent = message; $('#notice').hidden = false; }
$('#notice').onclick = () => { $('#notice').hidden = true; };
async function api(path, data, method = 'POST') {
    const response = await fetch(path, data === undefined ? {} : {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed.');
    return result;
}
function safely(action) { return async event => { try { await action(event); } catch (error) { notice(error.message); } }; }
function el(tag, text, attributes = {}) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    Object.assign(node, attributes);
    return node;
}
function categories(select, chosen) {
    select.replaceChildren(...state.categories.map(category => el('option', category.name, {value: category.id, selected: category.id === Number(chosen)})));
}
function showView(name) {
    document.querySelectorAll('.view').forEach(view => { view.hidden = view.id !== `view-${name}`; });
    document.querySelectorAll('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === name));
    if (name === 'incidents') loadIncidents().catch(error => notice(error.message));
    if (state && name === 'boats') renderBoats();
    if (state && name === 'settings') renderSettings();
    if (name === 'map') setTimeout(() => sendReplay({type: 'resize'}), 0);
}
document.querySelectorAll('[data-view]').forEach(button => { button.onclick = () => showView(button.dataset.view); });
function sendReplay(message) { $('#replay').contentWindow.postMessage(message, location.origin); }
function loadReplay(context = false) {
    const parameters = new URLSearchParams({day: replayDay});
    if (context) parameters.set('context', '1');
    if (pendingIncident) parameters.set('area_id', pendingIncident.area_id);
    $('#replay').src = `/replay?${parameters}`;
}
async function refresh(render = false) {
    state = await api('/api/state');
    const job = state.jobs.find(item => item.state === 'running' || item.state === 'queued');
    const active = state.areas.find(area => area.active_from && !area.active_until);
    const completed = state.jobs.find(item => item.kind === 'collect' && item.state === 'done');
    const failed = state.jobs.find(item => item.state === 'failed');
    $('#status').textContent = `${state.settings.enabled ? 'Recording enabled' : 'Automatic collection paused'} · ${active ? active.name : 'No active area'} · ` +
        (job ? `${job.state}: ${job.detail || job.kind}` : `Last successful collection: ${timeText(completed?.finished)}`) +
        (failed && (!completed || failed.id > completed.id) ? ` · ${failed.error}` : '') +
        (state.gaps.length ? ` · ${state.gaps.length} recovery gap(s); see Settings` : '');
    $('#status').classList.toggle('busy', Boolean(job));
    $('#toggle-collection').textContent = state.settings.enabled ? 'Pause recording' : 'Start recording';
    $('#collect').disabled = Boolean(state.jobs.find(item => item.kind === 'collect' && ['queued', 'running'].includes(item.state)));
    if (firstLoad) {
        replayDay = state.today;
        for (const id of ['export-start', 'export-end', 'incident-day']) $(`#${id}`).value = state.today;
        firstLoad = false;
        $('#controls').open = !window.matchMedia('(max-width: 760px)').matches;
        render = true;
        loadReplay();
    }
    if (render) {
        renderAreas(); renderBoats(); renderSettings();
        const exportSelect = $('#export-boats');
        const previous = new Set(Array.from(exportSelect.selectedOptions, option => option.value));
        exportSelect.replaceChildren(...state.boats.map(boat => el('option', boat.display_name, {value: boat.mmsi, selected: previous.has(String(boat.mmsi))})));
    }
    renderHistory();
}
function renderAreas() {
    const selected = $('#areas').value;
    $('#delete-area').disabled = state.areas.length === 0;
    $('#areas').replaceChildren(...state.areas.map(area => el('option', `${area.name}${area.active_from && !area.active_until ? ' · recording' : area.active_from ? ' · previous revision' : ''}`, {value: area.id})));
    if (state.areas.some(area => String(area.id) === selected)) $('#areas').value = selected;
    else { const active = state.areas.find(area => area.active_from && !area.active_until); if (active) $('#areas').value = active.id; }
}
async function updateBoat(mmsis, changes, reloadMap = true) {
    await api('/api/boats', {mmsis, ...changes});
    await refresh(true);
    if (reloadMap) loadReplay();
}
/** Render compact category columns; assignments persist through the boat API. */
function renderBoats() {
    const search = $('#boat-search').value.toLowerCase();
    // Keep categorised boats first so the default discovery bucket stays secondary.
    const ordered = [...state.categories].sort((a, b) => (a.id === 1) - (b.id === 1));
    $('#boat-board').replaceChildren(...ordered.map(category => {
        const column = el('section', null, {className: 'category-column'});
        column.style.setProperty('--category-color', category.color);
        const boats = state.boats.filter(boat => boat.category_id === category.id);
        const visible = boats.filter(boat => `${boat.display_name} ${boat.mmsi}`.toLowerCase().includes(search));
        const heading = el('div', null, {className: 'category-heading'});
        heading.append(el('strong', category.name), el('small', search ? `${visible.length}/${boats.length}` : boats.length));
        const settings = el('details', null, {className: 'category-settings'});
        settings.append(el('summary', 'Edit category'), categoryForm(category));
        column.append(heading, settings);
        column.ondragover = event => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; column.classList.add('drop-target'); };
        column.ondragleave = event => { if (!column.contains(event.relatedTarget)) column.classList.remove('drop-target'); };
        column.ondrop = safely(async event => {
            event.preventDefault();
            column.classList.remove('drop-target');
            const mmsi = event.dataTransfer.getData('text/plain');
            const boat = state.boats.find(item => String(item.mmsi) === mmsi);
            if (boat && boat.category_id !== category.id) await updateBoat([boat.mmsi], {category_id: category.id});
        });
        const cards = el('div', null, {className: 'boat-cards'});
        for (const boat of visible) cards.append(boatCard(boat));
        if (!visible.length) cards.append(el('small', search ? 'No matches' : 'Drop boats here', {className: 'empty-column'}));
        column.append(cards);
        return column;
    }));
}

/** Keep less frequent boat edits collapsed so the board remains an overview. */
function boatCard(boat) {
    const card = el('article', null, {className: 'boat-card', draggable: true});
    card.style.setProperty('--boat-color', boat.color);
    card.ondragstart = event => {
        if (event.target.closest('input, select, button, summary')) { event.preventDefault(); return; }
        event.dataTransfer.setData('text/plain', String(boat.mmsi));
        event.dataTransfer.effectAllowed = 'move';
    };
    card.ondragend = () => document.querySelectorAll('.drop-target').forEach(node => node.classList.remove('drop-target'));
    const title = el('div', null, {className: 'boat-card-title'});
    const color = el('input', null, {type: 'color', value: boat.color, draggable: false});
    color.setAttribute('aria-label', `Colour for ${boat.display_name}`);
    color.onchange = safely(() => updateBoat([boat.mmsi], {color_override: color.value}));
    title.append(el('strong', boat.display_name), color);
    card.append(title, el('small', boat.mmsi, {className: 'boat-mmsi'}));
    const details = el('details');
    details.append(el('summary', 'Move / edit'));
    const category = el('select'); categories(category, boat.category_id);
    category.setAttribute('aria-label', `Move ${boat.display_name} to category`);
    category.onchange = safely(() => updateBoat([boat.mmsi], {category_id: Number(category.value)}));
    const name = el('input', null, {value: boat.name, placeholder: boat.display_name});
    name.setAttribute('aria-label', `Name for ${boat.mmsi}`);
    name.onchange = safely(() => updateBoat([boat.mmsi], {name: name.value}));
    const reset = el('button', 'Use category shade');
    reset.onclick = safely(() => updateBoat([boat.mmsi], {color_override: null}));
    const role = el('select');
    role.setAttribute('aria-label', `Incident role for ${boat.display_name}`);
    for (const [value, text] of [['', 'No incident role'], ['small', 'Small'], ['big', 'Big']]) role.append(el('option', text, {value, selected: value === boat.role}));
    role.onchange = safely(() => updateBoat([boat.mmsi], {role: role.value}, false));
    details.append(category, name, reset, role, el('small', `Follow until: ${timeText(state.deadlines[boat.mmsi])}`));
    card.append(details);
    return card;
}

/** Reuse category editing directly within its board column. */
function categoryForm(category) {
    const form = el('form', null, {className: 'category-row'});
    const name = el('input', null, {name: 'name', value: category.name, required: true});
    name.setAttribute('aria-label', 'Category name');
    const color = el('input', null, {type: 'color', name: 'color', value: category.color});
    color.setAttribute('aria-label', `Base colour for ${category.name}`);
    const order = el('input', null, {type: 'number', name: 'sort_order', value: category.sort_order});
    order.setAttribute('aria-label', `Order for ${category.name}`);
    const save = el('button', 'Save', {type: 'submit'});
    const reset = el('button', 'Reset boat overrides', {type: 'button'});
    const remove = el('button', 'Delete', {type: 'button', disabled: category.id === 1});
    const update = async resetColors => {
        await api('/api/categories', {id: category.id, name: name.value, color: color.value, sort_order: Number(order.value), reset: resetColors});
        await refresh(true); loadReplay();
    };
    form.onsubmit = safely(event => { event.preventDefault(); return update(false); });
    reset.onclick = safely(() => { if (confirm(`Reset all individual colours in ${category.name}?`)) return update(true); });
    remove.onclick = safely(async () => {
        if (!confirm(`Delete ${category.name} and move its boats to the default category?`)) return;
        await api(`/api/categories/${category.id}`, {}, 'DELETE'); await refresh(true); loadReplay();
    });
    form.append(name, color, el('span', 'Order'), order, save, reset, remove);
    return form;
}
function renderSettings() {
    const form = $('#settings-form');
    for (const [key, value] of Object.entries(state.settings)) {
        const input = form.elements.namedItem(key);
        if (!input) continue;
        if (input.type === 'checkbox') input.checked = value;
        else input.value = value;
    }
    form.elements.client_id.value = state.credentials.client_id;
    form.elements.client_secret.value = '';
    $('#credential-status').textContent = state.credentials.secret_configured ? 'Saved secret configured. Leave blank to keep it.' : 'No secret configured yet.';
}
function renderHistory() {
    $('#archive-info').textContent = `Saved reports: ${timeText(state.first)} → ${timeText(state.last)}. Original reports are retained indefinitely.`;
    $('#job-history').replaceChildren(...state.jobs.map(job => el('div', `${timeText(job.created)} · ${job.kind} · ${job.state} · ${job.error || job.detail}`, {className: 'history-entry'})));
    $('#gap-history').replaceChildren(...(state.gaps.length ? state.gaps.map(gap => el('div', `${timeText(gap.start)} → ${timeText(gap.end)} · ${gap.reason}`, {className: 'history-entry'})) : [el('p', 'No unrecoverable collection gaps recorded.')]));
}
async function loadIncidents() {
    if (!$('#incident-day').value) return;
    const result = await api(`/api/incidents?day=${$('#incident-day').value}`);
    $('#analysis-status').replaceChildren(...(result.runs.length ? result.runs.map(run => {
        const missing = JSON.parse(run.missing);
        const block = el('div');
        const settings = JSON.parse(run.settings);
        block.append(el('p', `${run.area_name}: analysed ${timeText(run.updated)}. ${missing.length ? `No usable samples for: ${missing.join(', ')}. Other AIS gaps may also interrupt detection.` : 'Configured boats have samples; gaps within their tracks may still interrupt detection.'}`));
        const details = el('details');
        details.append(el('summary', 'Rules used for this result'), el('p', `Radius ${settings.radius_m} m · small boat ≤ ${settings.max_small_speed_knots} knots · prior ${settings.prior_minutes} min · together ${settings.together_minutes} min · AIS gap ≤ ${settings.max_gap_minutes} min`));
        block.append(details);
        return block;
    }) : [el('p', 'This day has not been analysed. Assign incident roles in Boats and choose Analyse day.')]));
    $('#incident-list').replaceChildren(...(result.incidents.length ? result.incidents.map(incident => {
        const item = el('article', null, {className: 'incident'});
        const heading = el('h2');
        heading.append(el('span', incident.big_name, {style: `color:${incident.big_color}`}), document.createTextNode(' → '), el('span', incident.small_name, {style: `color:${incident.small_color}`}));
        const details = el('details'); details.append(el('summary', 'Why flagged'), el('p', incident.reason));
        const button = el('button', 'Review on map');
        button.onclick = () => { pendingIncident = incident; replayDay = $('#incident-day').value; showView('map'); loadReplay(true); };
        item.append(heading, el('p', `${timeText(incident.joining_utc)} · ${incident.duration_minutes} min · closest ${incident.closest_distance_m} m · ${incident.area_name}`), details, button);
        return item;
    }) : [el('p', 'No matching flags in the available data for this day.')]));
}
// Synchronise on toggles and iframe readiness, including replay reloads.
$('#controls').ontoggle = () => sendReplay({type: 'area-visibility', visible: $('#controls').open});
$('#draw-area').onclick = () => { sendReplay({type: 'draw'}); $('#area-hint').textContent = 'Click two opposite corners on the map.'; };
$('#viewport-area').onclick = () => sendReplay({type: 'bounds'});
$('#show-area').onclick = () => { const area = state.areas.find(area => area.id === Number($('#areas').value)); if (area) sendReplay({type: 'show-area', geometry: area.geometry}); };
$('#import-area').onchange = safely(async event => {
    const file = event.target.files[0]; if (!file) return;
    pendingArea = JSON.parse(await file.text());
    $('#save-area').disabled = false; $('#area-hint').textContent = `Imported ${file.name}; give it a name and save.`;
    sendReplay({type: 'show-area', geometry: pendingArea});
});
$('#save-area').onclick = safely(async () => {
    const saved = await api('/api/areas', {name: $('#area-name').value, geometry: pendingArea});
    await refresh(true); $('#areas').value = saved.id; $('#area-hint').textContent = 'Saved. Activate watch to use this area.';
    $('#save-area').disabled = true; pendingArea = null;
});
$('#activate-area').onclick = safely(async () => { if (!$('#areas').value) throw new Error('Save an area first.'); await api(`/api/areas/${$('#areas').value}/activate`, {}); await refresh(true); loadReplay(); });
$('#delete-area').onclick = safely(async () => {
    const area = state.areas.find(area => area.id === Number($('#areas').value));
    if (!area || !confirm(`Delete ${area.name}? Its watch will stop; recorded tracks and incident history will be kept.`)) return;
    await api(`/api/areas/${area.id}`, {}, 'DELETE');
    await refresh(true);
    pendingArea = null;
    $('#save-area').disabled = true;
    $('#area-hint').textContent = 'Area deleted. Recorded data is preserved.';
    loadReplay();
});
$('#toggle-collection').onclick = safely(async () => {
    if (state.settings.enabled) {
        await api('/api/settings', {enabled: false});
        await refresh();
        return;
    }
    if (!state.areas.some(area => area.active_from && !area.active_until)) throw new Error('Draw, save and activate an area first.');
    const dialog = $('#recording-start');
    dialog.returnValue = 'cancel';
    dialog.showModal();
});
$('#recording-start').addEventListener('close', safely(async () => {
    const choice = $('#recording-start').returnValue;
    if (!['current', 'history'].includes(choice)) return;
    await api('/api/settings', {enabled: true, include_history: choice === 'history'});
    await refresh(true);
}));
$('#collect').onclick = safely(async () => { await api('/api/jobs', {kind: 'collect'}); await refresh(); });
$('#settings-form').onsubmit = safely(async event => {
    event.preventDefault(); const values = {};
    for (const input of event.target.elements) {
        if (!input.name) continue;
        values[input.name] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value;
    }
    await api('/api/settings', values); await refresh(true); notice('Settings saved.'); loadReplay();
});
$('#boat-search').oninput = renderBoats;
$('#add-boat').onclick = safely(async () => { await updateBoat([$('#new-mmsi').value.trim()], {name: $('#new-name').value}); $('#new-mmsi').value = ''; $('#new-name').value = ''; });
$('#category-add').onsubmit = safely(async event => { event.preventDefault(); await api('/api/categories', {name: event.target.elements.name.value, color: event.target.elements.color.value, sort_order: state.categories.length}); await refresh(true); event.target.reset(); });
$('#load-incidents').onclick = safely(loadIncidents);
$('#analyse-day').onclick = safely(async () => { await api('/api/jobs', {kind: 'analyse', day: $('#incident-day').value}); notice('Day analysis queued. Reload results when the job completes.'); await refresh(); });
$('#analyse-all').onclick = safely(async () => { await api('/api/jobs', {kind: 'analyse'}); notice('Archive analysis queued. Progress appears above.'); await refresh(); });
$('#incident-csv').onclick = () => { location.href = `/api/incidents?day=${$('#incident-day').value}&format=csv`; };
function downloadExport(format) {
    const parameters = new URLSearchParams({start: $('#export-start').value, end: $('#export-end').value, format});
    const mmsis = Array.from($('#export-boats').selectedOptions, option => option.value).join(','); if (mmsis) parameters.set('mmsis', mmsis);
    location.href = `/api/export?${parameters}`;
}
$('#export').onclick = () => downloadExport($('#export-format').value);
$('#export-scripts').onclick = () => downloadExport('scripts');
window.addEventListener('message', safely(async event => {
    if (event.origin !== location.origin || event.source !== $('#replay').contentWindow) return;
    if (event.data.type === 'replay-day') { replayDay = event.data.day; pendingIncident = null; loadReplay(); }
    if (event.data.type === 'boat-color') await updateBoat([event.data.mmsi], {color_override: event.data.color}, event.data.color === null);
    if (event.data.type === 'area') { pendingArea = event.data.geometry; $('#save-area').disabled = false; $('#area-hint').textContent = 'Rectangle ready. Give it a name and save.'; }
    if (event.data.type === 'replay-ready') sendReplay({type: 'area-visibility', visible: $('#controls').open});
    if (event.data.type === 'replay-ready' && pendingIncident) { sendReplay({type: 'focus-incident', incident: pendingIncident}); pendingIncident = null; }
}));
window.matchMedia('(max-width: 760px)').addEventListener('change', event => {
    $('#controls').open = !event.matches;
});
refresh().catch(error => notice(error.message));
setInterval(() => refresh().catch(error => notice(error.message)), 5000);
