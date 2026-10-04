/* Upstream dashboard: outbreak replay, live intake, household card.
   Modes (data-mode on #upstream): "app" (served by the FastAPI app, live intake enabled),
   "static" (a folder on any static host) and "artifact" (single self-contained page). */
(() => {
  'use strict';

  const root = document.getElementById('upstream');
  const MODE = root.dataset.mode || 'static';
  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmtM = (m) => (m == null ? '—' : m >= 1000 ? (m / 1000).toFixed(1) + ' km' : Math.round(m) + ' m');
  const fmtPct = (p) => (p == null ? '—' : p >= 0.995 ? '>99%' : p < 0.005 && p > 0 ? '<1%' : Math.round(p * 100) + '%');
  const pad2 = (n) => String(n).padStart(2, '0');
  const hhmm = (t) => { const m = ((Math.round(t) % 1440) + 1440) % 1440; return pad2(Math.floor(m / 60)) + ':' + pad2(m % 60); };
  const dayOf = (t) => Math.floor(t / 1440) + 1;
  const dayClock = (t) => `Day ${dayOf(t)} · ${hhmm(t)}`;
  const ANSWER = { fine: 'fine', smell: 'smells bad', dirty: 'looks dirty', ill: 'someone is ill' };
  const LEVEL = { green: 'Green', amber: 'Amber', red: 'Red' };

  let S = null;                 // scenario JSON
  const V = { net: 'gis', i: 0, playing: false, timer: null, speed: 240, truth: true, planb: false, risk: false, tab: 'replay' };
  const homes = {};             // household id -> household
  const reports = {};           // outbreak report id -> report
  const frameIdx = {};          // report id -> frame index
  let map = null;
  const LY = {};                // map layers
  const hhMarker = {};          // household id -> circle marker
  let tipInfo = {};             // household id -> {report, level, advisory, asked} for tooltips
  let live = null;              // latest live state (app mode)
  let liveTimer = null;

  // ------------------------------------------------------------------ load
  async function loadScenario() {
    const inline = document.getElementById('scenario-data');
    if (inline) return JSON.parse(inline.textContent);
    const res = await fetch(MODE === 'app' ? '/api/scenario' : 'data/scenario.json');
    if (!res.ok) throw new Error(`could not load the scenario (${res.status})`);
    return res.json();
  }

  function index() {
    for (const h of S.households) homes[h.id] = h;
    for (const c of S.outbreak.cycles) for (const r of c.reports) reports[r.id] = r;
    S.frames.forEach((f, i) => { frameIdx[f.report] = i; });
  }

  const cycleInfo = (c) => S.outbreak.cycles.find((x) => x.cycle === c);
  const baseTurb = (hid) => {
    const b = homes[hid] && homes[hid].baseline;
    return b && b.turb_med != null ? b.turb_med : S.baseline.population.turb_med;
  };
  const segsFor = (net) => S.segments[net] || S.segments.gis;

  // ------------------------------------------------------------------- map
  function pin(kind, label) {
    const svg = {
      dig: '<svg viewBox="-11 -11 22 22"><circle r="7"/><path d="M0 -11v6M0 5v6M-11 0h6M5 0h6"/></svg>',
      truth: '<svg viewBox="-11 -11 22 22"><circle r="8"/><path d="M-4 -4 4 4M4 -4 -4 4"/></svg>',
      inlet: '<svg viewBox="-11 -11 22 22"><rect x="-7" y="-7" width="14" height="14" rx="2"/></svg>',
    }[kind];
    return L.divIcon({ className: '', html: `<div class="pin ${kind}">${svg}<span>${esc(label)}</span></div>`,
      iconSize: [22, 22], iconAnchor: [11, 11] });
  }

  function initMap() {
    map = L.map('map', { zoomSnap: 0.25, zoomControl: true, attributionControl: true });
    map.attributionControl.setPrefix(false);
    if (!S.meta.synthetic && MODE !== 'artifact') {
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, opacity: 0.55 }).addTo(map);
    }
    map.attributionControl.addAttribution(esc(S.meta.attribution));
    const N = S.streets.nodes;
    const seg = (e) => [N[S.streets.edges[e][0]], N[S.streets.edges[e][1]]];
    LY.streets = L.polyline(S.streets.edges.map((_, e) => seg(e)), { className: 'lf-street', weight: 4, interactive: false }).addTo(map);
    LY.pipes = L.polyline(S.pipes.edges.map(seg), { className: 'lf-pipe', weight: 2.6, interactive: false });
    LY.rebuilt = L.polyline(S.rebuilt.edges.map(seg), { className: 'lf-rebuilt', weight: 2.6, dashArray: '7 5', interactive: false });
    LY.risk = L.layerGroup(S.pipes.edges.map((e, k) => L.polyline(seg(e), {
      className: 'lf-risk', weight: 7, opacity: 0.12 + 0.75 * S.pipes.risk[k], lineCap: 'butt',
    }).bindTooltip(() => riskTip(k), { sticky: true })));
    LY.planb = L.layerGroup();
    LY.post = L.layerGroup().addTo(map);
    LY.ring = L.circle([0, 0], { radius: 1, className: 'lf-ring', weight: 2, dashArray: '8 6', fillOpacity: 0.07, interactive: false });
    LY.dig = L.marker([0, 0], { icon: pin('dig', 'Dig here'), keyboard: false, zIndexOffset: 900 });
    LY.truth = L.marker([S.truth.lat, S.truth.lon], { icon: pin('truth', 'True breach'), keyboard: false, zIndexOffset: 800 });
    LY.inlet = L.marker([S.inlet.lat, S.inlet.lon], { icon: pin('inlet', 'Inlet'), keyboard: false, interactive: false }).addTo(map);
    LY.homes = L.layerGroup().addTo(map);
    for (const h of S.households) {
      const m = L.circleMarker([h.lat, h.lon], {
        radius: h.role === 'sentinel' ? 6.5 : 5.5, weight: h.role === 'sentinel' ? 2.5 : 1.5, fillOpacity: 1,
        className: 'hh hh-none' + (h.role === 'sentinel' ? ' hh-sentinel' : ''),
      }).bindTooltip(() => homeTip(h.id), { direction: 'top', offset: [0, -6] });
      m.addTo(LY.homes);
      hhMarker[h.id] = m;
    }
    map.fitBounds(L.latLngBounds(N), { padding: [24, 24] });
    syncNetLayers();
  }

  function setHomeClass(id, level, asked) {
    const el = hhMarker[id] && hhMarker[id].getElement();
    if (!el) return;
    el.classList.remove('hh-none', 'hh-green', 'hh-amber', 'hh-red', 'hh-asked');
    el.classList.add('hh-' + (level || 'none'));
    if (asked) el.classList.add('hh-asked');
    el.setAttribute('stroke-dasharray', asked ? '3 2' : '');
  }

  function riskTip(k) {
    const P = S.pipes;
    const yes = (v) => (v ? 'yes' : 'no');
    return `<b>Pipe risk ${Math.round(P.risk[k] * 100)}/100</b><br>Age ${P.age_years[k]} years · sewer in lane ${yes(P.sewer_alongside[k])}<br>`
      + `Sewer crossings ${P.sewer_crossings[k]} · past incidents ${P.past_incidents[k]}`
      + (P.sewage_source_above[k] ? '<br>Known sewage source above the line' : '')
      + '<br><span class="muted">Synthetic attributes</span>';
  }

  function readingText(r) {
    const bits = [];
    if (r.turbidity != null) bits.push(`cloudiness ${(r.turbidity - baseTurb(r.household) >= 0 ? '+' : '')}${(r.turbidity - baseTurb(r.household)).toFixed(2)}`);
    if (r.chlorine != null) bits.push(`Cl ${r.chlorine.toFixed(2)} mg/L`);
    if (r.answer) bits.push(ANSWER[r.answer] || r.answer);
    return bits.join(' · ') || 'photo received';
  }

  function homeTip(id) {
    const h = homes[id];
    const t = tipInfo[id] || {};
    let s = `<b>${esc(id)}</b> · ${esc(h.role)} (${h.pack}-strip pack)<br>Water usually arrives ${h.arrival_min.toFixed(0)} min after supply starts`;
    if (t.report) s += `<br>${esc(hhmm(t.report.t_obs))} — ${esc(readingText(t.report))}${t.level ? ` · <b>${LEVEL[t.level]}</b>` : ''}`;
    else s += '<br>No photo yet this supply';
    if (t.asked) s += '<br>Asked to test this supply';
    if (t.advisory) s += `<br>Safe after ${esc(hhmm(t.advisory.safe_after))} (${Math.round(t.advisory.p_affected * 100)}% likely downstream)`;
    return s;
  }

  function syncNetLayers() {
    const gis = V.net === 'gis';
    if (gis) { map.addLayer(LY.pipes); map.removeLayer(LY.rebuilt); } else { map.addLayer(LY.rebuilt); map.removeLayer(LY.pipes); }
    if (V.risk) map.addLayer(LY.risk); else map.removeLayer(LY.risk);
    if (V.planb) map.addLayer(LY.planb); else map.removeLayer(LY.planb);
    LY.homes.eachLayer((m) => m.bringToFront());
  }

  function paintPosterior(fr, segs) {
    LY.post.clearLayers();
    LY.planb.clearLayers();
    if (!fr) {
      map.removeLayer(LY.ring); map.removeLayer(LY.dig);
      return;
    }
    const entries = Object.entries(fr.post);
    let pmax = 0;
    for (const [, p] of entries) pmax = Math.max(pmax, p);
    entries.sort((a, b) => a[1] - b[1]);
    for (const [k, p] of entries) {
      const i = +k;
      L.polyline([segs.a[i], segs.b[i]], { className: 'lf-post', weight: 8, opacity: 0.1 + 0.9 * (p / pmax), lineCap: 'round' })
        .bindTooltip(`${(p * 100).toFixed(p < 0.01 ? 2 : 1)}% chance the breach is in this ${Math.round(segs.length_m[i])} m piece`, { sticky: true })
        .addTo(LY.post);
    }
    if (fr.plan_b && fr.plan_b.segments) {
      for (const i of fr.plan_b.segments) {
        L.polyline([segs.a[i], segs.b[i]], { className: 'lf-planb', weight: 2, dashArray: '2 5', interactive: false }).addTo(LY.planb);
      }
    }
    LY.ring.setLatLng([fr.ring.lat, fr.ring.lon]).setRadius(fr.ring.radius_m);
    LY.dig.setLatLng([fr.dig.lat, fr.dig.lon]);
    map.addLayer(LY.ring); map.addLayer(LY.dig);
    LY.homes.eachLayer((m) => m.bringToFront());
  }

  function paintTruth(show) {
    if (show && V.tab === 'replay') map.addLayer(LY.truth); else map.removeLayer(LY.truth);
  }

  // ---------------------------------------------------------------- replay
  const F = () => S.frames.length;

  function view() {
    // everything the replay shows at position V.i (number of photos applied)
    const i = V.i;
    const fr = i > 0 ? S.frames[i - 1] : null;
    const cycle = fr ? fr.cycle : S.outbreak.cycles[0].cycle;
    const info = cycleInfo(cycle);
    const lv = {};
    for (let j = 0; j < i; j++) Object.assign(lv, S.frames[j].levels);
    const arrived = info.reports.filter((r) => frameIdx[r.id] < i);
    const post = fr ? fr[V.net] || null : null;
    const ci = S.outbreak.cycles.indexOf(info);
    const prev = ci > 0 ? S.outbreak.cycles[ci - 1] : null;
    const atEnd = i >= F();
    let advisories = prev && prev.advisories ? prev.advisories : [];
    if (atEnd && S.final.advisories) advisories = S.final.advisories.filter((a) => a.status !== 'clear');
    return { i, fr, cycle, info, ci, prev, lv, arrived, post, atEnd, advisories };
  }

  function render() {
    const v = view();
    $('#scrub').value = String(v.i);
    const supplyStart = v.info.supply_start;
    $('#clock').textContent = v.fr ? dayClock(v.fr.t) : dayClock(supplyStart);
    $('#cycle-label').textContent = `Outbreak supply ${v.ci + 1} of ${S.outbreak.cycles.length} · photo ${v.arrived.length} of ${v.info.reports.length}`;

    // households
    tipInfo = {};
    const asked = new Set(v.info.requested || []);
    const advBy = {};
    for (const a of v.advisories) advBy[a.household] = a;
    const repBy = {};
    for (const r of v.arrived) repBy[r.household] = r;
    for (const id of Object.keys(homes)) {
      const r = repBy[id];
      const level = r ? v.lv[r.id] : null;
      tipInfo[id] = { report: r, level, asked: asked.has(id), advisory: advBy[id] };
      setHomeClass(id, level, asked.has(id) && !r);
    }
    paintPosterior(v.post, segsFor(V.net));
    paintTruth(V.truth);

    // banner
    const b = $('#banner');
    b.className = 'banner';
    const reds = v.arrived.filter((r) => v.lv[r.id] === 'red');
    const ambers = v.arrived.filter((r) => v.lv[r.id] === 'amber');
    const anyAlertSoFar = v.fr && v.fr.alert;
    if (v.i === 0) {
      b.textContent = `Water comes on at ${hhmm(supplyStart)}. Each home photographs its first glass as the front reaches it.`;
    } else if (v.prev && v.arrived.length <= 3 && v.info.requested && v.info.requested.length) {
      b.className = 'banner next';
      b.textContent = `Two days later. Upstream asked ${v.info.requested.length} homes to test with a strip at this supply.`;
    } else if (anyAlertSoFar) {
      b.className = 'banner alert';
      const first = reds[0] || S.outbreak.cycles.flatMap((c) => c.reports).find((r) => v.lv[r.id] === 'red');
      b.textContent = first
        ? `Red reading at ${first.household} (${readingText(first)}). The utility is alerted and Upstream is triangulating.`
        : 'Alert active: triangulating on every new photo.';
    } else if (ambers.length) {
      b.className = 'banner amber';
      b.textContent = `Amber at ${ambers.map((r) => r.household).join(', ')}. Nearby homes with strips will be asked to test at the next supply.`;
    } else {
      b.textContent = `${v.arrived.length} photos in. All within each tap's normal range so far.`;
    }

    // metrics
    const counts = { green: 0, amber: 0, red: 0 };
    for (const r of v.arrived) counts[v.lv[r.id]] = (counts[v.lv[r.id]] || 0) + 1;
    $('#m-reports').innerHTML = `${v.arrived.length} <small>${counts.green} green · ${counts.amber} amber · ${counts.red} red</small>`;
    const p = v.post;
    $('#m-pbreach').textContent = p ? fmtPct(p.p_breach) : '—';
    $('#m-ring').innerHTML = p ? `${fmtM(p.ring.radius_m)} <small>radius</small>` : '—';
    $('#m-region').innerHTML = p ? `${fmtM(p.region_length_m)} <small>90% region</small>` : '—';
    $('#m-error').innerHTML = p ? `${fmtM(p.error_m)} <small>${p.truth_in_region ? 'truth inside region' : 'truth outside region'}</small>` : '—';
    $('#m-planb').textContent = p && p.plan_b && p.plan_b.violations != null ? fmtM(p.plan_b.length_m) : '—';

    renderChart($('#chart'), v.info.reports, new Set(v.arrived.map((r) => r.id)), v.lv, p ? p.dig_arrival_min : null);
    renderFeed($('#feed'), v.arrived, v.lv);
    renderOutputs(v);
    $('#btn-play').textContent = V.playing ? 'Pause' : v.atEnd ? 'Replay' : 'Play';
    $('#btn-play').setAttribute('aria-label', V.playing ? 'Pause replay' : 'Play replay');
  }

  function renderFeed(el, arrived, lv) {
    const last = arrived.slice(-8).reverse();
    if (!last.length) { el.innerHTML = '<li class="empty">Waiting for the first photo.</li>'; return; }
    el.innerHTML = last.map((r) => `<li><span class="t">${esc(hhmm(r.t_obs))}</span><span class="hh">${esc(r.household)}</span>`
      + `<span class="what">${lv[r.id] ? `<span class="pill ${lv[r.id]}">${LEVEL[lv[r.id]]}</span> ` : ''}${esc(readingText(r))}`
      + `${(r.flags || []).map((f) => ` <span class="pill flag">${esc(f.replace(/_/g, ' '))}</span>`).join('')}</span></li>`).join('');
  }

  function renderOutputs(v) {
    const out = [];
    const p = v.post;
    if (p) {
      out.push(`<div class="out dig"><div class="eyebrow">Dig spot</div>
        <p class="big">${p.dig.lat.toFixed(5)}, ${p.dig.lon.toFixed(5)}</p>
        <p>${fmtPct(p.dig.prob)} of the probability is in this ${fmtM(segsFor(V.net).length_m[p.dig.segment])} piece; the 90% region covers ${fmtM(p.region_length_m)} of pipe inside a ${fmtM(p.ring.radius_m)} ring.
        Built from ${p.n_reports} photos${V.net === 'rebuilt' ? ' on a pipe map rebuilt from photo timings alone' : ''}.</p></div>`);
    }
    if (v.advisories.length) {
      const rows = v.advisories.slice().sort((a, b) => (a.safe_after || 1e12) - (b.safe_after || 1e12));
      const when = v.atEnd ? 'for the next supply' : 'sent before this supply';
      out.push(`<div class="out"><div class="eyebrow">Safe-after messages · ${rows.length} homes · ${when}</div>
        <div class="scroll"><table class="tbl"><thead><tr><th>Home</th><th>Fill drinking water after</th><th class="num">Downstream</th></tr></thead><tbody>
        ${rows.map((a) => `<tr><td class="mono">${esc(a.household)}</td><td>${a.status === 'no_safe_window' ? 'Use stored or boiled water today' : esc(hhmm(a.safe_after))}</td><td class="num">${fmtPct(a.p_affected)}</td></tr>`).join('')}
        </tbody></table></div>
        <p class="muted">Homes upstream of the suspected breach get no alarm.</p></div>`);
    }
    const req = v.atEnd ? (S.outbreak.cycles[S.outbreak.cycles.length - 1].next_requests || []) : (v.prev ? v.prev.next_requests || [] : []);
    if (req.length) {
      out.push(`<div class="out"><div class="eyebrow">Strip requests · ${v.atEnd ? 'next supply' : 'this supply'}</div>
        <p>${req.length} homes asked to test: ${esc(summariseReasons(req))}.</p></div>`);
    }
    if (v.atEnd && S.final[V.net]) {
      const f = S.final[V.net];
      const c = S.final.complaints;
      out.push(`<div class="out"><div class="eyebrow">Against one-ticket-at-a-time</div>
        <table class="tbl"><tbody>
        <tr><th scope="row">Upstream dig spot</th><td class="num">${fmtM(f.error_m)} from the breach</td></tr>
        <tr><th scope="row">First complaint ticket</th><td class="num">${fmtM(c.first_ticket_error_m)}</td></tr>
        <tr><th scope="row">Centre of all ${c.n_tickets} complaints</th><td class="num">${fmtM(c.centroid_error_m)}</td></tr>
        </tbody></table></div>`);
      const ev = (f.evidence || []).slice(0, 6);
      if (ev.length) {
        out.push(`<div class="out"><div class="eyebrow">Strongest evidence for the dig spot</div>
          <table class="tbl"><thead><tr><th>Photo</th><th>Home</th><th class="num">Weight</th></tr></thead><tbody>
          ${ev.map((e) => `<tr><td>${esc(reports[e.report] ? readingText(reports[e.report]) : e.report)}</td><td class="mono">${esc(e.household)}</td><td class="num">${e.log_lr >= 0 ? '+' : ''}${e.log_lr.toFixed(1)}</td></tr>`).join('')}
          </tbody></table><p class="muted">Weight is the log-likelihood ratio: how much more likely this photo is if the breach is at the dig spot than if there is none.</p></div>`);
      }
      const lanes = (f.lanes || []).slice(0, 3);
      if (lanes.length) {
        out.push(`<div class="out"><div class="eyebrow">Sentinel planner</div><p>One more participating home on street ${lanes.map((l) => `#${l.street_edge}`).join(', ')} would narrow the search most (${lanes[0].gain_bits.toFixed(2)} bits).</p></div>`);
      }
    }
    if (V.net === 'rebuilt') {
      const sc = S.rebuilt.score;
      out.push(`<div class="out"><div class="eyebrow">Rebuilt pipe map, scored against the real one</div>
        <table class="tbl"><tbody>
        <tr><th scope="row">Rebuilt pipes that are real</th><td class="num">${fmtPct(sc.pipe_precision)}</td></tr>
        <tr><th scope="row">Real pipes recovered</th><td class="num">${fmtPct(sc.pipe_recall)}</td></tr>
        <tr><th scope="row">Home pairs with the right upstream/downstream relation</th><td class="num">${fmtPct(sc.downstream_agreement)}</td></tr>
        </tbody></table></div>`);
    }
    $('#outputs').innerHTML = out.join('');
  }

  function summariseReasons(req) {
    const n = {};
    for (const r of req) n[r.reason.replace(/ at H-\d+$/, ' nearby')] = (n[r.reason.replace(/ at H-\d+$/, ' nearby')] || 0) + 1;
    return Object.entries(n).map(([k, c]) => `${c} ${k}`).join('; ');
  }

  // ----------------------------------------------------------------- chart
  function renderChart(el, cycleReports, arrivedIds, lv, digArrival) {
    const W = 420, H = 220, ml = 46, mr = 14, mt = 14, mb = 36;
    const rise = (r) => (r.turbidity == null ? null : r.turbidity - baseTurb(r.household));
    const all = cycleReports.filter((r) => rise(r) != null);
    const xmax = Math.max(60, Math.ceil(Math.max(0, ...all.map((r) => r.delay_min)) / 15) * 15);
    const ymaxRaw = Math.max(0.3, ...all.map(rise));
    const ymax = Math.ceil(ymaxRaw * 10) / 10;
    const ymin = -0.1;
    const X = (x) => ml + (x / xmax) * (W - ml - mr);
    const Y = (y) => mt + (1 - (y - ymin) / (ymax - ymin)) * (H - mt - mb);
    const parts = [];
    const ystep = ymax > 0.6 ? 0.2 : 0.1;
    for (let y = 0; y <= ymax + 1e-9; y += ystep) {
      parts.push(`<line class="grid" x1="${ml}" x2="${W - mr}" y1="${Y(y)}" y2="${Y(y)}"/>`);
      parts.push(`<text x="${ml - 6}" y="${Y(y) + 4}" text-anchor="end">${y.toFixed(1)}</text>`);
    }
    for (let x = 0; x <= xmax; x += 15) {
      parts.push(`<line class="axis" x1="${X(x)}" x2="${X(x)}" y1="${H - mb}" y2="${H - mb + 4}"/>`);
      parts.push(`<text x="${X(x)}" y="${H - mb + 16}" text-anchor="middle">${x}</text>`);
    }
    parts.push(`<line class="axis" x1="${ml}" x2="${W - mr}" y1="${H - mb}" y2="${H - mb}"/>`);
    parts.push(`<text class="lab" x="${(ml + W - mr) / 2}" y="${H - 4}" text-anchor="middle">minutes after the water came on</text>`);
    parts.push(`<text class="lab" transform="translate(12 ${(mt + H - mb) / 2}) rotate(-90)" text-anchor="middle">cloudiness above normal</text>`);
    if (digArrival != null && digArrival <= xmax) {
      parts.push(`<line class="vline" x1="${X(digArrival)}" x2="${X(digArrival)}" y1="${mt}" y2="${H - mb}"/>`);
    }
    const shown = all.filter((r) => arrivedIds.has(r.id));
    const lastId = shown.length ? shown.reduce((a, b) => (frameIdx[a.id] > frameIdx[b.id] ? a : b)).id : null;
    for (const r of shown) {
      const cls = lv[r.id] || 'green';
      parts.push(`<circle class="dot ${cls}${r.id === lastId ? ' last' : ''}" cx="${X(r.delay_min).toFixed(1)}" cy="${Y(Math.max(ymin, rise(r))).toFixed(1)}" r="4.5"/>`);
    }
    for (const r of shown) {
      parts.push(`<circle class="hit" data-id="${esc(r.id)}" cx="${X(r.delay_min).toFixed(1)}" cy="${Y(Math.max(ymin, rise(r))).toFixed(1)}" r="9"/>`);
    }
    if (!shown.length) parts.push(`<text class="empty" x="${(ml + W - mr) / 2}" y="${(mt + H - mb) / 2}" text-anchor="middle">No photos yet this supply</text>`);
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Scatter of photo time against cloudiness above normal for this supply cycle">${parts.join('')}</svg>`;
  }

  function chartTooltip() {
    const tip = $('#tooltip');
    const chart = $('#chart');
    chart.addEventListener('mousemove', (e) => {
      const hit = e.target.closest('.hit');
      if (!hit) { tip.hidden = true; return; }
      const r = reports[hit.dataset.id] || (live && live.reports.find((x) => x.id === hit.dataset.id));
      if (!r) return;
      const lvl = r.level || (V.tab === 'replay' ? view().lv[r.id] : null);
      tip.innerHTML = `<b>${esc(r.household)}</b> · ${esc(hhmm(r.t_obs))} (${r.delay_min.toFixed(0)} min after start)<br>${esc(readingText(r))}${lvl ? `<br>${LEVEL[lvl]}` : ''}`;
      tip.hidden = false;
      const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = `${x}px`;
      tip.style.top = `${e.clientY + 14}px`;
    });
    chart.addEventListener('mouseleave', () => { tip.hidden = true; });
  }

  // ------------------------------------------------------------- playback
  function stop() {
    V.playing = false;
    clearTimeout(V.timer);
    render();
  }

  function step() {
    if (V.i >= F()) { stop(); return; }
    V.i += 1;
    render();
    if (V.i >= F()) { stop(); return; }
    const cur = S.frames[V.i - 1];
    const next = S.frames[V.i];
    const ms = next.cycle !== cur.cycle ? 2400 : Math.min(1600, Math.max(60, ((next.t - cur.t) * 60000) / V.speed));
    V.timer = setTimeout(step, ms);
  }

  function play() {
    if (V.playing) { stop(); return; }
    if (V.i >= F()) V.i = 0;
    V.playing = true;
    render();
    V.timer = setTimeout(step, 700);
  }

  // ------------------------------------------------------------------ live
  async function api(path, opts) {
    const res = await fetch(path, opts);
    let body = null;
    try { body = await res.json(); } catch { body = null; }
    if (!res.ok) {
      const msg = body && (body.error || body.detail) ? (body.error || body.detail) : `request failed (${res.status})`;
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      err.body = body;
      throw err;
    }
    return body;
  }

  async function refreshLive() {
    try {
      live = await api('/api/live');
      renderLive();
    } catch (e) {
      $('#live-banner').textContent = `Could not reach the server: ${e.message}`;
    }
  }

  function renderLive() {
    if (!live || V.tab !== 'live') return;
    const sup = live.supply;
    $('#live-clock').textContent = sup ? `Cycle ${sup.cycle} · water on at ${new Date(sup.clock).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : 'No supply started';
    const cur = sup ? live.reports.filter((r) => r.cycle === sup.cycle) : [];
    const lv = {};
    for (const r of live.reports) lv[r.id] = r.level;
    const b = $('#live-banner');
    b.className = 'banner' + (live.alert ? ' alert' : live.counts && live.counts.amber ? ' amber' : '');
    b.textContent = live.alert ? 'Red reading: the utility is alerted and Upstream is triangulating.'
      : live.counts && live.counts.amber ? 'Amber reading: nearby homes will be asked to test at the next supply.'
        : cur.length ? `${cur.length} photos this supply, all normal.` : 'Send a photo to start.';
    tipInfo = {};
    const repBy = {};
    for (const r of cur) repBy[r.household] = r;
    const asked = new Set((live.requests || []).map((q) => q.household));
    const advBy = {};
    for (const a of live.advisories || []) advBy[a.household] = a;
    for (const id of Object.keys(homes)) {
      const r = repBy[id];
      tipInfo[id] = { report: r, level: r ? r.level : null, asked: asked.has(id), advisory: advBy[id] };
      setHomeClass(id, r ? r.level : null, asked.has(id) && !r);
    }
    const p = live.posterior;
    paintPosterior(p ? { ...p, plan_b: null } : null, S.segments.gis);
    $('#live-metrics').innerHTML = `
      <div><dt>Photos this supply</dt><dd>${cur.length} <small>${live.counts ? `${live.counts.green} green · ${live.counts.amber} amber · ${live.counts.red} red` : ''}</small></dd></div>
      <div><dt>Breach confidence</dt><dd>${p ? fmtPct(p.p_breach) : '—'}</dd></div>
      <div><dt>Uncertainty ring</dt><dd>${p ? fmtM(p.ring.radius_m) : '—'}</dd></div>
      <div><dt>Pipe left to search</dt><dd>${p ? fmtM(p.region_length_m) : '—'}</dd></div>`;
    renderFeed($('#live-feed'), cur.slice().sort((a, b) => a.t_obs - b.t_obs), lv);
    const out = [];
    if (p) {
      out.push(`<div class="out dig"><div class="eyebrow">Dig spot</div><p class="big">${p.dig.lat.toFixed(5)}, ${p.dig.lon.toFixed(5)}</p><p>${fmtPct(p.dig.prob)} in this piece · 90% region ${fmtM(p.region_length_m)} of pipe.</p></div>`);
    }
    if ((live.advisories || []).length) {
      out.push(`<div class="out"><div class="eyebrow">Safe-after messages · ${live.advisories.length} homes</div><div class="scroll"><table class="tbl"><tbody>
        ${live.advisories.map((a) => `<tr><td class="mono">${esc(a.household)}</td><td>${a.status === 'no_safe_window' ? 'Use stored or boiled water' : 'after ' + esc(a.safe_after_ampm)}</td><td class="num">${fmtPct(a.p_affected)}</td></tr>`).join('')}
        </tbody></table></div></div>`);
    }
    if ((live.requests || []).length) {
      out.push(`<div class="out"><div class="eyebrow">Ask these homes to test next supply</div><p class="mono">${live.requests.map((q) => esc(q.household)).join(', ')}</p></div>`);
    }
    $('#live-outputs').innerHTML = out.join('');
  }

  function showResult(el, data, isError) {
    if (isError) {
      const issues = data && data.issues ? data.issues.map((i) => i.split(': ').slice(-1)[0]) : [];
      el.innerHTML = `<b>${issues.length ? 'Please retake the photo' : 'Not sent'}</b>${issues.length ? `<ul>${issues.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>` : `<p>${esc(data && data.error ? data.error : 'Something went wrong.')}</p>`}`;
      return;
    }
    const r = data.report;
    el.innerHTML = `<div><span class="pill ${esc(data.level)}">${LEVEL[data.level] || esc(data.level)}</span> <b class="mono">${esc(r.household)}</b></div>
      <div>${esc(readingText(r))}</div><div>${esc(data.message)}</div>
      ${(r.flags || []).length ? `<div>${r.flags.map((f) => `<span class="pill flag">${esc(f.replace(/_/g, ' '))}</span>`).join(' ')}</div>` : ''}`;
  }

  function initLive() {
    const url = `${location.origin}/report`;
    $('#phone-url').textContent = url;
    $('#phone-qr').src = `/api/qr.png?data=${encodeURIComponent(url)}`;
    $('#btn-supply').addEventListener('click', async () => {
      await api('/api/live/supply', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      refreshLive();
    });
    const resetBtn = $('#btn-reset');
    resetBtn.addEventListener('click', async () => {
      if (resetBtn.dataset.confirm !== '1') {
        resetBtn.dataset.confirm = '1';
        resetBtn.textContent = 'Click again to clear';
        setTimeout(() => { resetBtn.dataset.confirm = ''; resetBtn.textContent = 'Clear live data'; }, 3500);
        return;
      }
      resetBtn.dataset.confirm = '';
      resetBtn.textContent = 'Clear live data';
      await api('/api/live/reset', { method: 'POST' });
      refreshLive();
    });
    const form = $('#photo-form');
    const result = $('#photo-result');
    async function sendPhoto(file) {
      const fd = new FormData();
      fd.append('photo', file, file.name || 'photo.jpg');
      if ($('#photo-hh').value.trim()) fd.append('household', $('#photo-hh').value.trim());
      if ($('#photo-answer').value) fd.append('answer', $('#photo-answer').value);
      result.innerHTML = 'Reading the photo…';
      try {
        showResult(result, await api('/api/photo', { method: 'POST', body: fd }), false);
      } catch (e) {
        showResult(result, e.body || { error: e.message }, true);
      }
      refreshLive();
    }
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const f = $('#photo-file').files[0];
      if (f) sendPhoto(f);
    });
    $('#btn-demo-photo').addEventListener('click', async () => {
      const hh = $('#photo-hh').value.trim() || (S.truth.downstream_households[0] || S.households[0].id);
      result.innerHTML = 'Making a photo of a muddy glass…';
      const res = await fetch(`/api/demo-photo?household=${encodeURIComponent(hh)}&turbidity=60&chlorine=0&seed=${Date.now() % 1000}`);
      const blob = await res.blob();
      sendPhoto(new File([blob], `demo-${hh}.jpg`, { type: 'image/jpeg' }));
    });
    $('#manual-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = {
        household: $('#man-hh').value.trim(),
        turbidity: $('#man-turb').value === '' ? null : Number($('#man-turb').value),
        chlorine: $('#man-cl').value === '' ? null : Number($('#man-cl').value),
      };
      try {
        showResult(result, await api('/api/reports', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }), false);
      } catch (err) {
        showResult(result, err.body || { error: err.message }, true);
      }
      refreshLive();
    });
  }

  // ----------------------------------------------------------------- tabs
  function setTab(tab) {
    V.tab = tab;
    for (const b of $$('.tabs button')) b.setAttribute('aria-selected', String(b.dataset.tab === tab));
    for (const s of ['replay', 'live', 'card']) { const el = $('#tab-' + s); if (el) el.hidden = s !== tab; }
    clearInterval(liveTimer);
    if (tab === 'live') {
      if (V.playing) stop();
      if (V.net !== 'gis') { V.net = 'gis'; $('#net-gis').checked = true; syncNetLayers(); }
      $('#net-rebuilt').disabled = true;
      paintTruth(false);
      refreshLive();
      liveTimer = setInterval(refreshLive, 3000);
    } else {
      $('#net-rebuilt').disabled = false;
      render();
    }
  }

  function initCard() {
    const img = $('#card-img');
    if (MODE === 'app') {
      const update = () => {
        const id = ($('#card-hh').value.trim() || 'H-0001').replace(/[^A-Za-z0-9_-]/g, '');
        img.src = `/card/${encodeURIComponent(id)}.svg`;
        $('#card-svg').href = `/card/${encodeURIComponent(id)}.svg`;
        $('#card-png').href = `/card/${encodeURIComponent(id)}.png?dpi=300`;
      };
      $('#card-hh').addEventListener('change', update);
      update();
    } else if (MODE === 'artifact' && $('#card-links')) {
      $('#card-links').hidden = true;
    }
  }

  function renderExplain() {
    const ev = S.evaluation;
    if (ev) {
      const g = ev.gis || {};
      const r = ev.rebuilt || {};
      $('#eval-block').innerHTML = `<h3 class="h3">Across ${ev.runs} simulated outbreaks</h3><div class="evals">
        <div><div class="v">${ev.detected} of ${ev.detectable}</div><div class="k">outbreaks with a participating home downstream of the crack raised a red alert within ${ev.outbreak_cycles} supplies</div></div>
        <div><div class="v">${fmtM(g.median_error_m)}</div><div class="k">median distance from the dig spot to the real crack, with the utility's pipe map (90th percentile ${fmtM(g.p90_error_m)})</div></div>
        <div><div class="v">${fmtPct(g.truth_in_region_rate)}</div><div class="k">of those alerts had the real crack inside the 90% search region (median ${fmtM(g.median_region_m)} of pipe)</div></div>
        <div><div class="v">${fmtM(r.median_error_m)}</div><div class="k">median distance with no pipe map at all, rebuilt from photo timings</div></div>
        <div><div class="v">${fmtM(ev.complaints.median_first_ticket_error_m)}</div><div class="k">median distance from the first complaint ticket to the crack, for comparison</div></div>
        <div><div class="v">${ev.false_alarms} of ${ev.runs}</div><div class="k">outbreaks raised an alert with no dirty reading downstream of the real crack (false alarms)</div></div>
        </div>`;
    }
    $('#caveat').textContent = S.meta.synthetic
      ? 'Everything on this page is simulated: a synthetic ward, synthetic pipes and risk attributes, and a simple slug-transport model whose true parameters differ from the ones the locator assumes. Run `upstream fetch-streets` to replay on a real OpenStreetMap street grid, and calibrate the card with real samples before trusting absolute readings.'
      : 'The street grid is real (OpenStreetMap); pipes, households, risk attributes and the outbreak are simulated. Calibrate the card with real samples before trusting absolute readings.';
    $('#attribution').textContent = S.meta.attribution;
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    if (MODE !== 'app') for (const el of $$('[data-app-only]')) el.hidden = true;
    try {
      S = await loadScenario();
    } catch (e) {
      $('#banner').textContent = `Could not load the replay: ${e.message}`;
      return;
    }
    index();
    const st = S.meta.synthetic ? '' : ' · OpenStreetMap streets';
    $('#ward-label').innerHTML = `<b>${esc(S.meta.name)}</b>${st} · ${S.households.length} homes · ${fmtM(S.rebuilt.score.true_length_m)} of pipe`;
    initMap();
    $('#scrub').max = String(F());
    V.i = F();  // open on the finished picture; Play replays it from the start
    $('#scrub').addEventListener('input', (e) => { if (V.playing) { V.playing = false; clearTimeout(V.timer); } V.i = +e.target.value; render(); });
    $('#btn-play').addEventListener('click', play);
    $('#btn-restart').addEventListener('click', () => { if (V.playing) { V.playing = false; clearTimeout(V.timer); } V.i = 0; render(); });
    $('#speed').addEventListener('change', (e) => { V.speed = +e.target.value; });
    for (const r of $$('input[name="net"]')) r.addEventListener('change', (e) => { V.net = e.target.value; syncNetLayers(); render(); });
    $('#lyr-truth').addEventListener('change', (e) => { V.truth = e.target.checked; paintTruth(V.truth); });
    $('#lyr-planb').addEventListener('change', (e) => { V.planb = e.target.checked; syncNetLayers(); });
    $('#lyr-risk').addEventListener('change', (e) => { V.risk = e.target.checked; syncNetLayers(); });
    for (const b of $$('.tabs button')) b.addEventListener('click', () => setTab(b.dataset.tab));
    chartTooltip();
    initCard();
    renderExplain();
    if (MODE === 'app') initLive();
    render();
    const h = location.hash.replace('#', '');
    if (h === 'card' || (h === 'live' && MODE === 'app')) setTab(h);
  }

  boot();
})();
