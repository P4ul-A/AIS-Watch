// Record a dedicated Full HD canvas so output resolution is independent of screen size.
const exportButton = document.getElementById('export-recording');
const exportStatus = document.getElementById('export-status');
const exportNames = document.getElementById('export-names');
const exportTracks = document.getElementById('export-tracks');
let stopExport = null;

async function createRecordingScene(boats, includeNames, includeTracks) {
    const canvas = document.createElement('canvas');
    canvas.width = 1920;
    canvas.height = 1080;
    const context = canvas.getContext('2d');
    context.font = '20px system-ui';
    // Wrap the legend before sizing the map, keeping every boat name in frame.
    let x = 24;
    let row = 0;
    const legend = (includeNames ? boats : []).map(boat => {
        const width = context.measureText(boat.name).width + 60;
        if (x + width > 1896 && x > 24) { x = 24; row++; }
        const item = {boat, x, row};
        x += width;
        return item;
    });
    const mapHeight = 1080 - 80 - (includeNames ? (row + 1) * 30 + 50 : 30);
    if (mapHeight < 400) throw new Error('Too many boats for the recording legend. Untick some ships or turn off Names.');
    const container = document.createElement('div');
    container.style.cssText = `position:fixed;left:-10000px;top:0;width:1920px;height:${mapHeight}px;pointer-events:none`;
    container.setAttribute('aria-hidden', 'true');
    document.body.appendChild(container);
    let recordingMap;
    let timeout;
    try {
        recordingMap = L.map(container, {
            zoomControl: false, attributionControl: false,
            fadeAnimation: false, zoomAnimation: false
        }).setView(map.getCenter(), map.getZoom(), {animate: false});
        const useNorway = map.hasLayer(norway);
        const base = useNorway ? norway : worldwide;
        const tiles = L.tileLayer(useNorway ? tileUrls.norway : tileUrls.worldwide, {
            maxZoom: base.options.maxZoom, maxNativeZoom: base.options.maxNativeZoom,
            crossOrigin: 'anonymous', keepBuffer: 0
        });
        await new Promise((resolve, reject) => {
            timeout = setTimeout(() => reject(new Error('Map tiles timed out. Check your connection and retry.')), 30000);
            tiles.once('load', resolve);
            tiles.once('tileerror', () => reject(new Error('Recording tiles could not load. Try the worldwide background or retry.')));
            tiles.addTo(recordingMap);
        });
        const images = [...container.querySelectorAll('img.leaflet-tile')];
        await Promise.all(images.map(image => image.decode()));
        const background = document.createElement('canvas');
        background.width = 1920;
        background.height = mapHeight;
        const backgroundContext = background.getContext('2d');
        backgroundContext.fillStyle = '#dde7ec';
        backgroundContext.fillRect(0, 0, 1920, mapHeight);
        const rectangle = container.getBoundingClientRect();
        images.forEach(image => {
            const tile = image.getBoundingClientRect();
            backgroundContext.drawImage(image, tile.left - rectangle.left, tile.top - rectangle.top, tile.width, tile.height);
        });
        // Check CORS before starting the recorder, so an unusable video is never offered.
        backgroundContext.getImageData(0, 0, 1, 1);
        const projected = boats.map(boat => ({
            ...boat,
            segments: boat.segments.map(segment => ({
                times: segment.times,
                drawn: 0,
                points: segment.coordinates.map(point => recordingMap.latLngToContainerPoint(point))
            }))
        }));
        const trails = document.createElement('canvas');
        trails.width = 1920;
        trails.height = mapHeight;
        const trailContext = trails.getContext('2d');
        const attribution = document.createElement('span');
        attribution.innerHTML = base.getAttribution();

        function drawFrame(time) {
            context.globalAlpha = 1;
            context.fillStyle = '#f6f8fa';
            context.fillRect(0, 0, 1920, 1080);
            context.fillStyle = '#111827';
            context.font = 'bold 28px system-ui';
            context.fillText('Historical ship tracks — Norway', 24, 49);
            context.font = '24px system-ui';
            context.textAlign = 'right';
            context.fillText(formatTime(new Date(time).toISOString()), 1896, 49);
            context.textAlign = 'left';
            context.drawImage(background, 0, 80);
            context.save();
            context.beginPath();
            context.rect(0, 80, 1920, mapHeight);
            context.clip();
            context.translate(0, 80);
            // Repaint cached geometry so existing trails can fade as replay time advances.
            trailContext.clearRect(0, 0, trails.width, trails.height);
            projected.forEach(boat => {
                trailContext.strokeStyle = boat.color;
                trailContext.fillStyle = boat.color;
                trailContext.lineWidth = boat.selected ? 6 : 3;
                trailContext.lineJoin = 'round';
                trailContext.lineCap = 'round';
                let latest = null;
                boat.segments.forEach(segment => {
                    const count = reportCount(segment.times, time);
                    if (!count) return;
                    latest = segment.points[count - 1];
                    if (!includeTracks) return;
                    if (count !== segment.drawn) {
                        segment.path = new Path2D();
                        segment.path.moveTo(segment.points[0].x, segment.points[0].y);
                        for (let index = 1; index < count; index++) {
                            segment.path.lineTo(segment.points[index].x, segment.points[index].y);
                        }
                        segment.drawn = count;
                    }
                    trailContext.globalAlpha = (fadeTrails ? trackOpacity(segment.times[count - 1], time) : 0.85) *
                        (boat.dimmed ? 0.3 / 0.85 : 1);
                    trailContext.stroke(segment.path);
                    if (segment.points.length === 1) {
                        trailContext.beginPath();
                        trailContext.arc(latest.x, latest.y, 4, 0, Math.PI * 2);
                        trailContext.fill();
                    }
                });
                boat.latest = latest;
            });
            if (includeTracks) context.drawImage(trails, 0, 0);
            projected.forEach(boat => {
                if (boat.latest) {
                    context.globalAlpha = boat.dimmed ? 0.3 : 0.95;
                    context.fillStyle = boat.color;
                    context.beginPath();
                    context.arc(boat.latest.x, boat.latest.y, 7, 0, Math.PI * 2);
                    context.fill();
                    context.strokeStyle = '#ffffff';
                    context.lineWidth = 2;
                    context.stroke();
                }
            });
            context.restore();
            context.font = '20px system-ui';
            legend.forEach(item => {
                const y = 80 + mapHeight + 28 + item.row * 30;
                context.fillStyle = item.boat.color;
                context.fillRect(item.x, y - 15, 18, 18);
                context.fillStyle = '#111827';
                context.fillText(item.boat.name, item.x + 28, y);
            });
            context.font = '14px system-ui';
            context.fillStyle = '#374151';
            context.fillText(attribution.textContent, 24, 1064, 1872);
        }
        return {canvas, drawFrame};
    } finally {
        clearTimeout(timeout);
        if (recordingMap) recordingMap.remove();
        container.remove();
    }
}

