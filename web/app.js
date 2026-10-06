/* Fleet Route Optimizer dashboard */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const S = {
    plan: null,
    planView: "opt",
    selected: null,
    detailCache: {},
    charts: {},
    uncFilter: { reason: "", q: "", trailer: "", priority: "", limit: 100 },
    polling: null,
  };

  // ------------------------------------------------------------------ formatting
  const nf = new Intl.NumberFormat("en-US");
  const num = (v) => nf.format(Math.round(v));
  const money = (v) => (v < 0 ? "−$" : "$") + nf.format(Math.round(Math.abs(v)));
  const moneyShort = (v) => {
    const a = Math.abs(v), s = v < 0 ? "−$" : "$";
    if (a >= 1e6) return s + (a / 1e6).toFixed(2) + "M";
    if (a >= 1e4) return s + Math.round(a / 1e3) + "K";
    return s + nf.format(Math.round(a));
  };
  const pct = (v, d = 1) => v.toFixed(d) + "%";
  const trailerName = (t) => ({ dry_van: "Dry van", reefer: "Reefer", flatbed: "Flatbed" }[t] || t);
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  let H0 = new Date(2026, 9, 12);
  const at = (m) => new Date(H0.getTime() + m * 60000);
  const hhmm = (d) => String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  const when = (m) => { const d = at(m); return `${DAYS[d.getDay()]} ${hhmm(d)}`; };
  const dayLabel = (m) => { const d = at(m); return `${DAYS[d.getDay()]} ${MONTHS[d.getMonth()]} ${d.getDate()}`; };
  const dur = (min) => {
    const h = Math.floor(min / 60), m = Math.round(min % 60);
    if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
    return h ? `${h}h${m ? " " + m + "m" : ""}` : `${m}m`;
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ------------------------------------------------------------------ tooltip
  const tip = $("#tooltip");
  function showTip(html, ev) {
    tip.innerHTML = html;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  // ------------------------------------------------------------------ theme
  function currentTheme() {
    const t = document.documentElement.dataset.theme;
    if (t) return t;
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  try {
    const saved = localStorage.getItem("fro-theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch (e) { /* storage unavailable */ }
  $("#themeBtn").addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("fro-theme", next); } catch (e) { /* ignore */ }
    onThemeChange();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", onThemeChange);
  function onThemeChange() {
    setTiles();
    if (S.plan) {
      renderCharts();
      drawOverviewLanes();
      if (S.selected && S.detailCache[S.selected]) drawTruckMap(S.detailCache[S.selected]);
      if (S.selected && S.detailCache[S.selected]) renderGantt(S.detailCache[S.selected]);
    }
  }

  // ------------------------------------------------------------------ maps
  const tileUrl = () => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${currentTheme() === "dark" ? "Dark" : "Light"}_Gray_Base/MapServer/tile/{z}/{y}/{x}`;
  const attribution = "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors";
  const maps = {};
  function makeMap(id) {
    const m = L.map(id, { zoomSnap: 0.25, preferCanvas: true, worldCopyJump: false }).setView([36.5, -94], 4.25);
    m._tiles = L.tileLayer(tileUrl(), { attribution, maxZoom: 12 }).addTo(m);
    m._layer = L.layerGroup().addTo(m);
    return m;
  }
  function setTiles() {
    Object.values(maps).forEach((m) => m._tiles.setUrl(tileUrl()));
  }

  // quadratic bezier so A->B and B->A lanes don't sit on top of each other
  function curve(a, b, bend = 0.12, n = 18) {
    const [y1, x1] = a, [y2, x2] = b;
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const dx = x2 - x1, dy = y2 - y1;
    const cx = mx - dy * bend, cy = my + dx * bend;
    const pts = [];
    for (let i = 0; i <= n; i++) {
      const t = i / n, u = 1 - t;
      pts.push([u * u * y1 + 2 * u * t * cy + t * t * y2, u * u * x1 + 2 * u * t * cx + t * t * x2]);
    }
    return pts;
  }

  function drawOverviewLanes() {
    const m = maps.overview;
    if (!m || !S.plan) return;
    m._layer.clearLayers();
    const P = S.plan;
    const lanes = S.planView === "opt" ? P.lanes : P.baseline_lanes;
    const showL = $("#showLoaded").checked, showE = $("#showEmpty").checked;
    const cLoaded = css("--series-opt"), cEmpty = css("--series-2");
    const maxL = Math.max(1, ...lanes.filter((l) => l.loaded).map((l) => l.count));
    const maxE = Math.max(1, ...lanes.filter((l) => !l.loaded).map((l) => l.count));
    // loaded lanes first, empty (dashed) on top so repositioning moves stay visible
    const sorted = [...lanes].sort((a, b) => (b.loaded - a.loaded) || (a.count - b.count));
    for (const ln of sorted) {
      if ((ln.loaded && !showL) || (!ln.loaded && !showE)) continue;
      const a = P.cities[ln.from], b = P.cities[ln.to];
      const r = Math.sqrt(ln.count / (ln.loaded ? maxL : maxE));
      const op = 0.18 + 0.67 * r;
      const line = L.polyline(curve([a.lat, a.lon], [b.lat, b.lon]), {
        color: ln.loaded ? cLoaded : cEmpty, weight: ln.loaded ? 0.75 + 6 * r : 1.5 + 4 * r, opacity: op,
        dashArray: ln.loaded ? null : "5 6", lineCap: "round",
      });
      line.bindTooltip(
        `<b>${esc(a.name)} → ${esc(b.name)}</b><br>${num(ln.count)} ${ln.loaded ? "loaded trips" : "empty repositioning trips"}<br>${num(ln.miles)} miles total`,
        { sticky: true });
      line.on("mouseover", () => line.setStyle({ opacity: 1 }));
      line.on("mouseout", () => line.setStyle({ opacity: op }));
      m._layer.addLayer(line);
    }
    if (!m._fitted) {
      m.invalidateSize();
      m.fitBounds(L.latLngBounds(P.cities.map((c) => [c.lat, c.lon])).pad(0.06));
      m._fitted = true;
    }
    // home bases (terminals) with truck counts
    const homes = {};
    for (const t of P.trucks) homes[t.home] = (homes[t.home] || 0) + 1;
    for (const [i, n] of Object.entries(homes)) {
      const l = P.locations[i];
      const c = L.circleMarker([l.lat, l.lon], {
        radius: 2.5 + Math.sqrt(n) * 0.6, color: css("--surface"), weight: 1.5, fillColor: css("--ink"), fillOpacity: 0.9,
      });
      c.bindTooltip(`<b>${esc(l.name)}</b><br>Home base for ${n} trucks`);
      m._layer.addLayer(c);
    }
  }

  function drawTruckMap(d) {
    const m = maps.truck;
    if (!m) return;
    m._layer.clearLayers();
    const P = S.plan;
    const locs = P.locations;
    const home = locs[d.home];
    const pts = [[home.lat, home.lon]];
    const cLoaded = css("--series-opt"), cEmpty = css("--series-2");
    d.leg_list.forEach((lg) => {
      const a = locs[lg.from], b = locs[lg.to];
      const line = L.polyline(curve([a.lat, a.lon], [b.lat, b.lon], 0.08), {
        color: lg.loaded ? cLoaded : cEmpty, weight: lg.loaded ? 4.5 : 3.5, opacity: 0.9,
        dashArray: lg.loaded ? null : "7 7", lineCap: "round",
      });
      line.bindTooltip(`<b>${lg.loaded ? "Hauling " + esc(lg.load_id) : "Driving empty"}</b><br>${esc(a.name)} → ${esc(b.name)}<br>${num(lg.miles)} mi · ${when(lg.depart)} → ${when(lg.arrive)}`, { sticky: true });
      m._layer.addLayer(line);
      pts.push([b.lat, b.lon]);
    });
    const loadLeg = {};
    for (const lg of d.leg_list) if (lg.loaded) loadLeg[lg.load_id] = lg;
    d.stops.forEach((s, i) => {
      const lo = locs[loadLeg[s.load_id].from], ld = locs[loadLeg[s.load_id].to];
      const pick = L.marker([lo.lat, lo.lon], {
        icon: L.divIcon({ className: "", html: `<div class="stop-marker" style="width:22px;height:22px">${i + 1}</div>`, iconSize: [22, 22], iconAnchor: [11, 11] }),
        zIndexOffset: 500,
      }).bindTooltip(`<b>Stop ${i + 1}: pick up ${esc(s.load_id)}</b><br>${esc(s.origin)}<br>${when(s.pickup_at)}`);
      const drop = L.circleMarker([ld.lat, ld.lon], { radius: 5, color: css("--surface"), weight: 2, fillColor: cLoaded, fillOpacity: 1 })
        .bindTooltip(`<b>Deliver ${esc(s.load_id)}</b><br>${esc(s.dest)}<br>${when(s.delivered_at)}`);
      m._layer.addLayer(drop);
      m._layer.addLayer(pick);
    });
    m._layer.addLayer(L.marker([home.lat, home.lon], {
      icon: L.divIcon({ className: "", html: `<div class="home-marker" style="width:26px;height:26px">⌂</div>`, iconSize: [26, 26], iconAnchor: [13, 13] }),
      zIndexOffset: 1000,
    }).bindTooltip(`<b>Home base</b><br>${esc(d.home_name)}`));
    m.invalidateSize();
    if (pts.length > 1) m.fitBounds(L.latLngBounds(pts).pad(0.15), { maxZoom: 8 });
    else m.setView(pts[0], 7);
  }

  // ------------------------------------------------------------------ KPIs
  function renderKpis() {
    const k = S.plan.kpis, b = S.plan.baseline;
    const delta = (cur, base, lowerBetter, fmt, unit = "") => {
      const d = cur - base;
      if (Math.abs(d) < 1e-9) return `<span class="delta">same</span>`;
      const good = lowerBetter ? d < 0 : d > 0;
      return `<span class="delta ${good ? "good" : "bad"}">${d < 0 ? "▼" : "▲"} ${fmt(Math.abs(d))}${unit}</span>`;
    };
    const tiles = [
      {
        hero: true, label: "Empty miles", value: pct(k.empty_pct),
        cmp: `${delta(k.empty_pct, b.empty_pct, true, (v) => v.toFixed(1), " pts")} vs naive dispatch (${pct(b.empty_pct)})`,
        help: `${num(k.empty_miles)} of ${num(k.miles)} miles are driven with an empty trailer. This is the main thing the optimizer reduces.`,
      },
      {
        label: "Loads delivered", value: `${num(k.loads_served)} <small style="font-size:14px;color:var(--ink-3);font-weight:550">of ${num(k.loads_total)}</small>`,
        cmp: `${delta(k.loads_served, b.loads_served, false, num)} vs naive (${num(b.loads_served)})`,
        help: "The fleet can't physically haul every load this week. See Uncovered loads for why.",
      },
      {
        label: "On-time deliveries", value: `${k.on_time_pct >= 99.95 ? '<span class="check">✓</span> ' : ""}${pct(k.on_time_pct)}`,
        cmp: `${num(k.late_deliveries)} late · naive ${pct(b.on_time_pct)}`,
        help: "Delivered by the customer's deadline.",
      },
      {
        label: "Trucks home on time", value: `${k.home_on_time_pct >= 99.95 ? '<span class="check">✓</span> ' : ""}${pct(k.home_on_time_pct)}`,
        cmp: `every tour ends at its home base`,
        help: "Back at home within the truck's allowed tour length.",
      },
      {
        label: "Cost per loaded mile", value: "$" + k.cost_per_loaded_mile.toFixed(2),
        cmp: `${delta(k.cost_per_loaded_mile, b.cost_per_loaded_mile, true, (v) => "$" + v.toFixed(2))} vs naive ($${b.cost_per_loaded_mile.toFixed(2)})`,
        help: "Total operating cost ÷ miles driven with freight.",
      },
      {
        label: "Operating margin", value: moneyShort(k.margin),
        cmp: `${delta(k.margin, b.margin, false, moneyShort)} vs naive (${moneyShort(b.margin)})`,
        help: `Revenue ${moneyShort(k.revenue)} − operating cost ${moneyShort(k.operating_cost)}.`,
      },
      {
        label: "Trucks dispatched", value: `${num(k.trucks_used)} <small style="font-size:14px;color:var(--ink-3);font-weight:550">of ${num(k.trucks_total)}</small>`,
        cmp: `${k.loads_per_truck.toFixed(1)} loads per truck · naive ${num(b.trucks_used)} trucks`,
        help: "Trucks that leave home this week. The rest stay put rather than chase money-losing loads.",
      },
    ];
    $("#kpis").innerHTML = tiles.map((t) => `
      <div class="kpi${t.hero ? " hero" : ""}">
        <div class="label">${t.label}</div>
        <div class="value">${t.value}</div>
        <div class="cmp">${t.cmp}</div>
        <div class="help">${t.help}</div>
      </div>`).join("");
  }

  // ------------------------------------------------------------------ charts
  function chartDefaults() {
    Chart.defaults.font.family = css("--font") || "system-ui";
    Chart.defaults.font.size = 11.5;
    Chart.defaults.color = css("--ink-3");
    Chart.defaults.borderColor = css("--grid");
    Chart.defaults.plugins.legend.display = false;
    Chart.defaults.plugins.tooltip.backgroundColor = css("--ink");
    Chart.defaults.plugins.tooltip.titleColor = css("--surface");
    Chart.defaults.plugins.tooltip.bodyColor = css("--surface");
    Chart.defaults.plugins.tooltip.padding = 9;
    Chart.defaults.plugins.tooltip.cornerRadius = 7;
    Chart.defaults.maintainAspectRatio = false;
    Chart.defaults.animation = { duration: 300 };
  }

  function renderCharts() {
    chartDefaults();
    Object.values(S.charts).forEach((c) => c.destroy());
    const P = S.plan, C = P.charts;
    const cOpt = css("--series-opt"), cBase = css("--series-base"), c2 = css("--series-2"), c3 = css("--series-3");
    const surf = css("--surface");

    // empty miles by home city
    const ec = C.empty_by_city;
    S.charts.city = new Chart($("#cityChart"), {
      type: "bar",
      data: {
        labels: ec.map((d) => d.city),
        datasets: [
          { label: "Optimized", data: ec.map((d) => d.optimized), backgroundColor: cOpt, borderRadius: 4, borderSkipped: "start", barPercentage: 0.9, categoryPercentage: 0.75 },
          { label: "Naive dispatch", data: ec.map((d) => d.baseline), backgroundColor: cBase, borderRadius: 4, borderSkipped: "start", barPercentage: 0.9, categoryPercentage: 0.75 },
        ],
      },
      options: {
        indexAxis: "y",
        interaction: { mode: "index", axis: "y", intersect: false },
        scales: {
          x: { beginAtZero: true, ticks: { callback: (v) => v + "%" }, grid: { color: css("--grid") }, border: { display: false } },
          y: { grid: { display: false }, ticks: { autoSkip: false, font: { size: 11 } } },
        },
        plugins: { tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${c.parsed.x.toFixed(1)}% empty` } } },
      },
    });

    // fleet over time
    const ft = C.fleet_time, step = ft.step_min;
    const labels = ft.on_road.map((_, i) => i * step);
    const lastIdx = Math.max(...ft.on_road.map((v, i) => (v > 0 ? i : 0)), ...ft.baseline_on_road.map((v, i) => (v > 0 ? i : 0))) + 2;
    const cut = (arr) => arr.slice(0, lastIdx);
    S.charts.time = new Chart($("#timeChart"), {
      type: "line",
      data: {
        labels: cut(labels),
        datasets: [
          { label: "On a tour (optimized)", data: cut(ft.on_road), borderColor: cOpt, backgroundColor: cOpt + "22", fill: true, borderWidth: 2, pointRadius: 0, tension: 0.25 },
          { label: "Hauling a load", data: cut(ft.hauling), borderColor: c3, borderWidth: 2, pointRadius: 0, tension: 0.25 },
          { label: "On a tour (naive)", data: cut(ft.baseline_on_road), borderColor: cBase, borderDash: [5, 4], borderWidth: 2, pointRadius: 0, tension: 0.25 },
        ],
      },
      options: {
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            grid: { color: (c) => (c.tick && c.tick.value % 12 === 0 ? css("--grid") : "transparent") }, border: { display: false },
            ticks: { autoSkip: false, maxRotation: 0, callback: (v, i) => (i % 12 === 0 ? dayLabel(labels[i]).slice(0, 3) + " " + at(labels[i]).getDate() : null) },
          },
          y: { beginAtZero: true, grid: { color: css("--grid") }, border: { display: false } },
        },
        plugins: {
          tooltip: {
            callbacks: {
              title: (items) => when(labels[items[0].dataIndex]) + " – " + hhmm(at(labels[items[0].dataIndex] + step)),
              label: (c) => ` ${c.dataset.label}: ${num(c.parsed.y)} trucks`,
            },
          },
        },
      },
    });

    // loads by trailer
    const bt = C.by_trailer;
    const stackDs = (label, key, color) => ({
      label, data: bt.map((d) => d[key]), backgroundColor: color, borderColor: surf, borderWidth: { right: 2 },
      borderSkipped: false, barPercentage: 0.7,
    });
    S.charts.trailer = new Chart($("#trailerChart"), {
      type: "bar",
      data: {
        labels: bt.map((d) => trailerName(d.trailer)),
        datasets: [stackDs("Delivered", "served", cOpt), stackDs("Uncovered", "uncovered", c2), stackDs("Excluded by settings", "excluded", cBase)],
      },
      options: {
        indexAxis: "y",
        interaction: { mode: "index", axis: "y", intersect: false },
        scales: {
          x: { stacked: true, grid: { color: css("--grid") }, border: { display: false } },
          y: { stacked: true, grid: { display: false } },
        },
        plugins: {
          tooltip: {
            callbacks: {
              label: (c) => {
                const d = bt[c.dataIndex], tot = d.served + d.uncovered + d.excluded;
                return ` ${c.dataset.label}: ${num(c.parsed.x)} (${((100 * c.parsed.x) / tot).toFixed(0)}%)`;
              },
            },
          },
        },
      },
    });
  }

  // ------------------------------------------------------------------ trucks tab
  function populateFilters() {
    const P = S.plan;
    const trailers = [...new Set(P.trucks.map((t) => t.trailer))].sort();
    const cities = [...new Set(P.trucks.map((t) => t.home_city))].sort();
    const fill = (sel, vals, label) => {
      const cur = sel.value;
      sel.innerHTML = `<option value="">${label}</option>` + vals.map((v) => `<option value="${esc(v)}">${esc(trailerName(v))}</option>`).join("");
      sel.value = vals.includes(cur) ? cur : "";
    };
    fill($("#fTrailer"), trailers, "All trailers");
    fill($("#uTrailer"), trailers, "All trailers");
    fill($("#fCity"), cities, "All home bases");
  }

  function renderTruckList() {
    const P = S.plan;
    const q = $("#truckSearch").value.trim().toLowerCase();
    const ft = $("#fTrailer").value, fc = $("#fCity").value, fs = $("#fStatus").value, sort = $("#fSort").value;
    let rows = P.trucks.filter((t) =>
      (!ft || t.trailer === ft) && (!fc || t.home_city === fc) &&
      (!fs || (fs === "busy" ? t.loads > 0 : t.loads === 0)) &&
      (!q || t.id.toLowerCase().includes(q) || t.home_city.toLowerCase().includes(q)));
    const cmp = {
      loads: (a, b) => b.loads - a.loads || a.empty_pct - b.empty_pct,
      empty: (a, b) => b.empty_pct - a.empty_pct,
      emptyLow: (a, b) => (a.loads ? 0 : 1) - (b.loads ? 0 : 1) || a.empty_pct - b.empty_pct,
      miles: (a, b) => b.miles - a.miles,
      id: (a, b) => a.id.localeCompare(b.id),
    }[sort];
    rows.sort(cmp);
    $("#truckListCount").textContent = `${num(rows.length)} trucks`;
    $("#truckList").innerHTML = rows.map((t) => {
      const lp = t.miles ? (100 * (t.miles - t.empty_miles)) / t.miles : 0;
      return `<div class="truck-row${t.id === S.selected ? " selected" : ""}" role="option" data-id="${t.id}" tabindex="0">
        <div><span class="tid">${t.id}</span> <span class="badge">${trailerName(t.trailer)}</span>${t.team ? ' <span class="badge">Team</span>' : ""}</div>
        <div class="loads">${t.loads ? `${t.loads} load${t.loads > 1 ? "s" : ""}` : "Stays home"}</div>
        <div class="meta">${esc(t.home_city)}${t.loads ? ` · ${num(t.miles)} mi · ${t.empty_pct.toFixed(0)}% empty` : ""}</div>
        ${t.loads ? `<div class="mini-bar" title="${lp.toFixed(0)}% loaded"><div class="l" style="width:${lp}%"></div><div class="e" style="flex:1"></div></div>` : ""}
      </div>`;
    }).join("") || `<div class="empty-state">No trucks match these filters.</div>`;
  }

  async function selectTruck(id) {
    S.selected = id;
    $$(".truck-row").forEach((r) => r.classList.toggle("selected", r.dataset.id === id));
    let d = S.detailCache[id];
    if (!d) {
      const r = await fetch(`/api/truck/${encodeURIComponent(id)}`);
      if (!r.ok) return;
      d = S.detailCache[id] = await r.json();
    }
    if (S.selected !== id) return;
    renderTruckDetail(d);
    drawTruckMap(d);
  }

  function renderTruckDetail(d) {
    const el = $("#truckDetail");
    if (!d.stops.length) {
      el.innerHTML = `
        <div class="detail-head"><div><h2>${d.id}</h2><p class="hint">${trailerName(d.trailer)} · Home: ${esc(d.home_name)}</p></div>
        <span class="badge">Stays home this week</span></div>
        <p>This truck isn't dispatched. Every load it could reach would cost more to run than it is worth, mostly from long empty drives. Or those loads were better covered by other trucks.</p>`;
      return;
    }
    const loadedMi = d.miles - d.empty_miles;
    const slack = d.deadline - d.return;
    const lateStops = d.stops.filter((s) => s.late_min > 0).length;
    el.innerHTML = `
      <div class="detail-head">
        <div>
          <h2>${d.id} <span class="badge">${trailerName(d.trailer)}</span>${d.team ? ' <span class="badge">Team drivers</span>' : ""}</h2>
          <p class="hint" style="margin-top:4px">Home: ${esc(d.home_name)} · ${d.max_days}-day tour limit</p>
        </div>
        <span class="badge ${d.late_return_h > 0 ? "bad" : "good"}">${d.late_return_h > 0 ? `⚠ Home ${d.late_return_h} h late` : "✓ Home on time"}</span>
      </div>
      <p style="margin:12px 0 0;color:var(--ink-2)">
        Leaves home <b>${when(d.depart)}</b> and hauls <b>${d.stops.length} load${d.stops.length > 1 ? "s" : ""}</b> through
        ${esc([...new Set(d.stops.flatMap((s) => [s.origin_city.split(",")[0], s.dest_city.split(",")[0]]))].join(", "))}.
        Back home <b>${when(d.return)}</b>, ${slack >= 0 ? `${dur(slack)} before its limit` : `${dur(-slack)} after its limit`}.
      </p>
      <div class="detail-stats">
        <div class="stat"><div class="k">Loaded miles</div><div class="v">${num(loadedMi)}</div></div>
        <div class="stat"><div class="k">Empty miles</div><div class="v">${num(d.empty_miles)} <small style="font-size:12px;color:var(--ink-3)">${d.empty_pct.toFixed(0)}%</small></div></div>
        <div class="stat"><div class="k">Revenue</div><div class="v">${money(d.revenue)}</div></div>
        <div class="stat"><div class="k">Operating cost</div><div class="v">${money(d.cost)}</div></div>
        <div class="stat"><div class="k">Driving time</div><div class="v">${d.drive_hours} h</div></div>
        <div class="stat"><div class="k">Rests · breaks</div><div class="v">${d.rests} · ${d.breaks}</div></div>
      </div>
      <h3 style="margin-top:6px">Schedule, day by day</h3>
      <div class="legend-inline" style="margin-top:8px">
        <span><i class="sw" style="background:var(--series-opt)"></i>Driving loaded</span>
        <span><i class="sw" style="background:var(--series-2)"></i>Driving empty</span>
        <span><i class="sw" style="background:var(--g-dock)"></i>Loading / unloading</span>
        <span><i class="sw" style="background:var(--g-wait)"></i>Waiting</span>
        <span><i class="sw hatch"></i>Rest / break (HOS)</span>
      </div>
      <div class="gantt" id="gantt"></div>
      <h3>Stops</h3>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>#</th><th>Load</th><th>Pick up</th><th>Deliver</th><th class="num">Miles</th><th class="num">Rate</th></tr></thead>
          <tbody>${d.stops.map((s, i) => `
            <tr>
              <td>${i + 1}</td>
              <td><b>${s.load_id}</b>${s.priority !== "standard" ? ` <span class="badge cap ${s.priority === "critical" ? "crit" : ""}">${s.priority}</span>` : ""}<div class="sub-line">${esc(s.commodity)} · ${num(s.weight)} lb</div></td>
              <td><b>${when(s.pickup_at)}</b><div class="sub-line">${esc(s.origin)}</div><div class="sub-line">window ${when(s.pickup_window[0])} – ${when(s.pickup_window[1])}</div></td>
              <td><b>${when(s.delivered_at)}</b> ${s.late_min > 0 ? `<span class="badge bad">⚠ ${dur(s.late_min)} late</span>` : `<span class="badge good">✓ on time</span>`}<div class="sub-line">${esc(s.dest)}</div><div class="sub-line">due by ${when(s.delivery_window[1])}</div></td>
              <td class="num">${num(s.miles)}</td>
              <td class="num">${money(s.rate)}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </div>
      ${lateStops ? `<p class="hint">${lateStops} late deliver${lateStops > 1 ? "ies" : "y"}: allowed because of your lateness setting.</p>` : ""}`;
    renderGantt(d);
  }

  const KIND = {
    drive_loaded: { label: "Driving loaded", color: "--series-opt" },
    drive_empty: { label: "Driving empty", color: "--series-2" },
    load: { label: "Loading", color: "--g-dock" },
    unload: { label: "Unloading", color: "--g-dock" },
    wait: { label: "Waiting", color: "--g-wait" },
    rest: { label: "10-hour rest (HOS)", color: "--g-rest", hatch: true },
    break: { label: "30-min break (HOS)", color: "--g-rest", hatch: true },
    restart: { label: "34-hour restart (HOS)", color: "--g-rest", hatch: true },
  };

  function renderGantt(d) {
    const host = $("#gantt");
    if (!host) return;
    const start = Math.min(d.depart, ...d.events.map((e) => e[1]));
    const end = Math.max(d.return, ...d.events.map((e) => e[2]));
    const d0 = Math.floor(start / 1440), d1 = Math.floor((end - 0.01) / 1440);
    const W = Math.max(host.clientWidth, 320), labelW = 84, rowH = 26, gap = 8, top = 18;
    const plotW = W - labelW - 6;
    const x = (min) => labelW + (min / 1440) * plotW;
    const rows = d1 - d0 + 1;
    const H = top + rows * (rowH + gap);
    let s = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Daily schedule for ${d.id}">
      <defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width="6" height="6" fill="${css("--surface-2")}"/><rect width="3" height="6" fill="${css("--g-rest")}"/></pattern></defs>`;
    for (let h = 0; h <= 24; h += 6) {
      s += `<line class="gridline" x1="${x(h * 60)}" x2="${x(h * 60)}" y1="${top - 4}" y2="${H}"/>`;
      s += `<text class="tick" x="${x(h * 60)}" y="11" text-anchor="${h === 0 ? "start" : h === 24 ? "end" : "middle"}">${String(h).padStart(2, "0")}:00</text>`;
    }
    for (let r = 0; r < rows; r++) {
      const y = top + r * (rowH + gap);
      s += `<text class="day-label" x="0" y="${y + rowH / 2 + 4}">${dayLabel((d0 + r) * 1440)}</text>`;
      s += `<rect class="lane-bg" x="${labelW}" y="${y}" width="${plotW}" height="${rowH}" rx="4"/>`;
    }
    const segs = [];
    d.events.forEach(([kind, a, b], idx) => {
      for (let day = Math.floor(a / 1440); day <= Math.floor((b - 0.01) / 1440); day++) {
        const sa = Math.max(a, day * 1440), sb = Math.min(b, (day + 1) * 1440);
        if (sb - sa < 0.5) continue;
        segs.push({ kind, a, b, sa, sb, row: day - d0, idx });
      }
    });
    segs.forEach((g, i) => {
      const k = KIND[g.kind] || { label: g.kind, color: "--ink-3" };
      const y = top + g.row * (rowH + gap);
      const fill = k.hatch ? "url(#hatch)" : css(k.color);
      const w = Math.max(1, x(g.sb - (g.row + d0) * 1440) - x(g.sa - (g.row + d0) * 1440));
      s += `<rect class="ev" data-i="${i}" x="${x(g.sa - (g.row + d0) * 1440)}" y="${y + (k.hatch ? 5 : 2)}" width="${w}" height="${rowH - (k.hatch ? 10 : 4)}" rx="2" fill="${fill}"/>`;
    });
    s += "</svg>";
    host.innerHTML = s;
    host.onmousemove = (ev) => {
      const t = ev.target.closest("rect.ev");
      if (!t) return hideTip();
      const g = segs[+t.dataset.i];
      const k = KIND[g.kind] || { label: g.kind };
      showTip(`<b>${k.label}</b><br>${when(g.a)} → ${when(g.b)} (${dur(g.b - g.a)})`, ev);
    };
    host.onmouseleave = hideTip;
  }

  // ------------------------------------------------------------------ uncovered tab
  const REASONS = {
    fleet_busy: { t: "Fleet already busy", d: "Nearby trucks that could make it are hauling other loads." },
    not_worth_it: { t: "Not worth the empty miles", d: "A truck could take it, but the empty driving costs more than the load is worth." },
    too_tight: { t: "Deadline too tight", d: "Even a fully rested solo driver can't legally drive it in time. Needs a team truck." },
    unreachable: { t: "Out of reach", d: "No suitable truck can make the pickup and get home in time." },
    excluded: { t: "Excluded by settings", d: "Tentative or high cancellation-risk loads you chose to skip." },
  };

  function renderReasonTiles() {
    const counts = {};
    for (const u of S.plan.uncovered) counts[u.code] = (counts[u.code] || 0) + 1;
    $("#reasonTiles").innerHTML = Object.entries(REASONS).filter(([c]) => counts[c]).map(([c, r]) => `
      <button class="reason${S.uncFilter.reason === c ? " active" : ""}" data-reason="${c}">
        <div class="n">${num(counts[c])}</div><div class="t">${r.t}</div><div class="d">${r.d}</div>
      </button>`).join("");
    $("#uncCount").textContent = num(S.plan.uncovered.length);
  }

  function renderUncovered() {
    const f = S.uncFilter;
    const q = f.q.toLowerCase();
    const rows = S.plan.uncovered.filter((u) =>
      (!f.reason || u.code === f.reason) && (!f.trailer || u.trailer === f.trailer) && (!f.priority || u.priority === f.priority) &&
      (!q || u.load_id.toLowerCase().includes(q) || u.origin_city.toLowerCase().includes(q) || u.dest_city.toLowerCase().includes(q)));
    const shown = rows.slice(0, f.limit);
    $("#uncTable tbody").innerHTML = shown.map((u) => `
      <tr>
        <td><b>${u.load_id}</b>${u.status === "tentative" ? '<div class="sub-line">tentative</div>' : ""}</td>
        <td class="lane">${esc(u.origin_city)}<span class="arrow">→</span>${esc(u.dest_city)}</td>
        <td>${trailerName(u.trailer)}</td>
        <td>${u.priority === "standard" ? '<span class="sub-line">standard</span>' : `<span class="badge cap ${u.priority === "critical" ? "crit" : ""}">${u.priority}</span>`}</td>
        <td class="num">${num(u.miles)}</td>
        <td class="num">${money(u.rate)}</td>
        <td style="white-space:nowrap">${when(u.pickup_window[0])} – ${hhmm(at(u.pickup_window[1]))}</td>
        <td class="why">${esc(u.text)}</td>
      </tr>`).join("") || `<tr><td colspan="8" class="empty-state">No loads match these filters.</td></tr>`;
    const more = $("#uncMore");
    more.hidden = rows.length <= f.limit;
    more.textContent = `Show more (${num(rows.length - f.limit)} remaining)`;
  }

  // ------------------------------------------------------------------ settings
  const DEFAULTS = { deadhead_penalty: 3.5, unserved_penalty: 5000, late_delivery_allow_h: 0, late_return_allow_h: 0, max_cancel_prob: 1, include_tentative: true, time_limit: 45 };
  const fields = {
    deadhead_penalty: { el: "#sDeadhead", out: "#oDeadhead", fmt: (v) => `$${(+v).toFixed(2)}/mi` },
    unserved_penalty: { el: "#sUnserved", out: "#oUnserved", fmt: (v) => money(+v) },
    late_delivery_allow_h: { el: "#sLateDel", out: "#oLateDel", fmt: (v) => (+v ? `${v} h` : "never late") },
    late_return_allow_h: { el: "#sLateRet", out: "#oLateRet", fmt: (v) => (+v ? `${v} h` : "never late") },
    max_cancel_prob: { el: "#sCancel", out: "#oCancel", fmt: (v) => (+v >= 1 ? "keep all" : `${Math.round(v * 100)}%`) },
    time_limit: { el: "#sTime", out: "#oTime", fmt: (v) => `${v} s` },
  };
  function setSettings(s) {
    for (const [k, f] of Object.entries(fields)) {
      const input = $(f.el);
      input.value = s[k] ?? DEFAULTS[k];
      $(f.out).textContent = f.fmt(input.value);
    }
    $("#sTentative").checked = s.include_tentative ?? true;
  }
  for (const f of Object.values(fields)) {
    $(f.el).addEventListener("input", (e) => { $(f.out).textContent = f.fmt(e.target.value); });
  }
  function openDrawer(open) {
    $("#drawer").classList.toggle("open", open);
    $("#drawer").setAttribute("aria-hidden", String(!open));
    $("#drawerBackdrop").hidden = !open;
  }
  $("#settingsBtn").addEventListener("click", () => openDrawer(true));
  $("#drawerClose").addEventListener("click", () => openDrawer(false));
  $("#drawerBackdrop").addEventListener("click", () => openDrawer(false));
  addEventListener("keydown", (e) => { if (e.key === "Escape") openDrawer(false); });
  $("#resetSettings").addEventListener("click", () => setSettings(DEFAULTS));
  $("#settingsForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {};
    for (const [k, f] of Object.entries(fields)) body[k] = parseFloat($(f.el).value);
    body.include_tentative = $("#sTentative").checked;
    const r = await fetch("/api/solve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok && r.status !== 409) {
      alert("Could not start the optimizer: " + (await r.text()));
      return;
    }
    openDrawer(false);
    startPolling();
  });

  // ------------------------------------------------------------------ progress
  function setRunning(st) {
    const box = $("#progress");
    box.hidden = !st.running;
    $("#runBtn").disabled = st.running;
    $("#settingsBtn").disabled = false;
    if (st.running) {
      $("#progPhase").textContent = st.phase + "…";
      const bits = [];
      if (st.iterations) bits.push(`${num(st.iterations)} improvement rounds`);
      $("#progDetail").textContent = bits.join(" · ") || `${Math.round(st.pct)}%`;
      $("#progFill").style.width = Math.max(2, st.pct) + "%";
      $("#statusPill").hidden = false;
      $("#statusPill").textContent = "Optimizing…";
    } else {
      $("#statusPill").hidden = true;
    }
  }
  function startPolling() {
    clearInterval(S.polling);
    let wasRunning = true;
    S.polling = setInterval(async () => {
      let st;
      try { st = await (await fetch("/api/status")).json(); } catch (e) { return; }
      setRunning(st);
      if (st.error) {
        clearInterval(S.polling);
        alert("The optimizer failed: " + st.error);
      }
      if (wasRunning && !st.running) {
        clearInterval(S.polling);
        if (st.has_plan) await loadPlan();
      }
      wasRunning = st.running;
    }, 700);
  }

  // ------------------------------------------------------------------ load & render
  async function loadPlan() {
    const r = await fetch("/api/plan");
    if (!r.ok) return false;
    const P = await r.json();
    S.plan = P;
    S.detailCache = {};
    H0 = new Date(P.horizon_start);
    renderAll();
    return true;
  }

  function renderAll() {
    const P = S.plan;
    const k = P.kpis;
    const end = at((P.horizon_days - 1) * 1440);
    $("#subtitle").textContent = `Week of ${dayLabel(0)} – ${MONTHS[end.getMonth()]} ${end.getDate()}, ${end.getFullYear()} · ${num(k.trucks_total)} trucks · ${num(k.loads_total + k.loads_excluded)} loads · solved in ${Math.round(k.solve_seconds)} s`;
    renderKpis();
    populateFilters();
    renderCharts();
    drawOverviewLanes();
    renderTruckList();
    renderReasonTiles();
    renderUncovered();
    setSettings(P.settings || DEFAULTS);
    if (S.selected) selectTruck(S.selected);
  }

  // ------------------------------------------------------------------ events
  $$(".tab").forEach((b) => b.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === b));
    $$(".panel-view").forEach((v) => v.classList.toggle("active", v.id === "view-" + b.dataset.tab));
    if (b.dataset.tab === "trucks") {
      if (!maps.truck) maps.truck = makeMap("truckMap");
      setTimeout(() => {
        maps.truck.invalidateSize();
        if (!S.selected && S.plan) {
          const first = $(".truck-row");
          if (first) selectTruck(first.dataset.id);
        } else if (S.selected && S.detailCache[S.selected]) {
          renderGantt(S.detailCache[S.selected]);
        }
      }, 30);
    }
    if (b.dataset.tab === "overview" && maps.overview) setTimeout(() => maps.overview.invalidateSize(), 30);
  }));
  $$(".seg-btn").forEach((b) => b.addEventListener("click", () => {
    $$(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
    S.planView = b.dataset.plan;
    drawOverviewLanes();
  }));
  $("#showLoaded").addEventListener("change", drawOverviewLanes);
  $("#showEmpty").addEventListener("change", drawOverviewLanes);
  ["#truckSearch", "#fTrailer", "#fCity", "#fStatus", "#fSort"].forEach((s) =>
    $(s).addEventListener(s === "#truckSearch" ? "input" : "change", renderTruckList));
  $("#truckList").addEventListener("click", (e) => {
    const row = e.target.closest(".truck-row");
    if (row) selectTruck(row.dataset.id);
  });
  $("#truckList").addEventListener("keydown", (e) => {
    const row = e.target.closest(".truck-row");
    if (!row) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectTruck(row.dataset.id); }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = e.key === "ArrowDown" ? row.nextElementSibling : row.previousElementSibling;
      if (next && next.classList.contains("truck-row")) { next.focus(); selectTruck(next.dataset.id); }
    }
  });
  $("#reasonTiles").addEventListener("click", (e) => {
    const b = e.target.closest(".reason");
    if (!b) return;
    S.uncFilter.reason = S.uncFilter.reason === b.dataset.reason ? "" : b.dataset.reason;
    S.uncFilter.limit = 100;
    renderReasonTiles();
    renderUncovered();
  });
  $("#uncSearch").addEventListener("input", (e) => { S.uncFilter.q = e.target.value; S.uncFilter.limit = 100; renderUncovered(); });
  $("#uTrailer").addEventListener("change", (e) => { S.uncFilter.trailer = e.target.value; S.uncFilter.limit = 100; renderUncovered(); });
  $("#uPriority").addEventListener("change", (e) => { S.uncFilter.priority = e.target.value; S.uncFilter.limit = 100; renderUncovered(); });
  $("#uncMore").addEventListener("click", () => { S.uncFilter.limit += 200; renderUncovered(); });
  let resizeT;
  addEventListener("resize", () => {
    clearTimeout(resizeT);
    resizeT = setTimeout(() => { if (S.selected && S.detailCache[S.selected]) renderGantt(S.detailCache[S.selected]); }, 150);
  });

  // ------------------------------------------------------------------ boot
  (async function boot() {
    maps.overview = makeMap("overviewMap");
    setSettings(DEFAULTS);
    const ok = await loadPlan();
    const st = await (await fetch("/api/status")).json();
    if (st.running || !ok) {
      $("#subtitle").textContent = "Optimizer is planning the week…";
      setRunning({ ...st, running: true });
      startPolling();
    }
  })();
})();
