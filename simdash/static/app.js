"use strict";

// ---------- helpers ----------
const $ = (s, root = document) => root.querySelector(s);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function fmtLap(t) {
  if (t == null || t <= 0) return "–";
  const m = Math.floor(t / 60);
  const s = (t - m * 60).toFixed(3).padStart(6, "0");
  return `${m}:${s}`;
}
// signed difference: +1.20, −0.35, ±0.00 when it rounds to zero
function fmtDiff(v, digits = 0) {
  const a = Math.abs(v).toFixed(digits);
  return (+a === 0 ? "±" : v > 0 ? "+" : "−") + a;
}
const fmtSec = (t) => (t == null || t <= 0 ? "–" : t.toFixed(3));
function fmtDur(s) {
  if (s == null) return "–";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h${String(m).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
function fmtClock(s) {
  if (s == null) return "";
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} left`;
}
const pref = {
  get(k, d) { try { const v = localStorage.getItem("simdash." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("simdash." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

// ---------- tabs ----------
let currentTab = "live";
function showTab(name) {
  currentTab = name;
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "history") loadSessions();
  if (name === "progress") loadProgress();
  if (name === "genie") loadPilotTab();
  if (name !== "genie" && pf.replay) stopPilotReplay();
  if (name === "shakers" && !sh.dirty) loadShakers();
  if (name === "system") { loadSystem(); loadSystemLog(); }
  if (name === "leds") loadLeds(); else if (led.timer) stopLeds();
  // drop a link's hash once its tab is left (#session=… belongs to history)
  const own = { history: /^#session=/, progress: /^#progress(=|$)/, genie: /^#genie$/, shakers: /^#shakers$/, system: /^#system$/, leds: /^#leds$/ }[name];
  if (location.hash && !own?.test(location.hash)) history.replaceState(null, "", location.pathname);
  live.dirty = true;
}
document.querySelectorAll("nav button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

// ---------- map transform (shared by live, replay and lap maps) ----------
// Screen coordinates with y pointing down. Default: world -x right, +z down.
let mapMirror = pref.get("mapMirror", false);
let mapRot = pref.get("mapRot", 0);
// AMS2 world axes are left-handed: negate x so tracks are not drawn mirrored
function mapXY(x, z) {
  let X = mapMirror ? x : -x, Y = z;
  for (let k = 0; k < mapRot; k++) [X, Y] = [-Y, X];
  return [X, Y];
}
$("#map-mirror").onclick = () => { mapMirror = !mapMirror; pref.set("mapMirror", mapMirror); redrawMaps(); };
$("#map-rotate").onclick = () => { mapRot = (mapRot + 1) % 4; pref.set("mapRot", mapRot); redrawMaps(); };
function redrawMaps() {
  live.dirty = true;
  if (hist.lapData) drawLapMap();
  if (replay.cars) drawReplay();
  rp.lastDraw = -1;
}

// view (optional), in map coordinates: { bounds: [x0, x1, y0, y1], marks: [{ p: [X, Y], color }],
//   labels: [{ p, text }] (corner names), ticks: [{ a, b, text }] (sector start lines) }
function drawMap(canvas, outline, cars, startLine, view = null) {
  const { ctx, w, h } = fitCanvas(canvas);
  const pts = (outline || []).map((p) => mapXY(p[0], p[1]));
  const carPts = cars.map((c) => ({ ...c, p: mapXY(c.x, c.z) }));
  const boundPts = pts.length > 20 ? pts : pts.concat(carPts.map((c) => c.p));
  if (!boundPts.length) {
    ctx.fillStyle = css("--muted");
    ctx.font = "13px system-ui";
    ctx.textAlign = "center";
    ctx.fillText("No track outline yet: it is drawn as you drive", w / 2, h / 2);
    return;
  }
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  if (view?.bounds) [x0, x1, y0, y1] = view.bounds;
  else for (const [X, Y] of boundPts) { x0 = Math.min(x0, X); x1 = Math.max(x1, X); y0 = Math.min(y0, Y); y1 = Math.max(y1, Y); }
  const pad = 22;
  const sc = Math.min((w - 2 * pad) / Math.max(x1 - x0, 1), (h - 2 * pad) / Math.max(y1 - y0, 1));
  const ox = (w - (x1 - x0) * sc) / 2, oy = (h - (y1 - y0) * sc) / 2;
  const S = ([X, Y]) => [ox + (X - x0) * sc, oy + (Y - y0) * sc];

  if (pts.length > 1) {
    ctx.lineJoin = ctx.lineCap = "round";
    ctx.strokeStyle = css("--neutral");
    ctx.lineWidth = 9;
    ctx.beginPath();
    pts.forEach((p, i) => { const [a, b] = S(p); i ? ctx.lineTo(a, b) : ctx.moveTo(a, b); });
    ctx.stroke();
    if (startLine && pts.length > 3) {
      const [ax, ay] = S(pts[0]), [bx, by] = S(pts[3]);
      const len = Math.hypot(bx - ax, by - ay) || 1;
      const nx = -(by - ay) / len, ny = (bx - ax) / len;
      ctx.strokeStyle = css("--text");
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(ax - nx * 9, ay - ny * 9);
      ctx.lineTo(ax + nx * 9, ay + ny * 9);
      ctx.stroke();
    }
  }
  if (pts.length > 1) {
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.font = "600 11px system-ui";
    ctx.lineWidth = 2;
    for (const t of view?.ticks || []) {
      const [ax, ay] = S(t.a), [bx, by] = S(t.b);
      ctx.strokeStyle = css("--text-2");
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
      ctx.fillStyle = css("--text-2");
      ctx.fillText(t.text, bx + 3, by);
    }
    ctx.textAlign = "center";
    ctx.font = "11px system-ui";
    ctx.lineWidth = 3;
    ctx.strokeStyle = css("--surface");
    ctx.fillStyle = css("--text");
    for (const l of view?.labels || []) {
      const [a, b] = S(l.p), half = ctx.measureText(l.text).width / 2 + 3;
      if (a < 0 || a > w || b < 0 || b > h) continue;  // corner outside a zoomed map
      const x = clamp(a, half, w - half), y = clamp(b - 14, 8, h - 8);  // keep names near the edge readable
      ctx.strokeText(l.text, x, y);
      ctx.fillText(l.text, x, y);
    }
  }
  for (const m of view?.marks || []) {
    const [a, b] = S(m.p);
    ctx.beginPath();
    ctx.arc(a, b, 6, 0, 2 * Math.PI);
    ctx.strokeStyle = m.color;
    ctx.lineWidth = 3;
    ctx.stroke();
  }
  carPts.sort((a, b) => (a.player ? 1 : 0) - (b.player ? 1 : 0));
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const c of carPts) {
    const [a, b] = S(c.p);
    const r = c.player ? 11 : 9;
    ctx.globalAlpha = c.pit ? 0.45 : 1;
    ctx.beginPath();
    ctx.arc(a, b, r + 2, 0, 2 * Math.PI);
    ctx.fillStyle = css("--surface");
    ctx.fill();
    ctx.beginPath();
    ctx.arc(a, b, r, 0, 2 * Math.PI);
    ctx.fillStyle = c.player ? css("--s-speed") : css("--car");
    ctx.fill();
    ctx.fillStyle = "#ffffff";
    ctx.font = `600 ${c.player ? 11 : 10}px system-ui`;
    ctx.fillText(c.pos || "", a, b + 0.5);
  }
  ctx.globalAlpha = 1;
}

// ---------- live ----------
const live = { data: null, trace: [], dirty: false, trackRev: -1, outline: null, outlineSrc: "", lastTrackFetch: 0, lastTable: 0 };

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "backlog") { live.trace = m.trace; m.trace.forEach((r) => { focusRow(r); pilotRow(r, false); }); }
    else {
      live.data = m.live;
      if (m.live?.session && m.live.lap) refreshFocusRef(m.live);  // the corner recap works from any tab
      for (const r of m.trace) { live.trace.push(r); focusRow(r); pilotRow(r, true); }
      const last = live.trace.length ? live.trace[live.trace.length - 1][0] : 0;
      let cut = 0;
      while (cut < live.trace.length && live.trace[cut][0] < last - 60) cut++;
      if (cut) live.trace.splice(0, cut);
    }
    live.dirty = true;
  };
  ws.onclose = () => {
    setStatus("", "simdash offline, retrying…");
    setTimeout(connect, 1500);
  };
}

function setStatus(cls, text) {
  $("#status").className = "status " + cls;
  $("#status-text").textContent = text;
}

async function refreshOutline() {
  live.lastTrackFetch = performance.now();
  try {
    const t = await (await fetch("/api/live/track")).json();
    live.outline = t.points;
    live.outlineSrc = t.source;
    live.trackRev = t.rev;
    $("#map-src").textContent = t.source === "lap" ? `outline from a ${fmtLap(t.lap_time)} lap` : "outline: your path so far";
    live.dirty = true;
  } catch { /* retried on next frame */ }
}

function renderLive() {
  const d = live.data;
  if (!d) return;
  const st = d.status;
  setStatus(st === "recording" ? "recording" : st === "paused" ? "paused" : "",
    st === "recording" ? "Recording" : st === "paused" ? "Paused" : d.detail || st);
  const liveSh = {};
  for (const [ch, [a, fx]] of Object.entries(d.shakers || {})) liveSh[ch] = { a, fx };
  paintShakerCar("#live-car .spot", liveSh, d.status === "recording");
  if (!d.car) {
    $("#session-line").innerHTML = `<span class="muted">${esc(d.detail || "Waiting for AMS2")}</span>`;
    drawTrace();
    return;
  }
  const s = d.session, c = d.car, l = d.lap;
  $("#session-line").innerHTML = `<b>${esc(s.type)}</b> · ${esc(s.track)} ${esc(s.variation)} · ${esc(s.car)}`
    + ` <span class="muted">(${esc(s.car_class)}) · air ${s.air_temp}° · track ${s.track_temp}°${s.rain > 0 ? " · rain " + Math.round(s.rain * 100) + "%" : ""}</span>`;

  $("#t-speed").textContent = Math.round(c.speed);
  $("#t-gear").textContent = c.gear;
  $("#t-rpm").textContent = `${c.rpm} rpm`;
  const rf = c.max_rpm ? c.rpm / c.max_rpm : 0;
  $("#rpmbar div").style.width = `${clamp(rf, 0, 1) * 100}%`;
  $("#rpmbar").classList.toggle("shift", rf > 0.95);
  $("#t-pos").textContent = l.pos ? `P${l.pos}` : "–";
  $("#t-cars").textContent = s.num_cars > 1 ? `of ${s.num_cars}` : "";
  $("#t-lap").textContent = s.laps_in_event ? `${l.lap}/${s.laps_in_event}` : l.lap || "–";
  $("#t-left").textContent = s.laps_in_event ? "" : fmtClock(s.time_left);
  $("#t-cur").textContent = fmtLap(l.current);
  $("#t-cur").classList.toggle("invalid", l.invalid);
  $("#t-dist").textContent = l.length ? `${l.dist} / ${l.length} m` : "";
  $("#t-last").textContent = fmtLap(l.last);
  $("#t-best").textContent = fmtLap(l.best);
  $("#t-fuel").textContent = c.fuel_l.toFixed(1);

  const bar = (id, v, txt) => { $(id).style.height = `${clamp(v, 0, 1) * 100}%`; $(txt).textContent = Math.round(v * 100); };
  bar("#b-thr", c.throttle, "#v-thr");
  bar("#b-brk", c.brake, "#v-brk");
  bar("#b-clu", c.clutch, "#v-clu");
  const sv = clamp(c.steer, -1, 1);
  Object.assign($("#b-steer").style, { left: `${50 + Math.min(0, sv) * 50}%`, width: `${Math.abs(sv) * 50}%` });
  $("#v-steer").textContent = Math.round(sv * 100);
  $("#abs").classList.toggle("on", c.abs);

  $("#v-g").textContent = `${Math.hypot(c.g_lat, c.g_long).toFixed(2)} g`;
  drawGG();

  for (let i = 0; i < 4; i++) {
    const el = $("#ty" + i), v = c.slip ? c.slip[i] : null;
    if (v == null) {
      el.querySelector(".pct").textContent = "–";
      el.style.backgroundColor = "";
    } else {
      el.querySelector(".pct").textContent = `${v > 0 ? "+" : ""}${Math.round(v * 100)}%`;
      const col = v > 0 ? "var(--s-spin)" : "var(--s-lock)";
      const p = Math.round(clamp(Math.abs(v) / 0.25, 0, 1) * 75);
      el.style.backgroundColor = p > 4 ? `color-mix(in srgb, ${col} ${p}%, var(--surface-2))` : "";
    }
    el.querySelector(".t").textContent = `${["FL", "FR", "RL", "RR"][i]} · ${c.tyre_temp[i]}° · brake ${c.brake_temp[i]}°`;
  }
  $("#slip-note").textContent = c.slip ? "+ wheelspin   − lock-up"
    : "Learning wheel size: coast straight above 50 km/h off the pedals for about 1 s.";
  const a = clamp(c.slip_angle, -15, 15);
  Object.assign($("#b-angle").style, { left: `${50 + Math.min(0, a) / 15 * 50}%`, width: `${Math.abs(a) / 15 * 50}%` });
  $("#v-angle").textContent = c.slip_angle.toFixed(1);

  drawTrace();

  if (d.track_rev !== live.trackRev || (live.outlineSrc === "trail" && performance.now() - live.lastTrackFetch > 4000)) {
    if (performance.now() - live.lastTrackFetch > 1000) refreshOutline();
  }
  refreshFocusRef(d);
  drawMap($("#map"), live.outline, d.cars, live.outlineSrc === "lap", liveMapView());
  if (focus.corners[focus.corner] && performance.now() - focus.lastDraw > 300) {
    focus.lastDraw = performance.now();
    drawCornerFocus();
  }

  if (performance.now() - live.lastTable > 250) {
    live.lastTable = performance.now();
    $("#standings tbody").innerHTML = d.cars.map((r) => `<tr class="${r.player ? "player" : ""}">
      <td class="r">${r.pos || ""}</td><td>${esc(r.name)}</td><td class="r">${r.lap}</td>
      <td class="r">${r.player ? "" : (r.gap > 0 ? "+" : "") + r.gap.toFixed(1)}</td>
      <td class="r">${fmtLap(r.last)}</td><td class="r">${fmtLap(r.best)}</td>
      <td class="muted">${esc(r.pit || (r.state !== "racing" && r.state !== "not started" ? r.state : ""))}</td></tr>`).join("");
  }
}

// ---------- corner names and sectors ----------
// AMS2 has no corner names: the user names corners, saved per track layout and matched to the
// detected corners by apex distance. Sector starts come from each lap's recorded sector number.
const cornerNames = {};  // "track|variation" -> [{ apex, name }]
async function loadCornerNames(track, variation) {
  try {
    const r = await fetch(`/api/corners?track=${encodeURIComponent(track)}&variation=${encodeURIComponent(variation)}`);
    cornerNames[`${track}|${variation}`] = r.ok ? await r.json() : [];
  } catch { /* keep the C1, C2... numbers */ }
}
const namedCorner = (tk, c) => (cornerNames[tk] || []).find((n) => Math.abs(n.apex - c.apex) <= 80);
const cornerName = (tk, c, k) => namedCorner(tk, c)?.name || `C${k + 1}`;

async function renameCorner(tk, c, k) {
  const [track, variation] = tk.split("|");
  const name = prompt(`Name for C${k + 1} (apex at ${Math.round(c.apex)} m). Leave empty to go back to C${k + 1}.`,
    namedCorner(tk, c)?.name || "");
  if (name === null) return;
  const r = await fetch("/api/corners", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ track, variation, apex: c.apex, name }) });
  if (!r.ok) return;
  cornerNames[tk] = await r.json();
  if (tk === hist.trackKey && hist.lapData) { drawCorners(); drawLapMap(); drawTraces(); if (replay.cars) drawReplay(); }
  if (tk === focus.trackKey) { liveCornerOptions(); focus.lastDraw = 0; live.dirty = true; }
}

// corner names at each apex and a short line across the track where each sector starts, in map coordinates
function trackMarks(L, tk) {
  if (!L?.lap_dist?.length) return { labels: [], ticks: [] };
  const P = (i) => mapXY(L.x[i], L.z[i]);
  const labels = findCorners(L).map((c, k) => ({ p: P(sampleAt(L, c.apex)), text: cornerName(tk, c, k) }));
  const ticks = [0, ...(L.sectors || [])].map((dd, n) => {
    const i = sampleAt(L, dd), [ax, ay] = P(Math.max(0, i - 3)), [bx, by] = P(Math.min(L.x.length - 1, i + 3));
    const len = Math.hypot(bx - ax, by - ay) || 1, nx = (-(by - ay) / len) * 18, ny = ((bx - ax) / len) * 18, [cx, cy] = P(i);
    return { a: [cx - nx, cy - ny], b: [cx + nx, cy + ny], text: `S${n + 1}` };
  });
  return { labels, ticks };
}
const sectorOf = (L, dd) => 1 + (L.sectors || []).filter((b) => b <= dd).length;

// ---------- live corner focus ----------
// The reference is your best lap on this track layout with this car, from any session. Picking one of
// its corners zooms the track map on it and compares every pass through it with the reference.
// Live trace rows: [ts, speed, throttle, brake, steer, g_lat, g_long, slip_angle, spin, lock,
//                   laps_completed, lap_dist, lap_time, gear, x, z]
const LT = { lap: 10, dist: 11, time: 12, gear: 13 };
const focus = { key: null, lastKey: null, fetching: false, trackKey: "", ref: null, refLabel: "", corners: [], corner: null,
  buf: [], bufLap: null, passDone: true, passes: [], lastDraw: 0 };

const rowsAsLap = (rows) => ({ lap_dist: rows.map((r) => r[LT.dist]), speed: rows.map((r) => r[1]), throttle: rows.map((r) => r[2]),
  brake: rows.map((r) => r[3]), gear: rows.map((r) => r[LT.gear]), lap_time: rows.map((r) => r[LT.time]) });
// part of the lap shown around a corner
const cornerWindow = (c) => [Math.max(c.s, c.apex - 300), Math.min(c.e, c.apex + 200)];

function focusRow(r) {
  if (r.length <= LT.gear) return;
  if (r[LT.lap] !== focus.bufLap) {
    finishPass();
    focus.bufLap = r[LT.lap];
    focus.buf = [];
    focus.passDone = false;
  }
  focus.buf.push(r);
  const c = focus.corners[focus.corner];
  if (c && !focus.passDone && r[LT.dist] > c.e) finishPass();
}

// a pass counts only if it was seen from the corner's segment start (or the lap start) to its end
function finishPass() {
  const c = focus.corners[focus.corner], rows = focus.buf;
  if (!c || focus.passDone || !focus.ref || !rows.length) return;
  focus.passDone = true;
  if (rows[0][LT.dist] > Math.max(c.s, 0) + 100 || (c.e !== Infinity && rows[rows.length - 1][LT.dist] < c.e)) return;
  const a = cornerStats(rowsAsLap(rows), c), b = cornerStats(focus.ref, c);
  if (!a) return;
  const [from, to] = cornerWindow(c);
  focus.passes.unshift({ lap: focus.bufLap + 1, a, dt: b ? a.time - b.time : null,
    rows: rows.filter((r) => r[LT.dist] >= from && r[LT.dist] <= to) });
  focus.passes.length = Math.min(focus.passes.length, 8);
}

async function refreshFocusRef(d) {
  const s = d.session, key = `${s.track}|${s.variation}|${s.car}|${d.lap.best ?? ""}`;
  if (key === focus.key || focus.fetching) return;
  focus.fetching = true;
  try {
    const r = await (await fetch("/api/live/reference")).json();
    focus.key = key;
    // a new best lap may reach the database just after the game reports it: look once more shortly after
    if (key !== focus.lastKey) setTimeout(() => { if (focus.key === key) focus.key = null; }, 3000);
    focus.lastKey = key;
    focus.ref = r.samples?.ts.length ? r.samples : null;
    focus.refLabel = focus.ref ? `vs best ${fmtLap(r.lap_time)} (${new Date(r.started_at * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" })})` : "";
    // the selected corner is kept by apex distance: a new reference can number the corners differently
    const trackKey = `${s.track}|${s.variation}`, sameTrack = trackKey === focus.trackKey;
    const apex = sameTrack && focus.corner != null ? focus.corners[focus.corner].apex
      : pref.get("cornerApex." + trackKey, null);
    focus.corners = focus.ref ? findCorners(focus.ref) : [];
    focus.trackKey = trackKey;
    await loadCornerNames(s.track, s.variation);
    const k = apex == null ? -1 : focus.corners.findIndex((c) => Math.abs(c.apex - apex) <= 80);
    if (k >= 0 && focus.corner != null && sameTrack) focus.corner = k;  // same corner: keep its passes
    else if (k >= 0) setCorner(k);
    else if (focus.corner != null) setCorner(null);
    liveCornerOptions();
  } catch { /* retried on the next frame */ } finally {
    focus.fetching = false;
  }
}

function liveCornerOptions() {
  $("#live-corner").innerHTML = `<option value="">Whole track</option>` + (focus.corners.length
    ? focus.corners.map((c, k) => `<option value="${k}">${esc(cornerName(focus.trackKey, c, k))} · ${Math.round(c.apex)} m</option>`).join("")
    : `<option disabled>corners: drive a valid lap first</option>`);
  $("#live-corner").value = focus.corner ?? "";
}

function setCorner(k) {
  const c = focus.corners[k];
  focus.corner = c ? k : null;
  focus.passes = [];
  // already past this corner's start on the current lap: wait for the next lap
  const last = focus.buf[focus.buf.length - 1];
  focus.passDone = !c || !last || last[LT.dist] > Math.max(c.s, 0) + 100;
  $("#live-corner").value = focus.corner ?? "";
  pref.set("cornerApex." + focus.trackKey, c ? c.apex : null);
  $("#corner-card").hidden = !c;
  if (!c) Plotly.purge("corner-plot");
  focus.lastDraw = 0;
  live.dirty = true;
}
$("#corner-title").onclick = (e) => {
  if (e.target.closest(".rename")) renameCorner(focus.trackKey, focus.corners[focus.corner], focus.corner);
};
$("#live-corner").onchange = (e) => setCorner(e.target.value === "" ? null : +e.target.value);

// corner names and sector lines from the reference lap; with a corner picked, the map zooms on it
// and marks the reference's braking point (red) and apex (blue)
function liveMapView() {
  const c = focus.corners[focus.corner], R = focus.ref;
  if (!R) return null;
  const view = trackMarks(R, focus.trackKey);
  if (!c) return view;
  const [from, to] = cornerWindow(c);
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  R.lap_dist.forEach((dd, i) => {
    if (dd < from || dd > to) return;
    const [X, Y] = mapXY(R.x[i], R.z[i]);
    x0 = Math.min(x0, X); x1 = Math.max(x1, X); y0 = Math.min(y0, Y); y1 = Math.max(y1, Y);
  });
  if (x0 === Infinity) return view;
  const pad = Math.max(x1 - x0, y1 - y0) * 0.1 + 15;
  const b = cornerStats(R, c), at = (dd) => { const i = sampleAt(R, dd); return mapXY(R.x[i], R.z[i]); };
  return { ...view, bounds: [x0 - pad, x1 + pad, y0 - pad, y1 + pad],
    marks: [...(b.brake != null ? [{ p: at(b.brake), color: css("--s-brake") }] : []), { p: at(c.apex), color: css("--s-speed") }] };
}

function drawCornerFocus() {
  const c = focus.corners[focus.corner], R = focus.ref;
  const [from, to] = cornerWindow(c);
  $("#corner-title").innerHTML = `${esc(cornerName(focus.trackKey, c, focus.corner))} · apex ${Math.round(c.apex)} m · S${sectorOf(R, c.apex)}`
    + ` <a class="rename" title="Name this corner">✎</a>`;
  $("#corner-ref").textContent = focus.refLabel;
  const inWin = (L) => L.lap_dist.map((dd, i) => i).filter((i) => L.lap_dist[i] >= from && L.lap_dist[i] <= to);
  const series = (L, idx, key, color, dash, width, opacity = 1, yaxis = "y") => ({
    x: idx.map((i) => L.lap_dist[i]), y: idx.map((i) => L[key][i] * (key === "speed" ? 1 : 100)), yaxis,
    type: "scatter", mode: "lines", hoverinfo: "skip", opacity, line: { color: css(color), dash, width } });
  const traces = [];
  // reference dotted, previous pass faint, current lap solid
  const cur = rowsAsLap(focus.buf), prev = focus.passes.find((p) => p.lap !== focus.bufLap + 1);
  for (const [L, dash, width, opacity] of [[R, "dot", 2, 1], [prev && rowsAsLap(prev.rows), "solid", 1.5, 0.35], [cur, "solid", 2.5, 1]]) {
    if (!L) continue;
    const idx = inWin(L);
    traces.push(series(L, idx, "speed", "--s-speed", dash, width, opacity),
      series(L, idx, "throttle", "--s-throttle", dash, width, opacity, "y2"),
      series(L, idx, "brake", "--s-brake", dash, width, opacity, "y2"));
  }
  Plotly.react("corner-plot", traces, plotLayout({
    showlegend: false, margin: { l: 40, r: 8, t: 6, b: 24 }, uirevision: `${focus.trackKey}:${focus.corner}`,
    xaxis: { ...axisStyle(), range: [from, to], fixedrange: true },
    yaxis: { ...axisStyle(), domain: [0.42, 1], fixedrange: true, title: { text: "km/h", font: { size: 10, color: css("--muted") } } },
    yaxis2: { ...axisStyle(), domain: [0, 0.36], range: [-5, 105], fixedrange: true, title: { text: "%", font: { size: 10, color: css("--muted") } } },
  }), { displayModeBar: false, responsive: true, staticPlot: true });

  const b = cornerStats(R, c);
  const diff = (v, r, unit = "") => (v != null && r != null ? ` <span class="muted">${fmtDiff(v - r)}${unit}</span>` : "");
  const brake = (a) => a.brake == null ? `<span class="muted">lift</span>` : `${Math.round(a.brake)} m`
    + (b.brake != null && a !== b ? ` <span class="muted">${Math.abs(a.brake - b.brake) < 3 ? "same" : `${Math.round(Math.abs(a.brake - b.brake))} m ${a.brake > b.brake ? "later" : "earlier"}`}</span>` : "");
  const row = (name, a, dt, cls = "") => `<tr class="${cls}"><td>${name}</td><td class="r">${brake(a)}</td>
    <td class="r">${Math.round(a.entry)}${a === b ? "" : diff(a.entry, b.entry)}</td>
    <td class="r">${Math.round(a.min)}${a === b ? "" : diff(a.min, b.min)}</td>
    <td class="r">${Math.round(a.exit)}${a === b ? "" : diff(a.exit, b.exit)}</td><td class="r">${a.gear ?? ""}</td>
    <td class="r ${dt == null ? "" : dt > 0.005 ? "lost" : dt < -0.005 ? "gained" : ""}">${dt == null ? "" : fmtDiff(dt, 2)}</td></tr>`;
  $("#corner-passes tbody").innerHTML = (b ? row("Reference", b, null, "ref") : "")
    + focus.passes.map((p) => row(`Lap ${p.lap}`, p.a, p.dt)).join("")
    + (focus.passes.length ? "" : `<tr><td colspan="7" class="empty">Drive through the corner: each pass shows up here.</td></tr>`);
}

function drawGG() {
  const { ctx, w, h } = fitCanvas($("#gg"));
  const now = live.trace.length ? live.trace[live.trace.length - 1][0] : 0;
  const recent = live.trace.filter((r) => r[0] > now - 3);
  let maxg = 2;
  for (const r of recent) maxg = Math.min(4, Math.max(maxg, Math.ceil(Math.hypot(r[5], r[6]))));
  const cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 16, k = R / maxg;
  ctx.strokeStyle = css("--grid");
  ctx.lineWidth = 1;
  for (let g = 1; g <= maxg; g++) { ctx.beginPath(); ctx.arc(cx, cy, g * k, 0, 2 * Math.PI); ctx.stroke(); }
  ctx.strokeStyle = css("--axis");
  ctx.beginPath(); ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy); ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R); ctx.stroke();
  ctx.fillStyle = css("--muted");
  ctx.font = "10px system-ui";
  ctx.textAlign = "center";
  ctx.fillText("accel", cx, 9);
  ctx.fillText("brake", cx, h - 3);
  ctx.textAlign = "left"; ctx.fillText("L", 1, cy - 4);
  ctx.textAlign = "right"; ctx.fillText("R", w - 1, cy - 4);
  ctx.fillText(`${maxg} g`, cx + R * 0.71 + 18, cy - R * 0.71);
  // x = lateral (right +), y = longitudinal (accel up)
  const P = (r) => [cx + r[5] * k, cy - r[6] * k];
  ctx.strokeStyle = css("--s-speed");
  ctx.lineWidth = 2;
  ctx.globalAlpha = 0.45;
  ctx.beginPath();
  recent.forEach((r, i) => { const [a, b] = P(r); i ? ctx.lineTo(a, b) : ctx.moveTo(a, b); });
  ctx.stroke();
  ctx.globalAlpha = 1;
  if (recent.length) {
    const [a, b] = P(recent[recent.length - 1]);
    ctx.beginPath(); ctx.arc(a, b, 7, 0, 2 * Math.PI); ctx.fillStyle = css("--surface"); ctx.fill();
    ctx.beginPath(); ctx.arc(a, b, 5, 0, 2 * Math.PI); ctx.fillStyle = css("--s-speed"); ctx.fill();
  }
}

function drawTrace() {
  const { ctx, w, h } = fitCanvas($("#trace"));
  const tr = live.trace;
  if (!tr.length) return;
  const t1 = tr[tr.length - 1][0], t0 = t1 - 30;
  const rows = tr.filter((r) => r[0] >= t0);
  let vmax = 100;
  for (const r of rows) vmax = Math.max(vmax, r[1]);
  vmax = Math.ceil((vmax * 1.05) / 50) * 50;
  const strips = [
    { name: "Speed", unit: "km/h", lo: 0, hi: vmax, frac: 0.3, series: [{ i: 1, c: "--s-speed" }] },
    { name: "Pedals", unit: "%", lo: 0, hi: 1, frac: 0.25, scale: 100, series: [{ i: 2, c: "--s-throttle" }, { i: 3, c: "--s-brake" }] },
    { name: "Wheel slip", unit: "%", lo: -0.3, hi: 0.3, frac: 0.25, scale: 100, zero: true,
      series: [{ f: (r) => (r[8] == null ? null : Math.max(0, r[8])), c: "--s-spin" }, { f: (r) => (r[9] == null ? null : Math.min(0, r[9])), c: "--s-lock" }] },
    { name: "Slip angle", unit: "°", lo: -15, hi: 15, frac: 0.2, zero: true, series: [{ i: 7, c: "--s-angle" }] },
  ];
  const left = 4, right = w - 4, X = (t) => left + ((t - t0) / 30) * (right - left);
  let y = 0;
  ctx.font = "11px system-ui";
  for (const s of strips) {
    const sh = h * s.frac, top = y + 14, bot = y + sh - 4;
    const Y = (v) => bot - ((clamp(v, s.lo, s.hi) - s.lo) / (s.hi - s.lo)) * (bot - top);
    ctx.strokeStyle = css("--grid");
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(left, bot + 0.5); ctx.lineTo(right, bot + 0.5); ctx.stroke();
    if (s.zero) { ctx.strokeStyle = css("--axis"); ctx.beginPath(); ctx.moveTo(left, Y(0) + 0.5); ctx.lineTo(right, Y(0) + 0.5); ctx.stroke(); }
    const last = rows[rows.length - 1];
    const vals = s.series.map((se) => (se.f ? se.f(last) : last[se.i]));
    ctx.fillStyle = css("--text-2");
    ctx.textAlign = "left";
    ctx.fillText(s.name, left, y + 10);
    ctx.textAlign = "right";
    ctx.fillText(vals.map((v) => (v == null ? "–" : (v * (s.scale || 1)).toFixed(s.unit === "°" ? 1 : 0))).join(" / ") + " " + s.unit, right, y + 10);
    for (const se of s.series) {
      ctx.strokeStyle = css(se.c);
      ctx.lineWidth = 2;
      ctx.beginPath();
      let pen = false;
      for (const r of rows) {
        const v = se.f ? se.f(r) : r[se.i];
        if (v == null) { pen = false; continue; }
        const px = X(r[0]), py = Y(v);
        pen ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
        pen = true;
      }
      ctx.stroke();
    }
    y += sh;
  }
}

function frame() {
  if (live.dirty && currentTab === "live") {
    live.dirty = false;
    renderLive();
  }
  requestAnimationFrame(frame);
}
window.addEventListener("resize", () => { live.dirty = true; if (replay.cars) drawReplay(); });

// ---------- history ----------
// cmp: compare lap number in this session, or null; a lap from another session has only cmpData / cmpLabel
const hist = { sessions: [], ref: null, detail: null, lap: null, lapData: null, cmp: null, cmpData: null, cmpLabel: null,
  lapReq: {}, loaded: {}, shown: new Set(), zoomDist: null, mapRev: null, mapMarker: 0 };

async function loadSessions() {
  try {
    hist.sessions = await (await fetch("/api/sessions")).json();
  } catch {
    return;
  }
  const days = [...new Set(hist.sessions.map((s) => s.day))];
  const sel = $("#f-day"), cur = sel.value;
  sel.innerHTML = `<option value="">All days</option>` + days.map((d) => `<option ${d === cur ? "selected" : ""}>${d}</option>`).join("");
  renderSessions();
  const m = location.hash.match(/session=([\d-]+:\d+)/);
  if (m && m[1] !== hist.ref) openSession(m[1]);
}

function renderSessions() {
  const q = $("#f-search").value.trim().toLowerCase();
  const type = $("#f-type").value, day = $("#f-day").value;
  const rows = hist.sessions.filter((s) => (!type || s.session_type === type) && (!day || s.day === day)
    && (!q || [s.track, s.variation, s.car, s.car_class, s.session_type].join(" ").toLowerCase().includes(q)));
  $("#f-count").textContent = `${rows.length} session${rows.length === 1 ? "" : "s"}`;
  const tb = $("#sessions-table tbody");
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="9" class="empty">${hist.sessions.length ? "No session matches." : "No sessions recorded yet. Drive in AMS2 with simdash running."}</td></tr>`;
    return;
  }
  tb.innerHTML = rows.map((s) => {
    const t = new Date(s.started_at * 1000);
    const pos = s.session_type === "Race" && s.start_pos ? `P${s.start_pos} → P${s.final_pos ?? "?"}` : s.final_pos ? `P${s.final_pos}` : "";
    return `<tr data-ref="${s.ref}" class="${s.ref === hist.ref ? "sel" : ""}">
      <td>${s.day}</td><td>${t.toTimeString().slice(0, 5)}</td>
      <td><span class="pill ${esc((s.session_type || "").toLowerCase())}">${esc(s.session_type)}</span></td>
      <td>${esc(s.track)} <span class="muted">${esc(s.variation)}</span></td><td>${esc(s.car)}</td>
      <td class="r">${s.laps ?? 0}</td><td class="r">${fmtLap(s.best_lap)}</td><td class="r">${pos}</td>
      <td class="r">${s.ended_at ? fmtDur(s.driving_s) : "live"}</td></tr>`;
  }).join("");
}
$("#sessions-table tbody").onclick = (e) => { const tr = e.target.closest("tr[data-ref]"); if (tr) openSession(tr.dataset.ref); };
$("#f-search").oninput = renderSessions;
$("#f-type").onchange = renderSessions;
$("#f-day").onchange = renderSessions;
$("#f-refresh").onclick = loadSessions;

async function openSession(ref) {
  const r = await fetch(`/api/sessions/${ref}`);
  if (!r.ok) return;
  const d = await r.json();
  hist.ref = ref;
  hist.detail = d;
  hist.trackKey = `${d.session.track}|${d.session.variation}`;
  loadCornerNames(d.session.track, d.session.variation).then(() => {
    if (hist.ref === ref && hist.lapData) { drawCorners(); drawLapMap(); drawTraces(); if (replay.cars) drawReplay(); }
  });
  history.replaceState(null, "", `#session=${ref}`);
  renderSessions();
  const s = d.session;
  $("#session-detail").hidden = false;
  $("#d-title").textContent = `${s.session_type} · ${s.track} ${s.variation}`;
  $("#d-sub").textContent = `${s.car} (${s.car_class}) · ${new Date(s.started_at * 1000).toLocaleString()}${d.live ? " · recording now" : ""}`;
  $("#d-csv").href = `/api/sessions/${ref}/samples.csv`;
  $("#d-progress").href = `#progress=${encodeURIComponent(comboKey(s))}`;
  const tile = (label, value, sub = "") => `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  $("#d-tiles").innerHTML = [
    tile("Best lap", fmtLap(s.best_lap)),
    tile("Laps", s.laps ?? 0, s.laps_in_event ? `of ${s.laps_in_event}` : ""),
    tile("Position", s.final_pos ? `P${s.final_pos}` : "–", s.start_pos && s.session_type === "Race" ? `started P${s.start_pos}` : ""),
    tile("Result", esc(s.result || "–")),
    tile("Driving", fmtDur(s.driving_s)),
    tile("Cars", s.num_cars ?? "–"),
    tile("Air / track", `${Math.round(s.air_temp)}° / ${Math.round(s.track_temp)}°`, s.rain > 0 ? `rain ${Math.round(s.rain * 100)}%` : ""),
  ].join("");

  const timed = d.laps.filter((l) => l.lap_time);
  const best = timed.filter((l) => l.valid).reduce((a, l) => (!a || l.lap_time < a.lap_time ? l : a), null);
  hist.bestLap = best ? best.lap : null;
  renderLaps();
  $("#compare").value = "";
  fillCompare([]);
  fetch(`/api/sessions/${ref}/references`).then((r) => (r.ok ? r.json() : [])).then((refs) => { if (hist.ref === ref) fillCompare(refs); });
  hist.cmp = null;
  hist.cmpData = null;
  hist.cmpLabel = null;
  hist.lapReq = {};
  hist.loaded = {};
  hist.shown = new Set(d.laps.map((l) => l.lap));
  hist.zoomDist = null;
  d.laps.forEach((l) => getLap(l.lap).then(scheduleLapMap));
  const first = best || timed[timed.length - 1] || d.laps[d.laps.length - 1];
  if (first) selectLap(first.lap);
  else {
    hist.lapData = null;
    Plotly.purge("lap-map");
    Plotly.purge("lap-traces");
    hist.mapRev = null;
    $("#map-laps").innerHTML = "";
    $("#lap-map").innerHTML = $("#lap-traces").innerHTML = `<div class="empty">No completed lap in this session.</div>`;
    $("#corners-table tbody").innerHTML = "";
  }

  $("#results-table tbody").innerHTML = d.participants.map((p) => `<tr class="${p.is_player ? "player" : ""}">
    <td class="r">${p.pos ?? ""}</td><td>${esc(p.name)}</td><td>${esc(p.car)}</td><td class="r">${p.laps_completed ?? ""}</td>
    <td class="r">${fmtLap(p.best_lap)}</td><td class="muted">${esc(p.race_state)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="empty">No participant data.</td></tr>`;
  initReplay(d);
  loadPositions(d);
}

function renderLaps() {
  const d = hist.detail;
  $("#laps-table tbody").innerHTML = d.laps.map((l) => `<tr data-lap="${l.lap}" class="${l.lap === hist.lap ? "sel" : ""}">
    <td class="r">${l.lap}</td>
    <td class="r ${l.lap === hist.bestLap ? "best" : l.valid ? "" : "invalid"}">${fmtLap(l.lap_time)}</td>
    <td class="r">${fmtSec(l.s1)}</td><td class="r">${fmtSec(l.s2)}</td><td class="r">${fmtSec(l.s3)}</td>
    <td class="r">${l.position ?? ""}</td><td class="r">${l.top_speed ? Math.round(l.top_speed) : ""}</td>
    <td class="r">${l.fuel_used ? l.fuel_used.toFixed(2) : ""}</td></tr>`).join("")
    || `<tr><td colspan="8" class="empty">No completed laps.</td></tr>`;
}
$("#laps-table tbody").onclick = (e) => { const tr = e.target.closest("tr[data-lap]"); if (tr) selectLap(+tr.dataset.lap); };
// compare picker values: "N" for lap N of this session, "<ref>/N" for lap N of another session
function fillCompare(refs) {
  const d = hist.detail, cur = $("#compare").value;
  const day = (r) => new Date(r.started_at * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  $("#compare").innerHTML = `<option value="">none</option><optgroup label="This session">`
    + d.laps.map((l) => `<option value="${l.lap}">Lap ${l.lap} · ${fmtLap(l.lap_time)}${l.valid ? "" : " (invalid)"}</option>`).join("")
    + `</optgroup>` + (refs.length ? `<optgroup label="Best laps, other sessions (same track and car)">`
    + refs.map((r) => `<option value="${r.ref}/${r.lap}" data-label="${esc(`${day(r)} lap ${r.lap}`)}">${day(r)} · ${esc(r.session_type)} · lap ${r.lap} · ${fmtLap(r.lap_time)}</option>`).join("")
    + `</optgroup>` : "");
  $("#compare").value = cur;
}
const extLaps = {};
$("#compare").onchange = async (e) => {
  const v = e.target.value, [ref, n] = v.includes("/") ? v.split("/") : [null, v];
  hist.cmp = v && !ref ? +n : null;
  hist.cmpLabel = !v ? null : ref ? e.target.selectedOptions[0].dataset.label : `lap ${n}`;
  hist.cmpData = !v ? null : ref ? await (extLaps[v] ??= fetchLap(n, ref)) : await getLap(hist.cmp);
  drawTraces();
  drawLapMap();
  drawCorners();
};
$("#map-color").onchange = () => drawLapMap();
$("#traces-zoom").onclick = (e) => {
  if (e.target.id === "traces-unzoom") Plotly.relayout("lap-map", { "xaxis.autorange": true, "yaxis.autorange": true });
};
// laps arrive one by one: redraw the map once per frame at most
let lapMapQueued = false;
function scheduleLapMap() {
  if (lapMapQueued) return;
  lapMapQueued = true;
  requestAnimationFrame(() => { lapMapQueued = false; if (hist.lapData) drawLapMap(); });
}

async function fetchLap(n, ref = hist.ref) {
  return (await fetch(`/api/sessions/${ref}/lap/${n}`)).json();
}
// one request per lap, shared by the map, the traces and the compare picker
function getLap(n) {
  const ref = hist.ref;
  hist.lapReq[n] ??= fetchLap(n).then((L) => { if (hist.ref === ref) hist.loaded[n] = L; return L; });
  return hist.lapReq[n];
}

async function selectLap(n) {
  hist.lap = n;
  hist.shown.add(n);
  renderLaps();
  hist.lapData = await getLap(n);
  drawLapMap();
  drawTraces();
  drawCorners();
}

function plotLayout(extra) {
  return {
    paper_bgcolor: css("--surface"), plot_bgcolor: css("--surface"),
    font: { family: "system-ui, sans-serif", size: 11, color: css("--text-2") },
    margin: { l: 48, r: 12, t: 8, b: 30 },
    hoverlabel: { bgcolor: css("--surface-2"), bordercolor: css("--axis"), font: { color: css("--text") } },
    ...extra,
  };
}
const axisStyle = () => ({ gridcolor: css("--grid"), zerolinecolor: css("--axis"), linecolor: css("--axis"), tickfont: { color: css("--muted") } });

function worstSlip(L) {
  return L.ts.map((_, i) => {
    const w = [L.slip_fl[i], L.slip_fr[i], L.slip_rl[i], L.slip_rr[i]].filter((v) => v != null);
    if (!w.length) return 0;
    const hi = Math.max(...w), lo = Math.min(...w);
    return hi > -lo ? hi : lo;
  });
}

// analysed lap blue, compare lap orange, the other laps cycle through the remaining series colours
const LAP_COLOURS = ["--s-throttle", "--s-angle", "--s-lock", "--s-clutch", "--s-brake"];
function lapSwatch(n) {
  if (n === hist.lap) return css("--s-speed");
  if (n === hist.cmp) return css("--s-spin");
  const others = hist.detail.laps.map((l) => l.lap).filter((m) => m !== hist.lap && m !== hist.cmp);
  return css(LAP_COLOURS[others.indexOf(n) % LAP_COLOURS.length]);
}

// colour by sector ranking: one hue per sector, darker = faster among the shown laps
const SECTOR_HUES = ["--s-speed", "--s-brake", "--s-throttle"];
// "#rrggbb" a mixed with b: f = 0 gives a, 1 gives b
function mixHex(a, b, f) {
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const [x, y] = [p(a), p(b)];
  return "#" + x.map((v, i) => Math.round(v + (y[i] - v) * f).toString(16).padStart(2, "0")).join("");
}
// lap -> [S1, S2, S3] colour (null without a time) and rank text, over the shown laps
function sectorShades() {
  const out = {}, laps = hist.detail.laps.filter((l) => hist.shown.has(l.lap));
  laps.forEach((l) => (out[l.lap] = { color: [null, null, null], rank: ["", "", ""] }));
  ["s1", "s2", "s3"].forEach((key, k) => {
    const timed = laps.filter((l) => l[key] > 0).sort((a, b) => a[key] - b[key]);
    const dark = mixHex(css(SECTOR_HUES[k]), "#000000", 0.35);
    timed.forEach((l, r) => {
      const f = timed.length > 1 ? r / (timed.length - 1) : 0;  // 0 fastest, 1 slowest
      out[l.lap].color[k] = mixHex(dark, "#ffffff", 0.78 * f);
      out[l.lap].rank[k] = `S${k + 1} ${fmtSec(l[key])} (P${r + 1}/${timed.length})`;
    });
  });
  return out;
}

function renderMapLaps() {
  const d = hist.detail, byLap = $("#map-color").value === "lap", bySector = $("#map-color").value === "sectors";
  const shades = bySector ? sectorShades() : null;
  const all = d.laps.every((l) => hist.shown.has(l.lap)), none = !d.laps.some((l) => hist.shown.has(l.lap));
  $("#map-laps").innerHTML = `<label class="all"><input type="checkbox" data-lap="all" ${all ? "checked" : ""}> All laps</label>`
    + d.laps.map((l) => `<label class="${l.lap === hist.lap ? "sel" : ""}" title="${l.lap === hist.lap ? "analysed lap" : l.lap === hist.cmp ? "compare lap" : ""}">
      <input type="checkbox" data-lap="${l.lap}" ${hist.shown.has(l.lap) ? "checked" : ""}>
      ${byLap ? `<span class="sw" style="background:${lapSwatch(l.lap)}"></span>` : ""}${shades?.[l.lap]
        ? shades[l.lap].color.map((c, k) => `<span class="sw sq" title="${shades[l.lap].rank[k]}" style="background:${c || "transparent"}"></span>`).join("") : ""}L${l.lap}
      <span class="t num ${l.lap === hist.bestLap ? "best" : l.valid ? "" : "invalid"}">${fmtLap(l.lap_time)}</span></label>`).join("");
  $("#map-laps input[data-lap=all]").indeterminate = !all && !none;
}
$("#map-laps").onchange = (e) => {
  const v = e.target.dataset.lap;
  if (v === "all") hist.shown = new Set(e.target.checked ? hist.detail.laps.map((l) => l.lap) : []);
  else if (e.target.checked) hist.shown.add(+v);
  else hist.shown.delete(+v);
  drawLapMap();
};

// surface classes sent by the lap API (surf_l / surf_r): worst surface under the left or right tyres
const SURFACES = ["track", "kerb", "off"];

// surface under the tyres: class and raw AMS2 name, "kerb (b1 rumbles)"; just "track" on plain road
function surfaceText(L, side, i) {
  const cls = SURFACES[L["surf_" + side][i]], raw = L.terrains?.[L["terr_" + side]?.[i]];
  return cls == null ? "?" : raw && raw !== "road" ? `${cls} (${raw})` : cls;
}

// colour of each raw surface: a hue per family, lighter for each further surface of the same family
const TERRAIN_FAMILIES = [
  [/rumble|drains|^paint concrete$/, "--warning"],
  [/wall|guardrail/, "--critical"],
  [/runoff|illegal/, "--s-lock"],
  [/grass|astroturf|verge/, "--s-throttle"],
  [/gravel|sand|dirt|clay|snow|ice/, "--surf-gravel"],
  [/marbles/, "--s-speed"],  // off-line rubber: own colour, it is still track but loses grip
  [/road|pavement|cobbles|tarmac|train/, "--muted"],
];
function terrainColours(names) {
  const used = {};
  return Object.fromEntries(names.map((name) => {
    const fam = TERRAIN_FAMILIES.find(([re]) => re.test(name))?.[1] || "--s-clutch";
    const j = (used[fam] = (used[fam] ?? -1) + 1);
    return [name, mixHex(css(fam), css("--surface"), Math.min(0.6, j * 0.28))];
  }));
}

// colour by raw surface: worst surface under either side, one trace per surface (legend entries),
// road first so everything else is drawn over it
function terrainTraces(laps) {
  const groups = new Map();
  for (const n of laps) {
    const L = hist.loaded[n];
    if (!L.terrains) continue;
    L.lap_dist.forEach((dd, i) => {
      const l = L.terr_l[i], r = L.terr_r[i];
      if (l == null && r == null) return;
      const left = r == null || (l != null && L.surf_l[i] >= L.surf_r[i]);
      const name = L.terrains[left ? l : r];
      if (!groups.has(name)) groups.set(name, { x: [], y: [], text: [], customdata: [], cls: left ? L.surf_l[i] : L.surf_r[i] });
      const g = groups.get(name), [X, Y] = mapXY(L.x[i], L.z[i]);
      g.x.push(X); g.y.push(-Y); g.customdata.push([n, i]);
      g.text.push(`lap ${n} · ${Math.round(dd)} m · left ${surfaceText(L, "l", i)} · right ${surfaceText(L, "r", i)}`);
    });
  }
  const order = (name) => (name === "road" ? -1 : groups.get(name).cls);
  const names = [...groups.keys()].sort((a, b) => order(a) - order(b) || a.localeCompare(b));
  const colours = terrainColours(names);
  return names.map((name) => {
    const g = groups.get(name);
    return { x: g.x, y: g.y, text: g.text, customdata: g.customdata, name, mode: "markers", type: "scattergl",
      marker: { size: g.cls ? 6 : 5, color: colours[name] }, hovertemplate: "%{text}<extra></extra>" };
  });
}

// one trace per lap and sector, slowest first so the fastest lap of each sector is on top;
// points carry [lap, index] for the hover line in the traces
function sectorTraces(laps) {
  const shades = sectorShades(), traces = [];
  for (let k = 0; k < 3; k++) {
    const key = `s${k + 1}`, time = (n) => hist.detail.laps.find((l) => l.lap === n)?.[key] || Infinity;
    for (const n of laps.filter((n) => shades[n]?.color[k]).sort((a, b) => time(b) - time(a))) {
      const L = hist.loaded[n], idx = L.lap_dist.map((_, i) => i).filter((i) => sectorOf(L, L.lap_dist[i]) === k + 1);
      const xy = idx.map((i) => mapXY(L.x[i], L.z[i]));
      traces.push({ x: xy.map((p) => p[0]), y: xy.map((p) => -p[1]), customdata: idx.map((i) => [n, i]),
        mode: "markers", type: "scattergl", marker: { size: 5, color: shades[n].color[k] },
        text: idx.map((i) => `lap ${n} · ${Math.round(L.lap_dist[i])} m · ${shades[n].rank[k]}`), hovertemplate: "%{text}<extra></extra>" });
    }
  }
  return traces;
}

function drawLapMap() {
  const A = hist.lapData;
  if (!A || !A.ts.length) return;
  renderMapLaps();
  const mode = $("#map-color").value;
  // other laps first, then the compare lap, the analysed lap on top
  const rank = (n) => (n === hist.lap ? 2 : n === hist.cmp ? 1 : 0);
  const laps = [...hist.shown].filter((n) => hist.loaded[n]?.ts.length).sort((a, b) => rank(a) - rank(b) || a - b);
  let cmin, cmax, scale, title;
  if (mode === "speed") {
    cmin = Math.min(...laps.map((n) => Math.min(...hist.loaded[n].speed)));
    cmax = Math.max(...laps.map((n) => Math.max(...hist.loaded[n].speed)));
    scale = [[0, css("--seq-lo")], [1, css("--seq-hi")]]; title = "km/h";
  } else if (mode === "pedals") {
    cmin = -1; cmax = 1;
    scale = [[0, css("--s-brake")], [0.5, css("--neutral")], [1, css("--s-throttle")]]; title = "brake ← → throttle";
  } else if (mode === "slip") {
    cmin = -0.25; cmax = 0.25;
    scale = [[0, css("--s-lock")], [0.5, css("--neutral")], [1, css("--s-spin")]]; title = "lock ← → spin";
  } else if (mode === "surface") {
    cmin = 0; cmax = 2;
    const [t, k, o] = [css("--muted"), css("--warning"), css("--critical")];
    scale = [[0, t], [1 / 3, t], [1 / 3, k], [2 / 3, k], [2 / 3, o], [1, o]]; title = "";
  }
  const traces = mode === "sectors" ? sectorTraces(laps) : mode === "terrain" ? terrainTraces(laps) : laps.map((n, k) => {
    const L = hist.loaded[n], xy = L.x.map((x, i) => mapXY(x, L.z[i]));
    let color, hover;
    if (mode === "speed") {
      color = L.speed;
      hover = L.speed.map((v) => `${Math.round(v)} km/h`);
    } else if (mode === "pedals") {
      color = L.throttle.map((t, i) => t - L.brake[i]);
      hover = L.throttle.map((t, i) => `throttle ${Math.round(t * 100)}% · brake ${Math.round(L.brake[i] * 100)}%`);
    } else if (mode === "slip") {
      color = worstSlip(L);
      hover = color.map((v) => `slip ${Math.round(v * 100)}%`);
    } else if (mode === "surface") {
      color = L.surf_l.map((l, i) => Math.max(l ?? 0, L.surf_r[i] ?? 0));
      hover = L.surf_l.map((l, i) => `left ${surfaceText(L, "l", i)} · right ${surfaceText(L, "r", i)}`);
    } else {
      color = lapSwatch(n);
      hover = L.speed.map((v) => `${Math.round(v)} km/h`);
    }
    const marker = mode === "lap"
      ? { size: n === hist.lap ? 3 : 6, color }  // analysed lap small on top: the laps under it stay visible
      : { size: 5, color, cmin, cmax, colorscale: scale, showscale: k === laps.length - 1,
          colorbar: { title: { text: title, side: "right" }, thickness: 10, outlinewidth: 0, tickfont: { color: css("--muted") },
            ...(mode === "surface" ? { tickvals: [1 / 3, 1, 5 / 3], ticktext: SURFACES } : {}) } };
    return { x: xy.map((p) => p[0]), y: xy.map((p) => -p[1]), mode: "markers", type: "scattergl", marker,
      text: L.lap_dist.map((dd, i) => `lap ${n} · ${Math.round(dd)} m · ${hover[i]} · gear ${L.gear[i]}`),
      hovertemplate: "%{text}<extra></extra>" };
  });
  // kerb and off-track points of every lap on top, else the laps drawn later hide them
  if (mode === "surface") {
    const o = { x: [], y: [], color: [], text: [], customdata: [] };
    traces.forEach((t, k) => t.marker.color.forEach((c, i) => {
      if (!c) return;
      o.x.push(t.x[i]); o.y.push(t.y[i]); o.color.push(c); o.text.push(t.text[i]); o.customdata.push([laps[k], i]);
    }));
    traces.push({ x: o.x, y: o.y, customdata: o.customdata, text: o.text, mode: "markers", type: "scattergl",
      marker: { size: 6, color: o.color, cmin, cmax, colorscale: scale }, hovertemplate: "%{text}<extra></extra>" });
  }
  traces.push({ x: [], y: [], mode: "markers", type: "scatter", hoverinfo: "skip", showlegend: false,
    marker: { size: 14, color: css("--s-speed"), line: { color: css("--surface"), width: 2 } } });
  hist.mapMarker = traces.length - 1;
  hist.mapLaps = laps;
  // keep the zoom while laps are toggled or recoloured; a new session or orientation resets it
  const rev = `${hist.ref}:${mapMirror}:${mapRot}`;
  if (hist.mapRev && rev !== hist.mapRev && hist.zoomDist) { hist.zoomDist = null; drawTraces(); }
  const first = !hist.mapRev;
  hist.mapRev = rev;
  const marks = trackMarks(A, hist.trackKey);
  Plotly.react("lap-map", traces, plotLayout({
    showlegend: mode === "terrain", margin: { l: 8, r: 8, t: 8, b: 8 }, uirevision: rev,
    legend: { x: 0, y: 0, xanchor: "left", yanchor: "bottom", bgcolor: css("--surface"), font: { size: 10, color: css("--text-2") } },
    annotations: marks.labels.map((l) => ({ x: l.p[0], y: -l.p[1], text: esc(l.text), showarrow: true, arrowhead: 0, arrowwidth: 1,
      arrowcolor: css("--muted"), ax: 0, ay: -24, font: { size: 11, color: css("--text") }, bgcolor: css("--surface"), opacity: 0.9 }))
      .concat(marks.ticks.map((t) => ({ x: t.b[0], y: -t.b[1], text: t.text, showarrow: false, xanchor: "left", xshift: 3,
        font: { size: 10, color: css("--text-2") } }))),
    shapes: marks.ticks.map((t) => ({ type: "line", x0: t.a[0], y0: -t.a[1], x1: t.b[0], y1: -t.b[1], line: { color: css("--text-2"), width: 2 } })),
    xaxis: { visible: false }, yaxis: { visible: false, scaleanchor: "x", scaleratio: 1 },
  }), { displaylogo: false, responsive: true, scrollZoom: true });
  if (first) {
    const el = $("#lap-map");
    el.on("plotly_relayout", onMapZoom);
    el.on("plotly_hover", onMapHover);
    el.on("plotly_unhover", () => Plotly.relayout("lap-traces", { shapes: hist.sectorShapes || [] }));
  }
}

// red line in the lap traces at the lap distance of the hovered map point
function onMapHover(ev) {
  const p = ev.points[0];
  // the surface overlay carries [lap, index]; every other trace is one lap
  const [n, idx] = p.customdata ?? [hist.mapLaps[p.curveNumber], p.pointIndex];
  const L = n != null && hist.loaded[n];
  if (!L || !$("#lap-traces").data) return;
  const dd = L.lap_dist[idx];
  Plotly.relayout("lap-traces", { shapes: [...(hist.sectorShapes || []), { type: "line", xref: "x", yref: "paper", x0: dd, x1: dd, y0: 0, y1: 1,
    line: { color: css("--critical"), width: 1.5 } }] });
}

// lap distances of the analysed lap inside the visible map area, as one [from, to] range
function onMapZoom(ev) {
  if (!Object.keys(ev).some((k) => k.startsWith("xaxis") || k.startsWith("yaxis"))) return;
  const A = hist.lapData, fl = $("#lap-map")._fullLayout;
  if (!A || !A.ts.length || !fl) return;
  const [x0, x1] = fl.xaxis.range, [y0, y1] = fl.yaxis.range;
  const ds = [];
  A.x.forEach((x, i) => {
    const [X, Y] = mapXY(x, A.z[i]);
    if (X >= x0 && X <= x1 && -Y >= y0 && -Y <= y1) ds.push(A.lap_dist[i]);
  });
  let zoom = null;
  if (ev["xaxis.autorange"] == null && ds.length && ds.length < A.ts.length) {
    // the box may catch another bit of track too: keep the stretch with the most samples
    ds.sort((a, b) => a - b);
    let best = [0, 0], from = 0;
    for (let i = 1; i <= ds.length; i++) {
      if (i === ds.length || ds[i] - ds[i - 1] > 150) {
        if (i - from > best[1] - best[0]) best = [from, i];
        from = i;
      }
    }
    zoom = [ds[best[0]] - 30, ds[best[1] - 1] + 30];
  }
  hist.zoomDist = zoom;
  drawTraces();
}

// time of lap B at each distance of lap A, minus lap A's time
function deltaTime(A, B) {
  const bd = [], bt = [];
  let last = -1;
  for (let i = 0; i < B.lap_dist.length; i++) if (B.lap_dist[i] > last) { last = B.lap_dist[i]; bd.push(last); bt.push(B.lap_time[i]); }
  const out = [];
  let j = 0;
  for (let i = 0; i < A.lap_dist.length; i++) {
    const dd = A.lap_dist[i];
    while (j < bd.length - 2 && bd[j + 1] < dd) j++;
    if (dd < bd[0] || dd > bd[bd.length - 1]) { out.push(null); continue; }
    const f = (dd - bd[j]) / Math.max(bd[j + 1] - bd[j], 1e-6);
    out.push(A.lap_time[i] - (bt[j] + f * (bt[j + 1] - bt[j])));
  }
  return out;
}

function drawTraces() {
  const A = hist.lapData, B = hist.cmpData;
  if (!A || !A.ts.length) return;
  const rows = [
    { key: "speed", title: "Speed km/h", series: [["speed", "Speed", "--s-speed", 1]] },
    { key: "pedals", title: "Pedals %", series: [["throttle", "Throttle", "--s-throttle", 100], ["brake", "Brake", "--s-brake", 100]] },
    { key: "gear", title: "Gear", series: [["gear", "Gear", "--s-clutch", 1]] },
    { key: "steer", title: "Steering %", series: [["steer", "Steering", "--text-2", 100]] },
    { key: "slip", title: "Wheel slip %", series: [["spin", "Wheelspin", "--s-spin", 100], ["lock", "Lock-up", "--s-lock", 100]] },
    { key: "surface", title: "Track limits", series: [["surf_l", "Left tyres", "--warning", -1], ["surf_r", "Right tyres", "--critical", 1]] },
  ];
  const worst = (L, f) => L.ts.map((_, i) => {
    const w = [L.slip_fl[i], L.slip_fr[i], L.slip_rl[i], L.slip_rr[i]].filter((v) => v != null);
    return w.length ? f(...w, 0) : null;
  });
  for (const L of [A, B]) if (L && !L.spin) { L.spin = worst(L, Math.max); L.lock = worst(L, Math.min); }
  if (B) rows.unshift({ key: "delta", title: `Δ vs ${hist.cmpLabel} s`, series: [["delta", "Δ time", "--s-angle", 1]] });
  if (B) A.delta = deltaTime(A, B);

  const z = hist.zoomDist;
  $("#traces-zoom").innerHTML = z ? `${Math.max(0, Math.round(z[0]))}–${Math.round(z[1])} m<a id="traces-unzoom">whole lap</a>` : "";
  // fit these rows' y axis to what is inside the zoomed stretch
  const yFit = (row) => {
    let lo = Infinity, hi = -Infinity;
    for (const [col, , , mul] of row.series) {
      for (const L of row.key === "delta" ? [A] : [A, B]) {
        if (!L) continue;
        L[col].forEach((v, i) => {
          if (v == null || L.lap_dist[i] < z[0] || L.lap_dist[i] > z[1]) return;
          lo = Math.min(lo, v * mul); hi = Math.max(hi, v * mul);
        });
      }
    }
    if (lo > hi) return {};
    const pad = Math.max((hi - lo) * 0.08, row.key === "delta" ? 0.02 : 1);
    return { range: [lo - pad, hi + pad] };
  };

  const traces = [];
  const n = rows.length, gap = 0.025, hEach = (1 - gap * (n - 1)) / n;
  const layout = plotLayout({
    showlegend: true, legend: { orientation: "h", y: 1.0, yanchor: "bottom", x: 0, font: { color: css("--text-2") } },
    hovermode: "x unified", margin: { l: 52, r: 12, t: 64, b: 34 },
  });
  // sector starts as dashed lines through every row, corner names along the top
  hist.sectorShapes = (A.sectors || []).map((dd) => ({ type: "line", xref: "x", yref: "paper", x0: dd, x1: dd, y0: 0, y1: 1,
    line: { color: css("--muted"), width: 1, dash: "dash" } }));
  layout.shapes = hist.sectorShapes;
  const label = (x, text, color) => ({ xref: "x", yref: "paper", x, y: 1, yanchor: "top", text, showarrow: false,
    font: { size: 10, color }, bgcolor: css("--surface") });
  layout.annotations = (A.sectors || []).map((dd, n) => ({ ...label(dd, `S${n + 2}`, css("--text-2")), xanchor: "left" }))
    .concat(findCorners(A).map((c, k) => label(c.apex, esc(cornerName(hist.trackKey, c, k)), css("--muted"))));
  rows.forEach((row, k) => {
    const ax = k === 0 ? "" : String(k + 1);
    const top = 1 - k * (hEach + gap);
    layout["yaxis" + ax] = { ...axisStyle(), domain: [top - hEach, top], title: { text: row.title, font: { size: 10, color: css("--muted") } }, fixedrange: true,
      ...(row.key === "slip" ? { range: [-30, 30] } : {}),
      ...(row.key === "surface" ? { range: [-2.4, 2.4], tickvals: [-2, -1, 0, 1, 2], ticktext: ["L off", "L kerb", "", "R kerb", "R off"] } : {}),
      ...(z && ["speed", "steer", "delta"].includes(row.key) ? yFit(row) : {}) };
    layout["xaxis" + ax] = { ...axisStyle(), anchor: "y" + ax, matches: k ? "x" : undefined, showticklabels: k === n - 1,
      ...(k === 0 && z ? { range: z } : {}),
      title: k === n - 1 ? { text: "lap distance (m)", font: { size: 10, color: css("--muted") } } : undefined };
    for (const [col, name, c, mul] of row.series) {
      for (const [L, label, dash] of [[A, `lap ${hist.lap}`, "solid"], [row.key === "delta" ? null : B, hist.cmpLabel, "dot"]]) {
        if (!L) continue;
        traces.push({
          x: L.lap_dist, y: L[col].map((v) => (v == null || (row.key === "surface" && !v) ? null : v * mul)), xaxis: "x" + ax, yaxis: "y" + ax,
          type: "scattergl", mode: "lines", name: `${name} · ${label}`, legendgroup: label,
          line: { color: css(c), width: 2, dash, shape: ["gear", "surface"].includes(row.key) ? "hv" : "linear" },
          ...(row.key === "surface"
            ? { text: L[col].map((_, i) => surfaceText(L, col.slice(-1), i)), hovertemplate: "%{text}" }
            : { hovertemplate: `%{y:.${row.key === "delta" ? 3 : 0}f}` }),
        });
      }
    }
  });
  Plotly.react("lap-traces", traces, layout, { displaylogo: false, responsive: true });
  const el = $("#lap-traces");
  el.removeAllListeners?.("plotly_hover");
  el.on("plotly_hover", (ev) => {
    const dd = ev.points[0].x;
    let i = A.lap_dist.findIndex((v) => v >= dd);
    if (i < 0) i = A.lap_dist.length - 1;
    const [X, Y] = mapXY(A.x[i], A.z[i]);
    Plotly.restyle("lap-map", { x: [[X]], y: [[-Y]] }, [hist.mapMarker]);
  });
}

// ---------- corners ----------
// A corner is a speed dip of more than CORNER_DIP km/h on the analysed lap. Its segment runs from the
// speed peak before it to the peak after it (the first from the lap start, the last to the lap end),
// so the segments add up to the whole lap and each carries the straight that its exit leads onto.
const CORNER_DIP = 10, BRAKING = 0.2, FULL_THROTTLE = 0.95;
function findCorners(L) {
  const v = L.speed, peaks = [], apexes = [];
  let hi = 0, lo = 0, falling = false;
  for (let i = 1; i < v.length; i++) {
    if (!falling) {
      if (v[i] >= v[hi]) hi = i;
      else if (v[hi] - v[i] > CORNER_DIP) { peaks.push(hi); falling = true; lo = i; }
    } else if (v[i] <= v[lo]) lo = i;
    else if (v[i] - v[lo] > CORNER_DIP) { apexes.push(lo); falling = false; hi = i; }
  }
  if (!falling) peaks.push(hi);
  const d = L.lap_dist;
  return apexes.map((a, k) => ({ s: k ? d[peaks[k]] : -Infinity, apex: d[a], e: k < apexes.length - 1 ? d[peaks[k + 1]] : Infinity }));
}

// first sample at or past a lap distance
function sampleAt(L, dd) {
  const i = L.lap_dist.findIndex((v) => v >= dd);
  return i < 0 ? L.lap_dist.length - 1 : i;
}
// lap time at a lap distance, interpolated (0 before the first sample, the lap's last time after the last)
function timeAt(L, dd) {
  const d = L.lap_dist, t = L.lap_time;
  if (dd === -Infinity) return 0;
  const i = d.findIndex((v) => v >= dd);
  if (i < 0) return t[t.length - 1];
  if (i === 0) return Math.max(0, t[0]);
  const f = (dd - d[i - 1]) / Math.max(d[i] - d[i - 1], 1e-6);
  return t[i - 1] + f * (t[i] - t[i - 1]);
}

function cornerStats(L, c) {
  let minI = -1;
  L.lap_dist.forEach((dd, i) => {
    if (dd < c.s || dd > c.e) return;
    if (minI < 0 || L.speed[i] < L.speed[minI]) minI = i;
  });
  if (minI < 0) return null;
  // gear at the slowest point: the most common forward gear within 30 m of it. Mid-shift samples read
  // 0 (neutral) or a passing gear (1st at 140 km/h), so the lowest gear seen would be wrong.
  const near = {};
  for (let i = 0; i < L.lap_dist.length; i++) {
    if (Math.abs(L.lap_dist[i] - L.lap_dist[minI]) <= 30 && L.gear[i] > 0) near[L.gear[i]] = (near[L.gear[i]] || 0) + 1;
  }
  const gear = Object.keys(near).length ? +Object.entries(near).sort((x, y) => y[1] - x[1])[0][0] : Infinity;
  const apex = L.lap_dist[minI];
  // braking point: first press after the speed peak before the corner (at most 600 m before the apex).
  // BRAKING is well above the 6-10 % the brake can read with the foot resting on the pedal.
  let brake = null;
  for (let i = 0; i < minI; i++) {
    const dd = L.lap_dist[i];
    if (dd >= c.s && dd >= apex - 600 && L.brake[i] > BRAKING) { brake = dd; break; }
  }
  // braking and full-throttle distances are measured from one fixed spot, the corner's apex (as found on the
  // lap the corners come from), so two laps compare only where they braked or floored it, not where their
  // own slowest points fell
  const mark = c.apex ?? apex;
  let full = null;
  if (L.throttle) {
    for (let i = minI; i < L.lap_dist.length && L.lap_dist[i] <= c.e; i++) {
      if (L.throttle[i] >= FULL_THROTTLE) { full = L.lap_dist[i] - mark; break; }
    }
  }
  return {
    brake, gear: gear === Infinity ? null : gear, min: L.speed[minI],
    brakeToApex: brake != null ? mark - brake : null, full,
    entry: L.speed[sampleAt(L, brake ?? Math.max(c.s, 0))],
    exit: L.speed[sampleAt(L, Math.min(c.apex + 100, c.e))],
    time: timeAt(L, c.e) - timeAt(L, c.s),
  };
}

async function drawCorners() {
  const A = hist.lapData, lap = hist.lap;
  const tb = $("#corners-table tbody");
  if (!A || !A.ts.length) { tb.innerHTML = ""; return; }
  // reference: the compare lap, else this session's best lap
  let B = hist.cmpData, label = hist.cmpLabel;
  if (!B && hist.bestLap != null && hist.bestLap !== lap) { B = await getLap(hist.bestLap); label = `best lap ${hist.bestLap}`; }
  if (hist.lapData !== A || lap !== hist.lap) return;
  $("#corners-ref").textContent = B ? `lap ${lap} vs ${label}` : `lap ${lap} · pick a compare lap to see time gained / lost`;
  hist.corners = findCorners(A);
  const diff = (a, b, unit = "") => (a != null && b != null && B ? ` <span class="muted">${fmtDiff(a - b)}${unit}</span>` : "");
  let total = 0, totalB = 0;
  tb.innerHTML = hist.corners.map((c, k) => {
    const a = cornerStats(A, c), b = B && cornerStats(B, c);
    if (!a) return "";
    total += a.time;
    const dt = b ? a.time - b.time : null;
    if (b) totalB += b.time;
    // braking later is positive
    const brake = a.brake == null ? `<span class="muted">lift</span>` : `${Math.round(a.brake)} m${b?.brake != null ? ` <span class="muted">${Math.abs(a.brake - b.brake) < 3 ? "same" : `${Math.round(Math.abs(a.brake - b.brake))} m ${a.brake > b.brake ? "later" : "earlier"}`}</span>` : ""}`;
    return `<tr data-k="${k}">
      <td>${esc(cornerName(hist.trackKey, c, k))} <span class="muted">S${sectorOf(A, c.apex)}</span> <a class="rename" title="Name this corner">✎</a></td><td class="r">${Math.round(c.apex)} m</td><td class="r">${brake}</td>
      <td class="r">${Math.round(a.entry)}${diff(a.entry, b?.entry)}</td>
      <td class="r">${Math.round(a.min)}${diff(a.min, b?.min)}</td>
      <td class="r">${Math.round(a.exit)}${diff(a.exit, b?.exit)}</td>
      <td class="r">${a.gear ?? ""}</td><td class="r">${a.time.toFixed(2)}</td>
      <td class="r ${dt == null ? "" : dt > 0.005 ? "lost" : dt < -0.005 ? "gained" : ""}">${dt == null ? "" : fmtDiff(dt, 2)}</td></tr>`;
  }).join("") + (hist.corners.length ? `<tr class="total"><td colspan="7">Whole lap</td><td class="r">${total.toFixed(2)}</td>
      <td class="r ${B ? (total - totalB > 0 ? "lost" : "gained") : ""}">${B ? fmtDiff(total - totalB, 2) : ""}</td></tr>`
    : `<tr><td colspan="9" class="empty">No corner found on this lap.</td></tr>`);
}
// click a corner: zoom the map on it, which zooms the traces too
$("#corners-table tbody").onclick = (e) => {
  const tr = e.target.closest("tr[data-k]"), A = hist.lapData;
  if (!tr || !A) return;
  if (e.target.closest(".rename")) { renameCorner(hist.trackKey, hist.corners[+tr.dataset.k], +tr.dataset.k); return; }
  const c = hist.corners[+tr.dataset.k], from = Math.max(c.s, c.apex - 250), to = Math.min(c.e, c.apex + 150);
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  A.lap_dist.forEach((dd, i) => {
    if (dd < from || dd > to) return;
    const [X, Y] = mapXY(A.x[i], A.z[i]);
    x0 = Math.min(x0, X); x1 = Math.max(x1, X); y0 = Math.min(y0, -Y); y1 = Math.max(y1, -Y);
  });
  if (x0 === Infinity) return;
  const pad = Math.max(x1 - x0, y1 - y0) * 0.15 + 10;
  document.querySelectorAll("#corners-table tr.sel").forEach((r) => r.classList.remove("sel"));
  tr.classList.add("sel");
  Plotly.relayout("lap-map", { "xaxis.range": [x0 - pad, x1 + pad], "yaxis.range": [y0 - pad, y1 + pad] });
  $("#lap-map").scrollIntoView({ behavior: "smooth", block: "nearest" });
};

// ---------- race replay ----------
const replay = { outline: null, cars: null, timer: null, pending: null, t0: 0, t1: 0, startTs: 0 };

async function initReplay(d) {
  clearInterval(replay.timer);
  replay.timer = null;
  $("#replay-play").textContent = "Play";
  replay.cars = null;
  if (!d.t0) { $("#replay-pane").hidden = true; return; }
  const path = await (await fetch(`/api/sessions/${hist.ref}/path`)).json();
  const out = [];
  for (let i = 0; i < path.x.length; i++) {
    const q = out[out.length - 1];
    if (!q || Math.hypot(path.x[i] - q[0], path.z[i] - q[1]) > 3) out.push([path.x[i], path.z[i]]);
  }
  replay.outline = out;
  hist.shakers = null;
  fetch(`/api/sessions/${hist.ref}/shakers`).then((r) => r.json()).then((sd) => {
    if (sd.ts?.length && d.t0 === replay.t0) { hist.shakers = sd; paintSessionCar(); }
  });
  replay.t0 = d.t0;
  replay.t1 = d.t1;
  const sl = $("#replay-slider");
  sl.min = 0;
  sl.max = Math.max(1, d.t1 - d.t0);
  sl.value = 0;
  $("#replay-pane").hidden = false;
  seekReplay(0);
}

async function seekReplay(offset) {
  replay.pending = offset;
  if (replay.busy) return;
  replay.busy = true;
  while (replay.pending != null) {
    const o = replay.pending;
    replay.pending = null;
    const r = await (await fetch(`/api/sessions/${hist.ref}/cars_at?ts=${replay.t0 + o}`)).json();
    replay.cars = r.cars.map((c) => ({ ...c, player: !!c.is_player, pit: c.pit > 0 }));
    $("#replay-time").textContent = `${fmtDur(o)} / ${fmtDur(replay.t1 - replay.t0)}`;
    replay.at = replay.t0 + o;
    paintSessionCar();
    drawReplay();
    if (hist.positions) Plotly.relayout("positions-plot", { shapes: positionCursor() });
  }
  replay.busy = false;
}
function paintSessionCar() {
  const sd = hist.shakers;
  if (!sd) { paintShakerCar("#sess-car .spot", {}, false); return; }
  const t = replay.at ?? replay.t0;
  let lo = 0, hi = sd.ts.length - 1;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (sd.ts[m] <= t) lo = m; else hi = m - 1; }
  const chan = {};
  for (const [ch, v] of Object.entries(sd.channels)) chan[ch] = { a: v.a[lo], fx: v.fx[lo] };
  paintShakerCar("#sess-car .spot", chan, !!replay.timer);
}
function drawReplay() { drawMap($("#replay-map"), replay.outline, replay.cars || [], false, trackMarks(hist.lapData, hist.trackKey)); }
$("#replay-slider").oninput = (e) => seekReplay(+e.target.value);

// ---------- folding the session page's cards (state kept per card title) ----------
const folded = new Set(pref.get("folded", []));
function foldKey(card) { return card.querySelector(":scope > h3").firstChild.textContent.trim(); }
for (const card of document.querySelectorAll("#session-detail .card")) {
  const h = card.querySelector(":scope > h3");
  if (!h) continue;
  card.classList.toggle("folded", folded.has(foldKey(card)));
  h.title = "Click to fold / unfold";
  h.addEventListener("click", (e) => {
    if (e.target.closest("select, input, button, a, label")) return;
    const k = foldKey(card), now = card.classList.toggle("folded");
    if (now) folded.add(k); else folded.delete(k);
    pref.set("folded", [...folded]);
    if (!now) {  // plots and canvases drawn while hidden have no size: redraw them
      for (const el of card.querySelectorAll(".js-plotly-plot")) Plotly.Plots.resize(el);
      if (card.id === "replay-card" && replay.outline) drawReplay();
    }
  });
}

// ---------- position over the session: one step line per car, the player's in blue ----------
async function loadPositions(d) {
  const ref = hist.ref;
  hist.positions = null;
  $("#positions-card").hidden = true;
  if (!d.t0) return;
  const r = await fetch(`/api/sessions/${ref}/positions`);
  const cars = r.ok ? await r.json() : [];
  if (hist.ref !== ref || cars.length < 2) return;
  hist.positions = cars;
  $("#positions-card").hidden = false;
  drawPositions();
}
function positionCursor() {
  if (replay.at == null) return [];
  const x = (replay.at - hist.detail.t0) / 60;
  return [{ type: "line", xref: "x", yref: "paper", x0: x, x1: x, y0: 0, y1: 1, line: { color: css("--text-2"), width: 1, dash: "dot" } }];
}
// you in blue; the car that finished just ahead of you orange, just behind aqua, the next ahead (or behind) yellow.
// Only these four hues stay apart for colour-blind readers; every other car is grey until hovered.
const RIVAL_COLOURS = ["--s-spin", "--s-throttle", "--s-angle"];
function positionColours(cars) {
  const fin = (c) => c.pos[c.pos.length - 1];
  const me = cars.find((c) => c.player), mine = me ? fin(me) : 0;
  const others = cars.filter((c) => c !== me);
  const at = (p) => others.find((c) => fin(c) === p);
  const picks = me ? [at(mine - 1), at(mine + 1), at(mine - 2) || at(mine + 2)] : others.sort((x, y) => fin(x) - fin(y)).slice(0, 3);
  const col = new Map(me ? [[me, css("--s-speed")]] : []);
  picks.filter(Boolean).forEach((c, i) => col.set(c, css(RIVAL_COLOURS[i])));
  return col;
}
// position of a car at time t, from its step series
function posAt(c, t) {
  let lo = 0, hi = c.ts.length - 1;
  if (t < c.ts[0]) return null;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (c.ts[m] <= t) lo = m; else hi = m - 1; }
  return c.pos[lo];
}
function drawPositions() {
  const cars = hist.positions;
  if (!cars) return;
  const t0 = hist.detail.t0, n = Math.max(...cars.flatMap((c) => c.pos));
  const colours = positionColours(cars), me = cars.find((c) => c.player);
  const order = [...cars].sort((a, b) => (colours.has(a) - colours.has(b)) || (a.player - b.player));  // coloured on top, you last
  const places = (d) => `${Math.abs(d)} place${Math.abs(d) > 1 ? "s" : ""}`;
  const traces = order.map((c) => {
    const start = c.pos[0];
    // hover text per point: the stint at that position, places won/lost since the start, where you were
    const text = c.ts.map((t, i) => {
      let a = i, b = i;
      while (a > 0 && c.pos[a - 1] === c.pos[i]) a--;
      while (b < c.ts.length - 1 && c.pos[b + 1] === c.pos[i]) b++;
      const end = b < c.ts.length - 1 ? c.ts[b + 1] : c.ts[b];
      const d = start - c.pos[i];
      const lines = [`<b>${esc(c.name)}</b>${c.player ? " (you)" : ""} · ${esc(c.car)}`,
        `<b>P${c.pos[i]}</b> at ${fmtDur(t - t0)} · lap ${c.lap[i] ?? "–"}`,
        `P${c.pos[i]} from ${fmtDur(c.ts[a] - t0)} to ${fmtDur(end - t0)} (${fmtDur(end - c.ts[a])})`,
        `started P${start}: ${d === 0 ? "same place" : d > 0 ? `${places(d)} gained` : `${places(d)} lost`}`];
      if (me && !c.player) {
        const mp = posAt(me, t);
        if (mp) lines.push(mp === c.pos[i] ? "" : `${places(mp - c.pos[i])} ${c.pos[i] < mp ? "ahead of" : "behind"} you (P${mp})`);
      }
      if (c.pits.length) lines.push(`pit stops: ${c.pits.map((p) => fmtDur(p - t0)).join(", ")}`);
      return lines.filter(Boolean).join("<br>");
    });
    const col = colours.get(c);
    return {
      type: "scatter", mode: "lines", name: c.name, x: c.ts.map((t) => (t - t0) / 60), y: c.pos, text,
      line: { shape: "hv", width: c.player ? 3 : col ? 2 : 1.2, color: col || css("--muted") },
      opacity: col ? 1 : 0.45, hovertemplate: "%{text}<extra></extra>",
      meta: { colour: col, player: c.player },
    };
  });
  // pit stops: a down triangle on the car's line, in its colour
  const pits = order.flatMap((c) => c.pits.map((p) => ({ c, p, pos: posAt(c, p) ?? c.pos[0] })));
  if (pits.length) traces.push({
    type: "scatter", mode: "markers", name: "pit stops", x: pits.map((q) => (q.p - t0) / 60), y: pits.map((q) => q.pos),
    marker: { symbol: "triangle-down", size: 9, color: pits.map((q) => colours.get(q.c) || css("--muted")), line: { color: css("--surface"), width: 1.5 } },
    text: pits.map((q) => `<b>${esc(q.c.name)}</b> pits at ${fmtDur(q.p - t0)}`), hovertemplate: "%{text}<extra></extra>",
  });
  hist.positionTraces = order.length;
  // names at the right end of each line; the coloured ones get a swatch, the text stays in text colours
  const labels = order.map((c) => {
    const col = colours.get(c), p = c.pos[c.pos.length - 1];
    return {
      x: (c.ts[c.ts.length - 1] - t0) / 60, y: p, xanchor: "left", xshift: 4, showarrow: false,
      text: `${col ? `<span style="color:${col}">━</span> ` : ""}P${p} ${c.player ? `<b>${esc(c.name)}</b>` : esc(c.name)}`,
      font: { size: col ? 11 : 10, color: col ? css("--text") : css("--muted") },
    };
  });
  Plotly.react("positions-plot", traces, plotLayout({
    showlegend: false, hovermode: "closest", hoverdistance: 12, margin: { l: 40, r: 160, t: 8, b: 36 },
    xaxis: { ...axisStyle(), title: { text: "minutes", font: { size: 11 } }, zeroline: false },
    yaxis: { ...axisStyle(), range: [n + 0.5, 0.5], dtick: n > 16 ? 2 : 1, tick0: 1, zeroline: false, title: { text: "position", font: { size: 11 } } },
    annotations: labels, shapes: positionCursor(),
  }), { displaylogo: false, responsive: true });
  const el = $("#positions-plot");
  if (!el.dataset.wired) {
    el.dataset.wired = "1";
    el.on("plotly_click", (ev) => {
      const sl = $("#replay-slider"), v = Math.min(+sl.max, Math.max(0, ev.points[0].x * 60));
      sl.value = v;
      seekReplay(v);
    });
    // hovering a line brings it forward: full opacity, thicker, grey cars in the text colour
    el.on("plotly_hover", (ev) => {
      const i = ev.points[0].curveNumber;
      if (i >= hist.positionTraces || el.dataset.hot == i) return;
      unhot(el);
      const m = el.data[i].meta;
      el.dataset.hot = i;
      Plotly.restyle(el, { opacity: 1, "line.width": m.player ? 3.5 : 2.5, "line.color": m.colour || css("--text") }, [i]);
    });
    el.on("plotly_unhover", () => unhot(el));
  }
}
function unhot(el) {
  const i = el.dataset.hot;
  if (i == null || i === "") return;
  el.dataset.hot = "";
  const m = el.data[+i]?.meta;
  if (m) Plotly.restyle(el, { opacity: m.colour ? 1 : 0.45, "line.width": m.player ? 3 : m.colour ? 2 : 1.2, "line.color": m.colour || css("--muted") }, [+i]);
}

$("#replay-play").onclick = () => {
  if (replay.timer) {
    clearInterval(replay.timer);
    replay.timer = null;
    $("#replay-play").textContent = "Play";
    return;
  }
  $("#replay-play").textContent = "Pause";
  replay.timer = setInterval(() => {
    const sl = $("#replay-slider");
    const v = Math.min(+sl.max, +sl.value + 0.25 * +$("#replay-speed").value);
    sl.value = v;
    seekReplay(v);
    if (v >= +sl.max) $("#replay-play").click();
  }, 250);
};

$("#d-delete").onclick = async () => {
  const s = hist.detail?.session;
  if (!s || !confirm(`Delete ${s.session_type} at ${s.track} (${new Date(s.started_at * 1000).toLocaleString()})? This cannot be undone.`)) return;
  const r = await fetch(`/api/sessions/${hist.ref}`, { method: "DELETE" });
  if (!r.ok) { alert((await r.json()).detail || "Delete failed"); return; }
  hist.ref = null;
  $("#session-detail").hidden = true;
  history.replaceState(null, "", location.pathname);
  loadSessions();
};

// ---------- shakers (monocoque Sound effects) ----------
const FX_COLOUR = { Engine: "--s-speed", Gear: "--s-angle", TyreSlip: "--s-spin", TyreLock: "--s-lock", ABS: "--s-brake", Suspension: "--s-throttle",
  Kerb: "--s-clutch", OffTrack: "--surf-gravel" };
const FX_NAME = { Engine: "Engine", Gear: "Gear shift", TyreSlip: "Wheelspin", TyreLock: "Lock-up", ABS: "ABS", Suspension: "Suspension",
  Kerb: "Kerb", OffTrack: "Off track" };
// played by simdash itself from AMS2's per-wheel terrain, not by monocoque
const OWN_FX = new Set(["Kerb", "OffTrack"]);
const FX_DEFAULTS = {
  Engine: { modulation: "frequency", frequency: 15, frequencyMax: 30, amplitude: 80, noise: 0 },
  Gear: { frequency: 15, amplitude: 100, duration: 0.25 },
  TyreSlip: { wheels: ["FL", "FR", "RL", "RR"], threshold: 0.3, modulation: "amplify", frequency: 32, amplitude: 50, amplitudeMax: 95 },
  TyreLock: { wheels: ["FL", "FR", "RL", "RR"], threshold: 0.3, modulation: "amplify", frequency: 40, amplitude: 50, amplitudeMax: 95 },
  ABS: { wheels: ["FL", "FR", "RL", "RR"], threshold: 0.3, modulation: "none", frequency: 45, amplitude: 70 },
  Suspension: { wheels: ["FL", "FR", "RL", "RR"], threshold: 0.1, modulation: "amplify", frequency: 20, amplitude: 30, amplitudeMax: 90 },
  Kerb: { wheels: ["FL", "FR", "RL", "RR"], modulation: "frequency", frequency: 30, frequencyMax: 60, amplitude: 70, noise: 6 },
  OffTrack: { wheels: ["FL", "FR", "RL", "RR"], modulation: "amplify", frequency: 22, amplitude: 40, amplitudeMax: 80, noise: 12 },
};
// AMS2 field audit (AMS2-TELEMETRY-FIELDS.md)
const FX_HINT = {
  Engine: "Pitch follows the RPM. The BST1 is weak below about 25 Hz.",
  Gear: "One kick per gear change.",
  TyreSlip: "AMS2: needs the car's tyre diameters, which simdash learns and gives monocoque.",
  TyreLock: "AMS2: needs the car's tyre diameters, which simdash learns and gives monocoque.",
  ABS: "AMS2: probably never fires (simapi reads the ABS flag as a number).",
  Suspension: "AMS2: not mapped by simapi, gives no vibration.",
  Kerb: "Played by simdash (not monocoque) while one of these wheels is on a kerb, from AMS2's raw terrain. Pitch or strength rises with speed (full at 200 km/h). Applies a few seconds after saving, no monocoque restart.",
  OffTrack: "Played by simdash (not monocoque) while one of these wheels is on grass, gravel, sand or runoff, from AMS2's raw terrain. Pitch or strength rises with speed (full at 200 km/h). Applies a few seconds after saving.",
};
const SPOT_XY = { seat: [120, 240], "seat-back": [120, 274], pedals: [120, 166], "front-left": [29, 95], "front-right": [211, 95], "rear-left": [29, 344], "rear-right": [211, 344] };
const SPOT_NAME = { none: "Not connected", seat: "Seat", "seat-back": "Seat back", pedals: "Pedals", "front-left": "Front left", "front-right": "Front right", "rear-left": "Rear left", "rear-right": "Rear right" };
const WHEEL_NAME = { FL: "front left", FR: "front right", RL: "rear left", RR: "rear right" };
// sh.active: index of the wheel effect whose wheels the car's tyres toggle
const sh = { loaded: null, effects: [], devid: "", layout: {}, sinks: [], opts: null, running: false, dirty: false, sel: null, playing: null, active: null };

async function loadShakers() {
  const r = await fetch("/api/shakers");
  const d = await r.json();
  if (!r.ok) { $("#sh-note").textContent = d.detail || "Cannot read monocoque.config"; return; }
  sh.loaded = d;
  sh.effects = structuredClone(d.effects);
  sh.devid = d.devid;
  sh.layout = d.layout;
  sh.sinks = d.sinks;
  sh.opts = d.options;
  sh.running = d.running;
  sh.dirty = false;
  sh.active = null;
  $("#sh-path").textContent = d.path;
  renderShakers();
  loadReplaySessions();
  scheduleReplayReload();
}

function chName(ch) {
  const l = sh.layout[ch];
  return `${ch} · ${sh.opts.channels[ch]}` + (l ? ` · ${l.label || SPOT_NAME[l.spot]}` : "");
}
const cardConnected = () => sh.sinks.some((s) => s.name === sh.devid);

function renderShakers() {
  renderSinks();
  renderCar();
  renderWheels();
  renderChannels();
  renderEffects();
  renderShakerState();
}

function renderShakerState() {
  $("#sh-save").disabled = !sh.dirty;
  $("#sh-revert").disabled = !sh.dirty;
  const ok = cardConnected();
  const st = $("#sh-card-state");
  st.textContent = !sh.devid ? "none chosen" : ok ? "connected" : "not connected";
  st.className = "pill " + (ok ? "ok" : "bad");
  $("#sh-note").textContent = sh.dirty ? "Unsaved changes" : sh.running ? "monocoque is running: after saving, Restart monocoque in System to apply" : "";
}

function renderSinks() {
  const opts = sh.sinks.map((s) => `<option value="${esc(s.name)}">${esc(s.description)}${s.channels < 8 ? ` (${s.channels} ch)` : ""}</option>`);
  if (sh.devid && !cardConnected()) opts.unshift(`<option value="${esc(sh.devid)}">${esc(sh.devid.replace(/^alsa_output\./, ""))} (not connected)</option>`);
  if (!sh.devid) opts.unshift(`<option value="">Choose the shaker sound card…</option>`);
  $("#sh-sink").innerHTML = opts.join("");
  $("#sh-sink").value = sh.devid;
}

function effectsOn(ch) { return sh.effects.map((e, i) => ({ e, i })).filter(({ e }) => e.pan === ch); }

function renderCar() {
  const used = Object.keys(sh.layout).map(Number).sort((a, b) => a - b);
  const bySpot = {};
  used.forEach((ch) => (bySpot[sh.layout[ch].spot] ??= []).push(ch));
  let html = "";
  for (const [spot, chs] of Object.entries(bySpot)) {
    const [x0, y] = SPOT_XY[spot] || [120, 220];
    chs.forEach((ch, k) => {
      const x = x0 + (k - (chs.length - 1) / 2) * 34;
      const fx = effectsOn(ch);
      const n = fx.length;
      const halos = fx.map(({ e }, j) => {
        // one arc per effect around the shaker
        const r = 19, a0 = (j / n) * 2 * Math.PI - Math.PI / 2, a1 = ((j + 1) / n) * 2 * Math.PI - Math.PI / 2 - (n > 1 ? 0.25 : 0);
        if (n === 1) return `<circle class="halo" r="${r}" style="stroke:var(${FX_COLOUR[e.effect]})"/>`;
        return `<path class="halo" d="M${r * Math.cos(a0)} ${r * Math.sin(a0)} A${r} ${r} 0 0 1 ${r * Math.cos(a1)} ${r * Math.sin(a1)}" style="stroke:var(${FX_COLOUR[e.effect]})"/>`;
      }).join("");
      const lbl = sh.layout[ch].label || SPOT_NAME[spot];
      html += `<g class="spot${sh.sel === ch ? " sel" : ""}${sh.playing === ch ? " playing" : ""}" data-ch="${ch}" transform="translate(${x} ${y})">
        <title>${esc(chName(ch))}: ${n ? fx.map(({ e }) => FX_NAME[e.effect]).join(", ") : "no effect"}. Click to identify.</title>
        ${halos}<circle class="base" r="14"/><text>${sh.opts.channels[ch]}</text>
        <text class="lbl" y="${spot.startsWith("front") || spot.startsWith("rear") ? 46 : 30}">${esc(lbl)}</text></g>`;
    });
  }
  if (!used.length) html = `<text x="120" y="230" text-anchor="middle" style="fill:var(--muted);font-size:12px">Place a shaker with the table below</text>`;
  $("#sh-spots").innerHTML = html;
  $("#sh-spots").querySelectorAll(".spot").forEach((g) => (g.onclick = () => {
    selectChannel(+g.dataset.ch);
    shakerTest({ pan: +g.dataset.ch }, "identify");
  }));
  if (rp.d) renderMiniCar();
  if (shCar.layout) loadShakerCars();
  const kinds = [...new Set(sh.effects.map((e) => e.effect))];
  $("#sh-legend").innerHTML = kinds.map((k) => `<span style="--c:var(${FX_COLOUR[k]})">${FX_NAME[k]}</span>`).join("");
}

function selectChannel(ch) {
  sh.sel = sh.sel === ch ? null : ch;
  renderCar();
  renderChannels();
}

function renderChannels() {
  $("#sh-channels tbody").innerHTML = sh.opts.channels.map((short, ch) => {
    const l = sh.layout[ch] || { spot: "none", label: "" };
    const fx = effectsOn(ch).map(({ e }) => `<span title="${FX_NAME[e.effect]}"><span class="fx-dot" style="--c:var(${FX_COLOUR[e.effect]})"></span></span>`).join("");
    return `<tr data-ch="${ch}" class="${sh.sel === ch ? "sel" : ""}">
      <td class="num">${ch} · ${short}</td>
      <td><select data-k="spot">${sh.opts.spots.map((s) => `<option value="${s}"${s === l.spot ? " selected" : ""}>${SPOT_NAME[s]}</option>`).join("")}</select></td>
      <td><input data-k="label" value="${esc(l.label)}" placeholder="${l.spot === "none" ? "" : "e.g. BST1"}" maxlength="24"></td>
      <td class="fx">${fx || (l.spot !== "none" ? '<span class="muted">none</span>' : "")}</td>
      <td><button data-k="identify" title="Three 40 Hz pulses on this channel">Identify</button></td></tr>`;
  }).join("");
  $("#sh-channels tbody").querySelectorAll("tr").forEach((tr) => {
    const ch = +tr.dataset.ch;
    tr.querySelector('[data-k="identify"]').onclick = () => shakerTest({ pan: ch }, "identify");
    tr.querySelector('[data-k="spot"]').onchange = (ev) => updateLayout(ch, { spot: ev.target.value });
    tr.querySelector('[data-k="label"]').onchange = (ev) => updateLayout(ch, { label: ev.target.value.trim() });
  });
}

async function updateLayout(ch, patch) {
  const cur = sh.layout[ch] || { spot: "none", label: "" };
  const next = { ...cur, ...patch };
  if (next.spot === "none") delete sh.layout[ch];
  else sh.layout[ch] = next;
  const r = await fetch("/api/shakers/layout", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sh.layout) });
  if (r.ok) sh.layout = await r.json();
  renderCar();
  renderChannels();
  renderEffects();
}

const FIELD_DEFAULT = { noise: 0, amplitude: 50, volume: 100, threshold: 0 };  // monocoque's when a key is missing
function fxField(e, i, key, label, min, max, step, unit = "") {
  const v = e[key] ?? FIELD_DEFAULT[key] ?? "";
  return `<label>${label}${unit ? ` (${unit})` : ""}<span class="row">
    <input type="range" min="${min}" max="${max}" step="${step}" value="${v}" data-k="${key}" data-i="${i}">
    <input type="number" min="${min}" max="${max}" step="${step}" value="${v}" data-k="${key}" data-i="${i}"></span></label>`;
}

function renderEffects() {
  const o = sh.opts;
  if (!sh.effects.length) {
    $("#sh-effects").innerHTML = `<div class="empty">No effect yet. Add one, then save.</div>`;
    return;
  }
  $("#sh-effects").innerHTML = sh.effects.map((e, i) => {
    const wheel = o.wheel_effects.includes(e.effect);
    const mod = e.modulation || "none";
    let f = "";
    if (wheel) f += `<label>Wheels<span class="wheel-chips">${o.wheels.map((w) => `<button type="button" class="chip${e.wheels.includes(w) ? " on" : ""}" data-wheel="${w}" data-i="${i}" title="${WHEEL_NAME[w]}">${w}</button>`).join("")}</span></label>`;
    if (e.effect !== "Gear") f += `<label>Modulation<select data-k="modulation" data-i="${i}">${o.modulations.map((m) => `<option value="${m}"${m === mod ? " selected" : ""}>${{ none: "None (constant)", frequency: "Pitch (frequency)", amplify: "Strength (amplitude)" }[m]}</option>`).join("")}</select></label>`;
    f += fxField(e, i, "volume", "Volume", 0, 100, 1, "%");
    f += fxField(e, i, "frequency", mod === "frequency" ? "Frequency min" : "Frequency", 5, 120, 1, "Hz");
    if (mod === "frequency") f += fxField(e, i, "frequencyMax", "Frequency max", 5, 120, 1, "Hz");
    f += fxField(e, i, "amplitude", mod === "amplify" ? "Strength min" : "Strength", 0, 100, 1, "%");
    if (mod === "amplify") f += fxField(e, i, "amplitudeMax", "Strength max", 0, 100, 1, "%");
    if (e.effect === "Gear") f += fxField(e, i, "duration", "Duration", 0.05, 1, 0.05, "s");
    if (wheel && !OWN_FX.has(e.effect)) f += fxField(e, i, "threshold", "Threshold", 0, 1, 0.05);
    f += fxField(e, i, "noise", "Roughness", 0, 20, 1, "Hz");
    const chOpts = o.channels.map((_, ch) => `<option value="${ch}"${ch === e.pan ? " selected" : ""}>${esc(chName(ch))}</option>`).join("");
    return `<div class="fx${sh.active === i ? " active" : ""}" data-i="${i}" style="--c:var(${FX_COLOUR[e.effect] || "--muted"})">
      <div class="fx-head">
        <select data-k="effect" data-i="${i}">${o.effects.map((x) => `<option value="${x}"${x === e.effect ? " selected" : ""}>${FX_NAME[x]}</option>`).join("")}</select>
        <span class="muted">on</span>
        <select data-k="pan" data-i="${i}" title="Shaker (sound card channel)">${chOpts}</select>
        <span class="grow"></span>
        <button class="test" data-act="test" data-i="${i}">Test</button>
        <button class="del" data-act="del" data-i="${i}" title="Remove this effect">Remove</button>
      </div>
      <div class="fx-fields">${f}</div>
      <div class="fx-hint">${wheel && !OWN_FX.has(e.effect) ? wheelNote(e) : !e.wheels?.length && wheel ? '<b class="warn">Pick at least one wheel. </b>' : ""}${FX_HINT[e.effect] || ""}</div></div>`;
  }).join("");

  const box = $("#sh-effects");
  box.querySelectorAll("[data-k]").forEach((el) => {
    el.oninput = el.onchange = (ev) => onEffectInput(ev);
  });
  box.querySelectorAll("[data-act=test]").forEach((b) => (b.onclick = () => shakerTest(sh.effects[+b.dataset.i])));
  box.querySelectorAll("[data-act=del]").forEach((b) => (b.onclick = () => {
    const i = +b.dataset.i;
    sh.effects.splice(i, 1);
    if (sh.active === i) sh.active = null;
    else if (sh.active > i) sh.active--;
    markShakersDirty(true);
  }));
  box.querySelectorAll(".chip").forEach((b) => (b.onclick = () => toggleWheel(+b.dataset.i, b.dataset.wheel)));
  box.querySelectorAll(".fx").forEach((card) => {
    const i = +card.dataset.i;
    card.onmouseenter = () => renderWheels(sh.effects[i]);
    card.onmouseleave = () => renderWheels();
    // a click on a wheel effect hands the car's tyres to it
    card.onclick = (ev) => {
      if (ev.target.closest("button, select, input") && !ev.target.closest(".chip")) return;
      setActiveEffect(i);
    };
  });
}

// monocoque names one wheel, an axle or all four; other sets are saved as one effect per part
function wheelParts(wheels) {
  const w = ["FL", "FR", "RL", "RR"].filter((x) => wheels.includes(x));
  if (w.length === 4) return [w];
  const parts = [];
  for (const pair of [["FL", "FR"], ["RL", "RR"]]) {
    const got = pair.filter((x) => w.includes(x));
    if (got.length === 2) parts.push(got);
    else got.forEach((x) => parts.push([x]));
  }
  return parts;
}
function wheelNote(e) {
  const parts = wheelParts(e.wheels);
  if (!parts.length) return `<b class="warn">Pick at least one wheel. </b>`;
  if (parts.length === 1) return "";
  const off = sh.opts.group_offset;
  return `Saved as ${parts.length} monocoque effects (${parts.map((p) => p.join("+")).join(", ")}), ` +
    `each ${off} Hz above the previous so they add up on the shaker instead of cancelling. `;
}

function setActiveEffect(i) {
  const e = sh.effects[i];
  const next = e && sh.opts.wheel_effects.includes(e.effect) ? i : null;
  if (next === sh.active) return;
  sh.active = next;
  document.querySelectorAll("#sh-effects .fx").forEach((c) => c.classList.toggle("active", +c.dataset.i === sh.active));
  renderWheels();
}

function toggleWheel(i, w) {
  const e = sh.effects[i];
  e.wheels = e.wheels.includes(w) ? e.wheels.filter((x) => x !== w) : [...e.wheels, w];
  sh.active = i;
  markShakersDirty(true);
}

function onEffectInput(ev) {
  const el = ev.target, i = +el.dataset.i, k = el.dataset.k, e = sh.effects[i];
  if (k === "effect") {
    // keep the shaker and volume, take the new effect's starting values
    sh.effects[i] = { device: "Sound", effect: el.value, pan: e.pan, volume: e.volume ?? 100, fps: e.fps ?? 60, ...structuredClone(FX_DEFAULTS[el.value]) };
    sh.active = sh.opts.wheel_effects.includes(el.value) ? i : null;
    markShakersDirty(true);
    return;
  }
  if (k === "modulation") {
    e[k] = el.value;
    if (k === "modulation" && el.value === "frequency" && !(e.frequencyMax >= e.frequency)) e.frequencyMax = e.frequency * 2;
    if (k === "modulation" && el.value === "amplify" && e.amplitudeMax == null) e.amplitudeMax = Math.min(100, e.amplitude + 40);
    markShakersDirty(true);
    return;
  }
  if (k === "pan") {
    e.pan = +el.value;
    markShakersDirty(false);
    renderCar();
    renderChannels();
    return;
  }
  if (el.value === "") return;
  e[k] = +el.value;
  // keep the slider and the number box of this field in step
  el.closest(".row").querySelectorAll("input").forEach((x) => { if (x !== el) x.value = el.value; });
  if (k === "frequency" && e.modulation === "frequency" && e.frequencyMax < e.frequency) e.frequencyMax = e.frequency;
  markShakersDirty(false);
}

function markShakersDirty(rerender) {
  sh.dirty = true;
  scheduleReplayReload();
  if (rerender) {
    if (sh.active != null && !sh.opts.wheel_effects.includes(sh.effects[sh.active]?.effect)) sh.active = null;
    renderEffects();
    renderCar();
    renderChannels();
    renderWheels();
  }
  renderShakerState();
}

// tyres on the car: the hovered effect's wheels, else the active effect's (clickable)
function renderWheels(preview = null) {
  const act = sh.active != null ? sh.effects[sh.active] : null;
  const e = preview && sh.opts.wheel_effects.includes(preview.effect) ? preview : act;
  const on = e && sh.opts.wheel_effects.includes(e.effect) ? e.wheels : [];
  document.querySelectorAll("#sh-car .tyre-r").forEach((t) => {
    t.classList.toggle("hl", on.includes(t.dataset.wheel));
    t.classList.toggle("pick", !!act);
    t.style.setProperty("--hl", e ? `var(${FX_COLOUR[e.effect]})` : "");
    t.innerHTML = act ? `<title>${WHEEL_NAME[t.dataset.wheel]}: click to ${act.wheels.includes(t.dataset.wheel) ? "remove from" : "add to"} ${FX_NAME[act.effect]}</title>` : "";
  });
  $("#sh-car-hint").textContent = act
    ? `click the wheels for ${FX_NAME[act.effect]} on ${sh.opts.channels[act.pan]}`
    : "click a shaker to test it, a wheel effect to pick its wheels";
}
document.querySelectorAll("#sh-car .tyre-r").forEach((t) => (t.onclick = () => {
  if (sh.active != null) toggleWheel(sh.active, t.dataset.wheel);
}));

let shPlayTimer = null;
async function shakerTest(effect, mode = "effect") {
  const r = await fetch("/api/shakers/test", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ devid: sh.devid, effect, mode }),
  });
  const d = await r.json();
  if (!r.ok) {
    $("#sh-note").textContent = d.detail === "the shaker sound card is not connected"
      ? "Test not played: the shaker sound card is not connected (plug it in, then pick it in Sound card)" : d.detail || "Test failed";
    return;
  }
  sh.playing = effect.pan;
  renderCar();
  clearTimeout(shPlayTimer);
  shPlayTimer = setTimeout(() => { sh.playing = null; renderCar(); }, d.seconds * 1000);
}

// pick up a card plugged in since the tab was opened
$("#sh-sink").onfocus = async () => {
  const r = await fetch("/api/shakers");
  if (!r.ok) return;
  sh.sinks = (await r.json()).sinks;
  renderSinks();
  renderShakerState();
};
$("#sh-sink").onchange = (ev) => {
  sh.devid = ev.target.value;
  markShakersDirty(false);
  renderSinks();
};
$("#sh-stop").onclick = async () => {
  await fetch("/api/shakers/stop", { method: "POST" });
  clearTimeout(shPlayTimer);
  sh.playing = null;
  renderCar();
};
$("#sh-revert").onclick = () => loadShakers();
$("#sh-add").onclick = () => {
  const pan = sh.sel ?? +(Object.keys(sh.layout)[0] ?? 0);
  sh.effects.push({ device: "Sound", effect: "Engine", pan, volume: 100, fps: 60, ...structuredClone(FX_DEFAULTS.Engine) });
  markShakersDirty(true);
};
$("#sh-save").onclick = async () => {
  const r = await fetch("/api/shakers", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ devid: sh.devid, effects: sh.effects }),
  });
  const d = await r.json();
  if (!r.ok) { $("#sh-note").textContent = d.detail || "Save failed"; return; }
  await loadShakers();
  $("#sh-note").textContent = d.running ? "Saved. monocoque is running: Restart monocoque in System to apply it now." : "Saved.";
};
addEventListener("beforeunload", (ev) => { if (sh.dirty) ev.preventDefault(); });

// ---------- shaker replay: a recorded session felt on the shakers ----------
// The server runs the clock and the sound; the page polls it and extrapolates between polls.
const rp = { d: null, outline: null, st: null, stAt: 0, lastDraw: -1, reloadTimer: null, poll: null };

async function loadReplaySessions() {
  let list;
  try { list = await (await fetch("/api/sessions")).json(); } catch { return; }
  const cur = $("#rp-session").value || rp.d?.ref || "";
  $("#rp-session").innerHTML = `<option value="">Choose a recorded session…</option>` + list.filter((x) => x.driving_s > 0).map((x) => {
    const t = new Date(x.started_at * 1000);
    return `<option value="${x.ref}">${x.day} ${t.toTimeString().slice(0, 5)} · ${esc(x.session_type)} · ${esc(x.track)} · ${esc(x.car)} · ${x.laps ?? 0} lap${x.laps === 1 ? "" : "s"}</option>`;
  }).join("");
  $("#rp-session").value = cur;
  if (!rp.d) {
    // a replay the server still has (page reloaded, or opened on another device)
    const st = await (await fetch("/api/shakers/replay")).json().catch(() => ({}));
    if (st.ref && !rp.d) { $("#rp-session").value = st.ref; loadReplay(st.ref); }
  }
}

async function loadReplay(ref) {
  if (!ref) return;
  $("#rp-note").textContent = "loading…";
  const r = await fetch("/api/shakers/replay", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref, devid: sh.devid, effects: sh.effects }),
  });
  const d = await r.json();
  if (!r.ok) { $("#rp-note").textContent = d.detail || "Cannot load this session"; return; }
  const fresh = rp.d?.ref !== ref;
  rp.d = d;
  if (fresh) {
    const out = [];
    for (let i = 0; i < d.x.length; i++) {
      const q = out[out.length - 1];
      if (!q || Math.hypot(d.x[i] - q[0], d.z[i] - q[1]) > 3) out.push([d.x[i], d.z[i]]);
    }
    rp.outline = out;
    const laps = [...new Set(d.lap)].filter((n) => n > 0);
    $("#rp-lap").innerHTML = `<option value="">Lap…</option>` + laps.map((n) => `<option value="${n}">Lap ${n}</option>`).join("");
    const sl = $("#rp-slider");
    sl.min = 0; sl.max = Math.max(1, d.t1 - d.t0);
  }
  setReplayState(d.state);
  ["#rp-lap", "#rp-play", "#rp-slider"].forEach((q) => ($(q).disabled = false));
  $("#rp-body").hidden = false;
  const notes = [];
  const wheelFx = sh.effects.some((e) => ["TyreSlip", "TyreLock", "ABS"].includes(e.effect));
  if (wheelFx && d.diameters === "simdash") notes.push("this car's tyre sizes are not in monocoque's file yet (simdash adds them at its next start), so in the car wheelspin and lock-up stay silent; the replay uses simdash's");
  if (wheelFx && !d.diameters) notes.push("no tyre sizes known for this car: wheelspin and lock-up stay silent");
  if (!cardConnected()) notes.push("shaker card not connected: showing only");
  $("#rp-note").textContent = notes.join(" · ");
  renderReplayFx();
  renderMiniCar();
  rp.lastDraw = -1;
}

// small top view of the car with its shakers (replay, live and session maps): same body and
// shaker spots as the big one on the Shakers tab, no labels
function buildShakerCar(svg, layout, channels) {
  const body = $("#sh-car .car-body").outerHTML.replace(/<title>.*?<\/title>/g, "");
  const bySpot = {};
  Object.keys(layout).map(Number).sort((a, b) => a - b).forEach((ch) => (bySpot[layout[ch].spot] ??= []).push(ch));
  let spots = "";
  for (const [spot, chs] of Object.entries(bySpot)) {
    const [x0, y] = SPOT_XY[spot] || [120, 220];
    chs.forEach((ch, k) => {
      const x = x0 + (k - (chs.length - 1) / 2) * 44;
      const name = `${ch} · ${channels[ch]}${layout[ch].label ? ` · ${layout[ch].label}` : ` · ${SPOT_NAME[spot]}`}`;
      spots += `<g class="spot" data-ch="${ch}" transform="translate(${x} ${y})"><title>${esc(name)}</title>
        <circle r="25"/><text>${channels[ch]}</text></g>`;
    });
  }
  $(svg).innerHTML = body + spots;
}
function renderMiniCar() { buildShakerCar("#rp-car", sh.layout, sh.opts.channels); }

// light each shaker with its strongest effect: chan = { channel: { a: strength %, fx: effect } }
function paintShakerCar(sel, chan, buzz) {
  document.querySelectorAll(sel).forEach((g) => {
    const c = chan[g.dataset.ch];
    const on = !!c && c.a > 0;
    g.style.setProperty("--lvl", on ? 0.25 + 0.75 * c.a / 100 : 0);
    if (on) g.style.setProperty("--glow", `var(${FX_COLOUR[c.fx]})`);
    g.classList.toggle("felt", on && buzz);
    const t = g.querySelector("title");
    if (t) t.textContent = t.textContent.replace(/: .*$/, "") + (on ? `: ${FX_NAME[c.fx]} ${c.a}%` : "");
  });
}

// the live and session cars need the shaker layout even before the Shakers tab is opened
const shCar = { layout: null, channels: null };
async function loadShakerCars() {
  if (sh.loaded) Object.assign(shCar, { layout: sh.layout, channels: sh.opts.channels });
  else {
    try {
      const d = await (await fetch("/api/shakers")).json();
      Object.assign(shCar, { layout: d.layout, channels: d.options.channels });
    } catch { return; }
  }
  for (const svg of ["#live-car", "#sess-car"]) buildShakerCar(svg, shCar.layout, shCar.channels);
  if (hist.shakers) paintSessionCar();
}

function setReplayState(st) {
  rp.st = st;
  rp.stAt = performance.now();
  $("#rp-play").textContent = st.playing ? "Pause" : "Play";
  if (st.error) $("#rp-note").textContent = st.error;
}
const replayTs = () => {
  const st = rp.st;
  if (!st) return 0;
  return st.playing ? Math.min(st.t1 ?? Infinity, st.ts + (performance.now() - rp.stAt) / 1000 * st.speed) : st.ts;
};

async function replayControl(body) {
  const r = await fetch("/api/shakers/replay/control", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ devid: sh.devid, speed: +$("#rp-speed").value, ...body }),
  });
  const d = await r.json();
  if (!r.ok) { $("#rp-note").textContent = d.detail || "Replay failed"; return; }
  setReplayState(d);
  clearInterval(rp.poll);
  if (d.playing) rp.poll = setInterval(pollReplay, 250);
}
async function pollReplay() {
  try {
    const st = await (await fetch("/api/shakers/replay")).json();
    if (st.ref !== rp.d?.ref) return;
    setReplayState(st);
    if (!st.playing) clearInterval(rp.poll);
  } catch { /* simdash restarting */ }
}

// effects in the replay: one row per effect card
function renderReplayFx() {
  const d = rp.d;
  $("#rp-fx").innerHTML = sh.effects.map((e, i) => {
    const wheels = sh.opts.wheel_effects.includes(e.effect) ? ` ${e.wheels.join("+")}` : "";
    return `<div class="rp-fx" style="--c:var(${FX_COLOUR[e.effect]})"><span class="n">${FX_NAME[e.effect]}${wheels} → ${esc(chName(e.pan))}</span>
      <span class="v" data-i="${i}"></span><div class="bar"><i data-i="${i}"></i></div></div>`;
  }).join("") || '<span class="muted">No effect configured.</span>';
  if (d && d.level.length !== sh.effects.length) scheduleReplayReload();
}

function replayIndex(t) {
  const ts = rp.d.ts;
  let lo = 0, hi = ts.length - 1;
  if (t <= ts[0]) return 0;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ts[m] <= t) lo = m; else hi = m - 1; }
  return lo;
}

function drawReplayFrame() {
  const d = rp.d;
  if (!d || currentTab !== "shakers") return;
  const t = replayTs();
  if (Math.abs(t - rp.lastDraw) < 0.01 && !rp.st?.playing) return;
  rp.lastDraw = t;
  const i = replayIndex(t), j = Math.min(i + 1, d.ts.length - 1);
  const u = j > i && d.ts[j] - d.ts[i] < 2 ? clamp((t - d.ts[i]) / (d.ts[j] - d.ts[i]), 0, 1) : 0;
  const x = d.x[i] + (d.x[j] - d.x[i]) * u, z = d.z[i] + (d.z[j] - d.z[i]) * u;
  drawMap($("#rp-map"), rp.outline, [{ x, z, player: true, pos: d.gear[i] > 0 ? d.gear[i] : d.gear[i] < 0 ? "R" : "N" }], true);

  if (document.activeElement !== $("#rp-slider")) $("#rp-slider").value = t - d.t0;
  $("#rp-time").textContent = `${fmtDur(t - d.t0)} / ${fmtDur(d.t1 - d.t0)}`;
  $("#rp-speed-v").textContent = d.speed[i];
  $("#rp-gear").textContent = d.gear[i] > 0 ? d.gear[i] : d.gear[i] < 0 ? "R" : "N";
  const rpm = d.rpm[i] + (d.rpm[j] - d.rpm[i]) * u, mx = d.maxrpm[i] || 0;
  $("#rp-rpm").textContent = `${Math.round(rpm)}${mx ? ` / ${mx}` : ""}`;
  const fill = $("#rp-rpmfill");
  fill.style.width = mx ? `${clamp(rpm / mx, 0, 1) * 100}%` : "0";
  fill.classList.toggle("red", mx > 0 && rpm >= 0.95 * mx);
  drawReplaySurface(i);
  $("#rp-lapv").textContent = d.lap[i] || "–";
  $("#rp-thr").style.width = `${d.throttle[i] * 100}%`;
  $("#rp-brk").style.width = `${d.brake[i] * 100}%`;

  // effect levels, and each shaker on the car lit by its strongest effect
  const chan = {};
  d.level.forEach((lv, k) => {
    const a = lv[i], e = sh.effects[k];
    const v = $(`#rp-fx .v[data-i="${k}"]`), bar = $(`#rp-fx i[data-i="${k}"]`);
    if (v) v.textContent = a ? `${d.freq[k][i]} Hz ${a}%` : "–";
    if (bar) bar.style.width = `${a}%`;
    if (e && a > (chan[e.pan]?.a ?? 0)) chan[e.pan] = { a, fx: e.effect };
  });
  paintShakerCar("#sh-spots .spot, #rp-car .spot", chan, !!rp.st?.playing);
}