async function exportRecording() {
    const controls = [playButton, speed, timeline, document.getElementById('show-all'), exportNames, exportTracks];
    let output;
    let cancelled = false;
    try {
        // Leaflet's layer control removes unticked vessel groups from the map.
        // Snapshot visibility and names before any asynchronous preparation.
        const visibleVessels = vessels.filter(vessel => map.hasLayer(vessel.layer));
        if (!visibleVessels.length) throw new Error('Tick at least one ship before exporting.');
        const highlighted = visibleVessels.includes(selectedVessel) ? selectedVessel : null;
        const boats = visibleVessels.map(vessel => ({
            ...vessel, selected: vessel === highlighted,
            dimmed: Boolean(highlighted) && vessel !== highlighted
        }));
        const includeNames = exportNames.checked;
        const includeTracks = exportTracks.checked;
        if (!window.VideoEncoder) {
            throw new Error('Fast video export needs WebCodecs. Open this map in a current Chrome, Edge, or another browser with video encoding support.');
        }
        pauseReplay();
        controls.forEach(control => { control.disabled = true; });
        exportButton.disabled = true;
        const from = replayStart;
        const multiplier = Number(speed.value);
        const duration = (replayEnd - from + 1) / multiplier / 1000;
        const frameRate = 30;
        const frameCount = Math.max(1, Math.ceil(duration * frameRate));
        const quality = new Mediabunny.Quality({bitrate: 8000000});
        const encoding = {width: 1920, height: 1080, quality, frameRate, latencyMode: 'realtime'};
        // Prefer widely compatible H.264 MP4; fall back to WebM encoders.
        let codec;
        for (const candidate of ['avc', 'vp8', 'vp9']) {
            if (await Mediabunny.canEncodeVideo(candidate, encoding)) {
                codec = candidate;
                break;
            }
        }
        if (!codec) throw new Error('This browser cannot encode Full HD video. Try a current Chrome or Edge browser.');
        const extension = codec === 'avc' ? 'mp4' : 'webm';
        const format = codec === 'avc'
            ? new Mediabunny.Mp4OutputFormat({fastStart: 'in-memory'})
            : new Mediabunny.WebMOutputFormat();
        exportStatus.textContent = `Preparing ${extension.toUpperCase()} · ${boats.length} ticked ships · full interval · ${duration.toFixed(1)}s video at selected speed…`;
        const scene = await createRecordingScene(boats, includeNames, includeTracks);
        output = new Mediabunny.Output({
            format,
            target: new Mediabunny.BufferTarget()
        });
        const video = new Mediabunny.CanvasSource(scene.canvas, {
            codec, quality, latencyMode: 'realtime'
        });
        output.addVideoTrack(video, {frameRate});
        await output.start();
        stopExport = () => { cancelled = true; };
        exportButton.disabled = false;
        exportButton.textContent = 'Cancel export';
        // Assign media timestamps directly: rendering never waits for playback time.
        for (let index = 0; index < frameCount; index++) {
            if (cancelled) {
                exportStatus.textContent = 'Export cancelled · no partial video downloaded';
                return;
            }
            const timestamp = index / frameRate;
            const time = index === frameCount - 1 ? replayEnd : from + timestamp * multiplier * 1000;
            scene.drawFrame(time);
            await video.add(timestamp, Math.min(1 / frameRate, duration - timestamp));
            if (index % 15 === 0) {
                exportStatus.textContent = `Rendering ${extension.toUpperCase()} Full HD · ${Math.round((index + 1) / frameCount * 100)}% · ${duration.toFixed(1)}s video`;
                // Yield briefly for progress updates and cancellation, without frame pacing.
                await new Promise(resolve => setTimeout(resolve, 0));
            }
        }
        if (cancelled) {
            exportStatus.textContent = 'Export cancelled · no partial video downloaded';
            return;
        }
        stopExport = null;
        exportButton.disabled = true;
        exportStatus.textContent = 'Finalizing complete recording…';
        video.close();
        await output.finalize();
        const blob = new Blob([output.target.buffer], {type: format.mimeType});
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `ship-tracks-${new Date(from).toISOString().slice(0, 10)}-1080p.${extension}`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        exportStatus.textContent = `${extension.toUpperCase()} downloaded · ${boats.length} ticked ships · full interval · ${duration.toFixed(1)}s · 1920 × 1080`;
    } catch (error) {
        exportStatus.textContent = `Export failed: ${error.message}`;
    } finally {
        if (output && output.state !== 'finalized') await output.cancel();
        stopExport = null;
        controls.forEach(control => { control.disabled = false; });
        exportButton.disabled = false;
        exportButton.textContent = 'Export recording · Full HD';
    }
}

exportButton.addEventListener('click', () => {
    if (stopExport) stopExport();
    else exportRecording();
});
