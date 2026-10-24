// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 David Maluenda
// The Retention page: the policy (saved for `restic-machine forget` to apply), a calendar of
// what it leaves alive on any day, and a check against restic itself (a dry run).

import { $, api, count, el, formatDate, formatDay, post, snapshotDate } from "./common.js";

const COUNTS = ["last", "hourly", "daily", "weekly", "monthly", "yearly"];
const DURATIONS = ["within", "within_hourly", "within_daily", "within_weekly", "within_monthly", "within_yearly"];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let saved = null;       // the policy as saved in the config file
let calendar = null;    // the last /api/retention/calendar answer
let selectedDay = null; // "YYYY-MM-DD" shown under the calendar
let refreshTimer = null;

const form = () => $("#policy-form");
const today = () => formatDay(new Date());

export function init() {
  form().addEventListener("submit", (event) => {
    event.preventDefault();
    save();
  });
  form().addEventListener("input", () => {
    markChanges();
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(loadCalendar, 500);
  });
  $("#policy-revert").addEventListener("click", () => {
    fill(saved);
    markChanges();
    loadCalendar();
  });
  $("#policy-simulate").addEventListener("click", checkWithRestic);
  $("#calendar-date").addEventListener("change", loadCalendar);
  $("#calendar-today").addEventListener("click", () => setDay(today()));
  for (const button of document.querySelectorAll("[data-shift]")) {
    button.addEventListener("click", () => {
      const day = new Date(`${$("#calendar-date").value || today()}T12:00:00`);
      day.setDate(day.getDate() + Number(button.dataset.shift));
      setDay(formatDay(day) < today() ? today() : formatDay(day));
    });
  }
}

export async function load() {
  let data;
  try {
    data = await api("/api/retention");
  } catch (error) {
    result(error.message, true);
    return;
  }
  saved = data.policy;
  fill(saved);
  $("#policy-command").textContent = data.command || "—";
  markChanges();
  if (!$("#calendar-date").value) $("#calendar-date").value = today();
  $("#calendar-date").min = today();
  loadCalendar();
}

// ---- the policy form -------------------------------------------------------------------

function fill(policy) {
  const fields = form().elements;
  for (const rule of [...COUNTS, ...DURATIONS]) fields[`keep_${rule}`].value = policy[`keep_${rule}`] ?? "";
  fields.keep_tag.value = (policy.keep_tag || []).join(", ");
  fields.group_by.value = policy.group_by || "";
  fields.prune.checked = policy.prune ?? true;
  if (DURATIONS.some((rule) => policy[`keep_${rule}`]) || policy.group_by) $("#policy-more").open = true;
}

function policyInForm() {
  const fields = form().elements;
  const policy = {};
  for (const rule of COUNTS) {
    const value = Number(fields[`keep_${rule}`].value || 0);
    if (value) policy[`keep_${rule}`] = value;
  }
  for (const rule of DURATIONS) {
    const value = fields[`keep_${rule}`].value.trim();
    if (value) policy[`keep_${rule}`] = value;
  }
  const tags = fields.keep_tag.value.split(",").map((tag) => tag.trim()).filter(Boolean);
  if (tags.length) policy.keep_tag = tags;
  if (fields.group_by.value.trim()) policy.group_by = fields.group_by.value.trim();
  policy.prune = fields.prune.checked;
  return policy;
}

function same(a, b) {
  const canonical = (policy) => JSON.stringify(Object.keys(policy || {}).sort().map((key) => [key, policy[key]]));
  return canonical(a) === canonical(b);
}

function markChanges() {
  const changed = saved != null && !same(policyInForm(), saved);
  $("#policy-revert").disabled = !changed;
  $("#policy-state").textContent = changed ? "Changed, not saved: the calendar shows the changes" : "";
}

async function save() {
  const policy = policyInForm();
  if (!confirm("Save this retention policy? Your backup job will apply it, deleting the snapshots it " +
               "doesn't keep, the next time it runs `restic-machine forget`.")) return;
  try {
    const data = await post("/api/retention", { policy });
    saved = data.policy;
    fill(saved);
    $("#policy-command").textContent = data.command || "—";
    markChanges();
    result("Saved.");
  } catch (error) {
    result(error.message, true);
  }
}

function result(text, error = false) {
  const box = $("#policy-result");
  box.textContent = text;
  box.classList.toggle("error", error);
}

// ---- the calendar ----------------------------------------------------------------------

function setDay(day) {
  $("#calendar-date").value = day;
  loadCalendar();
}

async function loadCalendar() {
  const policy = policyInForm();
  const summary = $("#calendar-summary");
  if (!Object.keys(policy).some((key) => key.startsWith("keep_"))) {
    summary.textContent = "Set at least one rule to see what it keeps.";
    $("#calendar").replaceChildren();
    $("#calendar-day").replaceChildren();
    return;
  }
  summary.textContent = "Working it out…";
  try {
    calendar = await post("/api/retention/calendar", { policy, date: $("#calendar-date").value || today() });
  } catch (error) {
    summary.textContent = error.message;
    return;
  }
  renderCalendar();
}

