'use strict';

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  wifi:     [],   // deduplicated by MAC
  aircraft: [],
  ais:      [],   // deduplicated by MMSI
  alerts:   [],
  remoteId: [],   // deduplicated by UAS ID
};

// ── Leaflet map ──────────────────────────────────────────────────────────────
const map = L.map('map', { zoomControl: true }).setView([51.5, -0.1], 10);

// Offline-first basemap: when the server has a local MBTiles pack
// (window.PV_OFFLINE_BASEMAP.available), draw from /tiles/... so the map works with
// no internet; otherwise use online OSM. The pack's own zoom range and center are
// honored so the operator lands on the surveyed area, not the default view.
const _basemap = window.PV_OFFLINE_BASEMAP || { available: false };

// Online OSM is the base layer so the whole world renders and the operator can pan/
// zoom beyond the surveyed area. When an offline MBTiles pack is present it is laid
// *on top*: its opaque tiles override OSM inside the surveyed box, and where the pack
// has no tile (outside the box, or zoomed past its range) the OSM base shows through.
// OPSEC: requesting online tiles reveals the node's view area to the OSM tile server.
// For a fully air-gapped deployment, remove this base layer and keep only /tiles.
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors',
  maxZoom: 19,
}).addTo(map);

if (_basemap.available) {
  L.tileLayer('/tiles/{z}/{x}/{y}.png', {
    attribution: _basemap.attribution || '© OpenStreetMap contributors',
    minZoom: _basemap.minzoom || 0,
    maxZoom: _basemap.maxzoom || 19,
  }).addTo(map);
  if (Array.isArray(_basemap.center) && _basemap.center.length === 2) {
    map.setView(_basemap.center, _basemap.maxzoom ? Math.max(_basemap.minzoom || 0, _basemap.maxzoom - 3) : 14);
  }
}

