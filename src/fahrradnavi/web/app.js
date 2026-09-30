"use strict";
// FahrradNavi – Weboberfläche. Leaflet wird lokal ausgeliefert (kein CDN).

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const COLORS = { own: "#1f9d55", calm: "#e0a100", road: "#d63c3c" };
const MAX_AREAS = 20, MAX_FAVS = 10, MAX_FAV_POINTS = 20000, STORE_KEY = "fn_overlays_v1";

let cfg = null, map;
const routeLayer = L.layerGroup(), baseLayer = L.layerGroup(), altLayer = L.layerGroup();
let areaLayer = null, favLayer = null;
let points = [];              // [{lat, lon, label}] (Lücken = null)
let markers = [];
let tab = "route";            // "route" | "trip"
let tripArmed = false;        // Rundreise wurde angefordert -> Änderungen rechnen neu
let mode = "route";           // "route" | "circle" | "polygon"
let draw = null;
let timer = null, reqId = 0, lastRequest = null;
let routes = [], selected = 0;
let curCoords = [], curLegEnds = [];
let areas = [], favs = [];    // Nutzer-Overlays (im Browser gespeichert)
let activeSlot = -1;          // zuletzt angeklicktes leeres Eingabefeld (wird per Karte/Suche gefüllt)
let ghost = null, ghostLeg = 0, ghostIdx = 0, ghostTimer = null, dragging = false;

const fmtKm = (m) => (m / 1000).toLocaleString("de-DE", { maximumFractionDigits: 1, minimumFractionDigits: 1 }) + " km";
const fmtTime = (s) => { const m = Math.round(s / 60); return m >= 60 ? Math.floor(m / 60) + " h " + String(m % 60).padStart(2, "0") + " min" : m + " min"; };
const uid = () => Math.random().toString(36).slice(2, 9);

function toast(text, ms = 3500) {
  const t = $("toast");
  t.textContent = text; t.style.display = "block";
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.style.display = "none"), ms);
}

// Regler ausblenden (Element bleibt erhalten, damit der Zugriff auf .value nicht fehlschlägt) und Hinweis zeigen
function disableSlider(boxId, text) {
  const box = $(boxId);
  [...box.children].forEach((c) => (c.style.display = "none"));
  const note = document.createElement("div");
  note.className = "note warn"; note.textContent = text;
  box.appendChild(note);
}

async function api(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (r.status === 401) { location.href = "/login"; throw new Error("Nicht angemeldet"); }
  return r;
}

// ---- Speicher (nur dieser Browser) -----------------------------------------------------------
function loadStore() {
  try {
    const s = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    areas = Array.isArray(s.areas) ? s.areas : []; favs = Array.isArray(s.favs) ? s.favs : [];
  } catch (e) { areas = []; favs = []; }
}
function saveStore() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify({ areas, favs })); }
  catch (e) { toast("Speichern im Browser nicht möglich (Speicher voll oder gesperrt) – Bereiche gelten nur bis zum Neuladen."); }
}

