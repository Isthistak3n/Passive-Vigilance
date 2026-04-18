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
  if (!ev.lat || !ev.lon) return;
  L.circleMarker([ev.lat, ev.lon], {
    radius: 7, color: wifiColor(ev.alert_level), fillOpacity: 0.8,
  }).bindPopup(
    `<b>${ev.mac}</b><br>Score: ${(ev.score || 0).toFixed(2)}<br>Level: ${ev.alert_level}`
  ).addTo(layers.wifi);
}

function addAircraftMarker(ev) {
  if (!ev.lat || !ev.lon) return;
  L.circleMarker([ev.lat, ev.lon], {
    radius: 6, color: ev.emergency ? '#f85149' : '#58a6ff', fillOpacity: 0.85,
  }).bindPopup(
    `<b>${ev.callsign || ev.icao}</b><br>Alt: ${ev.altitude} ft<br>Reg: ${ev.registration || '—'}`
  ).addTo(layers.aircraft);
}

function addDroneMarker(ev) {
  if (!ev.lat || !ev.lon) return;
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
  document.getElementById('aircraft-tbody').innerHTML = rows.map(e => `
    <tr>
      <td>${e.callsign || '—'}</td>
      <td><code>${e.icao || '—'}</code></td>
      <td>${e.registration || '—'}</td>
      <td>${e.altitude ?? '—'}</td>
      <td>${e.speed ?? '—'}</td>
      <td class="${e.emergency ? 'emergency-yes' : ''}">${e.emergency ? '🚨 YES' : 'No'}</td>
      <td>${fmtTime(e.timestamp)}</td>
    </tr>`).join('');
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
  } catch { /* network error — ignore */ }
}

setInterval(pollStatus, 5000);
pollStatus();

// ── Seed from REST on load ───────────────────────────────────────────────────
async function seedFromRest() {
  const endpoints = [
    { url: '/api/wifi',     key: 'wifi',     render: renderWifi,     badge: 'badge-wifi',     marker: addWifiMarker },
    { url: '/api/aircraft', key: 'aircraft', render: renderAircraft, badge: 'badge-aircraft', marker: addAircraftMarker },
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
      state.aircraft.push(data);
      setBadge('badge-aircraft', state.aircraft.length);
      addAircraftMarker(data);
      renderAircraft();
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
