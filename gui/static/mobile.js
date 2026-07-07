'use strict';

// Standalone GUI for NODE_MODE=mobile — no Leaflet/map (a static map is of
// little use on a moving node, and skipping it keeps the kiosk browser's
// memory/CPU/network footprint down during a walk).

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  wifi:     [],   // deduplicated by MAC
  aircraft: [],
  ais:      [],   // deduplicated by MMSI
  alerts:   [],
  nearby:   [],   // live "what's around me" feed, deduplicated by MAC
  survey:   [],   // recon-pair taskings (each with its bed-down findings)
};

// Whether the node currently has a GPS fix — mirrored from /api/status so the
// patrol bar can warn that a running patrol with no fix banks zero APs (design §11).
let gpsHasFix = false;

// ── Recon-pair survey/patrol state (design §5.5 / §10) ───────────────────────
// Declared up top because pollStatus() (which runs on load) reads surveyEnabled.
// surveyEnabled flips true only once /api/survey answers (SURVEY_ENABLED + token).
let surveyEnabled = false;
let patrolActive = false;
let patrolStartedAt = null;
let patrolWardriveAps = null;

// ── Tab switching ────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
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

function renderWifi() {
  const q = document.getElementById('wifi-search').value.toLowerCase();
  // Collapse rotating addresses: group entries by identity, keep the most recent
  // sighting as the representative row and count the distinct MACs seen under it.
  const groups = new Map();
  for (const e of state.wifi) {
    const id = wifiIdentity(e);
    const g = groups.get(id);
    if (!g) {
      groups.set(id, { latest: e, macs: new Set(e.mac ? [e.mac] : []) });
    } else {
      if (e.mac) g.macs.add(e.mac);
      const t1 = new Date(g.latest.last_seen || g.latest.timestamp || 0).getTime();
      const t2 = new Date(e.last_seen || e.timestamp || 0).getTime();
      if (t2 >= t1) g.latest = e;
    }
  }
  const alertClass = { high: 'alert-high', likely: 'alert-likely', suspicious: 'alert-suspicious' };
  const rows = [...groups.values()]
    .filter(g => !q || JSON.stringify(g.latest).toLowerCase().includes(q))
    .sort((a, b) => new Date(a.latest.last_seen || a.latest.timestamp || 0)
                  - new Date(b.latest.last_seen || b.latest.timestamp || 0))
    .slice(-200)
    .reverse();
  document.getElementById('wifi-tbody').innerHTML = rows.map(g => {
    const e = g.latest;
    const n = g.macs.size;
    const identity = e.fingerprint_label ? esc(e.fingerprint_label) : '—';
    const macCell = `<code>${e.mac || '—'}</code>`
      + (n > 1 ? ` <span class="addr-count" title="${n} rotating addresses">+${n - 1}</span>` : '');
    return `
    <tr>
      <td>${identity}</td>
      <td>${macCell}</td>
      <td>${e.ssid ? esc(e.ssid) : '—'}</td>
      <td>${e.device_type || '—'}</td>
      <td>${e.mac_type || '—'}</td>
      <td class="${alertClass[e.alert_level] || 'alert-new'}">${(e.score || 0).toFixed(2)}</td>
      <td class="${alertClass[e.alert_level] || 'alert-new'}">${e.alert_level || '—'}</td>
      <td>${e.observation_count || '—'}</td>
      <td>${e.manufacturer || '—'}</td>
      <td>${fmtTime(e.last_seen || e.timestamp)}</td>
    </tr>`;
  }).join('');
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
}

