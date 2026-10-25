// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 David Maluenda
import {
  $, api, count, el, formatDate, formatDay, formatSize, mtimeDate, post, snapshotDate, svg,
} from "./common.js";
import * as backups from "./backups.js";
import * as retention from "./retention.js";
import * as settings from "./settings.js";

const state = {
  tab: "browse",
  status: null,
  snapshots: [],          // visible snapshots of the current series, oldest first
  series: null,
  snapshot: null,         // restic id of the snapshot on screen
  compare: null,          // restic id of the older snapshot it is compared with, if any
  picked: new Set(),      // snapshots picked on the timeline (Ctrl+click) to compare
  previous: null,         // what the changes shown are measured against
  expanded: null,         // open folders (a Set of paths), remembered between visits
  cache: null,            // path -> {entries, removed} for the snapshot (and comparison) on screen
  cacheKey: null,
  autoOpen: false,
  excludes: false,        // whether an exclude file is set, so entries can be excluded
  sort: { key: "name", asc: true },
  showRemoved: false,
  detailPath: null,
  lastDone: 0,
  stopped: false,
};
let statusTimer = null;

const changesOf = (s) => (s.added == null ? null : s.added + s.modified + s.removed);
const joinPath = (dir, name) => (dir === "/" ? `/${name}` : `${dir}/${name}`);
const parentOf = (path) => (path.lastIndexOf("/") <= 0 ? "/" : path.slice(0, path.lastIndexOf("/")));
const current = () => state.snapshots.find((s) => s.id === state.snapshot);

function stopped(message) {
  state.stopped = true;
  clearTimeout(statusTimer);
  for (const button of document.querySelectorAll(".topbar button")) button.disabled = true;
  $("#index-state").textContent = "Stopped";
  showBanner(message, true);
}

function showBanner(message, warning = false) {
  const banner = $("#banner");
  banner.hidden = !message;
  banner.textContent = message || "";
  banner.classList.toggle("warning", warning);
}

// ---- address bar: #snapshot=…[&compare=…] (Browse) or #tab=… -------------------------------

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const tab = { repository: "cli", settings: "machine" }[params.get("tab")] || params.get("tab") || "browse";
  return { tab, snapshot: params.get("snapshot"), compare: params.get("compare") };
}

function writeHash(push) {
  const params = state.tab !== "browse" ? { tab: state.tab }
    : state.snapshot ? { snapshot: state.snapshot, ...(state.compare ? { compare: state.compare } : {}) } : {};
  const hash = "#" + new URLSearchParams(params);
  if (hash !== location.hash) history[push ? "pushState" : "replaceState"](null, "", hash);
}

// ---- tabs: Browser, Scheduler, Retention, restic-CLI, and the name itself (repository, index…) ------------------

const TABS = ["browse", "scheduler", "retention", "cli", "machine"];

function showTab(tab, push = true) {
  if (!state.status?.ready) tab = "machine";  // nothing else works without a repository
  state.tab = tab;
  for (const name of TABS) {
    $(`#view-${name}`).hidden = name !== tab;
    $(`#tab-${name}`).setAttribute("aria-selected", name === tab ? "true" : "false");
  }
  if (tab === "machine") settings.load();
  else if (tab === "scheduler") backups.load();
  else if (tab === "retention") retention.load();
  writeHash(push);
}

async function becameReady() {
  await refreshStatus();
  if (!state.snapshots.length) await loadSnapshots();
  showTab("browse");
}

// ---- status and indexing -------------------------------------------------------------

async function refreshStatus() {
  clearTimeout(statusTimer);
  if (state.stopped) return;
  let status;
  try {
    status = await api("/api/status");
  } catch (error) {
    // A new start gets a new address and key, so this page can't reconnect: say so and stop.
    stopped("restic-machine is not running any more (it stops by itself a while after its last " +
      "tab is closed). Start it again to keep browsing.");
    return;
  }
  const wasRunning = state.status?.indexing.running;
  state.status = status;
  renderStatus();
  const indexing = status.indexing;
  if (status.ready && ((wasRunning && !indexing.running) || (indexing.running && indexing.done !== state.lastDone))) {
    state.lastDone = indexing.done;
    await loadSnapshots();
  }
  statusTimer = setTimeout(refreshStatus, indexing.running ? 2000 : 30000);
}