// where the car is (worst surface under any tyre, from AMS2's raw terrain) and each wheel's
// terrain and monocoque slip, highlighted past the threshold of an effect on that wheel
const SURF_NAME = ["On track", "Kerb", "Off track"];
function slipThreshold(kind, w) {
  const th = sh.effects.filter((e) => e.effect === kind && e.wheels?.includes(w)).map((e) => e.threshold ?? 0);
  return th.length ? Math.min(...th) : null;
}
function drawReplaySurface(i) {
  const d = rp.d;
  const codes = d.terrain.map((col) => col[i]);
  const cls = codes.map((c) => (c == null ? null : d.surface[c] ?? null));
  const worst = Math.max(-1, ...cls.filter((c) => c != null));
  const where = $("#rp-where");
  where.textContent = worst < 0 ? "–" : SURF_NAME[worst];
  where.className = "pill" + (worst < 0 ? "" : ` s${worst}`);
  $("#rp-wheels").innerHTML = ["FL", "FR", "RL", "RR"].map((w, k) => {
    const name = codes[k] == null ? "–" : d.terrains[codes[k]] ?? `terrain ${codes[k]}`;
    const sl = d.slip[k][i];
    const lockTh = slipThreshold("TyreLock", w), spinTh = slipThreshold("TyreSlip", w);
    const over = sl > 0 ? lockTh != null && sl > lockTh : spinTh != null && -sl > spinTh;
    const colour = sl > 0 ? FX_COLOUR.TyreLock : FX_COLOUR.TyreSlip;
    const slip = Math.abs(sl) < 0.005 ? "" : ` · <span class="${over ? "over" : ""}" style="--c:var(${colour})">${sl > 0 ? "lock" : "spin"} ${Math.abs(sl).toFixed(2)}</span>`;
    return `<span><b>${w}</b> <span class="s${cls[k]}">${esc(name)}</span>${slip}</span>`;
  }).join("");
  document.querySelectorAll("#rp-car .tyre-r").forEach((t) => {
    const k = ["FL", "FR", "RL", "RR"].indexOf(t.dataset.wheel);
    t.setAttribute("class", `tyre-r s${cls[k] ?? 0}`);
  });
}

