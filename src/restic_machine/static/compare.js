// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 David Maluenda
// The compare window: two versions of a text file side by side, like a merge tool. Each column
// picks its version: any snapshot that had the file (one entry per distinct version), or the
// file on disk now. Long runs of unchanged lines fold; click one to unfold it.

import { $, api, el, formatDate, formatSize, snapshotDate } from "./common.js";

const CONTEXT = 3;  // unchanged lines kept on each side of a change

// Text files, by extension (and a few names without one): what is worth comparing line by line.
const TEXT = new Set(("txt md markdown rst tex bib sty cls log csv tsv json jsonl yaml yml toml ini cfg conf " +
  "properties env xml html htm css scss less svg js mjs cjs ts tsx jsx vue py pyw pyi ipynb r rmd jl m " +
  "c h cc cpp cxx hpp hh cs java kt kts go rs swift scala rb php pl lua sh bash zsh fish ps1 psm1 psd1 " +
  "bat cmd sql gradle cmake mk make dockerfile gitignore gitattributes editorconfig srt vtt reg").split(" "));
const TEXT_NAMES = new Set(["makefile", "dockerfile", "readme", "license", "notice", "changelog"]);
export const TEXT_LIMIT = 4 * 1024 * 1024;  // as the server's

export function isText(entry) {
  if (entry.type !== "file" || (entry.size ?? 0) > TEXT_LIMIT) return false;
  const name = entry.name.toLowerCase();
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? TEXT.has(name.slice(dot + 1)) : TEXT_NAMES.has(name);
}

let shown = null;      // { path, versions }
let loading = null;    // the AbortController of the comparison being read
let at = -1;           // the change last jumped to

export function initCompare() {
  $("#compare-close").addEventListener("click", closeCompare);
  $("#compare-left").addEventListener("change", compare);
  $("#compare-right").addEventListener("change", compare);
  $("#compare-prev").addEventListener("click", () => jump(-1));
  $("#compare-next").addEventListener("click", () => jump(1));
  document.addEventListener("keydown", (event) => {
    if ($("#compare").hidden) return;
    if (event.key === "Escape") closeCompare();
    else if (event.key === "F7" || (event.altKey && event.key === "ArrowDown")) jump(1, event);
    else if ((event.shiftKey && event.key === "F7") || (event.altKey && event.key === "ArrowUp")) jump(-1, event);
  });
}

// `snapshot`: the version shown on the left at first; the right one is the file on disk now.
export async function openCompare(path, snapshot) {
  $("#compare").hidden = false;
  $("#compare-title").textContent = path.slice(path.lastIndexOf("/") + 1);
  $("#compare-path").textContent = path;
  $("#compare-summary").textContent = "Reading…";
  $("#compare-table tbody").replaceChildren();
  let points;
  try {
    points = (await api("/api/history", { snapshot, path })).points;
  } catch (error) {
    $("#compare-summary").textContent = error.message;
    return;
  }
  // One entry per distinct version: consecutive snapshots with the same size and time are one.
  const versions = [];
  for (const p of points) {
    const last = versions.at(-1);
    if (!p.present) {
      if (last) last.closed = true;
      continue;
    }
    if (last && !last.closed && last.size === p.size && last.mtime_us === p.mtime_us) {
      last.until = p;
      last.ids.push(p.snapshot);
    } else versions.push({ from: p, until: null, size: p.size, mtime_us: p.mtime_us, ids: [p.snapshot] });
  }
  shown = { path, versions };
  const options = () => [
    el("option", { value: "disk" }, "On disk now"),
    ...[...versions].reverse().map((v) => el("option", { value: v.from.snapshot },
      formatDate(snapshotDate(v.from)) + (v.until ? ` … ${formatDate(snapshotDate(v.until))}` : "") +
      ` · ${formatSize(v.size)}`)),
  ];
  const chosen = versions.find((v) => v.ids.includes(snapshot));
  $("#compare-left").replaceChildren(...options());
  $("#compare-right").replaceChildren(...options());
  $("#compare-left").value = chosen ? chosen.from.snapshot : versions.at(-1)?.from.snapshot ?? "disk";
  $("#compare-right").value = "disk";
  compare();
}