async function init() {
  const cr = await fetch("/api/config");
  if (cr.status === 401) { location.href = "/login"; return; }
  cfg = await cr.json();
  if (cfg.auth) $("logout").style.display = "inline-block";
  const [minLon, minLat, maxLon, maxLat] = cfg.bbox;
  map = L.map("map", { zoomControl: true }).fitBounds([[minLat, minLon], [maxLat, maxLon]]);
  L.tileLayer(cfg.tiles.url, { attribution: cfg.tiles.attribution, maxZoom: 19 }).addTo(map);
  map.setMaxBounds([[minLat - 0.5, minLon - 0.5], [maxLat + 0.5, maxLon + 0.5]]);
  map.createPane("areas"); map.getPane("areas").style.zIndex = 350;
  areaLayer = L.layerGroup().addTo(map); favLayer = L.layerGroup().addTo(map);
  routeLayer.addTo(map); baseLayer.addTo(map); altLayer.addTo(map);
  for (const p of cfg.profiles) $("profile").add(new Option(p.label, p.id));
  $("profile").value = cfg.default_profile;
  if (!cfg.has_center) disableSlider("centerBox", "Keine Geschäfts-/Gastronomie-Daten im Graphen – „Innenstadt meiden“ ist nicht verfügbar (Graph neu bauen, siehe README).");
  if (!cfg.has_urban) disableSlider("urbanBox", "Keine Siedlungsflächen im Graphen – „Bebauung meiden“ ist nicht verfügbar (Graph neu bauen, siehe README).");
  if (!cfg.has_elevation) disableSlider("hillsBox", "Keine Höhendaten im Graphen – Steigungen werden nicht berücksichtigt (siehe README: fahrradnavi dem).");
  map.on("click", onMapClick);
  map.on("mousemove", onMapMove);
  map.on("dblclick", () => { if (mode === "polygon") finishPolygon(); });
  loadStore(); renderOverlays();
  renderPoints(); updateLabels();
  const h = location.hash.match(/^#r=(.+)$/);
  if (h) restoreFromHash(h[1]);
}

// ---- Punkte --------------------------------------------------------------------------------------
function filled() { return points.filter(Boolean); }
function onMapClick(e) {
  if (mode === "circle") return circleClick(e);
  if (mode === "polygon") return polyClick(e);
  addPoint(e.latlng.lat, e.latlng.lng);
}
function addPoint(lat, lon, label) {
  const p = { lat, lon, label: label || `${lat.toFixed(5)}, ${lon.toFixed(5)}` };
  const slots = tab === "trip" ? Math.max(1, points.length) : Math.max(2, points.length);
  let i = activeSlot >= 0 && activeSlot < slots && !points[activeSlot] ? activeSlot : -1;
  for (let k = 0; i < 0 && k < slots; k++) if (!points[k]) i = k;
  if (i < 0) { if (points.length >= 12) { toast("Höchstens 12 Punkte."); return; } i = points.length; }
  points[i] = p;
  activeSlot = -1;
  renderPoints();
  scheduleRoute();
  if (tab === "trip" && !tripArmed && i > 0) toast("Station hinzugefügt – „Rundreise berechnen“ starten.");
}
// Rundreise: leeres Stations-Feld am Ende anhängen
function addStationSlot() {
  if (!points.length) points.push(null);
  if (points.length >= 12) { toast("Höchstens 11 Stationen."); return; }
  points.push(null);
  activeSlot = points.length - 1;
  renderPoints();
  const inputs = $("points").querySelectorAll("input");
  if (inputs[activeSlot]) inputs[activeSlot].focus();
}
// Leeres Zwischenziel-Feld vor dem Ziel einfügen; es wird per Suche oder Klick auf die Karte gefüllt
function addViaSlot() {
  while (points.length < 2) points.push(null);
  if (points.length >= 12) { toast("Höchstens 12 Punkte."); return; }
  const pos = points.length - 1;
  points.splice(pos, 0, null);
  activeSlot = pos;
  renderPoints();
  const input = $("points").querySelectorAll("input")[pos];
  if (input) input.focus();
}
function removePoint(i) {
  activeSlot = -1;
  if (tab === "trip") { if (i === 0) points[0] = null; else points.splice(i, 1); }
  else if (Math.max(2, points.length) <= 2) points[i] = null; else points.splice(i, 1);
  while (tab === "route" && points.length && !points[points.length - 1] && points.length > 2) points.pop();
  renderPoints();
  scheduleRoute();
}
function renderPoints() {
  const box = $("points"); box.innerHTML = "";
  markers.forEach((m) => m.remove()); markers = [];
  const n = filled().length;
  const slots = tab === "trip" ? Math.max(1, points.length) : Math.max(2, points.length);
  for (let i = 0; i < slots; i++) {
    const p = points[i];
    const isStart = i === 0, isEnd = i === slots - 1 && tab !== "trip", isVia = !isStart && !isEnd;
    const color = isStart ? "#1f9d55" : isEnd ? "#d63c3c" : "#3b6fd4";
    const tag = isStart ? "A" : isEnd ? "B" : String(i);
    const row = document.createElement("div");
    row.className = "pt row"; row.style.marginBottom = "6px";
    row.innerHTML = `${slots > 1 ? '<button class="grip" title="Ziehen zum Umsortieren (oder Pfeiltasten ↑/↓)" aria-label="Punkt verschieben">⠿</button>' : ""}<span class="dot" style="background:${color}">${tag}</span>
      <input type="text" placeholder="${isStart ? "Start" : isEnd ? "Ziel" : tab === "trip" ? "Station" : "Zwischenziel"} suchen …" autocomplete="off">
      ${p || isVia ? '<button class="x" title="Entfernen">×</button>' : ""}`;
    const input = row.querySelector("input");
    input.value = p ? p.label : "";
    input.addEventListener("focus", () => {
      activeSlot = points[i] ? -1 : i;
      $("points").querySelectorAll(".pt").forEach((r, k) => r.classList.toggle("active", k === activeSlot));
    });
    if (!p && i === activeSlot) row.classList.add("active");
    setupSearch(row, input, i);
    const x = row.querySelector(".x");
    if (x) x.onclick = () => removePoint(i);
    const grip = row.querySelector(".grip");
    if (grip) setupReorder(grip, row, i, slots);
    box.appendChild(row);
    if (p) {
      const icon = L.divIcon({ className: "", html: `<div style="background:${color};color:#fff;width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-weight:700;border:2px solid #fff;box-shadow:0 1px 5px rgba(0,0,0,.4)">${tag}</div>`, iconSize: [26, 26], iconAnchor: [13, 13] });
      const m = L.marker([p.lat, p.lon], { icon, draggable: true }).addTo(map);
      m.on("dragend", () => { const ll = m.getLatLng(); p.lat = ll.lat; p.lon = ll.lng; p.label = `${ll.lat.toFixed(5)}, ${ll.lng.toFixed(5)}`; renderPoints(); scheduleRoute(); });
      markers.push(m);
    }
  }
  const emptyVia = tab === "route" && points.some((q, k) => !q && k > 0 && k < slots - 1);
  if (emptyVia) $("clickhint").textContent = "Leeres Zwischenziel: oben einen Ort suchen oder auf die Karte klicken.";
  if (tab === "trip") {
    const st = filled().length - (points[0] ? 1 : 0);
    $("tripHeading").disabled = st > 0;
    $("tripHeadingNote").style.display = st > 0 ? "block" : "none";
    $("clickhint").textContent = !points[0] ? "Startpunkt auf der Karte anklicken oder oben suchen."
      : "Weitere Klicks auf die Karte fügen Stationen hinzu, die die Rundreise anfährt (Reihenfolge wie in der Liste).";
    $("clickhint").style.display = "block";
    return;
  }
  $("clickhint").style.display = (n >= 2 && !emptyVia) ? "none" : "block";
}
// ---- Reihenfolge per Ziehen (Maus und Touch über Pointer-Events) oder Pfeiltasten ----------------
function movePoint(from, to, slots) {
  if (from === to || to < 0 || to >= slots) return false;
  while (points.length < slots) points.push(null);
  const [p] = points.splice(from, 1);
  points.splice(to, 0, p);
  activeSlot = -1;
  renderPoints();
  scheduleRoute();
  return true;
}
function setupReorder(grip, row, i, slots) {
  grip.addEventListener("keydown", (e) => {
    const to = e.key === "ArrowUp" ? i - 1 : e.key === "ArrowDown" ? i + 1 : -1;
    if (to < 0 && e.key !== "ArrowUp") return;
    e.preventDefault();
    if (movePoint(i, to, slots)) $("points").querySelectorAll(".grip")[to]?.focus();
  });
  grip.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    const rows = [...$("points").querySelectorAll(".pt")];
    const rects = rows.map((r) => r.getBoundingClientRect());
    const step = rects.length > 1 ? rects[1].top - rects[0].top : rects[0].height + 6;
    const y0 = e.clientY;
    let target = i;
    row.classList.add("dragging");
    const onMove = (ev) => {
      const dy = ev.clientY - y0;
      row.style.transform = `translateY(${dy}px)`;
      target = Math.max(0, Math.min(rows.length - 1, i + Math.round(dy / step)));
      rows.forEach((r, k) => {
        if (k === i) return;
        const shift = i < target && k > i && k <= target ? -step : i > target && k < i && k >= target ? step : 0;
        r.style.transform = shift ? `translateY(${shift}px)` : "";
      });
    };
    const onUp = () => {
      grip.removeEventListener("pointermove", onMove);
      grip.removeEventListener("pointerup", onUp);
      grip.removeEventListener("pointercancel", onUp);
      rows.forEach((r) => { r.style.transform = ""; r.classList.remove("dragging"); });
      movePoint(i, target, slots);
    };
    grip.addEventListener("pointermove", onMove);
    grip.addEventListener("pointerup", onUp);
    grip.addEventListener("pointercancel", onUp);
  });
}
function setupSearch(row, input, i) {
  let t = null, box = null;
  const close = () => { if (box) { box.remove(); box = null; } };
  input.addEventListener("input", () => {
    clearTimeout(t); close();
    const q = input.value.trim();
    if (q.length < 2) return;
    t = setTimeout(async () => {
      const c = map.getCenter();
      const r = await fetch(`/api/geocode?q=${encodeURIComponent(q)}&lat=${c.lat}&lon=${c.lng}`);
      if (r.status === 401) { location.href = "/login"; return; }
      if (!r.ok) return;
      const res = await r.json();
      close();
      if (!res.length) return;
      box = document.createElement("div"); box.className = "suggest";
      for (const s of res) {
        const d = document.createElement("div");
        d.appendChild(document.createTextNode(s.name));
        const sm = document.createElement("small"); sm.textContent = s.kind; d.appendChild(sm);
        d.onclick = () => {
          points[i] = { lat: s.lat, lon: s.lon, label: s.name };
          close(); renderPoints(); map.panTo([s.lat, s.lon]); scheduleRoute();
        };
        box.appendChild(d);
      }
      row.appendChild(box);
    }, 250);
  });
  input.addEventListener("blur", () => setTimeout(close, 200));
}
$("addVia").onclick = addViaSlot;
$("swap").onclick = () => { while (points.length < 2) points.push(null); points.reverse(); renderPoints(); scheduleRoute(); };
function clearResults() {
  routeLayer.clearLayers(); baseLayer.clearLayers(); altLayer.clearLayers(); hideGhost();
  if (map) map.closePopup();
  routes = []; curCoords = []; curLegEnds = []; lastRequest = null;
  $("alts").style.display = "none"; $("stats").style.display = "none"; $("msg").style.display = "none";
  $("toolSaveFav").disabled = true;
}
// Alles auf Anfang: Punkte, Stationen, Routen, laufende Berechnung, Zeichenmodus. Bereiche/Lieblingswege und Regler bleiben.
function resetAll() {
  clearTimeout(timer); reqId++; busy(false);
  cancelDraw();
  points = []; activeSlot = -1; tripArmed = false;
  $("loop").checked = false;
  $("tripHeading").value = "";
  renderPoints(); clearResults();
  history.replaceState(null, "", location.pathname);
  toast("Zurückgesetzt – gemiedene Bereiche, Lieblingswege und Einstellungen bleiben erhalten.");
}
$("reset").onclick = resetAll;
$("loop").onchange = () => scheduleRoute();