function scheduleReplayReload() {
  if (!rp.d) return;
  clearTimeout(rp.reloadTimer);
  rp.reloadTimer = setTimeout(() => loadReplay(rp.d.ref), 400);
}

$("#rp-session").onfocus = loadReplaySessions;
$("#rp-session").onchange = async (ev) => {
  if (rp.st?.playing) await replayControl({ action: "pause" });
  loadReplay(ev.target.value);
};
$("#rp-play").onclick = () => replayControl(rp.st?.playing ? { action: "pause" } : { action: "play", ts: replayTs() >= rp.d.t1 - 0.1 ? rp.d.t0 : replayTs() });
$("#rp-speed").onchange = () => { if (rp.st?.playing) replayControl({ action: "play", ts: replayTs() }); };
$("#rp-slider").oninput = (ev) => replayControl({ action: rp.st?.playing ? "play" : "seek", ts: rp.d.t0 + +ev.target.value });
$("#rp-lap").onchange = (ev) => {
  const n = +ev.target.value;
  const k = rp.d.lap.findIndex((l) => l === n);
  ev.target.value = "";
  if (k >= 0) replayControl({ action: rp.st?.playing ? "play" : "seek", ts: rp.d.ts[k] });
};
(function replayLoop() { drawReplayFrame(); requestAnimationFrame(replayLoop); })();

