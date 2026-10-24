// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 David Maluenda
// The Repository page: the retention policy, shown, simulated with `restic forget --dry-run`
// (which only reads) and saved for `restic-machine forget` to apply.

import { $, api, count, el, formatDate, post, snapshotDate } from "./common.js";

const COUNTS = ["last", "hourly", "daily", "weekly", "monthly", "yearly"];
const DURATIONS = ["within", "within_hourly", "within_daily", "within_weekly", "within_monthly", "within_yearly"];
let saved = null;  // the policy as saved in the config file

const form = () => $("#policy-form");

export function init() {
  form().addEventListener("submit", (event) => {
    event.preventDefault();
    save();
  });
  form().addEventListener("input", markChanges);
  $("#policy-simulate").addEventListener("click", simulate);
  $("#policy-revert").addEventListener("click", () => {
    fill(saved);
    markChanges();
    simulate();
  });
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
  if (data.command) {
    simulate();
  } else {
    $("#simulation").replaceChildren(el("p", { class: "hint" },
      "No policy saved yet. Set the rules and press Simulate to see what they would keep."));
  }
}

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
  $("#policy-state").textContent = changed ? "Changed, not saved" : "";
}

async function simulate() {
  const policy = policyInForm();
  result("Asking restic (dry run, nothing is changed)…");
  try {
    const data = await post("/api/retention/simulate", { policy });
    result("");
    renderSimulation(data, same(policy, saved));
  } catch (error) {
    result(error.message, true);
  }
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
    simulate();
  } catch (error) {
    result(error.message, true);
  }
}

function result(text, error = false) {
  const box = $("#policy-result");
  box.textContent = text;
  box.classList.toggle("error", error);
}

function renderSimulation(data, isSaved) {
  const total = data.kept + data.removed;
  const summary = el("p", { class: "summary" },
    isSaved ? "With the saved policy, restic would now keep " : "With these rules (not saved), restic would keep ",
    el("strong", {}, count(data.kept)), ` of ${count(total)} snapshots and remove `,
    el("strong", { class: data.removed ? "danger" : null }, count(data.removed)), ".");
  const strip = el("div", { class: "retention-strip", "aria-hidden": "true" }, data.snapshots.map((s) => el("span", {
    class: s.keep ? "snap" : "snap removed",
    title: `${formatDate(snapshotDate(s))} · ${s.id.slice(0, 8)}\n${s.keep ? `kept: ${s.reasons.join(", ")}` : "would be removed"}`,
  }, el("span", { class: "bar" }))));
  const rows = [...data.snapshots].reverse().map((s) => el("tr", { class: s.keep ? null : "removed" },
    el("td", {}, formatDate(snapshotDate(s))),
    el("td", { class: "id" }, s.id.slice(0, 8)),
    el("td", {}, s.tags.join(", ")),
    el("td", {}, s.keep ? s.reasons.join(", ") : "would be removed")));
  const table = el("table", { class: "simulation-table" },
    el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Snapshot"), el("th", { scope: "col" }, "ID"),
      el("th", { scope: "col" }, "Tags"), el("th", { scope: "col" }, "Kept because"))),
    el("tbody", {}, rows));
  $("#simulation").replaceChildren(summary, strip, table);
}