// ---- Reiter: Route / Rundreise -------------------------------------------------------------------
function setTab(t) {
  if (t === tab) return;
  tab = t; tripArmed = false;
  document.body.classList.toggle("trip", t === "trip");
  $("tabRoute").classList.toggle("on", t === "route");
  $("tabTrip").classList.toggle("on", t === "trip");
  $("tripBox").style.display = t === "trip" ? "flex" : "none";
  // Punkte bleiben erhalten: in der Rundreise werden Zwischenziele/Ziel zu Stationen und umgekehrt
  points = filled();
  if (t === "route") $("clickhint").textContent = "Auf die Karte klicken, um Start und Ziel zu setzen – oder oben nach Orten suchen. Route an der Linie ziehen, um Zwischenziele einzufügen.";
  renderPoints(); clearResults();
  if (t === "route") scheduleRoute();
}
$("tabRoute").onclick = () => setTab("route");
$("tabTrip").onclick = () => setTab("trip");
$("tripKm").oninput = () => { $("tripKmTxt").textContent = $("tripKm").value + " km"; scheduleRoute(); };
$("tripHeading").onchange = () => scheduleRoute();
$("addStation").onclick = addStationSlot;
$("tripGo").onclick = () => {
  if (!points[0]) { toast("Bitte zuerst den Startpunkt setzen (Karte anklicken oder suchen)."); return; }
  tripArmed = true; route();
};
$("tripKmTxt").textContent = $("tripKm").value + " km";

// ---- Einstellungen -------------------------------------------------------------------------------
function updateLabels() {
  const lv = (v, top) => (v === 0 ? "egal" : v < 1 ? "leicht" : v === 1 ? "normal" : v < 2.5 ? "stark" : top);
  $("centerTxt").textContent = lv(+$("center").value, "maximal");
  $("urbanTxt").textContent = lv(+$("urban").value, "maximal");
  $("calmTxt").textContent = lv(+$("calm").value, "maximal");
  const a = +$("avoid").value;
  $("avoidTxt").textContent = a === 0 ? "egal" : a < 1 ? "leicht" : a === 1 ? "konsequent" : "extrem";
  $("hillsTxt").textContent = lv(+$("hills").value, "sehr stark");
  $("surfaceTxt").textContent = lv(+$("surface").value, "sehr stark");
}
for (const id of ["avoid", "calm", "center", "urban", "hills", "surface", "profile"]) {
  $(id).addEventListener("input", () => { updateLabels(); scheduleRoute(); });
}