// ---------- Corner Genie: corner recap popup ----------
// As each corner is left, the live lap is compared with the reference lap (focus.ref: best lap on this
// track with this car) over that corner, up to the trigger point just after its apex.
// used until the server's settings arrive (or if this simdash is older than the settings API)
const PILOT_DEFAULTS = { enabled: true, duration: 4, position: "top", size: "screen", only: "all", min_delta: 0.05,
  fields: { time: true, brake: true, throttle: true, entry: true, min: true, exit: true, gear: true } };
const pf = { s: PILOT_DEFAULTS, saveT: null, replay: null, saved: true };
const pilot = { lap: null, rows: [], next: null, corners: null };
const pilotTrigger = (c) => Math.min(c.e, c.apex + 120);

async function loadPilotSettings() {
  try {
    const r = await fetch("/api/genie");
    pf.saved = r.ok;
    if (r.ok) pf.s = await r.json();
  } catch { /* keep the last ones */ }
}
loadPilotSettings();
setInterval(() => { if (currentTab !== "genie") loadPilotSettings(); }, 15000);

// the card waits for full throttle after the apex: at least 120 m past it, at most 250 m (or the corner's end)
const PILOT_MIN_AFTER = 120, PILOT_MAX_AFTER = 250;
function pilotTriggerFor(L, c) {
  const late = Math.min(c.e, c.apex + PILOT_MAX_AFTER);
  for (let i = sampleAt(L, c.apex); i < L.lap_dist.length && L.lap_dist[i] <= late; i++) {
    if (L.throttle[i] >= FULL_THROTTLE) return Math.min(late, Math.max(L.lap_dist[i], c.apex + PILOT_MIN_AFTER));
  }
  return late;
}
function pilotRecap(L, ref, c, k, tk, trig = pilotTriggerFor(L, c)) {
  const a = cornerStats(L, { ...c, e: trig });
  const b = cornerStats(ref, { ...c, e: Math.min(c.e, c.apex + PILOT_MAX_AFTER) });
  if (!a || !b) return null;
  const dt = (timeAt(L, trig) - timeAt(L, c.s)) - (timeAt(ref, trig) - timeAt(ref, c.s));
  return { name: cornerName(tk, c, k), dt, a, b, c, trig };
}
function pilotWanted(rec, s = pf.s) {
  if (!s) return false;
  if (s.only === "lost") return rec.dt >= s.min_delta;
  if (s.only === "gained") return rec.dt <= -s.min_delta;
  return true;
}
// line icons (24 × 24, drawn with the text colour); the corner ones share one bend so they read as a set
const PILOT_ICON_PATHS = {
  time: '<circle cx="12" cy="13" r="8"/><path d="M12 13V9M9 2h6M12 2v3"/>',
  brake: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.5"/><path d="M5 5.5A9.5 9.5 0 0 1 12 2.5" stroke-width="4"/>',
  entry: '<path d="M3 5h8q8 0 8 8v8" opacity=".3"/><path d="M3 5h8"/><path d="M8 2l3 3-3 3"/>',
  min: '<path d="M3 5h8q8 0 8 8v8" opacity=".3"/><circle cx="16.7" cy="7.3" r="2.6" fill="currentColor"/>',
  exit: '<path d="M3 5h8q8 0 8 8v8" opacity=".3"/><path d="M19 13v8"/><path d="M16 18l3 3 3-3"/>',
  gear: '<path d="M6 4v16M12 4v16M18 4v8M6 12h12"/><circle cx="6" cy="4" r="1.5" fill="currentColor"/><circle cx="12" cy="20" r="1.5" fill="currentColor"/>',
  throttle: '<path d="M8 3h8l1.5 13h-11z"/><path d="M9.5 7h5M9 10.5h6"/><path d="M12 16v5M8 21h8"/>',
};
const PILOT_ICON_NAMES = { time: "Time gained or lost in the corner", brake: "Braking point: metres before the apex",
  throttle: "Full throttle: metres after the apex", entry: "Entry speed",
  min: "Slowest speed", exit: "Exit speed", gear: "Gear at the slowest point" };