function renderStatus() {
  const { status } = state;
  const indexing = status.indexing;
  const short = status.repository ? status.repository.slice(0, 8) : "";
  $("#repo").replaceChildren(...(!status.ready ? [el("span", { class: "muted" }, "not set up yet")]
    : status.name ? [el("em", {}, status.name), " ", el("span", { class: "muted" }, `(${short})`)]
      : [el("span", { class: "muted" }, `repository ${short}`)]));
  document.title = status.name ? `${status.name} · restic-machine` : "restic-machine";
  $("#repo").title = status.repository || "";
  for (const name of TABS.filter((tab) => tab !== "machine")) $(`#tab-${name}`).disabled = !status.ready;
  $("#reindex").hidden = !status.ready;
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
    if (state.cacheKey !== treeKey()) await loadTree();
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
      s.id === state.snapshot ? "selected" : "",
      state.picked.has(s.id) || s.id === state.compare ? "picked" : ""].filter(Boolean).join(" ");
    const lines = [`${formatDate(date)} · ${s.id.slice(0, 8)}`];
    if (!s.indexed) lines.push("not indexed yet");
    else lines.push(`${formatSize(s.size)} in ${count(s.files)} files`,
      changed == null ? "first snapshot" : `+${count(s.added)} new · ~${count(s.modified)} modified · −${count(s.removed)} removed`);
    if (s.tags.length) lines.push(`tags: ${s.tags.join(", ")}`);

    const button = el("button", {
      type: "button", class: classes, role: "option", title: lines.join("\n"),
      "aria-selected": s.id === state.snapshot ? "true" : "false", "data-id": s.id,
      onclick: (event) => s.indexed && (event.ctrlKey || event.metaKey || event.shiftKey
        ? togglePick(s.id) : select(s.id, true)),
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
  if (!s) {
    info.replaceChildren();
    return;
  }
  const older = state.compare && state.snapshots.find((p) => p.id === state.compare);
  if (older) {
    const sizeDelta = (s.size ?? 0) - (older.size ?? 0), filesDelta = (s.files ?? 0) - (older.files ?? 0);
    info.replaceChildren(
      el("strong", {}, "Comparing"),
      el("span", {}, formatDate(snapshotDate(older)), " ", el("span", { class: "id" }, older.id.slice(0, 8))),
      el("span", {}, "→"),
      el("span", {}, formatDate(snapshotDate(s)), " ", el("span", { class: "id" }, s.id.slice(0, 8))),
      el("span", {}, `${sizeDelta >= 0 ? "+" : "−"}${formatSize(Math.abs(sizeDelta))}, `,
        `${filesDelta >= 0 ? "+" : "−"}${count(Math.abs(filesDelta))} files`),
      el("span", { class: "muted" }, "only what differs is shown"),
      el("button", { type: "button", onclick: stopCompare }, "Stop comparing"));
    return;
  }
  const previous = state.previous && state.snapshots.find((p) => p.id === state.previous);
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

// ---- picking snapshots to compare (Ctrl+click on the timeline) ----------------------------

function togglePick(id) {
  if (state.picked.has(id)) state.picked.delete(id);
  else state.picked.add(id);
  markBars();
  renderPickBar();
}

function renderPickBar() {
  const bar = $("#pick-bar");
  const n = state.picked.size;
  bar.replaceChildren(n === 0
    ? el("span", { class: "hint" }, "Ctrl+click two snapshots to compare them.")
    : el("span", {}, `${n} snapshot${n > 1 ? "s" : ""} picked`,
      el("button", { type: "button", class: "primary", disabled: n !== 2, onclick: startCompare,
        title: n === 2 ? "Show what changed from the older one to the newer one" : "Pick exactly two" }, "Compare"),
      el("button", { type: "button", onclick: () => {
        state.picked.clear();
        markBars();
        renderPickBar();
      } }, "Clear")));
}

function markBars() {
  for (const bar of $("#bars").children) {
    const id = bar.dataset.id;
    bar.classList.toggle("selected", id === state.snapshot);
    bar.setAttribute("aria-selected", id === state.snapshot ? "true" : "false");
    bar.classList.toggle("picked", state.picked.has(id) || id === state.compare);
  }
}

async function startCompare() {
  const [older, newer] = [...state.picked]
    .map((id) => state.snapshots.find((s) => s.id === id))
    .sort((a, b) => snapshotDate(a) - snapshotDate(b));
  state.picked.clear();
  state.compare = older.id;
  state.snapshot = newer.id;
  markBars();
  renderPickBar();
  writeHash(true);
  await loadTree();
  if (state.detailPath) await openDetail(state.detailPath);
}

function stopCompare() {
  state.compare = null;
  markBars();
  writeHash(true);
  loadTree();
}

async function select(id, push) {
  state.snapshot = id;
  state.compare = null;
  markBars();
  $("#bars .selected")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  writeHash(push);
  await loadTree();
  if (state.detailPath) await openDetail(state.detailPath);
}

function step(offset) {
  const indexed = state.snapshots.filter((s) => s.indexed);
  const at = indexed.findIndex((s) => s.id === state.snapshot);
  const next = indexed[Math.max(0, Math.min(indexed.length - 1, at + offset))];
  if (next && next.id !== state.snapshot) select(next.id, true);
}

// ---- the folder tree -------------------------------------------------------------------
// Folders open in place and are read when first opened; which ones are open is remembered.
// Right-click a folder for "expand one level", "expand all" and "collapse all".

const EXPAND_ALL_LIMIT = 5000;  // entries: "expand all" stops there, a snapshot can hold millions
const STATUS_ORDER = { added: 0, modified: 1, changed: 2, removed: 3 };
const STATUS_LABEL = { added: "new", modified: "modified", changed: "changed inside", removed: "removed" };

const treeKey = () => `${state.snapshot}|${state.compare || ""}`;
const expandedKey = () => `restic-machine-open-${state.status?.repository || ""}`;

function cache() {
  if (state.cacheKey !== treeKey()) {
    state.cache = new Map();
    state.cacheKey = treeKey();
  }
  return state.cache;
}

function saveExpanded() {
  try {
    localStorage.setItem(expandedKey(), JSON.stringify([...state.expanded]));
  } catch {
    // not kept between visits, then
  }
}

function restoreExpanded() {
  try {
    const saved = JSON.parse(localStorage.getItem(expandedKey()) || "null");
    return Array.isArray(saved) ? new Set(saved) : null;
  } catch {
    return null;
  }
}

async function fetchTree(path, extra = {}) {
  const params = { snapshot: state.snapshot, path, depth: 1, ...extra };
  if (state.compare) params.compare = state.compare;
  const data = await api("/api/tree", params);
  if (data.snapshot !== state.snapshot) return data;
  for (const [dir, listing] of Object.entries(data.dirs)) cache().set(dir, listing);
  state.excludes = data.excludes;
  state.previous = data.previous;
  return data;
}

// Read what the tree shows: the root, then each open folder whose parent is open and has it.
async function loadTree() {
  const key = treeKey();
  if (!state.expanded) {
    state.expanded = restoreExpanded();
    state.autoOpen = !state.expanded;  // first visit: open folders that are alone in their parent
    state.expanded ??= new Set();
  }
  setNote("");
  try {
    let level = ["/"];
    while (level.length) {
      await Promise.all(level.filter((path) => !cache().has(path)).map((path) => fetchTree(path)));
      if (key !== treeKey()) return;  // another snapshot was chosen meanwhile
      const next = [];
      for (const path of level) {
        const dirs = (cache().get(path)?.entries || []).filter((e) => e.type === "dir");
        if (state.autoOpen && level.length === 1 && dirs.length === 1) state.expanded.add(joinPath(path, dirs[0].name));
        for (const dir of dirs) {
          const child = joinPath(path, dir.name);
          if (state.expanded.has(child)) next.push(child);
        }
      }
      level = next;
    }
  } catch (error) {
    setNote(error.message);
    return;
  }
  state.autoOpen = false;
  saveExpanded();
  writeHash(false);
  renderSnapshotInfo();
  renderTree();
}

function setNote(text) {
  $("#listing-note").hidden = !text;
  $("#listing-note").textContent = text;
}

// By name: folders, then hidden folders (.name), then files, then hidden files; each group in order.
const nameGroup = (e) => (e.type === "dir" ? 0 : 2) + (e.name.startsWith(".") ? 1 : 0);

function sortEntries(entries) {
  const { key, asc } = state.sort;
  const value = (e) => (key === "status" ? STATUS_ORDER[e.status] ?? 9
    : key === "delta" ? (e.size ?? 0) - (e.prev_size ?? 0) : e[key]);
  return [...entries].sort((a, b) => {
    if (key === "name" && nameGroup(a) !== nameGroup(b)) return nameGroup(a) - nameGroup(b);
    const va = value(a), vb = value(b);
    let order = 0;
    if (va == null || vb == null) order = va == null ? (vb == null ? 0 : 1) : -1;
    else if (typeof va === "string") order = va.localeCompare(vb, undefined, { sensitivity: "base" });
    else order = va - vb;
    return asc ? order : -order;
  });
}

function renderTree() {
  const rows = [];
  let removedShown = 0, removedTotal = 0;
  const walk = (path, depth, parentSize) => {
    const listing = cache().get(path);
    if (!listing) {
      rows.push(noteRow(depth, "Reading…"));
      return;
    }
    let entries = sortEntries(listing.entries);
    if (state.compare) entries = entries.filter((e) => e.status);  // only what differs
    for (const entry of entries) {
      const child = joinPath(path, entry.name);
      rows.push(entryRow(entry, child, depth, parentSize, false));
      if (entry.type === "dir" && state.expanded.has(child)) walk(child, depth + 1, entry.size);
    }
    removedTotal += listing.removed.length;
    const removed = state.compare || state.showRemoved ? sortEntries(listing.removed) : [];
    removedShown += removed.length;
    for (const entry of removed) rows.push(entryRow(entry, joinPath(path, entry.name), depth, parentSize, true));
    if (!entries.length && !removed.length) {
      rows.push(noteRow(depth, state.compare ? "No differences here." : "Empty folder."));
    }
  };
  walk("/", 0, current()?.size);
  $("#listing tbody").replaceChildren(...rows);
  $("#listing").classList.toggle("comparing", Boolean(state.compare));
  $("#removed-toggle").hidden = Boolean(state.compare);
  $("#removed-count").textContent = state.previous ? `(${count(removedTotal)} in the open folders)` : "";
  $("#show-removed").disabled = !state.previous;
  for (const th of document.querySelectorAll("#listing th[data-sort]")) {
    const sorted = th.dataset.sort === state.sort.key;
    th.classList.toggle("sorted", sorted);
    th.classList.toggle("asc", sorted && state.sort.asc);
  }
}

function noteRow(depth, text) {
  const cell = el("td", { colspan: 8, class: "muted" }, text);
  cell.style.paddingLeft = `${30 + depth * 18}px`;
  return el("tr", { class: "note" }, cell);
}

function entryRow(entry, path, depth, parentSize, removed) {
  const isDir = entry.type === "dir";
  const open = isDir && state.expanded.has(path);
  const toggle = isDir && !removed
    ? el("button", { type: "button", class: "twisty", "aria-expanded": open ? "true" : "false",
      "aria-label": `${open ? "Close" : "Open"} ${entry.name}`, onclick: () => toggleFolder(path) }, open ? "▾" : "▸")
    : el("span", { class: "twisty", "aria-hidden": "true" }, entry.type === "symlink" ? "↪" : "");
  const name = el("button", {
    type: "button", class: "link name",
    onclick: isDir && !removed ? () => toggleFolder(path) : () => openDetail(path),
  }, entry.name,
  entry.excluded ? el("span", { class: "badge excluded", title: `Excluded from the next backups by: ${entry.excluded}` },
    "excluded") : null);
  const nameCell = el("td", { class: "tree-name" }, el("span", { class: "tree-line" }, toggle, name));
  nameCell.style.paddingLeft = `${6 + depth * 18}px`;

  const share = parentSize ? Math.min(1, (entry.size || 0) / parentSize) : 0;
  const bar = el("span", { class: "sizebar", "aria-hidden": "true" }, el("i"));
  bar.firstChild.style.width = `${(share * 100).toFixed(1)}%`;
  const delta = removed ? -(entry.size || 0) : (entry.size || 0) - (entry.prev_size ?? 0);
  const status = removed ? "removed" : entry.status;

  const row = el("tr", {
    class: [isDir ? "dir" : "", removed ? "removed" : "", entry.excluded ? "excluded" : "",
      state.detailPath === path ? "selected" : ""].filter(Boolean).join(" ") || null,
    "data-path": path,
  },
  nameCell,
  el("td", { class: "num size" }, formatSize(entry.size), bar),
  el("td", { class: `num delta ${delta > 0 ? "up" : delta < 0 ? "down" : ""}` },
    delta ? `${delta > 0 ? "+" : "−"}${formatSize(Math.abs(delta))}` : ""),
  el("td", { class: "num" }, isDir ? count(entry.files) : ""),
  el("td", {}, formatDate(mtimeDate(entry.mtime_us))),
  el("td", {}, status ? el("span", { class: `badge ${status}` }, STATUS_LABEL[status]) : ""),
  el("td", { class: "actions-cell" }, el("button", {
    type: "button", class: "history", title: "Size over time", "aria-label": `Size of ${entry.name} over time`,
    onclick: () => openDetail(path),
  }, svg("svg", { width: 16, height: 16, viewBox: "0 0 16 16", "aria-hidden": "true" },
    svg("polyline", { points: "1,13 5,8 8,10 11,4 15,6", fill: "none", stroke: "currentColor", "stroke-width": 1.6 }))),
  removed || !state.excludes ? null : excludeButton(entry, path)));
  if (isDir && !removed) row.addEventListener("contextmenu", (event) => openMenu(event, path));
  return row;
}

function toggleFolder(path) {
  if (state.expanded.has(path)) state.expanded.delete(path);
  else state.expanded.add(path);
  loadTree();
}

// ---- right-click menu on a folder --------------------------------------------------------

function openMenu(event, path) {
  event.preventDefault();
  const menu = $("#tree-menu");
  const item = (label, action) => el("button", { type: "button", role: "menuitem", onclick: () => {
    closeMenu();
    action();
  } }, label);
  menu.replaceChildren(
    el("div", { class: "menu-title" }, path.slice(path.lastIndexOf("/") + 1) || "/"),
    item("Expand one level", () => expandOneLevel(path)),
    item("Expand all", () => expandAll(path)),
    item("Collapse all", () => collapseAll(path)),
    el("hr"),
    item("Size over time", () => openDetail(path)));
  menu.hidden = false;
  const { innerWidth, innerHeight } = window;
  const box = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(event.clientX, innerWidth - box.width - 8)}px`;
  menu.style.top = `${Math.min(event.clientY, innerHeight - box.height - 8)}px`;
  menu.querySelector("button")?.focus();
}

function closeMenu() {
  $("#tree-menu").hidden = true;
}

// The folder and its immediate subfolders open.
async function expandOneLevel(path) {
  state.expanded.add(path);
  try {
    await fetchTree(path, { depth: 2 });
  } catch (error) {
    setNote(error.message);
    return;
  }
  for (const dir of (cache().get(path)?.entries || []).filter((e) => e.type === "dir" && (e.status || !state.compare))) {
    state.expanded.add(joinPath(path, dir.name));
  }
  loadTree();
}

// Everything below the folder opens (when comparing, only where something changed).
async function expandAll(path) {
  setNote("Opening everything below…");
  let data;
  try {
    data = await fetchTree(path, { depth: 1000, limit: EXPAND_ALL_LIMIT, ...(state.compare ? { changed_only: 1 } : {}) });
  } catch (error) {
    setNote(error.message);
    return;
  }
  for (const dir of Object.keys(data.dirs)) state.expanded.add(dir);
  await loadTree();
  if (data.truncated) {
    setNote(`Stopped after about ${count(EXPAND_ALL_LIMIT)} entries: open the deeper folders one by one.`);
  }
}

function collapseAll(path) {
  for (const open of [...state.expanded]) {
    if (open === path || open.startsWith(`${path === "/" ? "" : path}/`)) state.expanded.delete(open);
  }
  loadTree();
}

// ---- excluding from the next backups ---------------------------------------------------

function excludeButton(entry, path) {
  const ownLine = entry.excluded === entry.native;  // a line for exactly this path: it can be taken out
  if (entry.excluded && !ownLine) {
    return el("button", { type: "button", class: "history", disabled: true,
      title: `Excluded by the pattern ${entry.excluded}`, "aria-label": `${entry.name} is excluded by a pattern` },
    excludeIcon());
  }
  return el("button", {
    type: "button", class: entry.excluded ? "history active" : "history",
    title: entry.excluded ? "Back up again from the next backup" : "Exclude from the next backups",
    "aria-label": `${entry.excluded ? "Include" : "Exclude"} ${entry.name}`,
    onclick: () => (entry.excluded ? includeAgain(entry) : exclude(entry, path)),
  }, excludeIcon());
}

const excludeIcon = () => svg("svg", { width: 16, height: 16, viewBox: "0 0 16 16", "aria-hidden": "true" },
  svg("circle", { cx: 8, cy: 8, r: 6, fill: "none", stroke: "currentColor", "stroke-width": 1.6 }),
  svg("line", { x1: 3.8, y1: 12.2, x2: 12.2, y2: 3.8, stroke: "currentColor", "stroke-width": 1.6 }));

async function exclude(entry, path) {
  const what = entry.type === "dir" ? "this folder and everything in it" : "this file";
  if (!confirm(`Exclude ${what} from the next backups?\n\n${entry.native}\n\n` +
    "The snapshots already made keep it (taking it out of them is a separate step that rewrites " +
    "them). The line goes into the exclude file of your backup job.")) return;
  try {
    await post("/api/excludes", { path });
  } catch (error) {
    showBanner(error.message, true);
    return;
  }
  state.cacheKey = null;  // every listing marks what is excluded
  loadTree();
}

async function includeAgain(entry) {
  if (!confirm(`Back up this again from the next backup?\n\n${entry.native}`)) return;
  try {
    await post("/api/excludes/remove", { pattern: entry.excluded });
  } catch (error) {
    showBanner(error.message, true);
    return;
  }
  state.cacheKey = null;
  loadTree();
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
  $("#stop").addEventListener("click", async () => {
    try {
      await post("/api/stop");
    } catch {
      // it may be gone already
    }
    stopped("restic-machine has stopped. You can close this tab, and start it again any time.");
  });
  $("#series").addEventListener("change", (event) => {
    state.series = Number(event.target.value);
    state.snapshot = null;
    state.compare = null;
    state.picked.clear();
    closeDetail();
    loadSnapshots();
  });
  $("#show-removed").addEventListener("change", (event) => {
    state.showRemoved = event.target.checked;
    renderTree();
  });
  for (const th of document.querySelectorAll("#listing th[data-sort]")) {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      state.sort = { key, asc: state.sort.key === key ? !state.sort.asc : key === "name" };
      renderTree();
    });
  }
  $("#detail-close").addEventListener("click", closeDetail);
  $("#bars").addEventListener("keydown", (event) => {
    const moves = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity };
    if (!(event.key in moves)) return;
    event.preventDefault();
    step(Math.max(-1e6, Math.min(1e6, moves[event.key])));
  });
  for (const name of TABS) $(`#tab-${name}`).addEventListener("click", () => showTab(name));
  window.addEventListener("popstate", () => {
    const { tab, snapshot, compare } = readHash();
    if (tab !== state.tab) {
      showTab(tab, false);
      if (tab !== "browse") return;
    }
    if (!snapshot) return;
    state.snapshot = snapshot;
    state.compare = compare;
    markBars();
    loadTree();
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest("#tree-menu")) closeMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenu();
  });
  window.addEventListener("scroll", closeMenu, { passive: true });
  settings.init({ ready: becameReady });
  backups.init();
  retention.init();
}

async function init() {
  bind();
  const { tab, snapshot, compare } = readHash();
  state.tab = tab;  // before loading, so the address keeps the tab meanwhile
  state.snapshot = snapshot;
  state.compare = compare;
  renderPickBar();
  await refreshStatus();
  if (state.status?.ready && !state.snapshots.length) await loadSnapshots();
  showTab(state.tab, false);  // the tab may have changed meanwhile (back button)
}

init();