// ---- Routing -------------------------------------------------------------------------------------
function enoughPoints() { return tab === "trip" ? !!points[0] : filled().length >= 2; }
// Neu berechnen nach einer Änderung. Fehlen Punkte (kein Start bzw. weniger als Start+Ziel), werden alle Routen sofort
// entfernt und eine noch laufende Berechnung verworfen.
function scheduleRoute() {
  clearTimeout(timer);
  if (!enoughPoints()) {
    reqId++; busy(false); clearResults();
    if (tab === "route") history.replaceState(null, "", location.pathname);
    return;
  }
  if (tab === "trip" && !tripArmed) return;
  timer = setTimeout(route, 250);
}

function overlaysPayload() {
  const a = areas.filter((x) => x.active).slice(0, MAX_AREAS).map((x) => x.kind === "circle"
    ? { kind: "circle", lat: x.lat, lon: x.lon, radius_m: x.radius_m, strength: x.strength }
    : { kind: "polygon", points: x.points.map(([lat, lon]) => ({ lat, lon })), strength: x.strength });
  let budget = MAX_FAV_POINTS;
  const f = [];
  for (const x of favs.filter((y) => y.active).slice(0, MAX_FAVS)) {
    if (x.coords.length > budget) { toast(`Lieblingsweg „${x.name}“ wird nicht verwendet (zu viele Punkte insgesamt).`); continue; }
    budget -= x.coords.length;
    f.push({ coords: x.coords.map(([lat, lon]) => ({ lat: +lat.toFixed(5), lon: +lon.toFixed(5) })), strength: x.strength ?? 1 });
  }
  return { avoid_areas: a, favorites: f };
}
function requestBody(compare) {
  const base = {
    profile: $("profile").value,
    avoid_roads: +$("avoid").value, calm: +$("calm").value, urban: cfg.has_urban ? +$("urban").value : 0,
    center: cfg.has_center ? +$("center").value : 0, hills: +$("hills").value, surface: +$("surface").value,
    ...overlaysPayload(), alternatives: 3,
  };
  if (tab === "trip") {
    const h = $("tripHeading").value;
    // erster Punkt = Start, weitere = Stationen, die die Rundreise anfährt
    const pts = points[0] ? filled() : [];
    return { ...base, points: pts.map((p) => ({ lat: p.lat, lon: p.lon })), compare: false,
      roundtrip: { distance_km: +$("tripKm").value, heading: h === "" ? null : +h } };
  }
  return { ...base, points: filled().map((p) => ({ lat: p.lat, lon: p.lon })), loop: $("loop").checked, compare };
}
async function route() {
  if (!enoughPoints() || (tab === "trip" && !tripArmed)) return;
  const my = ++reqId;
  $("msg").style.display = "none";
  busy(true, tab === "trip" ? "Rundreise wird berechnet …" : "Route wird berechnet …");
  try {
    const body = requestBody(true);
    const r = await api("/api/route", body);
    if (my !== reqId) return;
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      const d = e.detail;
      throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : "Fehler " + r.status);
    }
    const d = await r.json();
    routes = d.routes || [d];
    selectRoute(d.recommended || 0, true);
    lastRequest = body;
    if (tab === "route") history.replaceState(null, "", "#r=" + btoa(unescape(encodeURIComponent(JSON.stringify(filled().map((p) => [+p.lat.toFixed(5), +p.lon.toFixed(5)]))))));
  } catch (err) {
    if (my !== reqId) return;
    clearResults();
    $("msg").textContent = err.message; $("msg").style.display = "block";
  } finally { if (my === reqId) busy(false); }
}
// Deutlich sichtbarer Hinweis während der Berechnung (Karte abgedunkelt, Zeitanzeige, Knopf gesperrt)
let busyTimer = null;
function busy(on, text) {
  const el = $("busy");
  clearInterval(busyTimer);
  document.body.style.cursor = on ? "progress" : "";
  $("tripGo").disabled = on;
  $("tripGo").textContent = on ? "Berechne …" : "Rundreise berechnen";
  clearTimeout(busy._show);
  if (!on) { el.style.display = "none"; return; }
  const t0 = Date.now();
  $("busyTxt").textContent = text;
  $("busyTime").textContent = "";
  busy._show = setTimeout(() => (el.style.display = "flex"), 400);  // schnelle Berechnungen nicht aufblitzen lassen
  busyTimer = setInterval(() => {
    const sec = Math.round((Date.now() - t0) / 1000);
    $("busyTime").textContent = sec >= 2 ? `${sec} s` + (tab === "trip" && sec >= 4 ? " – mehrere Varianten werden verglichen" : "") : "";
  }, 500);
}
function selectRoute(i, fit) {
  selected = i;
  renderAlts();
  altLayer.clearLayers();
  routes.forEach((r, j) => {
    if (j === i) return;
    const l = L.geoJSON(r.segments, { style: { color: "#3b6fd4", weight: 5, opacity: .55, lineCap: "round" } });
    l.on("click", (e) => { if (mode === "route") { L.DomEvent.stopPropagation(e); selectRoute(j, false); } });
    l.bindTooltip(`${r.label || "Alternative"} – klicken zum Auswählen`, { sticky: true });
    altLayer.addLayer(l);
  });
  show(routes[i], fit);
}
function renderAlts() {
  const box = $("alts");
  if (routes.length < 2) { box.style.display = "none"; return; }
  box.style.display = "flex"; box.innerHTML = "";
  routes.forEach((r, i) => {
    const s = r.stats;
    const b = document.createElement("button");
    b.className = "alt" + (i === selected ? " sel" : "");
    const extra = (cfg.has_urban && +$("urban").value > 0 ? " · Bebauung " + fmtKm(s.urban_m) : "") + (cfg.has_center && +$("center").value > 0 ? " · Innenstadt " + fmtKm(s.center_m) : "");
    b.innerHTML = `<b>${esc(r.label)}</b><span>${fmtKm(s.distance_m)} · Autostraße ${fmtKm(s.road_m)} · Wohnstraße ${fmtKm(s.calm_m)}${extra} · ${fmtTime(s.duration_s)}</span>`;
    b.onclick = () => selectRoute(i, false);
    box.appendChild(b);
  });
}
function show(d, fit = true) {
  routeLayer.clearLayers(); baseLayer.clearLayers(); hideGhost();
  curCoords = d.coordinates || []; curLegEnds = d.leg_ends || [];
  if (d.baseline && +$("avoid").value > 0) {
    L.geoJSON(d.baseline, { style: { color: "#7a7f8a", weight: 4, opacity: .8, dashArray: "6 8" }, interactive: false }).addTo(baseLayer);
  }
  routeLayer.addLayer(L.geoJSON(d.segments, { style: { color: "#fff", weight: 9, opacity: .9, lineCap: "round", lineJoin: "round" }, interactive: false }));
  const line = L.geoJSON(d.segments, {
    style: (f) => ({ color: COLORS[f.properties.kind] || "#333", weight: 5, opacity: 1, lineCap: "round", lineJoin: "round" }),
    onEachFeature: (f, layer) => {
      const p = f.properties;
      const kind = { own: "eigener Weg", calm: "ruhige Straße", road: "Autostraße" }[p.kind];
      layer.bindTooltip(`${esc(p.name || "(ohne Namen)")}<br>${kind} · ${esc(p.surface)} · ${Math.round(p.length_m)} m`, { sticky: true });
      layer.on("mousemove", (e) => { if (mode === "route" && tab === "route" && !dragging) showGhost(e.latlng); });
      layer.on("mouseout", () => { clearTimeout(ghostTimer); ghostTimer = setTimeout(hideGhost, 900); });
      // Nur im normalen Modus die Route bedienen; beim Zeichnen muss der Klick zur Karte durchgehen
      layer.on("click", (e) => { if (mode === "route") { L.DomEvent.stopPropagation(e); openRoutePopup(e.latlng); } });
    },
  });
  routeLayer.addLayer(line);
  if (fit) map.fitBounds(line.getBounds(), { padding: [50, 50] });
  $("toolSaveFav").disabled = false;

  const s = d.stats, tot = s.distance_m || 1;
  $("stats").style.display = "block";
  $("sDist").textContent = fmtKm(s.distance_m);
  $("sTime").textContent = fmtTime(s.duration_s);
  $("sAsc").textContent = s.has_elevation ? `↑ ${s.ascent_m} m  ↓ ${s.descent_m} m` : "–";
  $("sUnp").textContent = fmtKm(s.unpaved_m);
  $("sUrbBox").style.display = cfg.has_urban ? "" : "none";
  $("sUrb").textContent = fmtKm(s.urban_m || 0);
  $("sCenBox").style.display = cfg.has_center ? "" : "none";
  $("sCen").textContent = fmtKm(s.center_m || 0);
  $("sSig").textContent = s.signals;
  $("lOwn").textContent = fmtKm(s.own_m); $("lCalm").textContent = fmtKm(s.calm_m); $("lRoad").textContent = fmtKm(s.road_m);
  $("sBar").innerHTML = ["own", "calm", "road"].map((k) => `<div style="width:${(s[k + "_m"] / tot * 100).toFixed(1)}%;background:${COLORS[k]}"></div>`).join("");
  const dt = $("detour");
  if (s.roundtrip) {
    dt.innerHTML = `Rundreise${s.roundtrip.stations ? ` über ${s.roundtrip.stations} Station${s.roundtrip.stations > 1 ? "en" : ""}` : ""}: Wunsch <b>${fmtKm(s.roundtrip.target_m)}</b>, tatsächlich <b>${fmtKm(s.distance_m)}</b>. Überlappung (Hin-und-zurück): ${Math.round(s.roundtrip.overlap * 100)} %.` + (s.roundtrip.note ? `<br><span style="color:var(--road)">${esc(s.roundtrip.note)}</span>` : "");
    dt.style.display = "block";
  } else if (s.baseline) {
    const extra = s.detour_m;
    if (Math.abs(extra) < 50 && s.baseline.road_m - s.road_m < 50) dt.textContent = "Die kürzeste Route ist hier bereits die straßenärmste.";
    else dt.innerHTML = `Umweg gegenüber der kürzesten Route: <b>${extra >= 0 ? "+" : ""}${fmtKm(extra)}</b> – dafür <b>${fmtKm(Math.max(0, s.baseline.road_m - s.road_m))}</b> weniger auf Autostraßen (${fmtKm(s.baseline.road_m)} → ${fmtKm(s.road_m)}). Grau gestrichelt: kürzeste Route.`;
    dt.style.display = "block";
  } else dt.style.display = "none";
  if (s.road_m > 50) dt.innerHTML += `<br><span style="color:var(--road)">Rot markiert: ${fmtKm(s.road_m)} Autostraße ließen sich nicht umgehen.</span>`;
  if (dt.innerHTML) dt.style.display = "block";
  drawElevation(d.elevation);
}
function drawElevation(pts) {
  const c = $("elev");
  if (!pts || pts.length < 2) { c.style.display = "none"; return; }
  c.style.display = "block";
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, w, h);
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x1 = xs[xs.length - 1] || 1, lo = Math.min(...ys), hi = Math.max(...ys), span = Math.max(hi - lo, 20);
  const X = (x) => 4 + (x / x1) * (w - 8), Y = (y) => h - 14 - ((y - lo) / span) * (h - 24);
  g.beginPath(); g.moveTo(X(xs[0]), h - 14);
  pts.forEach((p) => g.lineTo(X(p[0]), Y(p[1])));
  g.lineTo(X(x1), h - 14); g.closePath();
  g.fillStyle = "rgba(31,157,85,.25)"; g.fill();
  g.beginPath(); pts.forEach((p, i) => (i ? g.lineTo(X(p[0]), Y(p[1])) : g.moveTo(X(p[0]), Y(p[1]))));
  g.strokeStyle = "#1f9d55"; g.lineWidth = 1.6; g.stroke();
  g.fillStyle = getComputedStyle(document.body).getPropertyValue("--muted"); g.font = "11px system-ui";
  g.fillText(Math.round(lo) + " m", 4, h - 2); g.fillText(Math.round(hi) + " m", 4, 11);
  g.textAlign = "right"; g.fillText(x1.toFixed(1) + " km", w - 4, h - 2);
}
// ---- GPX-Export -------------------------------------------------------------------------------------
// Die Datei wird im Browser aus der angezeigten Route erzeugt und im selben Klick heruntergeladen: keine erneute
// Berechnung, exportiert genau die gewählte Variante, und der Download bleibt eine direkte Folge des Klicks
// (Safari/iOS und Firefox blockieren Downloads, die erst nach einer Server-Antwort ausgelöst werden).
const xmlEsc = (t) => String(t).replace(/[<>&"']/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" }[c]));
function buildGpx(r) {
  const s = r.stats || {};
  const kind = s.roundtrip ? "Rundreise" : (cfg.profiles.find((p) => p.id === $("profile").value) || {}).label || "Route";
  const name = `FahrradNavi ${kind} ${fmtKm(s.distance_m || 0)}`;
  const now = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  const out = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<gpx version="1.1" creator="FahrradNavi" xmlns="http://www.topografix.com/GPX/1/1" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd">',
    `<metadata><name>${xmlEsc(name)}</name><time>${now}</time></metadata>`,
  ];
  const wpts = filled();
  wpts.forEach((p, i) => {
    const label = i === 0 ? "Start" : !s.roundtrip && i === wpts.length - 1 ? "Ziel" : s.roundtrip ? `Station ${i}` : `Zwischenziel ${i}`;
    out.push(`<wpt lat="${p.lat.toFixed(6)}" lon="${p.lon.toFixed(6)}"><name>${xmlEsc(label + (p.label && !/^[\d.,\s-]+$/.test(p.label) ? ": " + p.label : ""))}</name></wpt>`);
  });
  out.push(`<trk><name>${xmlEsc(name)}</name><trkseg>`);
  for (const [lon, lat, ele] of r.coordinates || []) {
    out.push(ele == null ? `<trkpt lat="${lat.toFixed(6)}" lon="${lon.toFixed(6)}"/>`
      : `<trkpt lat="${lat.toFixed(6)}" lon="${lon.toFixed(6)}"><ele>${ele.toFixed(1)}</ele></trkpt>`);
  }
  out.push("</trkseg></trk></gpx>");
  const file = `fahrradnavi-${s.roundtrip ? "rundreise" : "route"}-${((s.distance_m || 0) / 1000).toFixed(1).replace(".", "-")}km.gpx`;
  return { text: out.join("\n"), file };
}
$("gpx").onclick = () => {
  const r = routes[selected];
  if (!r || !(r.coordinates || []).length) { toast("Keine Route zum Exportieren – bitte zuerst eine Route berechnen."); return; }
  try {
    const { text, file } = buildGpx(r);
    const url = URL.createObjectURL(new Blob([text], { type: "application/gpx+xml" }));
    const a = document.createElement("a");
    a.href = url; a.download = file; a.rel = "noopener"; a.style.display = "none";
    document.body.appendChild(a);  // manche Browser lösen den Download nur für Links im Dokument aus
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    toast(`GPX gespeichert: ${file}`);
  } catch (e) {
    toast("Export fehlgeschlagen: " + e.message, 6000);
  }
};
function restoreFromHash(b64) {
  try {
    const arr = JSON.parse(decodeURIComponent(escape(atob(b64))));
    points = arr.map(([lat, lon]) => ({ lat, lon, label: `${lat.toFixed(5)}, ${lon.toFixed(5)}` }));
    renderPoints(); route();
  } catch (e) { /* ignorieren */ }
}

