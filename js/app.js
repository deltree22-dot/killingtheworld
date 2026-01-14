/* global L */
const map = L.map('map').setView([20, 0], 2);

// IMPORTANT: OSM tile server is community-funded and has a usage policy.
// For low-traffic personal sites, this is usually ok. If traffic grows, switch to a proper tile provider.
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

async function load() {
  const res = await fetch('data/conflicts.geojson', { cache: 'no-store' });
  if (!res.ok) throw new Error(`Konnte GeoJSON nicht laden: ${res.status}`);
  const geo = await res.json();

  const layer = L.geoJSON(geo, {
    pointToLayer: (feature, latlng) => L.circleMarker(latlng, { radius: 6 }),
    onEachFeature: (feature, l) => {
      const p = feature.properties || {};
      const sources = (p.sources || []).map(u => `<div><a href="${esc(u)}" target="_blank" rel="noopener">Quelle</a></div>`).join('');
      const badge = p.status ? `<span class="badge">${esc(p.status)}</span>` : '';
      const html = `
        <div class="popup">
          <h3>${esc(p.title || 'Ohne Titel')}</h3>
          ${badge}
          ${p.date ? `<p><strong>Datum:</strong> ${esc(p.date)}</p>` : ''}
          ${p.summary ? `<p>${esc(p.summary)}</p>` : ''}
          ${p.why_it_matters ? `<p><em>${esc(p.why_it_matters)}</em></p>` : ''}
          ${sources ? `<p><strong>Quellen</strong>${sources}</p>` : ''}
        </div>`;
      l.bindPopup(html);
    }
  }).addTo(map);

  if (layer.getBounds().isValid()) map.fitBounds(layer.getBounds().pad(0.2));
}

load().catch(err => {
  console.error(err);
  alert('Fehler beim Laden der Daten. Details in der Konsole.');
});
