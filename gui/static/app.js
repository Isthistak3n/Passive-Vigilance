'use strict';

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  wifi:     [],   // deduplicated by MAC
  aircraft: [],
  ais:      [],   // deduplicated by MMSI
  alerts:   [],
  remoteId: [],   // deduplicated by UAS ID
  survey:   [],   // recon-pair taskings (each with its bed-down findings)
};

// ── Recon-pair survey state (design §5.5) ────────────────────────────────────
// Declared up here (before the tables render) so the WiFi cell() reference is not
// in the temporal dead zone. surveyEnabled flips true only once /api/survey answers
// 200 (a fixed node with SURVEY_ENABLED + GUI_TOKEN), which also unhides the tab.
let surveyEnabled = false;
const surveyEvidenceMap = new Map();  // contact_key -> tasking evidence (for the POST)
const taskedKeys = new Set();          // contact_keys already tasked this session

// ── Leaflet map ──────────────────────────────────────────────────────────────
const map = L.map('map', { zoomControl: true }).setView([51.5, -0.1], 10);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors',
  maxZoom: 19,
}).addTo(map);

const layers = {
  wifi:     L.layerGroup().addTo(map),
  aircraft: L.layerGroup().addTo(map),
  ais:      L.layerGroup().addTo(map),
  survey:   L.layerGroup().addTo(map),
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

// Hover summary for the latest correlated ACARS message: label + text, plus any
// parsed flight / route / position the server folded onto the aircraft event.
function acarsSummary(latest, e) {
  const parts = [];
  const head = `${latest.label ? latest.label + ': ' : ''}${latest.text || ''}`.trim();
  if (head) parts.push(head);
  if (e && e.acars_flight) parts.push('Flight ' + e.acars_flight);
  if (e && e.route) parts.push(e.route);
  const plat = latest.lat, plon = latest.lon;
  if (plat != null && plon != null) parts.push(`pos ${(+plat).toFixed(3)}, ${(+plon).toFixed(3)}`);
  return parts.join('  |  ');
}

// A device's rotation-stable identity: its strong fingerprint if it has one,
// else its MAC. Rows sharing an identity are one logical device.
function wifiIdentity(e) {
  // The rotation-stable DISPLAY identity (strong or medium tier) collapses a
  // device's rotating addresses — and a returning device — into one contact.
  if (e.contact_key) return e.contact_key;
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
      { k: 'pnl', label: 'Known Networks' }, { k: 'affinity', label: 'Local networks' },
      { k: 'reconnect', label: 'Reconnect' }, { k: 'last', label: 'Last' },
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
        if (!g) { g = { latest: e, macs: new Set(), pnl: new Set(), affinity: new Set(), reconnect: false, returning: false, returnCount: 0 }; groups.set(id, g); }
        if (e.mac) g.macs.add(e.mac);
        for (const s of (e.probe_ssids_all || [])) g.pnl.add(s);
        for (const s of (e.network_affinity || [])) g.affinity.add(s);
        if (e.reconnect) g.reconnect = true;
        if (e.returning) g.returning = true;
        if (e.return_count) g.returnCount = Math.max(g.returnCount, e.return_count);
        if (e.returning_entity) g.returningEntity = true;
        if (e.entity_visits) g.entityVisits = Math.max(g.entityVisits || 0, e.entity_visits);
        if (e.entity_days_known) g.entityDays = Math.max(g.entityDays || 0, e.entity_days_known);
        if (e.entity_last_seen) g.entityLastSeen = e.entity_last_seen;
        const t1 = new Date(g.latest.last_seen || g.latest.timestamp || 0).getTime();
        const t2 = new Date(e.last_seen || e.timestamp || 0).getTime();
        if (t2 >= t1) g.latest = e;
      }
      return [...groups.values()].map(g => {
        const e = g.latest;
        // Recon-pair: remember the tasking evidence for this contact so the "Task
        // survey" click can POST it without re-deriving it in the browser.
        if (e.surveyable && e.contact_key && e.survey_evidence) {
          surveyEvidenceMap.set(e.contact_key, e.survey_evidence);
        }
        return {
          contact: e.contact || e.fingerprint_label || '',
          confidence: e.contact_confidence || '',
          surveyable: !!e.surveyable, contact_key: e.contact_key || '',
          returning: g.returning, return_count: g.returnCount,
          returning_entity: !!g.returningEntity, entity_visits: g.entityVisits || 0,
          entity_days: g.entityDays || 0, entity_last_seen: g.entityLastSeen || '',
          belongs: e.belongs || '', person_id: e.person_id || '', person_size: e.person_size || 0,
          mac: e.mac || '', ssid: e.ssid || '', mac_type: e.mac_type || '',
          score: e.score != null ? e.score : 0, alert_level: e.alert_level || '',
          seen: e.observation_count || 0, manufacturer: e.manufacturer || '',
          pnl: [...g.pnl].sort().join(', '), affinity: [...g.affinity].sort().join(', '),
          reconnect: g.reconnect ? 'yes' : '',
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
      const affinityCell = r.affinity
        ? `<span title="probed networks confirmed beaconing here: ${esc(r.affinity)}">${esc(r.affinity)}</span>`
        : '—';
      // Medium-confidence contacts are "likely same" (a looser identity anchor), so
      // mark them so an operator doesn't read a medium link as certain. Returning =
      // this contact came back after an absence, like a returning aircraft.
      const conf = r.confidence === 'medium'
        ? ' <span class="conf-medium" title="likely the same device — matched on a less-distinctive signature">~</span>'
        : '';
      const ret = r.returning
        ? ` <span class="returning" title="Re-seen after an absence — same contact (${r.return_count || 1}×)">↩ RETURN${(r.return_count || 1) > 1 ? ' ×' + r.return_count : ''}</span>`
        : '';
      // Cross-session: this contact was here on a PRIOR session/day — the "seen before"
      // (are-you-being-cased) signal, distinct from a within-session return above.
      const known = r.returning_entity
        ? ` <span class="known-entity" title="Seen on a prior session/day — last ${r.entity_last_seen || '?'}, known across ${r.entity_days || 1} day(s)">⌂ KNOWN${r.entity_visits > 1 ? ' ×' + r.entity_visits : ''}</span>`
        : '';
      // Environment: resident (probes a network beaconed here) vs a novel visitor
      // with no local affinity; and a person cluster of a mobile's linked radios.
      const belongs = r.belongs === 'resident'
        ? ' <span class="belongs-resident" title="Probes a network that beacons here — belongs to this place">⌂ resident</span>'
        : (r.belongs === 'visitor'
          ? ' <span class="belongs-visitor" title="Novel, with no affinity to any local network — a visitor">✦ VISITOR</span>'
          : '');
      const person = (r.person_id && r.person_size > 1)
        ? ` <span class="person-badge" title="Travels with ${r.person_size - 1} other radio(s) — likely one person">👥 ${r.person_size}</span>`
        : '';
      return `
    <tr class="${r.returning || r.returning_entity ? 'returning-row' : ''}${r.belongs === 'visitor' ? ' visitor-row' : ''}">
      <td>${r.contact ? esc(r.contact) : '—'}${conf}${ret}${known}${belongs}${person}</td>
      <td>${macCell}</td>
      <td>${r.ssid ? esc(r.ssid) : '—'}</td>
      <td>${r.mac_type || '—'}</td>
      <td class="${cls}">${(r.score || 0).toFixed(2)}</td>
      <td class="${cls}">${r.alert_level || '—'}</td>
      <td>${r.seen || '—'}</td>
      <td>${r.manufacturer ? esc(r.manufacturer) : '—'}</td>
      <td class="pnl-cell">${pnlCell}</td>
      <td class="pnl-cell">${affinityCell}</td>
      <td>${reconnectCell}</td>
      <td>${fmtTime(r.last)}</td>
      ${surveyEnabled ? `<td class="survey-cell">${surveyButtonCell(r)}</td>` : ''}
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
          // Route/flight enrichment surfaced from correlated ACARS (server folds
          // it onto the aircraft event); shown next to the badge, not just on hover.
          route: e.route || '',
          acars_flight: e.acars_flight || '',
          acars_text: latest ? acarsSummary(latest, e) : '',
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
      const acarsExtra = r.route
        ? ` <span class="acars-route">${esc(r.route)}</span>`
        : (r.acars_flight ? ` <span class="acars-route">${esc(r.acars_flight)}</span>` : '');
      const acars = r.acars > 0
        ? `<span class="acars-badge" title="${esc(r.acars_text)}">✉ ${r.acars}</span>${acarsExtra}`
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
    } else if (type === 'survey') {
      // A tasking was issued or findings were offloaded — refetch the survey view.
      if (surveyEnabled) loadSurvey();
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

// Refresh the survey view on the same cadence (only once the feature is live).
setInterval(() => { if (surveyEnabled) loadSurvey(); }, 15000);

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


// ── Recon-pair survey (design §5.5) ──────────────────────────────────────────
// The fixed node issues survey taskings and the mobile node offloads the bed-down
// locations it found. This block: (1) a "Task survey" action on each surveyable
// WiFi contact, and (2) the Survey tab that shows each tasking's ranked findings.
// The whole feature is inert unless /api/survey answers (SURVEY_ENABLED + a token).

// Same token dance as the mode control: the page loads with ?token=<GUI_TOKEN>,
// which the browser turns into a cookie carried on same-origin fetches. We also
// append ?token= as a belt-and-braces for the control POST.
const SURVEY_TOKEN = new URLSearchParams(location.search).get('token') || '';
function surveyUrl(path) {
  return SURVEY_TOKEN ? `${path}?token=${encodeURIComponent(SURVEY_TOKEN)}` : path;
}

function surveyButtonCell(r) {
  if (!r.surveyable || !r.contact_key) {
    return '<span class="no-pos" title="No rotation-stable fingerprint — cannot be surveyed by another node">—</span>';
  }
  if (taskedKeys.has(r.contact_key)) {
    return '<span class="tasked-badge" title="Survey tasking issued">✓ tasked</span>';
  }
  return `<button class="btn-task" data-key="${esc(r.contact_key)}" data-contact="${esc(r.contact || '')}">Task survey</button>`;
}

async function taskContact(key, designator) {
  const evidence = surveyEvidenceMap.get(key) || null;
  try {
    const resp = await fetch(surveyUrl('/api/tasking'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        identity_key: key, designator, reason: 'operator', evidence,
      }),
    });
    if (resp.ok) {
      taskedKeys.add(key);
      renderWifi();
      loadSurvey();
    } else {
      const err = await resp.json().catch(() => ({}));
      alert(`Could not task survey: ${err.error || resp.status}`);
    }
  } catch (e) {
    alert('Could not reach the node to issue the tasking.');
  }
}

// Event delegation — the WiFi tbody is re-rendered as innerHTML, so bind once here.
document.getElementById('wifi-tbody')?.addEventListener('click', (ev) => {
  const btn = ev.target.closest('.btn-task');
  if (!btn) return;
  taskContact(btn.dataset.key, btn.dataset.contact || '');
});

function fmtDwell(secs) {
  secs = Math.max(0, Math.floor(secs || 0));
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${secs}s`;
}

function addSurveyMarkers() {
  layers.survey.clearLayers();
  for (const t of state.survey) {
    const target = esc(t.designator || t.identity_key || 'target');
    // The located home AP is the bed-down headline (a star).
    if (t.home_ap && t.home_ap.lat != null && t.home_ap.lon != null) {
      L.circleMarker([t.home_ap.lat, t.home_ap.lon], {
        radius: 11, color: '#c586ff', weight: 3, fillOpacity: 0.6,
      }).bindPopup(
        `<b>${target}</b><br><b>Bed-down (home AP)</b>` +
        `<br>${esc(t.home_ap.ssid || '')} ${t.home_ap.bssid ? '(' + esc(t.home_ap.bssid) + ')' : ''}` +
        (t.home_ap.distance_m != null ? `<br>${fmtDist(t.home_ap.distance_m)} from node — ${t.home_ap.locality || ''}` : '')
      ).addTo(layers.survey);
    }
    // Direct sightings of the device itself (secondary).
    (t.clusters || []).forEach(f => {
      if (f.cluster_lat == null || f.cluster_lon == null) return;
      L.circleMarker([f.cluster_lat, f.cluster_lon], {
        radius: 6, color: '#8b949e', weight: 1, fillOpacity: 0.35,
      }).bindPopup(
        `<b>${target}</b><br>Seen here — dwell ${fmtDwell(f.dwell_seconds)}` +
        `<br>${f.visit_count || 0} visit(s), ${f.distinct_nights || 0} night(s)` +
        (f.is_overnight ? '<br><b>overnight</b>' : '')
      ).addTo(layers.survey);
    });
  }
}

const _SURVEY_OUTCOME = {
  resident:     { label: 'RESIDENT', cls: 'survey-out-resident', hint: 'Home AP found in the local area' },
  seen:         { label: 'SEEN — HOME ELSEWHERE', cls: 'survey-out-seen', hint: 'Device seen locally but its home network was not — a WiGLE candidate' },
  not_located:  { label: 'NOT LOCATED', cls: 'survey-out-absent', hint: 'Not found in the local wardrive — a WiGLE candidate' },
};

function fmtDist(m) {
  if (m == null) return '';
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}

function renderSurvey() {
  const list = document.getElementById('survey-list');
  if (!list) return;
  setBadge('badge-survey', state.survey.length);
  if (!state.survey.length) {
    list.innerHTML = '<p class="survey-empty">No survey taskings yet. Use "Task survey" on a WiFi contact to dispatch the mobile node.</p>';
    addSurveyMarkers();
    return;
  }
  list.innerHTML = state.survey.map(t => {
    const statusCls = `survey-status-${(t.status || 'open')}`;
    const out = _SURVEY_OUTCOME[t.outcome] || null;
    const ap = t.home_ap;
    // Headline: the located home AP (the bed-down), or a pending/not-found note.
    let headline;
    if (ap) {
      const where = ap.lat != null
        ? `<code>${(+ap.lat).toFixed(5)}, ${(+ap.lon).toFixed(5)}</code>` : '—';
      const dist = ap.distance_m != null
        ? ` <span class="survey-locality ${ap.locality || ''}">${fmtDist(ap.distance_m)} from node · ${ap.locality || ''}</span>` : '';
      headline = `<div class="survey-bed">🏠 <b>Beds down at</b> ${esc(ap.ssid || 'home AP')}
        ${ap.bssid ? `<code class="bssid">${esc(ap.bssid)}</code>` : ''} — ${where}${dist}</div>`;
    } else if (t.outcome === 'not_located' || t.outcome === 'seen') {
      headline = `<div class="survey-bed survey-wigle">🛰️ Home AP not found in the local wardrive —
        <b>WiGLE candidate</b> <span class="survey-note">(look up its home networks; the query is a separate, deliberate step)</span></div>`;
    } else {
      headline = '<div class="survey-bed survey-pending">Surveying — no bed-down located yet.</div>';
    }
    // Secondary: where the device itself was seen (annotation).
    const clusters = t.clusters || [];
    const rows = clusters.map((f, i) => `
      <tr>
        <td><code>${f.cluster_lat != null ? (+f.cluster_lat).toFixed(5) : '—'}, ${f.cluster_lon != null ? (+f.cluster_lon).toFixed(5) : '—'}</code></td>
        <td>${f.distance_m != null ? fmtDist(f.distance_m) : '—'}</td>
        <td>${fmtDwell(f.dwell_seconds)}</td>
        <td>${f.visit_count || 0}</td>
        <td>${f.distinct_nights || 0}${f.is_overnight ? ' 🌙' : ''}</td>
        <td>${f.max_rssi != null ? f.max_rssi : '—'}</td>
      </tr>`).join('');
    const seenTable = clusters.length ? `
      <details class="survey-seen"><summary>Also seen at ${clusters.length} spot(s)</summary>
        <table class="survey-table">
          <thead><tr><th>Location</th><th>Dist</th><th>Dwell</th><th>Visits</th><th>Nights</th><th>RSSI</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </details>` : '';
    return `
      <div class="survey-card">
        <div class="survey-card-head">
          <span class="survey-target">${esc(t.designator || t.identity_key || 'target')}</span>
          <span class="survey-status ${statusCls}">${esc(t.status || 'open')}</span>
          ${out ? `<span class="survey-outcome ${out.cls}" title="${out.hint}">${out.label}</span>` : ''}
          <span class="survey-reason">${esc(t.reason || '')}</span>
        </div>
        ${headline}
        ${seenTable}
      </div>`;
  }).join('');
  addSurveyMarkers();
}

async function loadSurvey() {
  try {
    const r = await fetch(surveyUrl('/api/survey'));
    if (r.status === 404 || r.status === 403) { return false; }  // feature off / no token
    if (!r.ok) return surveyEnabled;
    const data = await r.json();
    state.survey = Array.isArray(data) ? data : [];
    if (!surveyEnabled) {
      // First successful answer — reveal the tab, the WiFi Survey column, and re-render.
      surveyEnabled = true;
      document.getElementById('tabbtn-survey')?.removeAttribute('hidden');
      document.querySelector('.survey-col')?.removeAttribute('hidden');
      renderWifi();
    }
    renderSurvey();
    return true;
  } catch { return surveyEnabled; }
}

document.getElementById('survey-refresh')?.addEventListener('click', loadSurvey);

// Probe once on load; if survey is enabled this reveals the UI, otherwise it stays
// hidden and the feature is entirely dormant.
loadSurvey();