// ---- Route durch Markierungen verändern ------------------------------------------------------------
// Nächster Punkt der angezeigten Route zu einer Mausposition (in Bildschirmpixeln gerechnet).
function closestOnRoute(latlng) {
  if (curCoords.length < 2) return null;
  const p = map.latLngToContainerPoint(latlng);
  let best = null;
  let a = map.latLngToContainerPoint([curCoords[0][1], curCoords[0][0]]);
  for (let i = 0; i < curCoords.length - 1; i++) {
    const b = map.latLngToContainerPoint([curCoords[i + 1][1], curCoords[i + 1][0]]);
    const dx = b.x - a.x, dy = b.y - a.y, len2 = dx * dx + dy * dy;
    const t = len2 ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2)) : 0;
    const qx = a.x + t * dx, qy = a.y + t * dy, d2 = (p.x - qx) ** 2 + (p.y - qy) ** 2;
    if (!best || d2 < best.d2) best = { d2, index: t > 0.5 ? i + 1 : i, x: qx, y: qy };
    a = b;
  }
  return best && { index: best.index, latlng: map.containerPointToLatLng([best.x, best.y]) };
}
function legAt(index) {
  const j = curLegEnds.findIndex((e) => e >= index);
  return j < 0 ? Math.max(0, curLegEnds.length - 1) : j;
}
function showGhost(latlng) {
  clearTimeout(ghostTimer);
  const c = closestOnRoute(latlng);
  if (!c) return;
  ghostIdx = c.index;
  if (!ghost) {
    ghost = L.marker(c.latlng, { icon: L.divIcon({ className: "", html: '<div class="ghost"></div>', iconSize: [16, 16], iconAnchor: [8, 8] }), draggable: true, zIndexOffset: 2000, keyboard: false });
    ghost.on("dragstart", () => { dragging = true; ghostLeg = legAt(ghostIdx); });
    ghost.on("dragend", () => { dragging = false; const ll = ghost.getLatLng(); hideGhost(); insertVia(ghostLeg, ll); });
    ghost.on("click", () => { if (mode === "route") openRoutePopup(ghost.getLatLng()); });  // liegt beim Klick unter der Maus
    ghost.on("mouseover", () => clearTimeout(ghostTimer));
    ghost.on("mouseout", () => { if (!dragging) ghostTimer = setTimeout(hideGhost, 900); });
  }
  if (!dragging) { ghost.setLatLng(c.latlng); if (!map.hasLayer(ghost)) ghost.addTo(map); }
}
function hideGhost() { if (ghost && !dragging && map.hasLayer(ghost)) map.removeLayer(ghost); }
function insertVia(leg, ll) {
  points = filled();
  const pos = Math.min(leg + 1, $("loop").checked ? points.length : Math.max(1, points.length - 1));
  points.splice(pos, 0, { lat: ll.lat, lon: ll.lng, label: `${ll.lat.toFixed(5)}, ${ll.lng.toFixed(5)}` });
  renderPoints(); scheduleRoute();
}
function openRoutePopup(latlng) {
  const c = closestOnRoute(latlng);
  if (!c) return;
  const box = document.createElement("div");
  const b1 = document.createElement("button"); b1.className = "popbtn"; b1.textContent = "📍 Zwischenziel hier einfügen";
  b1.onclick = () => { map.closePopup(); insertVia(legAt(c.index), c.latlng); };
  const b2 = document.createElement("button"); b2.className = "popbtn"; b2.textContent = "🚫 Diese Stelle meiden (Kreis 60 m)";
  b2.onclick = () => { map.closePopup(); addArea({ kind: "circle", lat: c.latlng.lat, lon: c.latlng.lng, radius_m: 60, name: "Gemiedene Stelle" }); };
  if (tab === "route") box.append(b1);  // Rundreisen haben keine Zwischenziele
  box.append(b2);
  L.popup({ closeButton: false }).setLatLng(c.latlng).setContent(box).openOn(map);
}

