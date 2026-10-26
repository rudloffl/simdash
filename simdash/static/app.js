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
  else if (location.hash) history.replaceState(null, "", location.pathname);
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
}

function drawMap(canvas, outline, cars, startLine) {
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
  for (const [X, Y] of boundPts) { x0 = Math.min(x0, X); x1 = Math.max(x1, X); y0 = Math.min(y0, Y); y1 = Math.max(y1, Y); }
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
    if (m.type === "backlog") live.trace = m.trace;
    else {
      live.data = m.live;
      for (const r of m.trace) live.trace.push(r);
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
  drawMap($("#map"), live.outline, d.cars, live.outlineSrc === "lap");

  if (performance.now() - live.lastTable > 250) {
    live.lastTable = performance.now();
    $("#standings tbody").innerHTML = d.cars.map((r) => `<tr class="${r.player ? "player" : ""}">
      <td class="r">${r.pos || ""}</td><td>${esc(r.name)}</td><td class="r">${r.lap}</td>
      <td class="r">${r.player ? "" : (r.gap > 0 ? "+" : "") + r.gap.toFixed(1)}</td>
      <td class="r">${fmtLap(r.last)}</td><td class="r">${fmtLap(r.best)}</td>
      <td class="muted">${esc(r.pit || (r.state !== "racing" && r.state !== "not started" ? r.state : ""))}</td></tr>`).join("");
  }
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
const hist = { sessions: [], ref: null, detail: null, lap: null, lapData: null, cmp: null, cmpData: null,
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
  history.replaceState(null, "", `#session=${ref}`);
  renderSessions();
  const s = d.session;
  $("#session-detail").hidden = false;
  $("#d-title").textContent = `${s.session_type} · ${s.track} ${s.variation}`;
  $("#d-sub").textContent = `${s.car} (${s.car_class}) · ${new Date(s.started_at * 1000).toLocaleString()}${d.live ? " · recording now" : ""}`;
  $("#d-csv").href = `/api/sessions/${ref}/samples.csv`;
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
  $("#compare").innerHTML = `<option value="">none</option>` + d.laps.map((l) => `<option value="${l.lap}">Lap ${l.lap} · ${fmtLap(l.lap_time)}${l.valid ? "" : " (invalid)"}</option>`).join("");
  hist.cmp = null;
  hist.cmpData = null;
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
  }

  $("#results-table tbody").innerHTML = d.participants.map((p) => `<tr class="${p.is_player ? "player" : ""}">
    <td class="r">${p.pos ?? ""}</td><td>${esc(p.name)}</td><td>${esc(p.car)}</td><td class="r">${p.laps_completed ?? ""}</td>
    <td class="r">${fmtLap(p.best_lap)}</td><td class="muted">${esc(p.race_state)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="empty">No participant data.</td></tr>`;
  initReplay(d);
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
$("#compare").onchange = async (e) => {
  hist.cmp = e.target.value ? +e.target.value : null;
  hist.cmpData = hist.cmp ? await getLap(hist.cmp) : null;
  drawTraces();
  drawLapMap();
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

async function fetchLap(n) {
  return (await fetch(`/api/sessions/${hist.ref}/lap/${n}`)).json();
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

function lapSwatch(n) {
  return css(n === hist.lap ? "--s-speed" : n === hist.cmp ? "--s-spin" : "--muted");
}

function renderMapLaps() {
  const d = hist.detail, byLap = $("#map-color").value === "lap";
  const all = d.laps.every((l) => hist.shown.has(l.lap)), none = !d.laps.some((l) => hist.shown.has(l.lap));
  $("#map-laps").innerHTML = `<label class="all"><input type="checkbox" data-lap="all" ${all ? "checked" : ""}> All laps</label>`
    + d.laps.map((l) => `<label class="${l.lap === hist.lap ? "sel" : ""}" title="${l.lap === hist.lap ? "analysed lap" : l.lap === hist.cmp ? "compare lap" : ""}">
      <input type="checkbox" data-lap="${l.lap}" ${hist.shown.has(l.lap) ? "checked" : ""}>
      ${byLap ? `<span class="sw" style="background:${lapSwatch(l.lap)}"></span>` : ""}L${l.lap}
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
  }
  const traces = laps.map((n, k) => {
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
    } else {
      color = lapSwatch(n);
      hover = L.speed.map((v) => `${Math.round(v)} km/h`);
    }
    const marker = mode === "lap"
      ? { size: rank(n) ? 5 : 4, color, opacity: rank(n) ? 1 : 0.5 }
      : { size: 5, color, cmin, cmax, colorscale: scale, showscale: k === laps.length - 1,
          colorbar: { title: { text: title, side: "right" }, thickness: 10, outlinewidth: 0, tickfont: { color: css("--muted") } } };
    return { x: xy.map((p) => p[0]), y: xy.map((p) => -p[1]), mode: "markers", type: "scattergl", marker,
      text: L.lap_dist.map((dd, i) => `lap ${n} · ${Math.round(dd)} m · ${hover[i]} · gear ${L.gear[i]}`),
      hovertemplate: "%{text}<extra></extra>" };
  });
  traces.push({ x: [], y: [], mode: "markers", type: "scatter", hoverinfo: "skip",
    marker: { size: 14, color: css("--s-speed"), line: { color: css("--surface"), width: 2 } } });
  hist.mapMarker = traces.length - 1;
  hist.mapLaps = laps;
  // keep the zoom while laps are toggled or recoloured; a new session or orientation resets it
  const rev = `${hist.ref}:${mapMirror}:${mapRot}`;
  if (hist.mapRev && rev !== hist.mapRev && hist.zoomDist) { hist.zoomDist = null; drawTraces(); }
  const first = !hist.mapRev;
  hist.mapRev = rev;
  Plotly.react("lap-map", traces, plotLayout({
    showlegend: false, margin: { l: 8, r: 8, t: 8, b: 8 }, uirevision: rev,
    xaxis: { visible: false }, yaxis: { visible: false, scaleanchor: "x", scaleratio: 1 },
  }), { displaylogo: false, responsive: true, scrollZoom: true });
  if (first) {
    const el = $("#lap-map");
    el.on("plotly_relayout", onMapZoom);
    el.on("plotly_hover", onMapHover);
    el.on("plotly_unhover", () => Plotly.relayout("lap-traces", { shapes: [] }));
  }
}

// red line in the lap traces at the lap distance of the hovered map point
function onMapHover(ev) {
  const p = ev.points[0], n = hist.mapLaps[p.curveNumber];
  const L = n != null && hist.loaded[n];
  if (!L || !$("#lap-traces").data) return;
  const dd = L.lap_dist[p.pointIndex];
  Plotly.relayout("lap-traces", { shapes: [{ type: "line", xref: "x", yref: "paper", x0: dd, x1: dd, y0: 0, y1: 1,
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
  ];
  const worst = (L, f) => L.ts.map((_, i) => {
    const w = [L.slip_fl[i], L.slip_fr[i], L.slip_rl[i], L.slip_rr[i]].filter((v) => v != null);
    return w.length ? f(...w, 0) : null;
  });
  for (const L of [A, B]) if (L && !L.spin) { L.spin = worst(L, Math.max); L.lock = worst(L, Math.min); }
  if (B) rows.unshift({ key: "delta", title: `Δ vs lap ${hist.cmp} s`, series: [["delta", "Δ time", "--s-angle", 1]] });
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
  rows.forEach((row, k) => {
    const ax = k === 0 ? "" : String(k + 1);
    const top = 1 - k * (hEach + gap);
    layout["yaxis" + ax] = { ...axisStyle(), domain: [top - hEach, top], title: { text: row.title, font: { size: 10, color: css("--muted") } }, fixedrange: true,
      ...(row.key === "slip" ? { range: [-30, 30] } : {}),
      ...(z && ["speed", "steer", "delta"].includes(row.key) ? yFit(row) : {}) };
    layout["xaxis" + ax] = { ...axisStyle(), anchor: "y" + ax, matches: k ? "x" : undefined, showticklabels: k === n - 1,
      ...(k === 0 && z ? { range: z } : {}),
      title: k === n - 1 ? { text: "lap distance (m)", font: { size: 10, color: css("--muted") } } : undefined };
    for (const [col, name, c, mul] of row.series) {
      for (const [L, label, dash] of [[A, `lap ${hist.lap}`, "solid"], [row.key === "delta" ? null : B, `lap ${hist.cmp}`, "dot"]]) {
        if (!L) continue;
        traces.push({
          x: L.lap_dist, y: L[col].map((v) => (v == null ? null : v * mul)), xaxis: "x" + ax, yaxis: "y" + ax,
          type: "scattergl", mode: "lines", name: `${name} · ${label}`, legendgroup: label,
          line: { color: css(c), width: 2, dash, shape: row.key === "gear" ? "hv" : "linear" },
          hovertemplate: `%{y:.${row.key === "delta" ? 3 : 0}f}`,
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

// ---------- race replay ----------
const replay = { outline: null, cars: null, timer: null, pending: null, t0: 0, t1: 0, startTs: 0 };

async function initReplay(d) {
  clearInterval(replay.timer);
  replay.timer = null;
  $("#replay-play").textContent = "Play";
  replay.cars = null;
  if (!d.t0) { $("#replay-card").hidden = true; return; }
  const path = await (await fetch(`/api/sessions/${hist.ref}/path`)).json();
  const out = [];
  for (let i = 0; i < path.x.length; i++) {
    const q = out[out.length - 1];
    if (!q || Math.hypot(path.x[i] - q[0], path.z[i] - q[1]) > 3) out.push([path.x[i], path.z[i]]);
  }
  replay.outline = out;
  replay.t0 = d.t0;
  replay.t1 = d.t1;
  const sl = $("#replay-slider");
  sl.min = 0;
  sl.max = Math.max(1, d.t1 - d.t0);
  sl.value = 0;
  $("#replay-card").hidden = false;
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
    drawReplay();
  }
  replay.busy = false;
}
function drawReplay() { drawMap($("#replay-map"), replay.outline, replay.cars || [], false); }
$("#replay-slider").oninput = (e) => seekReplay(+e.target.value);
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

// ---------- start ----------
connect();
requestAnimationFrame(frame);
if (location.hash.startsWith("#session=")) showTab("history");
