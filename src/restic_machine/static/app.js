// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 David Maluenda
"use strict";

const $ = (selector) => document.querySelector(selector);

const state = {
  status: null,
  snapshots: [],          // visible snapshots of the current series, oldest first
  series: null,
  snapshot: null,         // restic id of the snapshot on screen
  path: "/",
  dir: null,              // last /api/dir answer
  sort: { key: "name", asc: true },
  showRemoved: false,
  detailPath: null,
  lastDone: 0,
};
let statusTimer = null;

// ---- small helpers -------------------------------------------------------------------

async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  const response = await fetch(url, { credentials: "same-origin" });
  const data = await response.json().catch(() => ({ error: response.statusText }));
  if (!response.ok) throw Object.assign(new Error(data.error || response.statusText), { status: response.status });
  return data;
}

async function post(path) {
  const response = await fetch(path, {
    method: "POST", credentials: "same-origin", headers: { "X-Restic-Machine": "1" },
  });
  const data = await response.json().catch(() => ({ error: response.statusText }));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  setAttributes(node, attrs);
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
}

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  setAttributes(node, attrs);
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
}

function setAttributes(node, attrs) {
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.setAttribute("class", value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
}

function formatSize(bytes) {
  if (bytes == null) return "";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return unit === 0 ? `${value} B` : `${value.toFixed(value < 10 ? 2 : 1)} ${units[unit]}`;
}

const count = (n) => (n == null ? "" : n.toLocaleString());
const pad = (n) => String(n).padStart(2, "0");
const formatDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const formatDate = (d) => (d ? `${formatDay(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}` : "");
// restic writes nanoseconds; keep milliseconds so every browser parses it
const snapshotDate = (s) => new Date(s.time.replace(/(\.\d{3})\d+/, "$1"));
const mtimeDate = (us) => (us == null ? null : new Date(us / 1000));
const changesOf = (s) => (s.added == null ? null : s.added + s.modified + s.removed);
const joinPath = (dir, name) => (dir === "/" ? `/${name}` : `${dir}/${name}`);
const parentOf = (path) => (path.lastIndexOf("/") <= 0 ? "/" : path.slice(0, path.lastIndexOf("/")));
const current = () => state.snapshots.find((s) => s.id === state.snapshot);

function showBanner(message, warning = false) {
  const banner = $("#banner");
  banner.hidden = !message;
  banner.textContent = message || "";
  banner.classList.toggle("warning", warning);
}

// ---- address bar: #snapshot=…&path=… -------------------------------------------------

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  return { snapshot: params.get("snapshot"), path: params.get("path") || "/" };
}

function writeHash(push) {
  const hash = "#" + new URLSearchParams({ snapshot: state.snapshot, path: state.path });
  if (hash !== location.hash) history[push ? "pushState" : "replaceState"](null, "", hash);
}

// ---- status and indexing -------------------------------------------------------------

async function refreshStatus() {
  clearTimeout(statusTimer);
  let status;
  try {
    status = await api("/api/status");
  } catch (error) {
    showBanner(`Can't reach restic-machine: ${error.message}`, true);
    statusTimer = setTimeout(refreshStatus, 10000);
    return;
  }
  const wasRunning = state.status?.indexing.running;
  state.status = status;
  renderStatus();
  const indexing = status.indexing;
  if ((wasRunning && !indexing.running) || (indexing.running && indexing.done !== state.lastDone)) {
    state.lastDone = indexing.done;
    await loadSnapshots();
  }
  statusTimer = setTimeout(refreshStatus, indexing.running ? 2000 : 30000);
}

function renderStatus() {
  const { status } = state;
  const indexing = status.indexing;
  $("#repo").textContent = `repository ${status.repository.slice(0, 8)}`;
  $("#repo").title = status.repository;
  $("#attribution").textContent = status.attribution;
  $("#version").textContent = `v${status.version}`;

  const select = $("#series");
  select.hidden = status.series.length < 2;
  select.replaceChildren(...status.series.map((s) =>
    el("option", { value: s.id }, `${s.hostname}: ${s.paths.join(", ")}`)));
  if (state.series != null) select.value = state.series;

  let text = "";
  if (indexing.running) {
    text = indexing.total
      ? `Reading snapshot ${Math.min(indexing.done + 1, indexing.total)} of ${indexing.total}` +
        (indexing.current ? ` (${formatDate(snapshotDate({ time: indexing.current }))})` : "") + "…"
      : "Looking for new snapshots…";
  } else if (indexing.finished_at && !indexing.error) {
    text = "Index up to date";
  }
  $("#index-state").textContent = text;
  $("#reindex").disabled = indexing.running;

  if (indexing.unavailable) {
    showBanner("The repository can't be read right now, so the index can't be updated. " +
      "You are browsing it as it was last updated.", true);
  } else if (indexing.error) {
    showBanner(indexing.error, true);
  } else {
    showBanner("");
  }
}