function renderCalendar() {
  const data = calendar;
  const days = new Map();  // "YYYY-MM-DD" -> snapshots made that day
  for (const s of data.snapshots) {
    const day = s.time.slice(0, 10);  // the snapshot's own date, as restic counts its days
    if (!days.has(day)) days.set(day, []);
    days.get(day).push(s);
  }
  const realTotal = data.real_kept + data.real_removed;
  const isToday = data.date === data.today;
  $("#calendar-summary").replaceChildren(el("span", {},
    isToday ? "Now, if it were applied, " : `On ${data.date}, `,
    el("strong", {}, count(data.real_kept)), ` of the ${count(realTotal)} snapshots there are now would be left`,
    isToday ? null : [", plus ", el("strong", {}, count(data.future_kept)), " of the backups made until then"],
    data.real_removed ? [" (", el("strong", { class: "danger" }, count(data.real_removed)), " gone)."] : ".",
    data.schedule.length ? el("span", { class: "hint block" },
      `Backups to come assumed at ${data.schedule.join(", ")} every day, the times of the last days.`) : null,
    data.grouping_differs ? el("span", { class: "hint block danger" },
      "The calendar groups snapshots by host and paths; your policy groups them otherwise, so trust the check with restic below.") : null));

  const first = [...days.keys()].sort()[0] || data.today;
  const months = [];
  for (let cursor = new Date(`${first.slice(0, 7)}-01T12:00:00`); formatDay(cursor) <= data.date;
    cursor.setMonth(cursor.getMonth() + 1)) {
    months.push(renderMonth(cursor.getFullYear(), cursor.getMonth(), days, data));
  }
  $("#calendar").replaceChildren(...months);
  if (!selectedDay || !days.has(selectedDay)) selectedDay = days.has(data.date) ? data.date : null;
  renderDay(days);
}

function renderMonth(year, month, days, data) {
  const title = new Date(year, month, 1).toLocaleDateString(undefined, { year: "numeric", month: "long" });
  const cells = WEEKDAYS.map((name) => el("div", { class: "weekday" }, name));
  const offset = (new Date(year, month, 1).getDay() + 6) % 7;  // Monday first
  for (let i = 0; i < offset; i++) cells.push(el("div", { class: "blank" }));
  const length = new Date(year, month + 1, 0).getDate();
  for (let d = 1; d <= length; d++) {
    const key = `${year}-${String(month + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const made = days.get(key) || [];
    const kept = made.filter((s) => s.keep).length;
    const future = made.length > 0 && made.every((s) => s.projected);
    const classes = ["day",
      made.length ? (kept === made.length ? "kept-all" : kept ? "kept-some" : "kept-none") : "",
      future ? "future" : "", key === data.today ? "today" : "", key === data.date ? "chosen" : "",
      key > data.date ? "after" : "", key === selectedDay ? "selected" : ""].filter(Boolean).join(" ");
    const label = made.length ? `${key}: ${kept} of ${made.length} snapshots kept` : key;
    cells.push(el("button", {
      type: "button", class: classes, title: label, "aria-label": label, disabled: !made.length,
      onclick: () => { selectedDay = key; renderCalendar(); },
    }, el("span", { class: "number" }, String(d)),
    made.length ? el("span", { class: "tally" }, `${kept}/${made.length}`) : null));
  }
  return el("div", { class: "month" }, el("div", { class: "month-title" }, title), el("div", { class: "grid" }, cells));
}

function renderDay(days) {
  const box = $("#calendar-day");
  if (!selectedDay) {
    box.replaceChildren(el("p", { class: "hint" }, "Choose a day in the calendar to see its snapshots."));
    return;
  }
  const rows = days.get(selectedDay).map((s) => el("tr", { class: s.keep ? null : "removed" },
    el("td", {}, formatDate(snapshotDate(s))),
    el("td", { class: "id" }, s.projected ? "to come" : s.id.slice(0, 8)),
    el("td", {}, s.tags.join(", ")),
    el("td", {}, s.keep ? s.reasons.join(", ") : `gone by ${calendar.date}`)));
  box.replaceChildren(el("h3", {}, `Snapshots of ${selectedDay}, as of ${calendar.date}`),
    el("table", { class: "simulation-table" },
      el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Snapshot"), el("th", { scope: "col" }, "ID"),
        el("th", { scope: "col" }, "Tags"), el("th", { scope: "col" }, "Kept because"))),
      el("tbody", {}, rows)));
}

// ---- the check with restic itself -------------------------------------------------------

async function checkWithRestic() {
  const policy = policyInForm();
  const box = $("#check-result");
  box.textContent = "Asking restic (dry run, nothing is changed)…";
  box.classList.remove("error");
  try {
    renderCheck(await post("/api/retention/simulate", { policy }));
  } catch (error) {
    box.textContent = error.message;
    box.classList.add("error");
  }
}

function renderCheck(data) {
  // Compare with the calendar for today, which uses restic's rules as copied in restic-machine.
  let agreement = "";
  if (calendar && calendar.date === calendar.today) {
    const ours = new Map(calendar.snapshots.filter((s) => !s.projected).map((s) => [s.id, s.keep]));
    const differ = data.snapshots.filter((s) => ours.get(s.id) !== s.keep).length;
    agreement = differ ? ` The calendar disagrees on ${differ} snapshot(s): trust restic.` : " The calendar agrees.";
  }
  $("#check-result").replaceChildren("restic would now keep ", el("strong", {}, count(data.kept)),
    ` of ${count(data.kept + data.removed)} snapshots and remove `,
    el("strong", { class: data.removed ? "danger" : null }, count(data.removed)), `.${agreement}`);
  const rows = [...data.snapshots].reverse().map((s) => el("tr", { class: s.keep ? null : "removed" },
    el("td", {}, formatDate(snapshotDate(s))),
    el("td", { class: "id" }, s.id.slice(0, 8)),
    el("td", {}, s.tags.join(", ")),
    el("td", {}, s.keep ? s.reasons.join(", ") : "would be removed")));
  $("#simulation").replaceChildren(el("table", { class: "simulation-table" },
    el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Snapshot"), el("th", { scope: "col" }, "ID"),
      el("th", { scope: "col" }, "Tags"), el("th", { scope: "col" }, "Kept because"))),
    el("tbody", {}, rows)));
}