function renderAis() {
  const q = document.getElementById('ais-search').value.toLowerCase();
  const rows = state.ais
    .filter(e => !q || JSON.stringify(e).toLowerCase().includes(q))
    .slice(-200)
    .reverse();
  document.getElementById('ais-tbody').innerHTML = rows.map(e => `
    <tr>
      <td>${e.name ? esc(e.name) : '—'}</td>
      <td>${e.mmsi ?? '—'}</td>
      <td>${e.ship_type ?? '—'}</td>
      <td>${e.lat ?? '—'}</td>
      <td>${e.lon ?? '—'}</td>
      <td>${fmtTime(e.last_seen || e.timestamp)}</td>
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

// ── Nearby (proximity) cards ────────────────────────────────────────────────
// RSSI -> proximity bucket. Kismet reports last_signal == 0 as a placeholder
// for "no real sample", not an actual 0 dBm reading — treat it as unknown.
function proximityClass(dbm) {
  if (dbm == null || dbm === 0) return 'prox-unknown';
  if (dbm >= -50) return 'prox-near';
  if (dbm >= -65) return 'prox-medium';
  if (dbm >= -80) return 'prox-far';
  return 'prox-veryfar';
}

function renderNearby() {
  // Cross-reference the persistence/alert feed (state.wifi) by fingerprint then
  // by MAC so a flagged device's alert tier appears on its nearby card even when
  // its MAC has rotated since the score event was recorded.
  const alertByFp  = new Map();
  const alertByMac = new Map();
  for (const e of state.wifi) {
    const fp = e.fingerprint || '';
    if (fp) alertByFp.set(fp, e.alert_level);
    if (e.mac) alertByMac.set(e.mac, e.alert_level);
  }
  const alertClass = { high: 'high', likely: 'likely', suspicious: 'suspicious' };

  const rows = [...state.nearby].sort((a, b) => {
    const sa = (a.last_signal == null || a.last_signal === 0) ? -999 : a.last_signal;
    const sb = (b.last_signal == null || b.last_signal === 0) ? -999 : b.last_signal;
    return sb - sa; // strongest signal (closest) first
  });

  document.getElementById('nearby-feed').innerHTML = rows.map(e => {
    const fp = e.fingerprint || '';
    const tier = alertClass[(fp && alertByFp.get(fp)) || alertByMac.get(e.mac)] || '';
    const name = e.name ? esc(e.name)
      : (e.fingerprint_label ? esc(e.fingerprint_label)
        : (e.probe_ssids && e.probe_ssids.length ? esc(e.probe_ssids[0]) : '(no name)'));
    const sig = (e.last_signal != null && e.last_signal !== 0) ? `${e.last_signal} dBm` : '—';
    const ident = e.fingerprint_label ? `${esc(e.fingerprint_label)} · ${e.mac}` : e.mac;
    const meta = [e.manufacturer || e.device_type || '—', ident, fmtTime(e.timestamp)]
      .filter(Boolean).join(' · ');
    return `
      <div class="nearby-card ${tier}">
        <div class="nearby-row">
          <span class="prox-dot ${proximityClass(e.last_signal)}"></span>
          <span class="nearby-name">${name}</span>
          <span class="nearby-signal">${sig}</span>
        </div>
        <div class="nearby-meta">${meta}</div>
      </div>`;
  }).join('');

  setBadge('badge-nearby', state.nearby.length);
}

// ── Nearby feed scroll buttons ──────────────────────────────────────────────
// A small touchscreen's drag-to-scroll can be unreliable, so give a
// tap-friendly up/down fallback that pages the feed by ~80% of its height.
(() => {
  const feed = document.getElementById('nearby-feed');
  const up = document.getElementById('nearby-scroll-up');
  const down = document.getElementById('nearby-scroll-down');
  if (!feed || !up || !down) return;
  up.addEventListener('click', () => {
    feed.scrollBy({ top: -feed.clientHeight * 0.8, behavior: 'smooth' });
  });
  down.addEventListener('click', () => {
    feed.scrollBy({ top: feed.clientHeight * 0.8, behavior: 'smooth' });
  });
})();

// Search filters
['wifi', 'aircraft', 'ais'].forEach(tab => {
  const el = document.getElementById(`${tab}-search`);
  if (el) el.addEventListener('input', () => window[`render${tab[0].toUpperCase()}${tab.slice(1)}`]());
});

// Clear buttons
document.getElementById('wifi-clear').addEventListener('click', () => {
  state.wifi = []; renderWifi(); setBadge('badge-wifi', 0);
});
document.getElementById('aircraft-clear').addEventListener('click', () => {
  state.aircraft = []; renderAircraft(); setBadge('badge-aircraft', 0);
});
document.getElementById('ais-clear').addEventListener('click', () => {
  state.ais = []; renderAis(); setBadge('badge-ais', 0);
});
document.getElementById('alerts-clear').addEventListener('click', () => {
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
    // shows as "off", not healthy. A missing modules_active key is treated as
    // active (back-compat). BLE has no separate sensor_health flip, so absence => ok.
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
    gpsHasFix = !!d.gps_fix;
    if (surveyEnabled) renderPatrol();  // reflect a fix gained/lost on the patrol bar
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
    { url: '/api/wifi',     key: 'wifi',     render: renderWifi,     badge: 'badge-wifi' },
    { url: '/api/aircraft', key: 'aircraft', render: renderAircraft, badge: 'badge-aircraft' },
    { url: '/api/ais',      key: 'ais',      render: renderAis,      badge: 'badge-ais' },
    { url: '/api/alerts',   key: 'alerts',   render: renderAlerts,   badge: 'badge-alerts' },
    { url: '/api/nearby',   key: 'nearby',   render: renderNearby,   badge: 'badge-nearby' },
  ];
  for (const ep of endpoints) {
    try {
      const r = await fetch(ep.url);
      if (!r.ok) continue;
      const items = await r.json();
      state[ep.key] = items;
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
    // Re-seed from REST on every (re)connect. The stream drops on a server
    // restart or a network/proxy blip; without this the mobile client resumes
    // SSE but misses everything that happened during the gap — nearby devices
    // seen while disconnected are gone until a manual refresh. Backfill on
    // every reconnect so the Nearby tab reflects current state after any gap.
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
      renderWifi();
      renderNearby(); // alert-tier accents on nearby cards may have changed
    } else if (type === 'aircraft') {
      // Deduplicate by ICAO — a moving plane is re-pushed as its track advances.
      const idx = state.aircraft.findIndex(e => e.icao === data.icao);
      if (idx >= 0) state.aircraft[idx] = data; else state.aircraft.push(data);
      setBadge('badge-aircraft', state.aircraft.length);
      renderAircraft();
    } else if (type === 'ais') {
      const idx = state.ais.findIndex(e => e.mmsi === data.mmsi);
      if (idx >= 0) state.ais[idx] = data; else state.ais.push(data);
      renderAis();
    } else if (type === 'alert') {
      state.alerts.push(data);
      setBadge('badge-alerts', state.alerts.length);
      renderAlerts();
    } else if (type === 'nearby') {
      const idx = state.nearby.findIndex(e => e.mac === data.mac);
      if (idx >= 0) state.nearby[idx] = data; else state.nearby.push(data);
      renderNearby();
    } else if (type === 'survey') {
      // A tasking was issued, findings offloaded, or a patrol toggled — refetch.
      if (surveyEnabled) loadSurvey();
      if (data && data.kind === 'patrol') loadPatrol();
    }
  };

  es.onerror = function() {
    // Reconnect after 5 s
    es.close();
    setTimeout(connectSSE, 5000);
  };
}

connectSSE();

// Safety-net resync: periodically re-seed every tab from REST so the view
// reflects current state even when SSE is gated (a device updating within the
// same alert level is not re-pushed) or the stream silently stalls without
// firing onerror. Together with the onopen re-seed this keeps the Nearby feed
// a live mirror rather than a freeze-on-reconnect snapshot.
setInterval(seedFromRest, 15000);

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

// ── Recon-pair survey + patrol (design §5.5 / §10) ───────────────────────────
// The fixed node issues taskings; this mobile node runs the patrol and shows where
// each tasked contact bed down. No map here (the mobile GUI is map-less), so this is
// the list view plus the Start/End patrol control. Token-gated like /api/mode: the
// dashboard carries ?token=<GUI_TOKEN>, reused for the control POST. Inert until
// /api/survey answers (SURVEY_ENABLED + a GUI_TOKEN on this node).
function surveyUrl(path) {
  return MODE_TOKEN ? `${path}?token=${encodeURIComponent(MODE_TOKEN)}` : path;
}

function fmtDist(m) {
  if (m == null) return '';
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}

function fmtDwell(secs) {
  secs = Math.max(0, Math.floor(secs || 0));
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${secs}s`;
}

function fmtElapsed(iso) {
  const t0 = iso ? Date.parse(iso) : NaN;
  if (Number.isNaN(t0)) return '';
  const s = Math.max(0, Math.floor((Date.now() - t0) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

const _SURVEY_OUTCOME = {
  resident:     { label: 'RESIDENT', cls: 'survey-out-resident', hint: 'Home AP found in the local area' },
  seen:         { label: 'SEEN — HOME ELSEWHERE', cls: 'survey-out-seen', hint: 'Device seen locally but its home network was not — a WiGLE candidate' },
  not_located:  { label: 'NOT LOCATED', cls: 'survey-out-absent', hint: 'Not found in the local wardrive — a WiGLE candidate' },
};

function renderSurvey() {
  const list = document.getElementById('survey-list');
  if (!list) return;
  setBadge('badge-survey', state.survey.length);
  if (!state.survey.length) {
    list.innerHTML = '<p class="survey-empty">No survey taskings yet. The fixed node dispatches targets here; start a patrol to map the area meanwhile.</p>';
    return;
  }
  list.innerHTML = state.survey.map(t => {
    const statusCls = `survey-status-${(t.status || 'open')}`;
    const out = _SURVEY_OUTCOME[t.outcome] || null;
    const ap = t.home_ap;
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
    const clusters = t.clusters || [];
    const rows = clusters.map(f => `
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
}

async function loadSurvey() {
  try {
    const r = await fetch(surveyUrl('/api/survey'));
    if (!r.ok) return surveyEnabled;
    const data = await r.json();
    state.survey = Array.isArray(data) ? data : [];
    if (!surveyEnabled) {
      // First successful answer — reveal the tab and start tracking the patrol.
      surveyEnabled = true;
      document.getElementById('tabbtn-survey')?.removeAttribute('hidden');
      loadPatrol();
    }
    renderSurvey();
    return surveyEnabled;
  } catch { return surveyEnabled; }
}

function renderPatrol() {
  const btn = document.getElementById('patrol-toggle');
  const label = document.getElementById('patrol-state');
  if (!btn || !label) return;
  btn.removeAttribute('hidden');
  btn.textContent = patrolActive ? 'End patrol' : 'Start patrol';
  btn.classList.toggle('patrol-on', patrolActive);
  const banked = (patrolWardriveAps != null)
    ? ` · ${patrolWardriveAps} APs wardriven` : '';
  // A patrol with no GPS fix banks zero APs (design §11) — call it out in the field
  // so the operator isn't left wondering why the count never moves.
  const noGps = (patrolActive && !gpsHasFix)
    ? ' · ⚠ NO GPS FIX — not banking APs' : '';
  label.textContent = patrolActive
    ? `● Patrol running — ${fmtElapsed(patrolStartedAt)} · tasks held open${banked}${noGps}`
    : `No patrol running — tasks close on the poll quota${banked}`;
  label.classList.toggle('patrol-on', patrolActive);
  label.classList.toggle('patrol-nogps', !!noGps);
}

async function loadPatrol() {
  try {
    const r = await fetch(surveyUrl('/api/patrol'));
    if (!r.ok) return;  // feature off / no token
    const s = await r.json();
    patrolActive = !!s.active;
    patrolStartedAt = s.started_at || null;
    patrolWardriveAps = (s.wardrive_aps != null) ? s.wardrive_aps : null;
    renderPatrol();
  } catch { /* an unreachable node is the normal field state */ }
}

async function togglePatrol() {
  const action = patrolActive ? 'end' : 'start';
  if (action === 'end'
      && !confirm('End the patrol? This finalizes every open tasking.')) return;
  try {
    const r = await fetch(surveyUrl('/api/patrol'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    if (r.ok) { await loadPatrol(); loadSurvey(); }
    else {
      const e = await r.json().catch(() => ({}));
      alert(`Patrol ${action} failed: ${e.error || r.status}`);
    }
  } catch { alert('Could not reach the node to change the patrol.'); }
}

document.getElementById('patrol-toggle')?.addEventListener('click', togglePatrol);
document.getElementById('survey-refresh')?.addEventListener('click', loadSurvey);

// Keep the running-elapsed label ticking without hammering the endpoint.
setInterval(() => { if (patrolActive) renderPatrol(); }, 30000);
// Refresh survey + patrol on the same slow cadence once the feature is live.
setInterval(() => { if (surveyEnabled) { loadSurvey(); loadPatrol(); } }, 15000);

// Probe once on load; if survey is enabled this reveals the tab, else it stays inert.
loadSurvey();