// ---- Bereiche zum Meiden ---------------------------------------------------------------------------
function setMode(m, text) {
  mode = m;
  $("toolCircle").classList.toggle("on", m === "circle");
  $("toolPolygon").classList.toggle("on", m === "polygon");
  const bar = $("modebar");
  bar.style.display = m === "route" ? "none" : "flex";
  $("modetxt").textContent = text || "";
  $("modeDone").style.display = m === "polygon" ? "" : "none";
  map.getContainer().style.cursor = m === "route" ? "" : "crosshair";
  if (m === "route") map.doubleClickZoom.enable(); else map.doubleClickZoom.disable();
}
function cancelDraw() {
  if (draw && draw.preview) draw.preview.remove();
  draw = null; setMode("route");
}
function startDraw(kind) {
  if (mode === kind) return cancelDraw();
  cancelDraw(); hideGhost();
  draw = { kind, pts: [], preview: null, center: null };
  setMode(kind, kind === "circle" ? "Kreis: Mittelpunkt anklicken, dann Radius per zweitem Klick festlegen." : "Fläche: Ecken anklicken. Doppelklick oder „Fertig“ schließt die Fläche.");
  $("toolsCard").open = true;
}
$("toolCircle").onclick = () => startDraw("circle");
$("toolPolygon").onclick = () => startDraw("polygon");
$("modeCancel").onclick = cancelDraw;
$("modeDone").onclick = () => finishPolygon();
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") cancelDraw();
  if (e.key === "Enter" && mode === "polygon") finishPolygon();
});
const PREVIEW = { color: "#d63c3c", weight: 2, dashArray: "5 5", fillOpacity: .12, interactive: false };
function circleClick(e) {
  if (!draw.center) {
    draw.center = e.latlng;
    draw.preview = L.circle(e.latlng, { ...PREVIEW, radius: 5 }).addTo(map);
    $("modetxt").textContent = "Radius: zweiten Punkt anklicken (Esc bricht ab).";
    return;
  }
  const r = draw.center.distanceTo(e.latlng);
  if (r < 20) { toast("Radius zu klein – bitte weiter vom Mittelpunkt klicken."); return; }
  const c = draw.center;
  cancelDraw();
  addArea({ kind: "circle", lat: c.lat, lon: c.lng, radius_m: Math.round(r) });
}
function polyClick(e) {
  const last = draw.pts[draw.pts.length - 1];
  if (last && last.distanceTo(e.latlng) < 2) return;  // Doppelklick erzeugt zwei Klicks am selben Ort
  draw.pts.push(e.latlng);
  updatePolyPreview(e.latlng);
}
function updatePolyPreview(mouse) {
  const pts = mouse ? [...draw.pts, mouse] : draw.pts;
  if (!draw.preview) draw.preview = L.polygon(pts, PREVIEW).addTo(map); else draw.preview.setLatLngs(pts);
}
function onMapMove(e) {
  if (mode === "circle" && draw && draw.center) draw.preview.setRadius(Math.max(5, draw.center.distanceTo(e.latlng)));
  else if (mode === "polygon" && draw && draw.pts.length) updatePolyPreview(e.latlng);
}
function finishPolygon() {
  if (!draw || draw.kind !== "polygon") return;
  if (draw.pts.length < 3) { toast("Eine Fläche braucht mindestens 3 Ecken."); return; }
  const pts = draw.pts.map((p) => [+p.lat.toFixed(6), +p.lng.toFixed(6)]);
  cancelDraw();
  addArea({ kind: "polygon", points: pts });
}
function addArea(a) {
  if (areas.length >= MAX_AREAS) { toast(`Höchstens ${MAX_AREAS} Bereiche – bitte einen löschen.`); return; }
  areas.push({ id: uid(), name: a.name || `Bereich ${areas.length + 1}`, strength: 1, active: true, ...a });
  renderOverlays(); $("toolsCard").open = true;
  toast("Bereich hinzugefügt – Wege dort werden gemieden, soweit es einen zumutbaren Umweg gibt.");
  scheduleRoute();
}

