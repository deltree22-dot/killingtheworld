/* global L */
const map = L.map('map').setView([20, 0], 2);

// IMPORTANT: Tile providers have usage policies. If traffic grows, switch to a proper plan/provider.
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  subdomains: 'abcd',
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors &copy; CARTO'
}).addTo(map);


const els = {
  countrySelect: document.getElementById('countrySelect'),
  conflictList: document.getElementById('conflictList'),
  summaryLine: document.getElementById('summaryLine'),
  emptyState: document.getElementById('emptyState')
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function statusKey(s) {
  return String(s || '').trim().toLowerCase();
}
function badge(text) {
  if (!text) return '';
  const k = statusKey(text);
  // Map German terms to CSS classes
  const cls =
    (k === 'aktiv') ? 'badge--aktiv' :
    (k === 'eskalierend') ? 'badge--eskalierend' :
    (k === 'fruehwarnung' || k === 'frühwarnung') ? 'badge--fruehwarnung' :
    (k === 'deeskalierend') ? 'badge--deeskalierend' :
    'badge--unbekannt';
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}

let allFeatures = [];
let currentLayer = null;
let markerByKey = new Map(); // key -> Leaflet layer

function markerStyle(status) {
  const k = statusKey(status);
  // colors match CSS variables; hardcoded here for Leaflet vector styling
  const color =
    (k === 'aktiv') ? '#ff4d4d' :
    (k === 'eskalierend') ? '#ff9f2e' :
    (k === 'fruehwarnung' || k === 'frühwarnung') ? '#ffd24d' :
    (k === 'deeskalierend') ? '#4dd58a' :
    '#9aa6b2';
  return { radius: 6, weight: 2, color, fillColor: color, fillOpacity: 0.55 };
}

function featureKey(f, idx) {
  const p = f.properties || {};
  return p.id || p.url || `${p.title || 'item'}|${p.date || ''}|${p.country || ''}|${idx}`;
}

function buildCountries(features) {
  const set = new Set();
  for (const f of features) {
    const c = (f.properties && f.properties.country) ? String(f.properties.country).trim() : '';
    if (c) set.add(c);
  }
  return Array.from(set).sort((a, b) => a.localeCompare(b, 'de'));
}

function populateCountryDropdown(countries) {
  // keep first option
  while (els.countrySelect.options.length > 1) els.countrySelect.remove(1);
  for (const c of countries) {
    const opt = document.createElement('option');
    opt.value = c;
    opt.textContent = c;
    els.countrySelect.appendChild(opt);
  }
}

function sortFeatures(features) {
  // newest first (ISO date string)
  return [...features].sort((a, b) => {
    const da = (a.properties && a.properties.date) ? String(a.properties.date) : '';
    const db = (b.properties && b.properties.date) ? String(b.properties.date) : '';
    if (da === db) return 0;
    return da < db ? 1 : -1;
  });
}

function renderList(features) {
  els.conflictList.innerHTML = '';
  const sorted = sortFeatures(features);

  if (!sorted.length) {
    els.emptyState.style.display = 'block';
    return;
  }
  els.emptyState.style.display = 'none';

  for (let i = 0; i < sorted.length; i++) {
    const f = sorted[i];
    const p = f.properties || {};
    const key = featureKey(f, i);

    const li = document.createElement('li');
    li.className = 'item';
    li.dataset.key = key;

    li.innerHTML = `
      <div class="top">
        <div class="title">${esc(p.title || 'Ohne Titel')}</div>
        ${badge(p.status)}
      </div>
      <div class="meta">
        ${p.date ? `<div><strong>Datum:</strong> ${esc(p.date)}</div>` : ''}
        ${p.country ? `<div><strong>Land:</strong> ${esc(p.country)}</div>` : ''}
        ${p.summary ? `<div style="margin-top:6px;">${esc(p.summary)}</div>` : ''}
        ${Array.isArray(p.sources) && p.sources[0] ? `<div style="margin-top:8px;"><a href="${esc(p.sources[0])}" target="_blank" rel="noopener">Quelle oeffnen</a></div>` : ''}
      </div>
    `;

    li.addEventListener('click', () => {
      const m = markerByKey.get(key);
      if (m) {
        // Zoom in a bit and open popup
        map.setView(m.getLatLng ? m.getLatLng() : m.getBounds().getCenter(), Math.max(map.getZoom(), 6), { animate: true });
        if (m.openPopup) m.openPopup();
      }
    });

    els.conflictList.appendChild(li);
  }
}

function rebuildLayer(features) {
  if (currentLayer) {
    currentLayer.remove();
    currentLayer = null;
  }
  markerByKey.clear();

  currentLayer = L.geoJSON(features, {
    pointToLayer: (feature, latlng) => L.circleMarker(latlng, markerStyle(feature.properties && feature.properties.status)),
    onEachFeature: (feature, layer) => {
      const p = feature.properties || {};
      const sources = (p.sources || []).map(u => `<div><a href="${esc(u)}" target="_blank" rel="noopener">Quelle</a></div>`).join('');
      const html = `
        <div class="popup">
          <h3>${esc(p.title || 'Ohne Titel')}</h3>
          ${p.status ? `<div style="margin-bottom:6px;">${badge(p.status)}</div>` : ''}
          ${p.date ? `<p><strong>Datum:</strong> ${esc(p.date)}</p>` : ''}
          ${p.country ? `<p><strong>Land:</strong> ${esc(p.country)}</p>` : ''}
          ${p.summary ? `<p>${esc(p.summary)}</p>` : ''}
          ${p.why_it_matters ? `<p><em>${esc(p.why_it_matters)}</em></p>` : ''}
          ${sources ? `<p><strong>Quellen</strong>${sources}</p>` : ''}
        </div>`;
      layer.bindPopup(html);

      // store marker reference for list interactions
      const idx = markerByKey.size;
      const key = featureKey(feature, idx);
      markerByKey.set(key, layer);
    }
  }).addTo(map);

  // Zoom to results
  try {
    const b = currentLayer.getBounds();
    if (b && b.isValid()) map.fitBounds(b.pad(0.2));
  } catch (e) {
    // ignore
  }
}

function applyFilter() {
  const selected = els.countrySelect.value;
  const filtered = selected
    ? allFeatures.filter(f => (f.properties && String(f.properties.country || '').trim() === selected))
    : allFeatures;

  els.summaryLine.textContent = selected
    ? `${filtered.length} Eintraege fuer ${selected}`
    : `${filtered.length} Eintraege (alle Laender)`;

  // Rebuild map and list with the same filtered features
  rebuildLayer(filtered);
  renderList(filtered);
}

async function load() {
  const res = await fetch('data/conflicts.geojson', { cache: 'no-store' });
  if (!res.ok) throw new Error(`Konnte GeoJSON nicht laden: ${res.status}`);
  const geo = await res.json();

  allFeatures = Array.isArray(geo.features) ? geo.features : [];
  populateCountryDropdown(buildCountries(allFeatures));

  els.countrySelect.addEventListener('change', applyFilter);

  applyFilter();
}

load().catch(err => {
  console.error(err);
  els.summaryLine.textContent = 'Fehler beim Laden der Daten. Details in der Konsole.';
  els.emptyState.style.display = 'block';
});