const pilotIcon = (k) => `<svg class="pp-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="${PILOT_ICON_NAMES[k]}"><title>${PILOT_ICON_NAMES[k]}</title>${PILOT_ICON_PATHS[k]}</svg>`;

function pilotHtml(rec, s) {
  const f = s.fields, { a, b } = rec;
  const item = (k, body) => `<span class="pp-item">${pilotIcon(k)}${body}</span>`;
  const sp = (k, va, vb) => {
    const d = Math.round(va) - Math.round(vb);
    return item(k, `<b>${Math.round(va)}</b><span class="${d > 0 ? "pp-good" : d < 0 ? "pp-bad" : "pp-same"}">${fmtDiff(d)}</span>`);
  };
  // distances: green when better than the best lap (braking later, full throttle sooner), red when worse
  const dist = (k, va, vb) => {
    if (va == null) return item(k, `<b class="pp-strike" title="${k === "brake" ? "no braking" : "not back to full throttle"}">—</b>`);
    const d = Math.round(va) - Math.round(vb ?? va);
    const cls = vb == null || Math.abs(d) < 3 ? "pp-same" : d < 0 ? "pp-good" : "pp-bad";
    return item(k, `<b>${Math.round(va)} m</b>${vb == null ? "" : `<span class="${cls}">${Math.abs(d) < 3 ? "✓" : fmtDiff(d)}</span>`}`);
  };
  // a grid: columns in driving order (entry | apex | exit), one row per kind of value (distances, speeds, gear)
  const rows = [
    [f.brake && (a.brakeToApex != null || b.brakeToApex != null) ? dist("brake", a.brakeToApex, b.brakeToApex) : "", "",
      f.throttle ? dist("throttle", a.full, b.full) : ""],
    [f.entry ? sp("entry", a.entry, b.entry) : "", f.min ? sp("min", a.min, b.min) : "", f.exit ? sp("exit", a.exit, b.exit) : ""],
    ["", f.gear && a.gear != null ? item("gear", `<b>${a.gear}</b>${b.gear != null && b.gear !== a.gear ? `<span class="pp-same">best ${b.gear}</span>` : ""}`) : "", ""],
  ].filter((r) => r.some(Boolean));
  const dtCls = rec.dt < -0.02 ? "pp-good" : rec.dt > 0.02 ? "pp-bad" : "";
  const dt = f.time ? `<span class="pp-dt ${dtCls}">${pilotIcon("time")}${rec.dt > 0 ? "+" : rec.dt < 0 ? "−" : "±"}${Math.abs(rec.dt).toFixed(2)} s</span>` : "";
  const cls = ["pp-in", "pp-apex", "pp-out"];
  return `<div class="pp-head"><span class="pp-name">${esc(rec.name)}</span>${dt}</div>`
    + (rows.length ? `<div class="pp-grid">${rows.map((r) => r.map((c, i) => `<div class="pp-cell ${cls[i]}">${c}</div>`).join("")).join("")}</div>` : "");
}
function showPilot(el, rec, s = pf.s, stay = false) {
  if (!s) return;
  el.className = `pilot-pop pp-${s.size} pp-${s.position} ${rec.dt < -0.02 ? "pp-gain" : rec.dt > 0.02 ? "pp-loss" : ""}`;
  el.innerHTML = pilotHtml(rec, s);
  el.hidden = false;
  clearTimeout(el._fade);
  clearTimeout(el._hide);
  if (stay) return;  // the preview box keeps its card
  el._fade = setTimeout(() => el.classList.add("fade"), s.duration * 1000);
  el._hide = setTimeout(() => { el.hidden = true; }, s.duration * 1000 + 400);
}
// every live trace row; backlog rows (fresh = false) only fill the lap buffer
function pilotRow(r, fresh) {
  if (r.length <= LT.gear) return;
  if (r[LT.lap] !== pilot.lap) { pilot.lap = r[LT.lap]; pilot.rows = []; pilot.next = null; }
  pilot.rows.push(r);
  const corners = focus.corners;
  if (!corners.length || !focus.ref) return;
  if (pilot.corners !== corners) { pilot.corners = corners; pilot.next = null; }  // new reference lap
  const d = r[LT.dist];
  if (pilot.next == null) {  // joined mid-lap: skip the corners already left
    pilot.next = corners.findIndex((c) => pilotTrigger(c) > d);
    if (pilot.next < 0) pilot.next = corners.length;
  }
  const ready = (c) => d >= Math.min(c.e, c.apex + PILOT_MAX_AFTER)
    || (d >= c.apex + PILOT_MIN_AFTER && r[2] >= FULL_THROTTLE);
  while (pilot.next < corners.length && ready(corners[pilot.next])) {
    const k = pilot.next++, c = corners[k];
    if (!fresh || !pf.s?.enabled) continue;
    if (pilot.rows[0][LT.dist] > Math.max(c.s, 0) + 100) continue;  // not seen from the corner's start
    const rec = pilotRecap(rowsAsLap(pilot.rows), focus.ref, c, k, focus.trackKey, Math.min(d, c.e));
    if (rec && pilotWanted(rec)) showPilot($("#pilot-pop"), rec);
  }
}