// ---- Lieblingswege (GPX) ---------------------------------------------------------------------------
function thin(coords, minM, maxPts) {
  // Punkte ausdünnen (Abstand >= minM), bei zu vielen Punkten den Abstand vergrößern
  const kx = 111320 * Math.cos((coords[0][0] * Math.PI) / 180), ky = 110574;
  for (let spacing = minM; ; spacing *= 1.5) {
    const out = [coords[0]];
    for (let i = 1; i < coords.length; i++) {
      const l = out[out.length - 1];
      if (Math.hypot((coords[i][1] - l[1]) * kx, (coords[i][0] - l[0]) * ky) >= spacing) out.push(coords[i]);
    }
    if (out[out.length - 1] !== coords[coords.length - 1]) out.push(coords[coords.length - 1]);
    if (out.length <= maxPts) return out;
  }
}
function parseGpx(text, fallbackName) {
  const doc = new DOMParser().parseFromString(text, "application/xml");
  if (doc.getElementsByTagName("parsererror").length) throw new Error("Keine gültige GPX-Datei.");
  let nodes = [...doc.getElementsByTagName("trkpt")];
  if (!nodes.length) nodes = [...doc.getElementsByTagName("rtept")];
  const coords = nodes.map((n) => [parseFloat(n.getAttribute("lat")), parseFloat(n.getAttribute("lon"))])
    .filter(([la, lo]) => Number.isFinite(la) && Number.isFinite(lo) && Math.abs(la) <= 90 && Math.abs(lo) <= 180);
  if (coords.length < 2) throw new Error("Die GPX-Datei enthält keinen Track (mindestens 2 Punkte).");
  const nm = doc.querySelector("trk > name") || doc.querySelector("metadata > name") || doc.querySelector("rte > name");
  return { name: (nm && nm.textContent.trim()) || fallbackName, coords: thin(coords, 8, 4000) };
}
function addFav(name, coords) {
  if (favs.length >= MAX_FAVS) { toast(`Höchstens ${MAX_FAVS} Lieblingswege – bitte einen löschen.`); return; }
  if (favs.reduce((n, f) => n + f.coords.length, 0) + coords.length > MAX_FAV_POINTS) { toast("Zu viele Punkte in den Lieblingswegen insgesamt – bitte einen löschen."); return; }
  favs.push({ id: uid(), name, coords, strength: 1, active: true });
  renderOverlays(); $("toolsCard").open = true;
  toast(`Lieblingsweg „${name}“ gespeichert – wird bevorzugt.`);
  scheduleRoute();
}
$("toolGpx").onclick = () => $("gpxFile").click();
$("gpxFile").onchange = async () => {
  const f = $("gpxFile").files[0];
  $("gpxFile").value = "";
  if (!f) return;
  if (f.size > 8_000_000) { toast("Datei zu groß (max. 8 MB)."); return; }
  try {
    const g = parseGpx(await f.text(), f.name.replace(/\.gpx$/i, ""));
    addFav(g.name, g.coords);
  } catch (e) { toast(e.message); }
};
$("toolSaveFav").onclick = () => {
  const r = routes[selected];
  if (!r) return;
  addFav(`Route ${fmtKm(r.stats.distance_m)}`, thin(r.coordinates.map((c) => [c[1], c[0]]), 8, 4000));
};

