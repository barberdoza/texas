(function () {
  "use strict";

  const CATEGORY_ORDER = ["BARBER", "SALON", "UNSPLIT", "UNKNOWN"];

  const state = {
    data: null,
    selectedCounty: null, // null = statewide
    countyCentroids: null,
    map: null,
    clusterLayer: null,
  };

  const els = {
    search: document.getElementById("county-search"),
    countyList: document.getElementById("county-list"),
    finderHint: document.getElementById("finder-hint"),
    summary: document.getElementById("summary"),
    rankTitle: document.getElementById("rank-title"),
    rankSub: document.getElementById("rank-sub"),
    rankChart: document.getElementById("rank-chart"),
    tableBody: document.getElementById("data-table-body"),
    sampleBanner: document.getElementById("sample-banner"),
    unclassifiedBanner: document.getElementById("unclassified-banner"),
    unclassifiedText: document.getElementById("unclassified-text"),
    sourceLabel: document.getElementById("source-label"),
    updatedLabel: document.getElementById("updated-label"),
    geocodeLabel: document.getElementById("geocode-label"),
  };

  function fmtNumber(n) {
    if (n === null || n === undefined) return "—";
    return n.toLocaleString("en-US");
  }

  function findRollup(countyName) {
    const q = countyName.trim().toLowerCase();
    if (!q) return null;
    return (
      state.data.rollup.find((r) => r.county.toLowerCase() === q) ||
      state.data.rollup.find((r) => r.county.toLowerCase().startsWith(q)) ||
      null
    );
  }

  function statewideTotals() {
    const totals = { total: 0 };
    CATEGORY_ORDER.forEach((code) => (totals[code] = 0));
    state.data.rollup.forEach((r) => {
      totals.total += r.total;
      CATEGORY_ORDER.forEach((code) => {
        totals[code] += r[code] || 0;
      });
    });
    return totals;
  }

  function renderSummary() {
    const sel = state.selectedCounty ? findRollup(state.selectedCounty) : null;
    const title = sel ? `${sel.county} County` : "Texas (all counties)";
    const totals = sel || statewideTotals();

    let rankBadge = "";
    if (sel) {
      const sorted = [...state.data.rollup].sort((a, b) => b.total - a.total);
      const rank = sorted.findIndex((r) => r.county === sel.county) + 1;
      rankBadge = `<span class="rank-badge">#${rank} of ${sorted.length} counties by total shops</span>`;
    }

    const boards = CATEGORY_ORDER
      .filter((code) => (totals[code] || 0) > 0 || !sel) // hide zero categories for a selected county, show all statewide
      .map((code) => `
        <div class="board">
          <h3>${state.data.categories[code] || code}</h3>
          <dl>
            <div class="row"><dt>Active licensed shops</dt><dd>${fmtNumber(totals[code] || 0)}</dd></div>
          </dl>
        </div>`)
      .join("");

    els.summary.innerHTML = `
      <div class="detail-heading">
        <h2>${title}</h2>
        ${rankBadge}
      </div>
      <p class="summary-total">
        <span class="summary-total-value">${fmtNumber(totals.total)}</span>
        total active licensed shops
      </p>
      <div class="board-grid">${boards}</div>
    `;
  }

  function renderRankChart() {
    const ranked = [...state.data.rollup].sort((a, b) => b.total - a.total);
    const max = ranked[0] ? ranked[0].total : 1;
    const showCount = 15;
    let list = ranked.slice(0, showCount);

    if (state.selectedCounty) {
      const sel = findRollup(state.selectedCounty);
      if (sel && !list.find((r) => r.county === sel.county)) list = list.concat([sel]);
    }

    els.rankChart.innerHTML = list
      .map((r) => {
        const pct = Math.max(2, (r.total / max) * 100);
        const isCurrent = state.selectedCounty && r.county.toLowerCase() === state.selectedCounty.trim().toLowerCase();
        return `
          <div class="rank-row${isCurrent ? " is-current" : ""}">
            <div class="rank-name">${r.county}</div>
            <div class="rank-bar-track"><div class="rank-bar-fill" style="width:${pct}%"></div></div>
            <div class="rank-value">${fmtNumber(r.total)}</div>
          </div>`;
      })
      .join("");

    if (ranked.length > showCount) {
      const more = document.createElement("p");
      more.className = "rank-more";
      more.textContent = `Showing top ${showCount} of ${ranked.length} counties. Full list in the table below.`;
      els.rankChart.appendChild(more);
    }
  }

  function renderTable() {
    const query = els.search.value.trim().toLowerCase();
    const rows = state.data.rollup
      .filter((r) => !query || r.county.toLowerCase().includes(query))
      .map((r) => {
        const isCurrent = state.selectedCounty && r.county.toLowerCase() === state.selectedCounty.trim().toLowerCase();
        return `
          <tr class="${isCurrent ? "is-current-row" : ""}" data-county="${r.county}">
            <td>${r.county}</td>
            <td>${fmtNumber(r.BARBER || 0)}</td>
            <td>${fmtNumber(r.SALON || 0)}</td>
            <td>${fmtNumber(r.UNSPLIT || 0)}</td>
            <td class="td-total">${fmtNumber(r.total)}</td>
          </tr>`;
      })
      .join("");

    els.tableBody.innerHTML = rows || `<tr><td colspan="5">No counties match "${els.search.value}".</td></tr>`;
  }

  function computeCountyCentroids() {
    const sums = {};
    state.data.shops.forEach(([, , , , , county, , lat, lon]) => {
      if (lat == null || lon == null) return;
      const b = sums[county] || (sums[county] = { lat: 0, lon: 0, n: 0 });
      b.lat += lat;
      b.lon += lon;
      b.n += 1;
    });
    const out = {};
    Object.entries(sums).forEach(([county, b]) => {
      out[county] = { lat: b.lat / b.n, lon: b.lon / b.n };
    });
    return out;
  }

  function escapeHtml(str) {
    return String(str || "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function buildMap() {
    state.map = L.map("map", { scrollWheelZoom: true }).setView([31.0, -99.5], 6);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 18,
    }).addTo(state.map);

    state.clusterLayer = L.markerClusterGroup({ maxClusterRadius: 50 });

    state.data.shops.forEach(([name, category, subtype, address, city, county, zip, lat, lon]) => {
      if (lat == null || lon == null) return;
      const marker = L.marker([lat, lon]);
      const catLabel = state.data.categories[category] || category;
      marker.bindPopup(
        `<div class="shop-popup"><strong>${escapeHtml(name)}</strong><br />
         <span class="shop-popup-cat">${escapeHtml(catLabel)}</span><br />
         ${escapeHtml(address)}, ${escapeHtml(city)}, ${escapeHtml(county)} County ${escapeHtml(zip)}</div>`
      );
      state.clusterLayer.addLayer(marker);
    });

    state.map.addLayer(state.clusterLayer);
  }

  function flyToCounty(countyName) {
    if (!state.map) return;
    const centroid = state.countyCentroids[countyName];
    if (centroid) {
      state.map.flyTo([centroid.lat, centroid.lon], 9, { duration: 0.8 });
    }
  }

  function renderAll() {
    renderSummary();
    renderRankChart();
    renderTable();
  }

  function selectFromSearch() {
    const q = els.search.value.trim();
    const match = q ? findRollup(q) : null;
    state.selectedCounty = match ? match.county : null;
    els.finderHint.textContent = state.selectedCounty
      ? `Showing ${state.selectedCounty} County. Clear the search to see statewide totals.`
      : "Showing statewide totals until you pick a county.";
    renderAll();
    if (match) flyToCounty(match.county);
  }

  function init(data) {
    state.data = data;

    if (data.is_sample) {
      els.sampleBanner.hidden = false;
    }

    const unknownTypes = Object.keys(data.unclassified_license_types || {});
    if (unknownTypes.length) {
      els.unclassifiedBanner.hidden = false;
      const totalUnknown = Object.values(data.unclassified_license_types).reduce((a, b) => a + b, 0);
      els.unclassifiedText.textContent =
        `Heads up: ${totalUnknown.toLocaleString()} records had a license type this app didn't recognize ` +
        `(${unknownTypes.slice(0, 5).join(", ")}${unknownTypes.length > 5 ? ", …" : ""}) — ` +
        `they're counted under "Unclassified" rather than dropped.`;
    }

    els.sourceLabel.textContent = data.source;
    els.updatedLabel.textContent = new Date(data.generated_at).toLocaleDateString("en-US", {
      year: "numeric", month: "long", day: "numeric",
    });
    const gc = data.geocode_coverage || {};
    els.geocodeLabel.textContent = gc.total
      ? `${fmtNumber(gc.geocoded)} of ${fmtNumber(gc.total)} shops mapped (${Math.round((gc.geocoded / gc.total) * 100)}%)`
      : "—";

    els.countyList.innerHTML = data.rollup.map((r) => `<option value="${r.county}"></option>`).join("");

    state.countyCentroids = computeCountyCentroids();

    els.search.addEventListener("input", selectFromSearch);

    els.tableBody.addEventListener("click", (e) => {
      const row = e.target.closest("tr[data-county]");
      if (!row) return;
      els.search.value = row.dataset.county;
      selectFromSearch();
    });

    buildMap();
    renderAll();
  }

  fetch("data/tx_shops.json")
    .then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(init)
    .catch((err) => {
      els.summary.innerHTML = `<p class="muted">Couldn't load data/tx_shops.json (${err.message}). If you're running this locally, serve the folder with a local server (e.g. <code>python3 -m http.server</code>) rather than opening the file directly.</p>`;
    });
})();