function closeCompare() {
  loading?.abort();
  $("#compare").hidden = true;
  shown = null;
}

async function compare() {
  if (!shown) return;
  loading?.abort();
  const controller = new AbortController();
  loading = controller;
  const left = $("#compare-left").value, right = $("#compare-right").value;
  $("#compare-summary").textContent = "Comparing…";
  $("#compare-body").classList.add("loading");
  let data;
  try {
    data = await api("/api/diff", { path: shown.path, left, right }, { signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) return;
    $("#compare-summary").textContent = error.message;
    $("#compare-table tbody").replaceChildren();
    return;
  } finally {
    if (loading === controller) $("#compare-body").classList.remove("loading");
  }
  const notes = [];
  if (data.missing.left) notes.push("not there on the left");
  if (data.missing.right) notes.push(right === "disk" ? "not on disk now" : "not there on the right");
  $("#compare-summary").textContent = [
    data.changes ? `${data.changes} change${data.changes > 1 ? "s" : ""}` : "identical", ...notes].join(" · ");
  render(data.rows);
}

// Unchanged runs longer than 2 × CONTEXT + 1 lines fold, keeping CONTEXT lines next to each change.
function render(rows) {
  const out = [];
  let change = 0;
  for (let i = 0; i < rows.length;) {
    if (rows[i][0] !== "same") {
      out.push(rowFor(rows[i], change++));
      i++;
      while (i < rows.length && rows[i][0] !== "same") out.push(rowFor(rows[i++]));
      continue;
    }
    let end = i;
    while (end < rows.length && rows[end][0] === "same") end++;
    const keepBefore = i === 0 ? 0 : CONTEXT, keepAfter = end === rows.length ? 0 : CONTEXT;
    if (end - i > keepBefore + keepAfter + 1) {
      for (let k = i; k < i + keepBefore; k++) out.push(rowFor(rows[k]));
      out.push(foldRow(rows.slice(i + keepBefore, end - keepAfter)));
      for (let k = end - keepAfter; k < end; k++) out.push(rowFor(rows[k]));
    } else for (let k = i; k < end; k++) out.push(rowFor(rows[k]));
    i = end;
  }
  $("#compare-table tbody").replaceChildren(...out);
  at = -1;
  $("#compare-position").textContent = "";
  $("#compare-body").scrollTop = 0;
  if (change) jump(1);
}

function rowFor([kind, leftNo, leftText, rightNo, rightText], change = null) {
  let left = leftText, right = rightText;
  if (kind === "changed") [left, right] = highlight(leftText, rightText);
  return el("tr", { class: kind, ...(change != null ? { "data-change": change } : {}) },
    el("td", { class: "no" }, leftNo ?? ""), el("td", { class: "text left" }, left ?? ""),
    el("td", { class: "no" }, rightNo ?? ""), el("td", { class: "text right" }, right ?? ""));
}

function foldRow(hidden) {
  const row = el("tr", { class: "fold" }, el("td", { colspan: 4 },
    el("button", { type: "button", class: "link", onclick: () => row.replaceWith(...hidden.map((r) => rowFor(r))) },
      `⋯ ${hidden.length.toLocaleString()} unchanged lines`)));
  return row;
}

// What differs inside a changed line: everything between the common start and the common end.
function highlight(a, b) {
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let end = 0;
  while (end < a.length - start && end < b.length - start && a[a.length - 1 - end] === b[b.length - 1 - end]) end++;
  const mark = (s) => [s.slice(0, start), el("mark", {}, s.slice(start, s.length - end)), s.slice(s.length - end)];
  return [el("span", {}, ...mark(a)), el("span", {}, ...mark(b))];
}

function jump(direction, event) {
  event?.preventDefault();
  const changes = [...document.querySelectorAll("#compare-table tr[data-change]")];
  if (!changes.length) return;
  at = Math.max(0, Math.min(changes.length - 1, at + direction));
  for (const row of changes) row.classList.toggle("current", Number(row.dataset.change) === at);
  changes[at].scrollIntoView({ block: "center" });
  $("#compare-position").textContent = `${at + 1} / ${changes.length}`;
}