// ---- the Corner Genie tab ----
async function loadPilotTab() {
  await loadPilotSettings();
  renderPilotForm();
  pilotPreview();
  if (!$("#pf-session").options.length) loadPilotSessions();
}
function renderPilotForm() {
  const s = pf.s;
  if (!s) return;
  $("#pf-enabled").checked = s.enabled;
  $("#pf-duration").value = $("#pf-duration-n").value = s.duration;
  $("#pf-position").value = s.position;
  $("#pf-size").value = s.size;
  $("#pf-only").value = s.only;
  $("#pf-min").value = s.min_delta;
  $("#pf-min").disabled = s.only === "all";
  document.querySelectorAll(".pf-fields input[data-f]").forEach((i) => (i.checked = !!s.fields[i.dataset.f]));
}
function pilotFormValues() {
  const fields = {};
  document.querySelectorAll(".pf-fields input[data-f]").forEach((i) => (fields[i.dataset.f] = i.checked));
  return { enabled: $("#pf-enabled").checked, duration: +$("#pf-duration-n").value, position: $("#pf-position").value,
    size: $("#pf-size").value, only: $("#pf-only").value, min_delta: +$("#pf-min").value, fields };
}
function schedulePilotSave() {
  pf.s = { ...pf.s, ...pilotFormValues() };  // the preview follows at once
  $("#pf-min").disabled = pf.s.only === "all";
  pilotPreview();
  clearTimeout(pf.saveT);
  pf.saveT = setTimeout(async () => {
    const r = await fetch("/api/genie", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pilotFormValues()) });
    if (r.ok) pf.s = await r.json();
    else if (r.status === 404) $("#pf-save-note").textContent = "Not saved: restart simdash once (it predates these settings). The popup uses them until you reload the page.";
    else alert((await r.json()).detail || "Save failed");
  }, 300);
}
$("#pf-duration").oninput = (e) => { $("#pf-duration-n").value = e.target.value; schedulePilotSave(); };
$("#pf-duration-n").oninput = (e) => { $("#pf-duration").value = e.target.value; schedulePilotSave(); };
for (const id of ["#pf-enabled", "#pf-position", "#pf-size", "#pf-only", "#pf-min"]) $(id).onchange = schedulePilotSave;
$(".pf-fields").onchange = schedulePilotSave;
document.querySelectorAll(".pf-fields input[data-f]").forEach((i) => i.insertAdjacentHTML("afterend", pilotIcon(i.dataset.f)));

