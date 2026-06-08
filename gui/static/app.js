'use strict';

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  wifi:     [],   // deduplicated by MAC
  aircraft: [],
  drone:    [],
  alerts:   [],
};

// ── Leaflet map ──────────────────────────────────────────────────────────────
const map = L.map('map', { zoomControl: true }).setView([51.5, -0.1], 10);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors',
  maxZoom: 19,
}).addTo(map);

const layers = {
  wifi:     L.layerGroup().addTo(map),
  aircraft: L.layerGroup().addTo(map),
  drone:    L.layerGroup().addTo(map),
};

function wifiColor(level) {
  return { high: '#f85149', likely: '#ff8c00', suspicious: '#d29922' }[level] || '#8b949e';
}

function addWifiMarker(ev) {
  if (ev.lat == null || ev.lon == null) return;
  L.circleMarker([ev.lat, ev.lon], {
    radius: 7, color: wifiColor(ev.alert_level), fillOpacity: 0.8,
  }).bindPopup(
    `<b>${ev.mac}</b><br>Score: ${(ev.score || 0).toFixed(2)}<br>Level: ${ev.alert_level}`
  ).addTo(layers.wifi);
}

function addAircraftMarker(ev) {
  if (ev.lat == null || ev.lon == null) return;
  L.circleMarker([ev.lat, ev.lon], {
    radius: 6, color: ev.emergency ? '#f85149' : '#58a6ff', fillOpacity: 0.85,
  }).bindPopup(
    `<b>${ev.callsign || ev.icao}</b><br>Alt: ${ev.altitude} ft<br>Reg: ${ev.registration || '—'}`
  ).addTo(layers.aircraft);
}

function addDroneMarker(ev) {
  if (ev.lat == null || ev.lon == null) return;
  L.circleMarker([ev.lat, ev.lon], {
    radius: 8, color: '#bc8cff', fillOpacity: 0.9,
  }).bindPopup(
    `<b>Drone RF</b><br>${ev.freq_mhz} MHz  ${ev.power_db} dBm`
  ).addTo(layers.drone);
}

// ── Tab switching ────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
    if (btn.dataset.tab === 'map') { setTimeout(() => map.invalidateSize(), 50); }
  });
});

// ── Badge helpers ────────────────────────────────────────────────────────────
function setBadge(id, n) {
  const el = document.getElementById(id);
  if (el) el.textContent = n;
}

// ── Table renderers ──────────────────────────────────────────────────────────
function fmtTime(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleTimeString(); } catch { return iso; }
}

function renderWifi() {
  const q = document.getElementById('wifi-search').value.toLowerCase();
  const rows = state.wifi
    .filter(e => !q || JSON.stringify(e).toLowerCase().includes(q))
    .slice(-200)
    .reverse();
  const alertClass = { high: 'alert-high', likely: 'alert-likely', suspicious: 'alert-suspicious' };
  document.getElementById('wifi-tbody').innerHTML = rows.map(e => `
    <tr>
      <td><code>${e.mac || '—'}</code></td>
      <td>${e.mac_type || '—'}</td>
      <td class="${alertClass[e.alert_level] || 'alert-new'}">${(e.score || 0).toFixed(2)}</td>
      <td class="${alertClass[e.alert_level] || 'alert-new'}">${e.alert_level || '—'}</td>
      <td>${e.observation_count || '—'}</td>
      <td>${e.manufacturer || '—'}</td>
      <td>${fmtTime(e.last_seen || e.timestamp)}</td>
    </tr>`).join('');
}

function renderAircraft() {
  const q = document.getElementById('aircraft-search').value.toLowerCase();
  const rows = state.aircraft
    .filter(e => !q || JSON.stringify(e).toLowerCase().includes(q))
    .slice(-200)
    .reverse();
  document.getElementById('aircraft-tbody').innerHTML = rows.map(e => {
    const pos = (e.lat != null && e.lon != null)
      ? `${(+e.lat).toFixed(3)}, ${(+e.lon).toFixed(3)}`
      : '<span class="no-pos">no position</span>';
    return `
    <tr>
      <td>${e.callsign || '—'}</td>
      <td><code>${e.icao || '—'}</code></td>
      <td>${e.registration || '—'}</td>
      <td>${e.altitude ?? '—'}</td>
      <td>${e.speed ?? '—'}</td>
      <td>${pos}</td>
      <td class="${e.emergency ? 'emergency-yes' : ''}">${e.emergency ? '🚨 YES' : 'No'}</td>
      <td>${fmtTime(e.timestamp)}</td>
    </tr>`;
  }).join('');
  // Rebuild the aircraft map layer from the (deduped) state so a moving plane
  // does not pile up duplicate markers; position-less aircraft are omitted from
  // the map but remain listed in the table above.
  layers.aircraft.clearLayers();
  state.aircraft.forEach(addAircraftMarker);
}

