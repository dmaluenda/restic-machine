// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 David Maluenda
// Helpers shared by every page of the interface.

export const $ = (selector, root = document) => root.querySelector(selector);

// `signal` (an AbortController's) lets a newer request cancel this one; the server stops too.
export async function api(path, params = {}, { signal } = {}) {
  const url = new URL(path, location.origin);
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  const response = await fetch(url, { credentials: "same-origin", signal });
  const data = await response.json().catch(() => ({ error: response.statusText }));
  if (!response.ok) throw Object.assign(new Error(data.error || response.statusText), { status: response.status });
  return data;
}

// Actions are POST with our own header (the server refuses them without it), JSON body optional.
export async function post(path, body) {
  const headers = { "X-Restic-Machine": "1" };
  const init = { method: "POST", credentials: "same-origin", headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({ error: response.statusText }));
  if (!response.ok) throw Object.assign(new Error(data.error || response.statusText), { status: response.status });
  return data;
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  setAttributes(node, attrs);
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
}

export function svg(tag, attrs = {}, ...children) {
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

export function formatSize(bytes) {
  if (bytes == null) return "";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return unit === 0 ? `${value} B` : `${value.toFixed(value < 10 ? 2 : 1)} ${units[unit]}`;
}

export const count = (n) => (n == null ? "" : n.toLocaleString());
const pad = (n) => String(n).padStart(2, "0");
export const formatDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
export const formatDate = (d) => (d ? `${formatDay(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}` : "");
// restic writes nanoseconds; keep milliseconds so every browser parses it
export const snapshotDate = (s) => new Date(s.time.replace(/(\.\d{3})\d+/, "$1"));
export const mtimeDate = (us) => (us == null ? null : new Date(us / 1000));