const PILOT_SAMPLE = { name: "C6 Hawthorn", dt: 0.12,
  a: { brake: 2290, brakeToApex: 97, full: 52, entry: 149, min: 93, exit: 127, gear: 3 },
  b: { brake: 2302, brakeToApex: 85, full: 38, entry: 153, min: 91, exit: 130, gear: 3 } };
// tests show the card twice: in the preview box, and as the real popup over the page
const pilotTest = (rec) => { pilotPreview(rec); showPilot($("#pilot-pop"), rec); };
// the preview box always shows a card: the latest test or replay one, else the sample
function pilotPreview(rec = pf.last || PILOT_SAMPLE) { pf.last = rec; showPilot($("#pf-pop"), rec, pf.s, true); }
$("#pf-sample").onclick = () => pilotTest(PILOT_SAMPLE);

async function loadPilotSessions() {
  const all = (await (await fetch("/api/sessions")).json()).filter((s) => s.best_lap).slice(0, 60);
  $("#pf-session").innerHTML = all.map((s) => `<option value="${s.ref}" data-track="${esc(s.track)}" data-variation="${esc(s.variation)}">`
    + `${new Date(s.started_at * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`
    + ` · ${esc(s.session_type)} · ${esc(s.track)} · ${esc(s.car)}</option>`).join("");
  if (all.length) loadPilotLaps();
}
// replay map: the replayed lap's line, corner names from the reference, the car and the corner just reported
const pfMap = { L: null, R: null, tk: "", rec: null, raf: null };
function drawPilotMap(t = 0) {
  const L = pfMap.L, canvas = $("#pf-map");
  if (!L?.x?.length) { drawMap(canvas, [], [], false); return; }
  const outline = [];
  for (let i = 0; i < L.x.length; i += 3) outline.push([L.x[i], L.z[i]]);
  let i = 0;
  while (i < L.lap_time.length - 1 && L.lap_time[i + 1] <= t) i++;
  const marks = trackMarks(pfMap.R || L, pfMap.tk);
  const rec = pfMap.rec, R = pfMap.R;
  if (rec && R) {
    const j = sampleAt(R, rec.c.apex);
    marks.marks = [{ p: mapXY(R.x[j], R.z[j]), color: css(rec.dt < -0.02 ? "--good" : rec.dt > 0.02 ? "--critical" : "--muted") }];
  }
  drawMap(canvas, outline, [{ x: L.x[i], z: L.z[i], player: true, pos: "" }], true, marks);
}
async function previewPilotLap() {
  const opt = $("#pf-session").selectedOptions[0], lapNo = +$("#pf-lap").value;
  if (!opt || !lapNo) return;
  pfMap.L = await fetchLap(lapNo, opt.value);
  pfMap.R = null;
  pfMap.rec = null;
  pfMap.tk = `${opt.dataset.track}|${opt.dataset.variation}`;
  await loadCornerNames(opt.dataset.track, opt.dataset.variation);
  drawPilotMap(0);
}
$("#pf-lap").onchange = previewPilotLap;

async function loadPilotLaps() {
  const ref = $("#pf-session").value;
  const d = await (await fetch(`/api/sessions/${ref}`)).json();
  const timed = d.laps.filter((l) => l.lap_time);
  $("#pf-lap").innerHTML = timed.map((l) => `<option value="${l.lap}">Lap ${l.lap} · ${fmtLap(l.lap_time)}${l.valid ? "" : " (invalid)"}</option>`).join("");
  if (timed.length) $("#pf-lap").value = timed[timed.length - 1].lap;
  previewPilotLap();
}
$("#pf-session").onchange = loadPilotLaps;

function stopPilotReplay(msg = "") {
  const r = pf.replay;
  if (r) { r.timers.forEach(clearTimeout); clearInterval(r.tick); cancelAnimationFrame(pfMap.raf); }
  pf.replay = null;
  $("#pf-play").textContent = "Play";
  $("#pf-replay-state").textContent = msg;
}
$("#pf-play").onclick = async () => {
  if (pf.replay) { stopPilotReplay("Stopped."); return; }
  const opt = $("#pf-session").selectedOptions[0], ref = $("#pf-session").value, lapNo = +$("#pf-lap").value;
  if (!opt || !lapNo) return;
  $("#pf-replay-state").textContent = "Loading…";
  // reference: the fastest valid lap on this track with this car, this session included
  const [others, detail] = await Promise.all([(await fetch(`/api/sessions/${ref}/references`)).json(), (await fetch(`/api/sessions/${ref}`)).json()]);
  const own = detail.laps.filter((l) => l.valid && l.lap_time).sort((x, y) => x.lap_time - y.lap_time)[0];
  const best = [...others.map((o) => ({ ref: o.ref, lap: o.lap, t: o.lap_time })), ...(own ? [{ ref, lap: own.lap, t: own.lap_time }] : [])]
    .sort((x, y) => x.t - y.t)[0];
  if (!best) { $("#pf-replay-state").textContent = "No valid lap on this track with this car to compare with."; return; }
  const tk = `${opt.dataset.track}|${opt.dataset.variation}`;
  const [L, R] = await Promise.all([fetchLap(lapNo, ref), fetchLap(best.lap, best.ref), loadCornerNames(opt.dataset.track, opt.dataset.variation)]);
  const corners = findCorners(R), speed = +$("#pf-speed").value, total = L.lap_time[L.lap_time.length - 1];
  Object.assign(pfMap, { L, R, tk, rec: null });
  const r = pf.replay = { timers: [], t0: performance.now() };
  $("#pf-play").textContent = "Stop";
  corners.forEach((c, k) => {
    const rec = pilotRecap(L, R, c, k, tk);
    if (!rec || !pilotWanted(rec)) return;
    r.timers.push(setTimeout(() => { pfMap.rec = rec; pilotTest(rec); }, timeAt(L, rec.trig) / speed * 1000));
  });
  r.timers.push(setTimeout(() => stopPilotReplay(`Lap ${lapNo} done, against ${fmtLap(best.t)}${best.ref === ref && best.lap === lapNo ? " (this very lap: everything matches)" : ""}.`), total / speed * 1000 + 500));
  const vs = `against ${fmtLap(best.t)}${best.ref === ref ? ` (lap ${best.lap} of this session)` : ""}`;
  const anim = () => {
    if (pf.replay !== r) return;
    drawPilotMap((performance.now() - r.t0) / 1000 * speed);
    pfMap.raf = requestAnimationFrame(anim);
  };
  pfMap.raf = requestAnimationFrame(anim);
  r.tick = setInterval(() => {
    const t = Math.min(total, (performance.now() - r.t0) / 1000 * speed);
    $("#pf-replay-state").textContent = `Lap ${lapNo}: ${fmtDur(t)} / ${fmtDur(total)} · ${vs}`;
  }, 250);
};

// ---------- progress: one track layout + car over every session ----------
const pg = { combos: [], data: null, key: null, laps: {} };
const comboKey = (s) => `${s.track}|${s.variation}|${s.car}`;
const median = (a) => { const b = [...a].sort((x, y) => x - y), m = b.length >> 1; return b.length ? (b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2) : null; };
const fmtDelta = (v) => (v == null ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v).toFixed(3)}`);

async function loadProgress() {
  const all = await (await fetch("/api/sessions")).json();
  const by = new Map();
  for (const s of all) {
    const k = comboKey(s);
    const c = by.get(k) || { key: k, track: s.track, variation: s.variation, car: s.car, n: 0, last: 0, best: null };
    c.n++;
    c.last = Math.max(c.last, s.started_at);
    if (s.best_lap && (c.best == null || s.best_lap < c.best)) c.best = s.best_lap;
    by.set(k, c);
  }
  pg.combos = [...by.values()].sort((a, b) => b.last - a.last);
  const linked = location.hash.startsWith("#progress=") ? decodeURIComponent(location.hash.slice(10)) : null;
  const keep = linked && by.has(linked) ? linked : pg.key && by.has(pg.key) ? pg.key : pref.get("progressCombo", null);
  pg.key = keep && by.has(keep) ? keep : pg.combos[0]?.key;
  $("#pg-combo").innerHTML = pg.combos.map((c) => `<option value="${esc(c.key)}">${esc(c.track)}${c.variation && c.variation !== c.track ? ` ${esc(c.variation)}` : ""} · ${esc(c.car)} (${c.n} session${c.n > 1 ? "s" : ""})</option>`).join("");
  if (!pg.key) { $("#pg-tiles").innerHTML = `<div class="empty">No session recorded yet.</div>`; return; }
  $("#pg-combo").value = pg.key;
  await showProgress();
}
$("#pg-combo").onchange = (e) => {
  pg.key = e.target.value;
  pref.set("progressCombo", pg.key);
  history.replaceState(null, "", `#progress=${encodeURIComponent(pg.key)}`);
  showProgress();
};

// per-session figures from its laps
function sessionStats(s) {
  const valid = s.laps.filter((l) => l.valid && l.lap_time > 0);
  const best = valid.reduce((a, l) => (!a || l.lap_time < a.lap_time ? l : a), null);
  const clean = best ? valid.filter((l) => l.lap_time <= best.lap_time * 1.05).map((l) => l.lap_time) : [];
  const mean = clean.reduce((a, v) => a + v, 0) / (clean.length || 1);
  const spread = clean.length > 1 ? Math.sqrt(clean.reduce((a, v) => a + (v - mean) ** 2, 0) / (clean.length - 1)) : null;
  const sec = (k) => { const v = valid.map((l) => l[k]).filter((x) => x > 0); return v.length ? Math.min(...v) : null; };
  const [s1, s2, s3] = [sec("s1"), sec("s2"), sec("s3")];
  return { valid, best, median: median(clean), spread, s1, s2, s3, theo: s1 && s2 && s3 ? s1 + s2 + s3 : null };
}