function renderDrone() {
  const q = document.getElementById('drone-search').value.toLowerCase();
  const rows = state.drone
    .filter(e => !q || JSON.stringify(e).toLowerCase().includes(q))
    .slice(-200)
    .reverse();
  document.getElementById('drone-tbody').innerHTML = rows.map(e => `
    <tr>
      <td>${e.freq_mhz ?? '—'}</td>
      <td>${e.power_db ?? '—'}</td>
      <td>${e.lat ?? '—'}</td>
      <td>${e.lon ?? '—'}</td>
      <td>${fmtTime(e.timestamp)}</td>
    </tr>`).join('');
}

function renderAlerts() {
  document.getElementById('alerts-feed').innerHTML = state.alerts
    .slice(-100)
    .reverse()
    .map(a => `
      <div class="alert-card ${a.kind || ''}">
        <div class="alert-title">${a.title || a.type || 'Alert'}</div>
        <div class="alert-body">${a.body || JSON.stringify(a)}</div>
      </div>`).join('');
}

// Search filters
['wifi', 'aircraft', 'drone'].forEach(tab => {
  const el = document.getElementById(`${tab}-search`);
  if (el) el.addEventListener('input', () => window[`render${tab[0].toUpperCase()}${tab.slice(1)}`]());
});

// Clear buttons
document.getElementById('wifi-clear').addEventListener('click', () => {
  state.wifi = []; layers.wifi.clearLayers(); renderWifi(); setBadge('badge-wifi', 0);
});
document.getElementById('aircraft-clear').addEventListener('click', () => {
  state.aircraft = []; layers.aircraft.clearLayers(); renderAircraft(); setBadge('badge-aircraft', 0);
});
document.getElementById('drone-clear').addEventListener('click', () => {
  state.drone = []; layers.drone.clearLayers(); renderDrone(); setBadge('badge-drone', 0);
});
document.getElementById('alerts-clear').addEventListener('click', () => {
  state.alerts = []; renderAlerts(); setBadge('badge-alerts', 0);
});

// ── Status polling ───────────────────────────────────────────────────────────
function applyHealth(health) {
  const map_ = { gps: 's-gps', kismet: 's-kismet', adsb: 's-adsb', 'drone_rf': 's-drone-rf' };
  Object.entries(map_).forEach(([key, id]) => {
    const el = document.getElementById(id);
    if (!el) return;
    const ok = health[key];
    el.classList.toggle('ok', !!ok);
    el.classList.toggle('err', !ok);
  });
}

async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) return;
    const d = await r.json();
    if (d.session_id) {
      document.getElementById('session-id').textContent = d.session_id;
    }
    if (d.sensor_health) applyHealth(d.sensor_health);
    renderBaseline(d.scoring);
  } catch { /* network error — ignore */ }
}

setInterval(pollStatus, 5000);
pollStatus();

// ── Node mode toggle ─────────────────────────────────────────────────────────
// The dashboard authenticates by carrying ?token=<GUI_TOKEN> in its URL; we
// reuse it for the control endpoint so POST /api/mode passes check_auth.
const MODE_TOKEN = new URLSearchParams(location.search).get('token') || '';
function modeUrl() {
  return MODE_TOKEN ? `/api/mode?token=${encodeURIComponent(MODE_TOKEN)}` : '/api/mode';
}

async function initModeControl() {
  const sel = document.getElementById('mode-select');
  const btn = document.getElementById('mode-save');
  const msg = document.getElementById('mode-msg');
  if (!sel || !btn || !msg) return;
  try {
    const r = await fetch(modeUrl());
    if (r.status === 401) {
      msg.textContent = 'locked — open with ?token=';
      return;
    }
    if (!r.ok) return;
    const d = await r.json();
    if (d.mode) sel.value = d.mode;
    if (!d.control_enabled) {
      // GUI_TOKEN not set on the node — control is unavailable, not silent.
      sel.disabled = true;
      btn.disabled = true;
      msg.textContent = 'set GUI_TOKEN to enable';
      return;
    }
    sel.disabled = false;
    btn.disabled = false;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      msg.classList.remove('mode-restart');
      msg.textContent = 'saving…';
      try {
        const resp = await fetch(modeUrl(), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: sel.value }),
        });
        const body = await resp.json().catch(() => ({}));
        if (resp.ok) {
          msg.classList.add('mode-restart');
          msg.textContent = body.message || 'Saved — restart required to take effect.';
        } else {
          msg.textContent = body.error || `error (${resp.status})`;
        }
      } catch {
        msg.textContent = 'network error';
      } finally {
        btn.disabled = false;
      }
    });
  } catch { /* ignore */ }
}