// ---- Overlays anzeigen und verwalten ----------------------------------------------------------------
function iconBtn(text, title, fn) {
  const b = document.createElement("button"); b.textContent = text; b.title = title; b.onclick = fn; return b;
}
function overlayItem(o, color, onChange, onZoom, onDelete) {
  const item = document.createElement("div"); item.className = "ov-item";
  const head = document.createElement("div"); head.className = "head";
  const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = o.active; cb.title = "aktiv";
  cb.style.width = "auto"; cb.onchange = () => { o.active = cb.checked; onChange(); };
  const sw = document.createElement("span"); sw.className = "sw"; sw.style.background = color;
  const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = o.name; nm.title = "Doppelklick zum Umbenennen";
  nm.ondblclick = () => { const v = prompt("Name:", o.name); if (v && v.trim()) { o.name = v.trim().slice(0, 60); onChange(); } };
  head.append(cb, sw, nm, iconBtn("🔍", "Auf der Karte zeigen", onZoom), iconBtn("×", "Löschen", onDelete));
  const sl = document.createElement("input"); sl.type = "range"; sl.min = "0.25"; sl.max = "1"; sl.step = "0.25"; sl.value = String(o.strength ?? 1);
  sl.title = "Stärke"; sl.onchange = () => { o.strength = +sl.value; onChange(); };
  item.append(head, sl);
  return item;
}
function renderOverlays() {
  areaLayer.clearLayers(); favLayer.clearLayers();
  const aBox = $("areaList"), fBox = $("favList");
  aBox.innerHTML = ""; fBox.innerHTML = "";
  const changed = () => { renderOverlays(); scheduleRoute(); };
  for (const a of areas) {
    const st = { pane: "areas", color: "#d63c3c", weight: 2, dashArray: a.active ? null : "4 6", fillColor: "#d63c3c", fillOpacity: a.active ? 0.08 + 0.17 * (a.strength ?? 1) : 0.02, interactive: false };
    const layer = a.kind === "circle" ? L.circle([a.lat, a.lon], { ...st, radius: a.radius_m }) : L.polygon(a.points, st);
    areaLayer.addLayer(layer);
    aBox.appendChild(overlayItem(a, "#d63c3c", changed, () => map.fitBounds(layer.getBounds(), { padding: [40, 40] }),
      () => { areas = areas.filter((x) => x !== a); changed(); }));
  }
  for (const f of favs) {
    const layer = L.polyline(f.coords, { color: "#8e44ad", weight: 4, opacity: f.active ? 0.85 : 0.25, dashArray: "2 8", interactive: false });
    favLayer.addLayer(layer);
    fBox.appendChild(overlayItem(f, "#8e44ad", changed, () => map.fitBounds(layer.getBounds(), { padding: [40, 40] }),
      () => { favs = favs.filter((x) => x !== f); changed(); }));
  }
  saveStore();
}

$("logout").onclick = async () => { await fetch("/logout", { method: "POST" }); location.href = "/login"; };
init();