async function showProgress() {
  const c = pg.combos.find((x) => x.key === pg.key);
  if (!c) return;
  const key = pg.key;
  const q = new URLSearchParams({ track: c.track, variation: c.variation, car: c.car });
  const data = await (await fetch(`/api/progress?${q}`)).json();
  if (pg.key !== key) return;
  data.forEach((s) => (s.st = sessionStats(s)));
  pg.data = data;
  const timed = data.filter((s) => s.st.best);
  const pb = timed.reduce((a, s) => (!a || s.st.best.lap_time < a.st.best.lap_time ? s : a), null);
  const bestSec = (k) => { const v = timed.map((s) => s.st[k]).filter(Boolean); return v.length ? Math.min(...v) : null; };
  const theo = ["s1", "s2", "s3"].map(bestSec);
  const theoAll = theo.every(Boolean) ? theo[0] + theo[1] + theo[2] : null;
  const day = (t) => new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const last = timed[timed.length - 1];
  const tile = (label, value, sub = "") => `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  $("#pg-tiles").innerHTML = [
    tile("Best lap ever", pb ? fmtLap(pb.st.best.lap_time) : "–", pb ? `${day(pb.started_at)}, ${esc(pb.session_type)}` : ""),
    tile("Best sectors", theoAll ? fmtLap(theoAll) : "–", theoAll && pb ? `${(pb.st.best.lap_time - theoAll).toFixed(3)} s to find` : "your best S1 + S2 + S3"),
    tile("Last session", last ? fmtLap(last.st.best.lap_time) : "–", last && pb ? (last === pb ? "your best ever" : `${fmtDelta(last.st.best.lap_time - pb.st.best.lap_time)} s vs best`) : ""),
    tile("Sessions", data.length, ((n) => `${n} race${n === 1 ? "" : "s"}`)(data.filter((s) => s.session_type === "Race").length)),
    tile("Valid laps", data.reduce((a, s) => a + s.st.valid.length, 0), `of ${data.reduce((a, s) => a + s.laps.length, 0)}`),
    tile("Driving", fmtDur(data.reduce((a, s) => a + (s.driving_s || 0), 0))),
  ].join("");
  drawProgressChart(data, pb);
  renderProgressTable(data, pb);
  drawProgressCorners(c, data, pb);
}

function drawProgressChart(data, pb) {
  const ss = data.filter((s) => s.st.best);
  if (!ss.length) { Plotly.purge("pg-chart"); $("#pg-chart").innerHTML = `<div class="empty">No valid lap yet.</div>`; return; }
  $("#pg-chart").innerHTML = "";
  $("#pg-chart-note").textContent = ss.length < 2 ? "Only one session with a valid lap on this track with this car so far: the trend shows from the second one." : "";
  const label = (s) => `${new Date(s.started_at * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}<br>${s.session_type}`;
  const x = ss.map(label);
  let run = Infinity;
  const pbSoFar = ss.map((s) => (run = Math.min(run, s.st.best.lap_time)));
  const line = (name, y, colour, extra = {}) => ({
    type: "scatter", mode: "lines+markers", name, x, y, customdata: y.map((v) => (v == null ? "" : fmtLap(v))),
    line: { color: css(colour), width: 2, ...extra.line }, marker: { size: 8, color: css(colour), line: { color: css("--surface"), width: 2 } },
    hovertemplate: `${name}: %{customdata}<extra></extra>`, connectgaps: true, ...extra.trace,
  });
  const traces = [
    line("Best so far", pbSoFar, "--muted", { line: { shape: "hv", dash: "dot", width: 1.5 }, trace: { mode: "lines" } }),
    line("Median lap", ss.map((s) => s.st.median), "--s-spin"),
    line("Best sectors", ss.map((s) => s.st.theo), "--s-throttle", { line: { dash: "dash" } }),
    line("Best lap", ss.map((s) => s.st.best.lap_time), "--s-speed", { line: { width: 2.5 } }),
  ];
  const ys = traces.flatMap((t) => t.y).filter((v) => v != null);
  const lo = Math.min(...ys), hi = Math.max(...ys), step = hi - lo > 6 ? 2 : hi - lo > 2.5 ? 1 : 0.5;
  const tickvals = [];
  for (let v = Math.floor(lo / step) * step; v <= hi + step; v += step) tickvals.push(+v.toFixed(3));
  Plotly.react("pg-chart", traces, plotLayout({
    hovermode: "x unified", margin: { l: 70, r: 12, t: 8, b: 56 },
    legend: { orientation: "h", x: 0, y: 1.12, font: { size: 11, color: css("--text-2") } },
    xaxis: { ...axisStyle(), type: "category", tickfont: { size: 10, color: css("--muted") } },
    yaxis: { ...axisStyle(), tickvals, ticktext: tickvals.map((v) => fmtLap(v)), zeroline: false },
  }), { displaylogo: false, responsive: true });
  const el = $("#pg-chart");
  if (!el.dataset.wired) {
    el.dataset.wired = "1";
    el.on("plotly_click", (ev) => { const s = pg.data.filter((q) => q.st.best)[ev.points[0].pointIndex]; if (s) openFromProgress(s.ref); });
  }
}

function renderProgressTable(data, pb) {
  const pbt = pb?.st.best.lap_time;
  let run = Infinity;
  const rows = data.map((s) => {
    const t = s.st.best?.lap_time, isPb = t != null && t < run;
    if (t != null) run = Math.min(run, t);
    return `<tr data-ref="${s.ref}" class="${s === pb ? "player" : ""}">
      <td>${new Date(s.started_at * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
      <td><span class="pill ${s.session_type.toLowerCase().replace(/ /g, "-")}">${esc(s.session_type)}</span></td>
      <td class="r">${s.st.valid.length} / ${s.laps.length}</td>
      <td class="r ${s === pb ? "best" : ""}">${t != null ? fmtLap(t) : "–"}${isPb && s !== data[0] ? " <span class='muted' title='new best at the time'>PB</span>" : ""}</td>
      <td class="r num">${t != null && pbt != null ? (s === pb ? "best" : fmtDelta(t - pbt)) : "–"}</td>
      <td class="r">${s.st.theo ? fmtLap(s.st.theo) : "–"}</td>
      <td class="r">${s.st.median ? fmtLap(s.st.median) : "–"}</td>
      <td class="r">${s.st.spread != null ? `±${s.st.spread.toFixed(2)} s` : "–"}</td>
      <td class="r">${s.final_pos ? `P${s.final_pos}` : ""}${s.start_pos && s.session_type === "Race" ? ` <span class="muted">from P${s.start_pos}</span>` : ""}</td></tr>`;
  });
  $("#pg-sessions tbody").innerHTML = rows.reverse().join("") || `<tr><td colspan="9" class="empty">No session.</td></tr>`;
}
$("#pg-sessions tbody").onclick = (e) => { const tr = e.target.closest("tr[data-ref]"); if (tr) openFromProgress(tr.dataset.ref); };
function openFromProgress(ref) { showTab("history"); openSession(ref); }

async function drawProgressCorners(c, data, pb) {
  const key = pg.key, tk = `${c.track}|${c.variation}`;
  const body = $("#pg-corners tbody"), head = $("#pg-corners thead");
  if (!pb) { head.innerHTML = ""; body.innerHTML = `<tr><td class="empty">No valid lap yet.</td></tr>`; return; }
  body.innerHTML = `<tr><td class="empty">Loading laps…</td></tr>`;
  // the latest 10 sessions with a valid lap, plus the one holding the best lap ever
  const ss = data.filter((s) => s.st.best);
  const cols = [...new Set([...ss.slice(-10), pb])].sort((a, b) => a.started_at - b.started_at);
  const get = (s) => (pg.laps[`${s.ref}/${s.st.best.lap}`] ??= fetchLap(s.st.best.lap, s.ref));
  const [ref, laps] = await Promise.all([get(pb), Promise.all(cols.map(get)), loadCornerNames(c.track, c.variation)]);
  if (pg.key !== key) return;
  const corners = findCorners(ref);
  const refStats = corners.map((k) => cornerStats(ref, k));
  $("#pg-corners-ref").textContent = `best lap ${fmtLap(pb.st.best.lap_time)}, ${new Date(pb.started_at * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" })}`;
  head.innerHTML = `<tr><th>Corner</th><th class="r">Best ever</th>${cols.map((s) => `<th class="r" title="${esc(s.session_type)}">${new Date(s.started_at * 1000).toLocaleString(undefined, { day: "numeric", month: "short" })}<br><span class="muted">${new Date(s.started_at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</span></th>`).join("")}</tr>`;
  const cell = (st, r, s) => {
    if (!st || !r) return `<td class="r muted">–</td>`;
    if (s === pb) return `<td class="r muted">best</td>`;
    const d = st.time - r.time, f = Math.min(1, Math.abs(d) / 0.3);
    const col = d < 0 ? "--s-speed" : "--s-spin";
    const tip = `braking ${st.brake != null ? `at ${Math.round(st.brake)} m` : "–"}${st.brake != null && r.brake != null ? ` (${fmtDiff(st.brake - r.brake)} m)` : ""}`
      + ` · entry ${Math.round(st.entry)} · slowest ${Math.round(st.min)} · exit ${Math.round(st.exit)} km/h`
      + ` (best lap: ${Math.round(r.entry)} / ${Math.round(r.min)} / ${Math.round(r.exit)})`;
    return `<td class="r num" title="${tip}" style="background:color-mix(in srgb, var(${col}) ${Math.round(f * 45)}%, transparent)">${fmtDelta(d)}</td>`;
  };
  body.innerHTML = corners.map((k, i) => {
    const nm = namedCorner(tk, k);
    return `<tr><td>${esc(nm ? nm.name : `C${i + 1}`)} <span class="muted">${Math.round(k.apex)} m</span></td>
      <td class="r">${refStats[i] ? `${refStats[i].time.toFixed(2)} s` : "–"}</td>
      ${cols.map((s, j) => cell(cornerStats(laps[j], k), refStats[i], s)).join("")}</tr>`;
  }).join("") || `<tr><td class="empty">No corner found on the best lap.</td></tr>`;
}

// ---------- wheel LEDs (Moza shift light) ----------
const led = { setup: null, timer: null, saveTimer: null, axesKey: "", test: null };
const LED_COLOUR = (i) => (i < 3 ? "--good" : i < 7 ? "--warning" : "--critical");
const fmtRpm = (v) => (v == null ? "–" : Math.round(v).toLocaleString());

async function loadLeds() {
  const r = await fetch("/api/leds");
  if (!r.ok) return;
  led.setup = await r.json();
  renderLeds();
  clearInterval(led.timer);
  led.timer = setInterval(pollLeds, 100);
}
function stopLeds() {
  clearInterval(led.timer);
  led.timer = null;
  if (led.test) ledTest("stop");
}
function renderLeds() {
  const d = led.setup, s = d.settings;
  if (!$("#led-strip").children.length)
    $("#led-strip").innerHTML = Array.from({ length: d.n_leds }, (_, i) => `<span style="--c:var(${LED_COLOUR(i)})"></span>`).join("");
  $("#led-port").textContent = d.port ? "Moza base found on USB" : "no Moza base on USB";
  document.querySelectorAll("#led-driver button").forEach((b) => b.classList.toggle("on", b.dataset.d === s.driver));
  const stale = d.monocoque_running && ((s.driver === "monocoque") !== d.monocoque_entry || s.driver === "simdash");
  $("#led-driver-hint").innerHTML = {
    simdash: "simdash lights the LEDs from the live RPM, with the shift light below. monocoque's Moza device is taken out of its config, so only one program writes to the port.",
    monocoque: "monocoque lights the LEDs in its fixed steps: 10 % to 92 % of max RPM, flashing from 94 %. The shift light settings below don't apply.",
    off: "Nothing lights the LEDs during races; the rim shows its idle animation. The tests below still work.",
  }[s.driver] + (stale ? ` <span class="warn">monocoque is running with its previous config: restart it (System tab) or start a new session.</span>` : "");
  for (const [id, v] of [["shift", Math.round(s.shift * 100)], ["start", Math.round(s.start * 100)], ["blink", s.blink_hz]]) {
    if (document.activeElement !== $(`#led-${id}`)) $(`#led-${id}`).value = v;
    if (document.activeElement !== $(`#led-${id}-n`)) $(`#led-${id}-n`).value = v;
  }
  $("#led-topgear").checked = !!s.top_gear_blink;
  $("#led-invert").checked = !!s.axis_inverts[s.axis];
  led.axesKey = "";  // names or reversal may have changed: rebuild the pedal rows
  const cars = Object.entries(s.cars).sort(([a], [b]) => a.localeCompare(b));
  $("#led-cars").innerHTML = cars.map(([c, v]) => `<tr><td>${esc(c)}</td><td class="r num">${fmtRpm(v.shift_rpm)}</td>
    <td class="r"><button data-del="${esc(c)}" title="Back to the percentage">Remove</button></td></tr>`).join("")
    || `<tr><td colspan="3" class="empty">No car of its own: every car shifts at ${Math.round(s.shift * 100)} % of its max RPM.</td></tr>`;
  shiftHint();
}
function shiftHint(max = led.liveMax) {
  const s = led.setup?.settings;
  if (!s) return;
  const ex = max || 8000, top = s.shift * ex;
  $("#led-shift-hint").textContent = `${max ? "For the car on track" : "With 8,000 max RPM"}: first LED at ${fmtRpm(s.start * top)} rpm, `
    + `all ${led.setup.n_leds} lit just before ${fmtRpm(top)} rpm, then they flash until you change gear`
    + (s.top_gear_blink ? "." : " (not in top gear).");
}
async function saveLeds(patch) {
  const r = await fetch("/api/leds", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
  if (!r.ok) { alert((await r.json()).detail || "Save failed"); return; }
  led.setup.settings = await r.json();
  renderLeds();
}
function scheduleLedSave() {
  clearTimeout(led.saveTimer);
  led.saveTimer = setTimeout(() => saveLeds({
    shift: +$("#led-shift-n").value / 100, start: +$("#led-start-n").value / 100,
    blink_hz: +$("#led-blink-n").value, top_gear_blink: $("#led-topgear").checked,
  }), 300);
}
for (const id of ["shift", "start", "blink"]) {
  $(`#led-${id}`).oninput = (e) => { $(`#led-${id}-n`).value = e.target.value; scheduleLedSave(); };
  $(`#led-${id}-n`).oninput = (e) => { $(`#led-${id}`).value = e.target.value; scheduleLedSave(); };
}
$("#led-topgear").onchange = scheduleLedSave;
$("#led-invert").onchange = (e) => {
  const s = led.setup.settings;
  saveLeds({ axis_inverts: { ...s.axis_inverts, [s.axis]: e.target.checked } });
};
$("#led-driver").onclick = async (e) => {
  const b = e.target.closest("button[data-d]");
  if (!b || b.dataset.d === led.setup?.settings.driver) return;
  const r = await fetch("/api/leds/driver", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ driver: b.dataset.d }) });
  if (!r.ok) { alert((await r.json()).detail || "Failed"); return; }
  led.setup = await r.json();
  renderLeds();
};
async function ledTest(mode) {
  await fetch("/api/leds/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) });
}
$("#led-test-pedal").onclick = () => ledTest("pedal");
$("#led-test-sweep").onclick = () => ledTest("sweep");
$("#led-test-stop").onclick = () => ledTest("stop");
$("#led-axes").onchange = (e) => { if (e.target.name === "led-axis") saveLeds({ axis: +e.target.value }); };
$("#led-car-save").onclick = () => {
  const name = $("#led-car-name").value.trim(), rpm = Math.round(+$("#led-car-rpm").value);
  if (!name || !(rpm >= 1000)) { alert("Give a car name and a shift RPM (1000 or more)."); return; }
  saveLeds({ cars: { ...led.setup.settings.cars, [name]: { shift_rpm: rpm } } });
  $("#led-car-rpm").value = "";
};
$("#led-cars").onclick = (e) => {
  const c = e.target.closest("button[data-del]")?.dataset.del;
  if (!c) return;
  const cars = { ...led.setup.settings.cars };
  delete cars[c];
  saveLeds({ cars });
};
async function pollLeds() {
  let st;
  try { st = await (await fetch("/api/leds/state")).json(); } catch { return; }
  if (currentTab !== "leds") return;
  led.test = st.test;
  [...$("#led-strip").children].forEach((el, i) => el.classList.toggle("on", !!(st.mask >> i & 1)));
  const now = $("#led-now");
  now.classList.toggle("shift", st.phase === "shift");
  const pct = (f) => `${Math.round(f * 100)} %`;
  now.textContent = st.error ? st.error
    : st.source === "game" ? `${fmtRpm(st.rpm)} rpm · shift at ${fmtRpm(st.shift_rpm)} · ${st.car}${st.phase === "shift" ? " · SHIFT" : ""}`
    : st.source === "pedal" ? `pedal ${pct(st.fraction)}${st.phase === "shift" ? " · SHIFT" : ""}`
    : st.source === "sweep" ? `sweep ${pct(st.fraction)}${st.phase === "shift" ? " · SHIFT" : ""}`
    : led.setup?.settings.driver === "simdash" ? "waiting for the car on track" : "idle";
  $("#led-test-pedal").classList.toggle("on", st.test === "pedal");
  $("#led-test-sweep").classList.toggle("on", st.test === "sweep");
  $("#led-test-hint").textContent = st.test ? `Testing: the LEDs follow the ${st.test === "pedal" ? "pedal" : "sweep"}. It stops by itself after 2 minutes or when you leave this tab.` : "";
  // pedal axes: rows built once, bars updated
  const axes = st.axes || [], key = axes.map((a) => a.code).join(",");
  if (key !== led.axesKey) {
    led.axesKey = key;
    const lab = led.setup?.settings.axis_labels || {}, inv = led.setup?.settings.axis_inverts || {};
    $("#led-axes").innerHTML = axes.length ? axes.map((a) => `<input type="radio" name="led-axis" value="${a.code}" id="led-ax-${a.code}">
      <label for="led-ax-${a.code}">${lab[a.code] ? `<b>${esc(lab[a.code])}</b> <span class='muted'>(${esc(a.name)}${inv[a.code] ? ", reversed" : ""})</span>`
        : `<span class='muted'>${esc(a.name)}${a.code === 0 ? " (steering)" : ""}</span>`}</label>
      <div class="track"><div id="led-axv-${a.code}"></div></div><span class="v" id="led-axn-${a.code}"></span>`).join("")
      : `<span class="muted" style="grid-column:1/-1">No Moza input device found.</span>`;
  }
  for (const a of axes) {
    const v = led.setup?.settings.axis_inverts?.[a.code] ? 1 - a.value : a.value;
    $(`#led-axv-${a.code}`).style.width = `${(v * 100).toFixed(1)}%`;
    $(`#led-axn-${a.code}`).textContent = pct(v);
    const r = $(`#led-ax-${a.code}`);
    if (r) r.checked = led.setup?.settings.axis === a.code;
  }
  // per-car shift point: offer the car on track
  if (st.live_car) {
    if (!$("#led-car-name").value && document.activeElement !== $("#led-car-name")) $("#led-car-name").value = st.live_car;
    const names = [st.live_car, ...Object.keys(led.setup?.settings.cars || {})];
    const html = [...new Set(names)].map((n) => `<option value="${esc(n)}">`).join("");
    if ($("#led-car-list").innerHTML !== html) $("#led-car-list").innerHTML = html;
  }
  if (st.live_max_rpm !== led.liveMax) { led.liveMax = st.live_max_rpm || null; shiftHint(); }
}

// ---------- storage: old days slimmed and compressed ----------
const fmtMB = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(b < 1e7 ? 1 : 0)} MB`);
const ST_STATE = { full: "full detail", slim: "slimmed", compressed: "slimmed + compressed" };
async function loadStorage() {
  let d;
  try { d = await (await fetch("/api/storage")).json(); } catch { return; }
  renderStorage(d);
}
function renderStorage(d) {
  const total = d.days.reduce((a, x) => a + x.bytes, 0);
  $("#st-total").textContent = `${fmtMB(total)} in ${d.days.length} days`;
  const st = d.stats;
  if (st) {
    const tile = (label, value, sub = "") => `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
    const km = (v) => Math.round(v).toLocaleString();
    const compressed = d.days.filter((x) => x.state === "compressed").length;
    $("#st-tiles").innerHTML = [
      tile("Days", st.days, compressed ? `${compressed} compressed` : ""),
      tile("Size", fmtMB(total), d.cache_bytes ? `+ ${fmtMB(d.cache_bytes)} cache` : ""),
      tile("Sessions", st.sessions),
      tile("Laps", st.laps.toLocaleString(), "completed"),
      tile("Distance", `${km(st.km)} km`, "completed laps"),
      tile("Driving", fmtDur(st.driving_s)),
      tile("Cars", Object.keys(st.cars).length),
      tile("Tracks", Object.keys(st.tracks).length),
    ].join("");
    const rows = (o, cls) => Object.entries(o).sort((a, b) => b[1].km - a[1].km).map(([n, v]) => `<tr><td>${esc(n)}${cls && v.class ? ` <span class="muted">${esc(v.class)}</span>` : ""}</td>
      <td class="r">${v.sessions}</td><td class="r">${v.laps}</td><td class="r num">${km(v.km)}</td></tr>`).join("");
    $("#st-cars tbody").innerHTML = rows(st.cars, true);
    $("#st-tracks tbody").innerHTML = rows(st.tracks, false);
  }
  $("#st-rule").textContent = `Days older than ${d.older_than} days are slimmed and compressed automatically (checked every hour, never during a session). `
    + `Good laps keep every detail: each session's best lap, laps within ${Math.round((d.good_ratio - 1) * 100)} % of it, and every lap that was your best ever on its track with its car. `
    + `Other laps keep 2 samples per second (speed, pedals, gear, line) without the per-wheel data; other cars keep their results and 1 position per second. `
    + `A compressed day unpacks by itself when you open one of its sessions.`;
  $("#st-days tbody").innerHTML = d.days.map((x) => `<tr><td>${x.day}</td>
    <td>${ST_STATE[x.state] || x.state}${x.state === "full" && x.eligible ? ' <span class="muted">(due)</span>' : ""}</td>
    <td class="r num">${fmtMB(x.bytes)}</td></tr>`).join("");
  $("#st-run").disabled = d.busy || d.recording || !d.days.some((x) => x.eligible && x.state !== "compressed");
  $("#st-msg").textContent = d.busy ? "Working…" : d.recording ? "Waiting: a session is being recorded." : "";
}
$("#st-run").onclick = async () => {
  $("#st-run").disabled = true;
  $("#st-msg").textContent = "Working… (a few seconds per day)";
  const r = await fetch("/api/storage/run", { method: "POST" });
  const d = await r.json();
  if (!r.ok) { $("#st-msg").textContent = d.detail || "Failed"; loadStorage(); return; }
  renderStorage(d);
  const saved = d.done.reduce((a, x) => a + x.before - x.after, 0);
  $("#st-msg").textContent = d.done.length ? `${d.done.length} day${d.done.length > 1 ? "s" : ""} done, ${fmtMB(saved)} freed.` : "Nothing to do.";
};

// ---------- system: telemetry chain status and restarts ----------
const sys = { data: null, busy: null, timer: null };
const fmtAge = (since, now) => {
  if (!since) return "–";
  const s = Math.max(0, Math.round(now - since));
  return s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min` : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
};

// [state, short text, hint when broken] for each link, from the server's raw status
function chainStates(d) {
  const on = d.ams2.running;
  // the file outlives AMS2, with the last state in it
  const shmLive = on && ["recording", "paused", "idle"].includes(d.shm.status);
  const st = {};
  st.ams2 = on ? ["ok", d.busy.ams2 || "running"] : [d.busy.ams2 ? "warn" : "off", d.busy.ams2 || "not running"];
  st.bridge = !on ? ["off", "–"] : d.bridge.running ? ["ok", "running"]
    : ["bad", "not running", "The bridge is not running: Restart AMS2 (simd starts it when AMS2 launches)."];
  st.shm = shmLive ? ["ok", { recording: "on track", paused: "paused", idle: "in menus" }[d.shm.status]]
    : !on ? ["off", d.shm.exists ? "left over" : "none"]
    : ["bad", "no data", "No AMS2 data: AMS2 Options > System > Shared Memory must be Project CARS 2, then Restart AMS2."];
  st.simd = d.simd.state !== "active" ? ["bad", d.simd.state, "simd is not running: Restart simd."]
    : d.simd.check === "live" ? ["ok", "live"]
    : on && shmLive ? ["bad", "sees no game", "simd does not see AMS2 (it stops finding it after a few restarts): Restart AMS2, or Restart simd."]
    : ["ok", "waiting for a game"];
  st.monocoque = d.monocoque.stale ? ["bad", "reads an old simd", "monocoque was started before simd was restarted and reads data that no longer changes (no vibration from it): Restart monocoque."]
    : d.monocoque.running ? ["ok", "running"]
    : on ? ["warn", "not running", "monocoque is not running, so no vibration: Restart monocoque."] : ["off", "not running"];
  st.card = d.card.connected ? ["ok", "connected"] : ["bad", "not connected", "The shaker sound card is unplugged: plug the USB 7.1 card in."];
  st.crewchief = d.crewchief.running ? (d.crewchief.listening ? ["ok", "listening"] : ["warn", "starting"])
    : on ? ["warn", "not running"] : ["off", "not running"];
  return st;
}

function dl(rows) {
  return rows.filter(Boolean).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
}
const stDot = ([s]) => `<span class="dot st-${s}"></span>`;

function renderSystem() {
  const d = sys.data;
  if (!d) return;
  const st = chainStates(d);
  const chain = [["ams2", "AMS2"], ["bridge", "Bridge"], ["shm", "$pcars2$"], ["simd", "simd"], ["monocoque", "monocoque"], ["card", "Shaker card"]];
  $("#sys-chain").innerHTML = chain.map(([k, n], i) =>
    `${i ? '<span class="arrow">→</span>' : ""}<div class="node st-${st[k][0]}"><div class="n">${stDot(st[k])}${n}</div><div class="s">${esc(st[k][1])}</div></div>`).join("");
  const hints = chain.map(([k]) => st[k][2]).filter(Boolean);
  $("#sys-hint").innerHTML = hints.length ? `<span class="bad">${esc(hints[0])}</span>` : d.ams2.running ? "All links up." : "AMS2 is not running. Start session launches everything.";
  const worst = Object.values(st).some((x) => x[0] === "bad") ? "bad" : Object.values(st).some((x) => x[0] === "warn") ? "warn" : "";
  $("#sys-dot").className = "nav-dot " + worst;
  $("#sys-updated").textContent = "updated " + new Date(d.now * 1000).toLocaleTimeString();

  const card = (c, s) => document.querySelector(`.sys-card[data-c="${c}"] h3`).className = "st-" + s[0];
  card("ams2", st.ams2);
  card("simd", st.simd);
  card("monocoque", st.monocoque);
  $("#sys-ams2-state").textContent = st.ams2[1];
  $("#sys-simd-state").textContent = st.simd[1];
  $("#sys-mono-state").textContent = st.monocoque[1];

  $("#sys-ams2").innerHTML = dl([
    ["Process", d.ams2.pid ? `pid ${d.ams2.pid}, up ${fmtAge(d.ams2.since, d.now)}` : "–"],
    ["Bridge", `${stDot(st.bridge)} ${st.bridge[1]}`],
    ["Shared memory", `${stDot(st.shm)} ${esc(st.shm[1])}`],
    d.shm.track && ["Session", `${esc(d.shm.track)} · ${esc(d.shm.car || "")}`],
  ]);
  const sa = d.simd.simapi || {};
  $("#sys-simd").innerHTML = dl([
    ["Service", `${esc(d.simd.state)}${d.simd.pid ? `, pid ${d.simd.pid}, up ${fmtAge(d.simd.since, d.now)}` : ""}`],
    ["Telemetry", `${stDot(st.simd)} ${esc(d.simd.check)}${sa["data age"] && sa["data age"] !== "-1 ms" ? ` (data age ${esc(sa["data age"])})` : ""}`],
    sa.car && ["Car", esc(sa.car)],
    sa.track && ["Track", esc(sa.track)],
  ]);
  $("#sys-mono").innerHTML = dl([
    ["Process", d.monocoque.pid ? `pid ${d.monocoque.pid}, up ${fmtAge(d.monocoque.since, d.now)}` : "–"],
    ["Last log", `<span class="muted">${esc((d.monocoque.log || []).slice(-1)[0] || "–")}</span>`],
  ]);
  $("#sys-others").innerHTML = dl([
    ["CrewChief", `${stDot(st.crewchief)} ${st.crewchief[1]}${d.crewchief.running ? (d.crewchief.listening ? " (UDP 5606)" : "") : ""}`],
    ["Shaker card", `${stDot(st.card)} ${esc(d.card.description || d.card.devid || "none chosen")}`],
    ["start-race", d.session ? '<span class="dot st-ok"></span> session running' : '<span class="dot st-off"></span> no session'],
  ]);
  $("#sys-actions").innerHTML = (d.actions || []).map((a) =>
    `<li><span class="t">${new Date(a.t * 1000).toLocaleTimeString()}</span>${esc(a.text)}</li>`).join("") || '<li class="muted">None yet</li>';

  // buttons that make no sense in the current state
  const can = {
    "ams2/start": !d.ams2.running && !d.session && !d.busy.ams2,
    "ams2/restart": !d.busy.ams2,
    "ams2/quit": d.ams2.running && !d.busy.ams2,
    "simd/restart": true,
    "monocoque/restart": true,
    "monocoque/stop": d.monocoque.running,
  };
  document.querySelectorAll("#tab-system [data-act]").forEach((b) => {
    b.disabled = sys.busy === b.dataset.act || !can[b.dataset.act];
    b.classList.toggle("busy", sys.busy === b.dataset.act || (b.dataset.act.startsWith("ams2/") && !!d.busy.ams2));
  });
}

async function loadSystem() {
  loadStorage();
  try {
    const r = await fetch("/api/system");
    if (r.ok) sys.data = await r.json();
  } catch { /* simdash restarting */ }
  renderSystem();
}

async function loadSystemLog() {
  const r = await fetch(`/api/system/log/${$("#sys-log").value}`);
  if (!r.ok) return;
  const box = $("#sys-logbox");
  const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  box.textContent = (await r.json()).lines.join("\n") || "(empty)";
  if (atEnd) box.scrollTop = box.scrollHeight;
}

document.querySelectorAll("#tab-system [data-act]").forEach((b) => (b.onclick = async () => {
  if (b.dataset.confirm && sys.data?.ams2.running && !confirm(b.dataset.confirm)) return;
  sys.busy = b.dataset.act;
  renderSystem();
  try {
    const r = await fetch(`/api/system/${b.dataset.act}`, { method: "POST" });
    const d = await r.json();
    if (r.ok) sys.data = d;
    else alert(d.detail || "Failed");
  } finally {
    sys.busy = null;
    renderSystem();
    loadSystemLog();
  }
}));
$("#sys-log").onchange = loadSystemLog;

// refresh every 2 s on the System tab, every 15 s elsewhere for the tab's warning dot
setInterval(() => {
  if (currentTab === "system") {
    loadSystem();
    loadSystemLog();
  }
}, 2000);
setInterval(() => { if (currentTab !== "system") loadSystem(); }, 15000);
loadSystem();

// ---------- start ----------
loadShakerCars();
connect();
requestAnimationFrame(frame);
if (location.hash.startsWith("#session=")) showTab("history");
else if (location.hash.startsWith("#progress")) showTab("progress");
else if (["#genie", "#shakers", "#system", "#leds"].includes(location.hash)) showTab(location.hash.slice(1));
// in-page links such as "Progress on this track"
window.addEventListener("hashchange", () => { if (location.hash.startsWith("#progress")) showTab("progress"); });