initModeControl();

// ── Seed from REST on load ───────────────────────────────────────────────────
async function seedFromRest() {
  const endpoints = [
    { url: '/api/wifi',     key: 'wifi',     render: renderWifi,     badge: 'badge-wifi',     marker: addWifiMarker },
    { url: '/api/aircraft', key: 'aircraft', render: renderAircraft, badge: 'badge-aircraft', marker: null },
    { url: '/api/drone',    key: 'drone',    render: renderDrone,    badge: 'badge-drone',    marker: addDroneMarker },
    { url: '/api/alerts',   key: 'alerts',   render: renderAlerts,   badge: 'badge-alerts',   marker: null },
  ];
  for (const ep of endpoints) {
    try {
      const r = await fetch(ep.url);
      if (!r.ok) continue;
      const items = await r.json();
      state[ep.key] = items;
      if (ep.marker) items.forEach(ep.marker);
      setBadge(ep.badge, items.length);
      ep.render();
    } catch { /* ignore */ }
  }
}

seedFromRest();

// ── SSE listener ─────────────────────────────────────────────────────────────
function connectSSE() {
  const es = new EventSource('/stream');

  es.onmessage = function(evt) {
    let data;
    try { data = JSON.parse(evt.data); } catch { return; }
    const type = data.type;

    if (type === 'heartbeat') return;

    if (type === 'wifi') {
      // Deduplicate by MAC
      const idx = state.wifi.findIndex(e => e.mac === data.mac);
      if (idx >= 0) state.wifi[idx] = data; else state.wifi.push(data);
      setBadge('badge-wifi', state.wifi.length);
      addWifiMarker(data);
      renderWifi();
    } else if (type === 'aircraft') {
      // Deduplicate by ICAO — a moving plane is re-pushed as its track advances.
      const idx = state.aircraft.findIndex(e => e.icao === data.icao);
      if (idx >= 0) state.aircraft[idx] = data; else state.aircraft.push(data);
      setBadge('badge-aircraft', state.aircraft.length);
      renderAircraft();   // rebuilds the table and the map markers from state
    } else if (type === 'drone') {
      state.drone.push(data);
      setBadge('badge-drone', state.drone.length);
      addDroneMarker(data);
      renderDrone();
    } else if (type === 'alert') {
      state.alerts.push(data);
      setBadge('badge-alerts', state.alerts.length);
      renderAlerts();
    }
  };

  es.onerror = function() {
    // Reconnect after 5 s
    es.close();
    setTimeout(connectSSE, 5000);
  };
}

connectSSE();

// ── Baseline-state header ─────────────────────────────────────────────────────
function fmtDuration(secs) {
  secs = Math.max(0, Math.floor(secs));
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function renderBaseline(scoring) {
  const bar = document.getElementById('baseline-bar');
  const label = document.getElementById('baseline-state');
  const detail = document.getElementById('baseline-detail');
  bar.classList.remove('baseline-learning', 'baseline-frozen', 'baseline-mobile', 'baseline-unknown');
  if (!scoring) {
    bar.classList.add('baseline-unknown');
    label.textContent = 'Baseline: —';
    detail.textContent = 'scoring not active';
    return;
  }
  if (scoring.mode === 'fixed') {
    if (scoring.learning) {
      bar.classList.add('baseline-learning');
      const remain = (new Date(scoring.freeze_time).getTime() - Date.now()) / 1000;
      label.textContent = 'Baseline: LEARNING';
      detail.textContent =
        `freezes in ${fmtDuration(remain)} · ${scoring.baseline_devices ?? 0} devices learned`;
    } else {
      bar.classList.add('baseline-frozen');
      label.textContent = 'Baseline: FROZEN';
      detail.textContent =
        `${scoring.baseline_devices ?? 0} baseline devices · flagging deviations`;
    }
  } else {
    bar.classList.add('baseline-mobile');
    label.textContent = 'Mode: MOBILE';
    detail.textContent = `${scoring.total_devices_tracked ?? 0} devices tracked`;
  }
}