const layers = {
  wifi:     L.layerGroup().addTo(map),
  aircraft: L.layerGroup().addTo(map),
  ais:      L.layerGroup().addTo(map),
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

// Two lenses on the aircraft picture:
//  - the TABLE keeps a persistent detection log (survives a refresh, like WiFi/BT)
//    up to RETENTION (matches the server's AIRCRAFT_RETENTION_SECONDS, now 24h);
//  - the MAP shows the current sky: a marker appears within MAP_DECAY and
//    shrinks/fades with age, then expires, so it reads what's overhead *now*.
// MAP_DECAY is 10 min, not seconds: under sparse single-target RTL-SDR reception a
// plane that's still overhead can go a minute-plus between polls, so a tight window
// blanked the map (a plane in the table + in readsb but no marker). 10 min keeps a
// genuinely-present aircraft on the map while still expiring departed ones.
const AIRCRAFT_RETENTION_MS = 86400000;
const AIRCRAFT_MAP_DECAY_MS = 600000;

function aircraftAgeMs(e) {
  const t = e.last_seen || e.timestamp;
  if (!t) return Infinity;
  const ms = Date.now() - new Date(t).getTime();
  return Number.isNaN(ms) ? Infinity : ms;
}

function addAircraftMarker(ev) {
  if (ev.lat == null || ev.lon == null) return;
  const age = aircraftAgeMs(ev);
  if (age > AIRCRAFT_MAP_DECAY_MS) return;   // left the current sky — off the map
  const frac = Math.max(0, 1 - age / AIRCRAFT_MAP_DECAY_MS);
  // Returning airframes (re-seen after an absence) draw amber to stand out.
  const color = ev.emergency ? '#f85149' : (ev.returning ? '#d29922' : '#58a6ff');
  const ret = ev.returning ? `<br><b class="returning">↩ returned</b> (${ev.return_count || 1}×)` : '';
  L.circleMarker([ev.lat, ev.lon], {
    radius: 4 + 4 * frac,
    color,
    opacity: 0.3 + 0.7 * frac,
    fillOpacity: 0.2 + 0.6 * frac,
  }).bindPopup(
    `<b>${ev.callsign || ev.icao}</b><br>Alt: ${ev.altitude} ft<br>Reg: ${ev.registration || '—'}${ret}`
  ).addTo(layers.aircraft);
}

function addAisMarker(ev) {
  if (ev.lat == null || ev.lon == null) return;
  L.circleMarker([ev.lat, ev.lon], {
    radius: 6, color: '#3fb950', fillOpacity: 0.85,
  }).bindPopup(
    `<b>${ev.name ? esc(ev.name) : ('MMSI ' + ev.mmsi)}</b>` +
    `<br>MMSI: ${ev.mmsi}` + (ev.ship_type != null ? `<br>Type: ${esc(String(ev.ship_type))}` : '')
  ).addTo(layers.ais);
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

// Escape free-text fields (e.g. SSID) before injecting into innerHTML — an AP can
// broadcast arbitrary bytes in its SSID, so treat it as untrusted.
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// A device's rotation-stable identity: its strong fingerprint if it has one,
// else its MAC. Rows sharing an identity are one logical device.
function wifiIdentity(e) {
  const fp = e.fingerprint || '';
  return (fp.indexOf('wifi-fp:') === 0 || fp.indexOf('ble-fp:') === 0) ? fp : (e.mac || '');
}

// ── Sortable / filterable table controller ───────────────────────────────────
// Each table tab gets clickable sort headers, dropdown + min-count filters, a live
// "showing X of Y" count, and CSV export — all composing with the existing search
// box, and all persisted per tab in localStorage so the view returns as you left it.
function loadView() { try { return JSON.parse(localStorage.getItem('pv_view') || '{}'); } catch { return {}; } }
const _view = loadView();
function saveView() { try { localStorage.setItem('pv_view', JSON.stringify(_view)); } catch { /* quota / private mode */ } }

function _cmp(a, b, type) {
  if (type === 'num') {
    const x = parseFloat(a), y = parseFloat(b);
    return (Number.isNaN(x) ? -Infinity : x) - (Number.isNaN(y) ? -Infinity : y);
  }
  if (type === 'time') {
    return (new Date(a || 0).getTime() || 0) - (new Date(b || 0).getTime() || 0);
  }
  return String(a ?? '').toLowerCase().localeCompare(String(b ?? '').toLowerCase());
}

function _csv(v) {
  const s = (v == null ? '' : String(v));
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
function downloadCSV(name, columns, rows) {
  const lines = [columns.map(c => _csv(c.label)).join(',')];
  for (const r of rows) lines.push(columns.map(c => _csv(r[c.k])).join(','));
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

// cfg: { tab, rows():[obj], cell(obj):'<tr>…', columns:[{k,label}] (CSV schema),
//        filters:[{k,label}], min:{k,label}|null, defaultSort:{k,dir,type} }
function createTable(cfg) {
  const panel = document.getElementById(`tab-${cfg.tab}`);
  if (!panel) return { render() {} };           // stale cached DOM — no-op, never throw
  const tbody = panel.querySelector('tbody');
  const toolbar = panel.querySelector('.toolbar');
  const searchEl = panel.querySelector('input[type="text"]');
  const v = (_view[cfg.tab] ||= {});
  if (!v.sort) v.sort = cfg.defaultSort ? { ...cfg.defaultSort } : null;
  v.filters ||= {};
  let lastRows = [];

  // Controls: filter dropdowns, optional min-count, count readout, CSV export.
  const bar = document.createElement('span');
  bar.className = 'tbl-controls';
  const selects = {};
  for (const f of cfg.filters) {
    const sel = document.createElement('select');
    sel.className = 'tbl-filter'; sel.title = f.label;
    sel.addEventListener('change', () => { v.filters[f.k] = sel.value; saveView(); render(); });
    selects[f.k] = sel; bar.appendChild(sel);
  }
  if (cfg.min) {
    const minEl = document.createElement('input');
    minEl.type = 'number'; minEl.min = '0'; minEl.className = 'tbl-min';
    minEl.placeholder = cfg.min.label;
    if (v.min != null) minEl.value = v.min;
    minEl.addEventListener('input', () => { v.min = minEl.value === '' ? null : Number(minEl.value); saveView(); render(); });
    bar.appendChild(minEl);
  }
  const csvBtn = document.createElement('button');
  csvBtn.className = 'btn-sm'; csvBtn.textContent = 'CSV';
  csvBtn.title = 'Export the current filtered + sorted view as CSV';
  csvBtn.addEventListener('click', () => downloadCSV(`${cfg.tab}-${Date.now()}.csv`, cfg.columns, lastRows));
  bar.appendChild(csvBtn);
  const countEl = document.createElement('span');
  countEl.className = 'tbl-count';
  bar.appendChild(countEl);
  if (toolbar) toolbar.appendChild(bar);

  // Restore + wire the existing search box (persisted per tab).
  if (searchEl) {
    if (v.search) searchEl.value = v.search;
    searchEl.addEventListener('input', () => { v.search = searchEl.value; saveView(); render(); });
  }

  // Clickable sort headers (data-col / data-type on each <th>).
  panel.querySelectorAll('th[data-col]').forEach(th => {
    th.classList.add('sortable');
    th.addEventListener('click', () => {
      const k = th.dataset.col, type = th.dataset.type || 'text';
      if (v.sort && v.sort.k === k) v.sort.dir = v.sort.dir === 'asc' ? 'desc' : 'asc';
      else v.sort = { k, type, dir: type === 'text' ? 'asc' : 'desc' };
      saveView(); render();
    });
  });

  function refreshOptions(rows) {
    for (const f of cfg.filters) {
      const sel = selects[f.k];
      const cur = v.filters[f.k] || '';
      const vals = [...new Set(rows.map(r => r[f.k]).filter(x => x != null && x !== ''))]
        .sort((a, b) => String(a).localeCompare(String(b)));
      sel.innerHTML = `<option value="">${esc(f.label)}: all</option>`
        + vals.map(x => `<option value="${esc(x)}"${String(x) === cur ? ' selected' : ''}>${esc(x)}</option>`).join('');
      sel.value = cur;
    }
  }
  function refreshArrows() {
    panel.querySelectorAll('th[data-col]').forEach(th => {
      th.classList.remove('sort-asc', 'sort-desc');
      if (v.sort && v.sort.k === th.dataset.col) th.classList.add(v.sort.dir === 'asc' ? 'sort-asc' : 'sort-desc');
    });
  }

  function render() {
    const all = cfg.rows();
    refreshOptions(all);
    const q = (v.search || '').toLowerCase();
    let rows = all.filter(r => {
      if (q && !JSON.stringify(r).toLowerCase().includes(q)) return false;
      for (const f of cfg.filters) {
        const fv = v.filters[f.k];
        if (fv && String(r[f.k] ?? '') !== fv) return false;
      }
      if (cfg.min && v.min != null && Number(r[cfg.min.k] || 0) < v.min) return false;
      return true;
    });
    const s = v.sort;
    if (s && s.k) rows.sort((a, b) => _cmp(a[s.k], b[s.k], s.type || 'text') * (s.dir === 'asc' ? 1 : -1));
    lastRows = rows;
    countEl.textContent = `${Math.min(rows.length, 200)} of ${all.length}`;
    refreshArrows();
    tbody.innerHTML = rows.slice(0, 200).map(cfg.cell).join('');
  }

  return { render };
}

const _ALERT_CLASS = { high: 'alert-high', likely: 'alert-likely', suspicious: 'alert-suspicious' };

const tables = {
  wifi: createTable({
    tab: 'wifi',
    columns: [
      { k: 'contact', label: 'Contact' }, { k: 'mac', label: 'MAC' }, { k: 'ssid', label: 'SSID' },
      { k: 'mac_type', label: 'MAC Type' }, { k: 'score', label: 'Score' }, { k: 'alert_level', label: 'Alert' },
      { k: 'seen', label: 'Seen' }, { k: 'manufacturer', label: 'Manufacturer' },
      { k: 'pnl', label: 'Known Networks' }, { k: 'reconnect', label: 'Reconnect' }, { k: 'last', label: 'Last' },
    ],
    filters: [{ k: 'manufacturer', label: 'Mfr' }, { k: 'mac_type', label: 'MAC type' }, { k: 'alert_level', label: 'Alert' }],
    min: { k: 'seen', label: 'Seen ≥' },
    defaultSort: { k: 'seen', dir: 'desc', type: 'num' },
    rows() {
      // Collapse rotating addresses: group by rotation-stable identity, keep the
      // most-recent sighting as the representative and count distinct MACs. The
      // preferred-network list ("known networks") and reconnect intent accumulate
      // across the whole group, since they're observed a slice at a time.
      const groups = new Map();
      for (const e of state.wifi) {
        const id = wifiIdentity(e);
        let g = groups.get(id);
        if (!g) { g = { latest: e, macs: new Set(), pnl: new Set(), reconnect: false }; groups.set(id, g); }
        if (e.mac) g.macs.add(e.mac);
        for (const s of (e.probe_ssids_all || [])) g.pnl.add(s);
        if (e.reconnect) g.reconnect = true;
        const t1 = new Date(g.latest.last_seen || g.latest.timestamp || 0).getTime();
        const t2 = new Date(e.last_seen || e.timestamp || 0).getTime();
        if (t2 >= t1) g.latest = e;
      }
      return [...groups.values()].map(g => {
        const e = g.latest;
        return {
          contact: e.contact || e.fingerprint_label || '',
          mac: e.mac || '', ssid: e.ssid || '', mac_type: e.mac_type || '',
          score: e.score != null ? e.score : 0, alert_level: e.alert_level || '',
          seen: e.observation_count || 0, manufacturer: e.manufacturer || '',
          pnl: [...g.pnl].sort().join(', '), reconnect: g.reconnect ? 'yes' : '',
          last: e.last_seen || e.timestamp || '', _addr: g.macs.size, _pnlCount: g.pnl.size,
        };
      });
    },
    cell(r) {
      const cls = _ALERT_CLASS[r.alert_level] || 'alert-new';
      const macCell = `<code>${r.mac || '—'}</code>`
        + (r._addr > 1 ? ` <span class="addr-count" title="${r._addr} rotating addresses">+${r._addr - 1}</span>` : '');
      const pnlCell = r.pnl
        ? `<span title="${esc(r.pnl)}">${esc(r.pnl)}</span> <span class="addr-count">${r._pnlCount}</span>`
        : '—';
      const reconnectCell = r.reconnect
        ? '<span class="reconnect-badge" title="directed advert / solicited service — calling out to reconnect">↩ yes</span>'
        : '—';
      return `
    <tr>
      <td>${r.contact ? esc(r.contact) : '—'}</td>
      <td>${macCell}</td>
      <td>${r.ssid ? esc(r.ssid) : '—'}</td>
      <td>${r.mac_type || '—'}</td>
      <td class="${cls}">${(r.score || 0).toFixed(2)}</td>
      <td class="${cls}">${r.alert_level || '—'}</td>
      <td>${r.seen || '—'}</td>
      <td>${r.manufacturer ? esc(r.manufacturer) : '—'}</td>
      <td class="pnl-cell">${pnlCell}</td>
      <td>${reconnectCell}</td>
      <td>${fmtTime(r.last)}</td>
    </tr>`;
    },
  }),

  aircraft: createTable({
    tab: 'aircraft',
    columns: [
      { k: 'callsign', label: 'Callsign' }, { k: 'icao', label: 'ICAO' }, { k: 'registration', label: 'Reg' },
      { k: 'altitude', label: 'Alt_ft' }, { k: 'speed', label: 'Speed' }, { k: 'lat', label: 'Lat' },
      { k: 'lon', label: 'Lon' }, { k: 'acars', label: 'ACARS_count' },
      { k: 'emergency', label: 'Emergency' }, { k: 'time', label: 'Time' },
    ],
    filters: [{ k: 'emergency', label: 'Emergency' }],
    min: null,
    defaultSort: { k: 'time', dir: 'desc', type: 'time' },
    rows() {
      return state.aircraft.map(e => {
        const ac = Array.isArray(e.acars) ? e.acars : [];
        const latest = ac.length ? ac[ac.length - 1] : null;
        return {
          callsign: e.callsign || '', icao: e.icao || '', registration: e.registration || '',
          altitude: e.altitude ?? '', speed: e.speed ?? '', lat: e.lat ?? '', lon: e.lon ?? '',
          emergency: e.emergency ? 'Yes' : 'No', time: e.timestamp || '',
          returning: !!e.returning, return_count: e.return_count || 0,
          acars: ac.length,
          acars_text: latest ? `${latest.label ? latest.label + ': ' : ''}${latest.text || ''}`.trim() : '',
        };
      });
    },
    cell(r) {
      const pos = (r.lat !== '' && r.lat != null && r.lon !== '' && r.lon != null)
        ? `${(+r.lat).toFixed(3)}, ${(+r.lon).toFixed(3)}`
        : '<span class="no-pos">no position</span>';
      const ret = r.returning
        ? ` <span class="returning" title="Re-seen after an absence — same airframe (${r.return_count || 1}×)">↩ RETURN${(r.return_count || 1) > 1 ? ' ×' + r.return_count : ''}</span>`
        : '';
      const acars = r.acars > 0
        ? `<span class="acars-badge" title="${esc(r.acars_text)}">✉ ${r.acars}</span>`
        : '—';
      return `
    <tr class="${r.returning ? 'returning-row' : ''}">
      <td>${r.callsign ? esc(r.callsign) : '—'}${ret}</td>
      <td><code>${r.icao || '—'}</code></td>
      <td>${r.registration ? esc(r.registration) : '—'}</td>
      <td>${r.altitude === '' ? '—' : r.altitude}</td>
      <td>${r.speed === '' ? '—' : r.speed}</td>
      <td>${pos}</td>
      <td>${acars}</td>
      <td class="${r.emergency === 'Yes' ? 'emergency-yes' : ''}">${r.emergency === 'Yes' ? '🚨 YES' : 'No'}</td>
      <td>${fmtTime(r.time)}</td>
    </tr>`;
    },
  }),

  ais: createTable({
    tab: 'ais',
    columns: [
      { k: 'name', label: 'Vessel' }, { k: 'mmsi', label: 'MMSI' }, { k: 'ship_type', label: 'Type' },
      { k: 'lat', label: 'Lat' }, { k: 'lon', label: 'Lon' }, { k: 'seen', label: 'Seen' },
      { k: 'time', label: 'Time' },
    ],
    filters: [{ k: 'ship_type', label: 'Type' }],
    min: null,
    defaultSort: { k: 'time', dir: 'desc', type: 'time' },
    rows() {
      return state.ais.map(e => ({
        name: e.name || '', mmsi: e.mmsi ?? '', ship_type: e.ship_type ?? '',
        lat: e.lat ?? '', lon: e.lon ?? '', seen: e.observation_count ?? '',
        time: e.last_seen || e.timestamp || '',
      }));
    },
    cell(r) {
      const pos = (r.lat !== '' && r.lat != null && r.lon !== '' && r.lon != null)
        ? `${(+r.lat).toFixed(3)}, ${(+r.lon).toFixed(3)}`
        : '<span class="no-pos">no position</span>';
      const lat = (r.lat !== '' && r.lat != null) ? (+r.lat).toFixed(3) : '—';
      const lon = (r.lon !== '' && r.lon != null) ? (+r.lon).toFixed(3) : '—';
      return `
    <tr>
      <td>${r.name ? esc(r.name) : '—'}</td>
      <td><code>${r.mmsi || '—'}</code></td>
      <td>${r.ship_type === '' ? '—' : esc(String(r.ship_type))}</td>
      <td>${lat}</td>
      <td>${lon}</td>
      <td>${r.seen === '' ? '—' : r.seen}</td>
      <td>${fmtTime(r.time)}</td>
    </tr>`;
    },
  }),

  remoteId: createTable({
    tab: 'remote-id',
    columns: [
      { k: 'uas_id', label: 'UAS_ID' }, { k: 'id_type', label: 'ID_Type' }, { k: 'ua_type', label: 'UA_Type' },
      { k: 'operator_id', label: 'Operator' }, { k: 'drone_lat', label: 'Drone_Lat' }, { k: 'drone_lon', label: 'Drone_Lon' },
      { k: 'drone_alt_m', label: 'Alt_m' }, { k: 'rssi', label: 'RSSI' }, { k: 'time', label: 'Time' },
    ],
    filters: [{ k: 'ua_type', label: 'UA type' }, { k: 'id_type', label: 'ID type' }],
    min: null,
    defaultSort: { k: 'time', dir: 'desc', type: 'time' },
    rows() {
      return state.remoteId.map(e => ({
        uas_id: e.uas_id || '', id_type: e.id_type || '', ua_type: e.ua_type || '',
        operator_id: e.operator_id || '', drone_lat: e.drone_lat ?? '', drone_lon: e.drone_lon ?? '',
        drone_alt_m: e.drone_alt_m ?? '', rssi: e.rssi ?? '', time: e.timestamp || '',
      }));
    },
    cell(r) {
      const lat = r.drone_lat !== '' && r.drone_lat != null ? (+r.drone_lat).toFixed(4) : '—';
      const lon = r.drone_lon !== '' && r.drone_lon != null ? (+r.drone_lon).toFixed(4) : '—';
      return `
    <tr>
      <td><code>${r.uas_id ? esc(r.uas_id) : '—'}</code></td>
      <td>${r.id_type || '—'}</td>
      <td>${r.ua_type || '—'}</td>
      <td>${r.operator_id ? esc(r.operator_id) : '—'}</td>
      <td>${lat}</td>
      <td>${lon}</td>
      <td>${r.drone_alt_m === '' ? '—' : r.drone_alt_m}</td>
      <td>${r.rssi === '' ? '—' : r.rssi}</td>
      <td>${fmtTime(r.time)}</td>
    </tr>`;
    },
  }),
};

function renderWifi() { tables.wifi.render(); }

function renderAircraft() {
  // Persistent log bounded by the retention window; the current-sky decay is applied
  // to the MAP markers (addAircraftMarker), not the table.
  state.aircraft = state.aircraft.filter(e => aircraftAgeMs(e) <= AIRCRAFT_RETENTION_MS);
  setBadge('badge-aircraft', state.aircraft.length);
  tables.aircraft.render();
  // Rebuild the aircraft map layer from the (deduped) state so a moving plane does
  // not pile up duplicate markers; position-less aircraft are omitted from the map.
  layers.aircraft.clearLayers();
  state.aircraft.forEach(addAircraftMarker);
}

function renderAis() {
  setBadge('badge-ais', state.ais.length);
  tables.ais.render();
  layers.ais.clearLayers();
  state.ais.forEach(addAisMarker);
}

function renderRemoteId() {
  if (!document.getElementById('remote-id-tbody')) return;   // stale cached DOM — no-op
  tables.remoteId.render();
}

function renderAlerts() {
  document.getElementById('alerts-feed').innerHTML = state.alerts
    .slice(-100)
    .reverse()
    .map(a => {
      const ts = a.timestamp ? new Date(a.timestamp).toLocaleString() : '';
      return `
      <div class="alert-card ${a.kind || ''}">
        <div class="alert-head">
          <span class="alert-title">${esc(a.title || a.type || 'Alert')}</span>
          <span class="alert-time">${esc(ts)}</span>
        </div>
        <div class="alert-body">${esc(a.body || JSON.stringify(a))}</div>
      </div>`;
    }).join('');
}

// Search inputs are wired inside createTable (per-tab, persisted), so no separate
// search-filter wiring is needed here.

// Clear buttons. Wired null-safe (optional chaining): a single missing element —
// e.g. a browser holding a stale cached index.html that predates a newly added
// tab — must never throw at top level and halt the rest of init (seedFromRest,
// connectSSE, status polling), which would blank the whole dashboard.
document.getElementById('wifi-clear')?.addEventListener('click', () => {
  state.wifi = []; layers.wifi.clearLayers(); renderWifi(); setBadge('badge-wifi', 0);
});
document.getElementById('aircraft-clear')?.addEventListener('click', () => {
  state.aircraft = []; layers.aircraft.clearLayers(); renderAircraft(); setBadge('badge-aircraft', 0);
});
document.getElementById('ais-clear')?.addEventListener('click', () => {
  state.ais = []; layers.ais.clearLayers(); renderAis(); setBadge('badge-ais', 0);
});
document.getElementById('remote-id-clear')?.addEventListener('click', () => {
  state.remoteId = []; renderRemoteId(); setBadge('badge-remote-id', 0);
});
document.getElementById('alerts-clear')?.addEventListener('click', () => {
  state.alerts = []; renderAlerts(); setBadge('badge-alerts', 0);
});

// ── Status polling ───────────────────────────────────────────────────────────
function applyHealth(health, active, gpsFix) {
  const map_ = { gps: 's-gps', kismet: 's-kismet', ble: 's-ble', adsb: 's-adsb', ais: 's-ais', acars: 's-acars' };
  active = active || {};
  Object.entries(map_).forEach(([key, id]) => {
    const el = document.getElementById(id);
    if (!el) return;
    // A sensor that isn't running (e.g. AIS/ACARS off, or BLE controller down)
    // shows as "off", not healthy. A missing modules_active key is treated as active
    // (back-compat). BLE has no separate sensor_health flip, so absence => ok.
    const isActive = active[key] !== false;
    const ok = (key in health) ? health[key] : true;
    el.classList.remove('ok', 'warn', 'err', 'disabled');
    if (!isActive) {
      el.classList.add('disabled');
    } else if (!ok) {
      el.classList.add('err');
    } else if (key === 'gps' && !gpsFix) {
      // gpsd is reachable but hasn't produced a position fix yet (mode < 2).
      el.classList.add('warn');
    } else {
      el.classList.add('ok');
    }
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
    if (d.sensor_health) applyHealth(d.sensor_health, d.modules_active, d.gps_fix);
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
    { url: '/api/wifi',     key: 'wifi',     render: renderWifi,     badge: 'badge-wifi',     marker: addWifiMarker,  layer: layers.wifi },
    { url: '/api/aircraft', key: 'aircraft', render: renderAircraft, badge: 'badge-aircraft', marker: null,           layer: null },
    { url: '/api/ais',      key: 'ais',      render: renderAis,      badge: 'badge-ais',      marker: addAisMarker,   layer: layers.ais },
    { url: '/api/remote_id', key: 'remoteId', render: renderRemoteId, badge: 'badge-remote-id', marker: null,         layer: null },
    { url: '/api/alerts',   key: 'alerts',   render: renderAlerts,   badge: 'badge-alerts',   marker: null,           layer: null },
  ];
  for (const ep of endpoints) {
    try {
      const r = await fetch(ep.url);
      if (!r.ok) continue;
      const items = await r.json();
      state[ep.key] = items;
      // Idempotent: this runs repeatedly (periodic resync + on every SSE reconnect),
      // so clear the marker layer before re-adding or markers pile up on the map.
      if (ep.layer) ep.layer.clearLayers();
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

  es.onopen = function() {
    // Re-seed from REST on every (re)connect. The stream drops on a server restart
    // or a network/proxy blip, and the old code reconnected WITHOUT re-seeding — so
    // anything that appeared during the gap was missing from the live view until a
    // manual refresh (the 18→24 aircraft / 500→504 wifi jump). Backfill on connect.
    seedFromRest();
  };

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
    } else if (type === 'ais') {
      // Deduplicate by MMSI — a vessel re-reports position/static periodically.
      const idx = state.ais.findIndex(e => e.mmsi === data.mmsi);
      if (idx >= 0) state.ais[idx] = data; else state.ais.push(data);
      renderAis();
    } else if (type === 'acars') {
      // Raw ACARS feed; correlated messages already ride on the aircraft event
      // (event.acars → the Aircraft tab's ACARS column), so nothing to do here.
      return;
    } else if (type === 'remote_id') {
      // Deduplicate by UAS ID — a broadcasting drone is re-pushed as its track advances.
      const idx = state.remoteId.findIndex(e => e.uas_id === data.uas_id);
      if (idx >= 0) state.remoteId[idx] = data; else state.remoteId.push(data);
      setBadge('badge-remote-id', state.remoteId.length);
      renderRemoteId();
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

// Safety-net resync: periodically re-seed every tab from REST so the view reflects
// current state even when (a) SSE is gated — updates to an existing contact within
// the same alert level are not pushed — or (b) the stream has silently stalled
// without firing onerror (no reconnect). Together with the on-connect re-seed this
// makes the dashboard a live mirror of the JSON, not a refresh-to-update snapshot.
// Cheap full re-seed; markers are rebuilt idempotently (see seedFromRest).
setInterval(seedFromRest, 15000);

// Re-render the aircraft panel on a timer so recency decay applies (and stale
// contacts expire) even during quiet periods with no new SSE pushes — e.g. while
// readsb is stopped during an AIS/ACARS SDR slice.
setInterval(renderAircraft, 5000);

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