// ---- timeline ------------------------------------------------------------------------

async function loadSnapshots() {
  const all = await api("/api/snapshots");
  const wanted = all.find((s) => s.id === state.snapshot);
  if (wanted) state.series = wanted.series;
  if (state.series == null && all.length) state.series = all[all.length - 1].series;
  state.snapshots = all.filter((s) => s.series === state.series);
  if (state.status) $("#series").value = state.series;
  renderTimeline();

  const shown = current();
  if (shown?.indexed) {
    renderSnapshotInfo();
    if (!state.dir) await loadDir(false);
    return;
  }
  const latest = [...state.snapshots].reverse().find((s) => s.indexed);
  if (latest) {
    await select(latest.id, false);
  } else {
    $("#snapshot-info").textContent = "No snapshot indexed yet: reading the repository…";
  }
}

function renderTimeline() {
  const bars = $("#bars");
  const dates = state.snapshots.map(snapshotDate);
  const changes = state.snapshots.map(changesOf);
  const max = Math.max(1, ...changes.filter((c) => c != null));
  // A spike is judged by changes per hour, so a monthly snapshot isn't one just for being far apart.
  const rates = changes.map((c, i) => (c == null || i === 0 ? null
    : c / Math.max(1, (dates[i] - dates[i - 1]) / 3600000)));
  const sortedRates = rates.filter((r) => r != null).sort((a, b) => a - b);
  const medianRate = sortedRates.length ? sortedRates[Math.floor(sortedRates.length / 2)] : 0;
  let previous = null, x = 0, lastLabelX = -Infinity;

  bars.replaceChildren(...state.snapshots.map((s, i) => {
    const date = dates[i];
    const changed = changes[i];
    const gap = previous && date - previous > 36 * 3600 * 1000;
    const spike = changed != null && changed >= 1000 && rates[i] > 10 * Math.max(medianRate, 1);
    x += gap ? 24 : 14;
    const classes = ["snap", s.indexed ? "" : "pending", spike ? "spike" : "", gap ? "gap" : "",
      s.id === state.snapshot ? "selected" : ""].filter(Boolean).join(" ");
    const lines = [`${formatDate(date)} · ${s.id.slice(0, 8)}`];
    if (!s.indexed) lines.push("not indexed yet");
    else lines.push(`${formatSize(s.size)} in ${count(s.files)} files`,
      changed == null ? "first snapshot" : `+${count(s.added)} new · ~${count(s.modified)} modified · −${count(s.removed)} removed`);
    if (s.tags.length) lines.push(`tags: ${s.tags.join(", ")}`);

    const button = el("button", {
      type: "button", class: classes, role: "option", title: lines.join("\n"),
      "aria-selected": s.id === state.snapshot ? "true" : "false", "data-id": s.id,
      onclick: () => s.indexed && select(s.id, true),
    }, el("span", { class: "bar" }));
    const height = !s.indexed ? 6 : changed == null ? 100 : 6 + 94 * Math.log1p(changed) / Math.log1p(max);
    button.firstChild.style.height = `${height}%`;
    if (!previous || formatDay(date) !== formatDay(previous)) {
      if (x - lastLabelX >= 64) {
        const sameYear = previous && previous.getFullYear() === date.getFullYear();
        const label = date.toLocaleDateString(undefined,
          sameYear ? { month: "short", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" });
        button.append(el("span", { class: "day" }, label));
        lastLabelX = x;
      }
    }
    previous = date;
    return button;
  }));
  bars.querySelector(".selected")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function renderSnapshotInfo() {
  const s = current();
  const info = $("#snapshot-info");
  if (!s) { info.replaceChildren(); return; }
  const previous = state.dir?.previous && state.snapshots.find((p) => p.id === state.dir.previous);
  const parts = [
    el("strong", {}, formatDate(snapshotDate(s))),
    el("span", { class: "id", title: s.id }, s.id.slice(0, 8)),
    el("span", {}, `${formatSize(s.size)} in ${count(s.files)} files`),
  ];
  if (s.added != null) {
    parts.push(el("span", {}, `+${count(s.added)} new · ~${count(s.modified)} modified · −${count(s.removed)} removed`,
      previous ? el("span", { class: "muted" }, ` since ${formatDate(snapshotDate(previous))}`) : null));
  }
  if (s.tags.length) parts.push(el("span", { class: "muted" }, `tags: ${s.tags.join(", ")}`));
  info.replaceChildren(...parts);
}

async function select(id, push) {
  state.snapshot = id;
  for (const bar of $("#bars").children) {
    const selected = bar.dataset.id === id;
    bar.classList.toggle("selected", selected);
    bar.setAttribute("aria-selected", selected ? "true" : "false");
  }
  $("#bars .selected")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  await loadDir(push);
  if (state.detailPath) await openDetail(state.detailPath);
}

function step(offset) {
  const indexed = state.snapshots.filter((s) => s.indexed);
  const at = indexed.findIndex((s) => s.id === state.snapshot);
  const next = indexed[Math.max(0, Math.min(indexed.length - 1, at + offset))];
  if (next && next.id !== state.snapshot) select(next.id, true);
}

// ---- folder listing ------------------------------------------------------------------

async function loadDir(push) {
  let path = state.path, missing = null, data = null;
  for (;;) {
    try {
      data = await api("/api/dir", { snapshot: state.snapshot, path });
      break;
    } catch (error) {
      if (error.status === 404 && path !== "/") {
        missing ??= path;
        path = parentOf(path);
      } else {
        $("#listing-note").hidden = false;
        $("#listing-note").textContent = error.message;
        return;
      }
    }
  }
  state.path = data.path;
  state.dir = data;
  writeHash(push);
  renderSnapshotInfo();
  renderCrumbs();
  renderListing(missing ? `${missing} did not exist in this snapshot; showing ${data.path} instead.` : "");
}

function navigate(path) {
  state.path = path;
  loadDir(true);
}

function renderCrumbs() {
  const parts = state.path.split("/").filter(Boolean);
  const crumbs = [];
  const add = (label, path, last) => crumbs.push(last
    ? el("span", { class: "current" }, label)
    : el("button", { type: "button", onclick: () => navigate(path) }, label));
  add("/", "/", parts.length === 0);
  parts.forEach((name, i) => {
    if (i > 0) crumbs.push(el("span", { class: "sep" }, "/"));
    add(name, "/" + parts.slice(0, i + 1).join("/"), i === parts.length - 1);
  });
  $("#crumbs").replaceChildren(...crumbs);
}

const STATUS_ORDER = { added: 0, modified: 1, changed: 2, removed: 3 };
const STATUS_LABEL = { added: "new", modified: "modified", changed: "changed inside", removed: "removed" };

function sortEntries(entries) {
  const { key, asc } = state.sort;
  const value = (e) => (key === "status" ? STATUS_ORDER[e.status] ?? 9 : e[key]);
  return [...entries].sort((a, b) => {
    if (key === "name" && (a.type === "dir") !== (b.type === "dir")) return a.type === "dir" ? -1 : 1;
    const va = value(a), vb = value(b);
    let order = 0;
    if (va == null || vb == null) order = va == null ? (vb == null ? 0 : 1) : -1;
    else if (typeof va === "string") order = va.localeCompare(vb, undefined, { sensitivity: "base" });
    else order = va - vb;
    return asc ? order : -order;
  });
}

function renderListing(note = "") {
  const { dir } = state;
  const rows = sortEntries(dir.entries).map((entry) => entryRow(entry, false));
  const removed = dir.removed;
  if (state.showRemoved && removed.length) {
    const previous = state.snapshots.find((s) => s.id === dir.previous);
    rows.push(el("tr", { class: "separator" }, el("td", { colspan: 6 },
      `Removed since ${previous ? formatDate(snapshotDate(previous)) : "the previous snapshot"}`)));
    rows.push(...sortEntries(removed).map((entry) => entryRow(entry, true)));
  }
  $("#listing tbody").replaceChildren(...rows);

  const notes = [note, dir.entries.length ? "" : "This folder is empty in this snapshot."].filter(Boolean);
  $("#listing-note").hidden = !notes.length;
  $("#listing-note").textContent = notes.join(" ");
  $("#removed-count").textContent = dir.previous ? `(${count(removed.length)})` : "";
  $("#show-removed").disabled = !dir.previous || !removed.length;

  for (const th of document.querySelectorAll("#listing th[data-sort]")) {
    const sorted = th.dataset.sort === state.sort.key;
    th.classList.toggle("sorted", sorted);
    th.classList.toggle("asc", sorted && state.sort.asc);
  }
}

function entryRow(entry, removed) {
  const path = joinPath(state.dir.path, entry.name);
  const isDir = entry.type === "dir";
  const kind = isDir ? "▸" : entry.type === "symlink" ? "↪" : "";
  const name = el("button", {
    type: "button", class: "link name",
    onclick: isDir && !removed ? () => navigate(path) : () => openDetail(path),
  }, el("span", { class: "kind", "aria-hidden": "true" }, kind), entry.name);
  const status = removed ? "removed" : entry.status;
  return el("tr", {
    class: [isDir ? "dir" : "", removed ? "removed" : "", state.detailPath === path ? "selected" : ""]
      .filter(Boolean).join(" ") || null,
    "data-path": path,
  },
  el("td", {}, name),
  el("td", { class: "num" }, formatSize(entry.size)),
  el("td", { class: "num" }, isDir ? count(entry.files) : ""),
  el("td", {}, formatDate(mtimeDate(entry.mtime_us))),
  el("td", {}, status ? el("span", { class: `badge ${status}` }, STATUS_LABEL[status]) : ""),
  el("td", {}, el("button", {
    type: "button", class: "history", title: "Size over time", "aria-label": `Size of ${entry.name} over time`,
    onclick: () => openDetail(path),
  }, svg("svg", { width: 16, height: 16, viewBox: "0 0 16 16", "aria-hidden": "true" },
    svg("polyline", { points: "1,13 5,8 8,10 11,4 15,6", fill: "none", stroke: "currentColor", "stroke-width": 1.6 })))));
}

// ---- size over time ------------------------------------------------------------------

async function openDetail(path) {
  state.detailPath = path;
  for (const row of document.querySelectorAll("#listing tbody tr")) {
    row.classList.toggle("selected", row.dataset.path === path);
  }
  const detail = $("#detail");
  const wasHidden = detail.hidden;
  detail.hidden = false;
  if (wasHidden) detail.scrollIntoView({ block: "nearest" });
  $("#detail-title").textContent = path === "/" ? "/" : path.slice(path.lastIndexOf("/") + 1);
  $("#detail-path").textContent = path;
  let data;
  try {
    data = await api("/api/history", { snapshot: state.snapshot, path });
  } catch (error) {
    $("#chart").textContent = error.message;
    $("#versions tbody").replaceChildren();
    return;
  }
  if (state.detailPath !== path) return;  // another one was opened meanwhile
  renderChart(data.points);
  renderVersions(data.points);
}

function closeDetail() {
  state.detailPath = null;
  $("#detail").hidden = true;
  for (const row of document.querySelectorAll("#listing tbody tr.selected")) row.classList.remove("selected");
}

function renderChart(points) {
  const W = 420, H = 190, left = 64, right = 10, top = 12, bottom = 26;
  const n = points.length;
  const max = Math.max(1, ...points.filter((p) => p.present).map((p) => p.size || 0));
  const x = (i) => left + (n > 1 ? i * (W - left - right) / (n - 1) : (W - left - right) / 2);
  const y = (v) => top + (1 - (v || 0) / max) * (H - top - bottom);
  const base = H - bottom;

  let d = "", previous = null;
  points.forEach((p, i) => {
    if (!p.present) { previous = null; return; }
    d += previous == null ? `M${x(i)},${y(p.size)}` : `H${x(i)}V${y(p.size)}`;
    previous = p;
  });

  const marks = [];
  points.forEach((p, i) => {
    const date = formatDate(snapshotDate(p));
    if (!p.present) {
      marks.push(svg("rect", { class: "absent", x: x(i) - 1.5, y: base - 4, width: 3, height: 4 },
        svg("title", {}, `${date}: not there`)));
      return;
    }
    const before = points[i - 1];
    const isChange = !before || !before.present || before.size !== p.size || before.mtime_us !== p.mtime_us;
    const isCurrent = p.snapshot === state.snapshot;
    if (!isChange && !isCurrent) return;
    marks.push(svg("circle", {
      class: isCurrent ? "dot current" : "dot", cx: x(i), cy: y(p.size), r: isCurrent ? 4.5 : 3,
      onclick: () => select(p.snapshot, true),
    }, svg("title", {}, `${date}: ${formatSize(p.size)}${p.files != null ? ` in ${count(p.files)} files` : ""}`)));
  });

  const first = points[0], last = points[n - 1];
  $("#chart").replaceChildren(svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Size over ${n} snapshots, up to ${formatSize(max)}` },
    svg("line", { class: "axis", x1: left, x2: W - right, y1: base, y2: base }),
    svg("line", { class: "axis", x1: left, x2: left, y1: top, y2: base }),
    svg("text", { class: "label", x: left - 6, y: top + 4, "text-anchor": "end" }, formatSize(max)),
    svg("text", { class: "label", x: left - 6, y: base, "text-anchor": "end" }, "0"),
    first ? svg("text", { class: "label", x: left, y: H - 8 }, formatDay(snapshotDate(first))) : null,
    last && n > 1 ? svg("text", { class: "label", x: W - right, y: H - 8, "text-anchor": "end" },
      formatDay(snapshotDate(last))) : null,
    svg("path", { class: "line", d }),
    marks));
}

function renderVersions(points) {
  // Consecutive snapshots in which the path looked the same make one row; newest first.
  const runs = [];
  for (const p of points) {
    const run = runs[runs.length - 1];
    const same = run && run.present === p.present && run.size === p.size &&
      run.files === p.files && run.mtime_us === p.mtime_us;
    if (same) run.until = p;
    else runs.push({ ...p, from: p, until: p });
  }
  $("#versions tbody").replaceChildren(...runs.reverse().map((run) => el("tr", {},
    el("td", {}, formatDate(snapshotDate(run.from))),
    el("td", {}, run.until === run.from ? "" : formatDate(snapshotDate(run.until))),
    el("td", { class: "num" }, run.present ? formatSize(run.size) : "—"),
    el("td", { class: "num" }, run.present ? count(run.files) : ""),
    el("td", {}, run.present ? formatDate(mtimeDate(run.mtime_us)) : "not there"))));
}

// ---- wiring ----------------------------------------------------------------------------

function bind() {
  $("#reindex").addEventListener("click", async () => {
    $("#reindex").disabled = true;
    try {
      await post("/api/index");
    } catch (error) {
      showBanner(error.message, true);
    }
    refreshStatus();
  });
  $("#series").addEventListener("change", (event) => {
    state.series = Number(event.target.value);
    state.snapshot = null;
    state.path = "/";
    state.dir = null;
    closeDetail();
    loadSnapshots();
  });
  $("#show-removed").addEventListener("change", (event) => {
    state.showRemoved = event.target.checked;
    renderListing();
  });
  for (const th of document.querySelectorAll("#listing th[data-sort]")) {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      state.sort = { key, asc: state.sort.key === key ? !state.sort.asc : key === "name" };
      renderListing();
    });
  }
  $("#detail-close").addEventListener("click", closeDetail);
  $("#bars").addEventListener("keydown", (event) => {
    const moves = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity };
    if (!(event.key in moves)) return;
    event.preventDefault();
    step(Math.max(-1e6, Math.min(1e6, moves[event.key])));
  });
  window.addEventListener("popstate", () => {
    const { snapshot, path } = readHash();
    state.path = path;
    if (snapshot && snapshot !== state.snapshot) select(snapshot, false);
    else loadDir(false);
  });
}

async function init() {
  bind();
  const { snapshot, path } = readHash();
  state.snapshot = snapshot;
  state.path = path;
  await refreshStatus();
  if (!state.snapshots.length) await loadSnapshots();
}

init();
