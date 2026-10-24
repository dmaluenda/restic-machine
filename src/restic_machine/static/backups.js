// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 David Maluenda
// The Backups block of the Retention page: what is backed up, when, and the Windows task that
// does it on its own (with the page closed and nobody logged in).

import { $, api, el, formatDate, post } from "./common.js";

let data = null;
const form = () => $("#backup-form");

export function init() {
  form().addEventListener("submit", (event) => {
    event.preventDefault();
    save(false);
  });
  form().addEventListener("input", summarize);
  $("#backup-install").addEventListener("click", () => save(true));
  $("#backup-run").addEventListener("click", runNow);
}

export async function load() {
  try {
    data = await api("/api/backup");
  } catch (error) {
    result(error.message, true);
    return;
  }
  fill();
  renderState();
}

function fill() {
  const f = form().elements, s = data.settings;
  f.every_hours.value = s.schedule.every_hours;
  f.from.value = s.schedule.from;
  f.to.value = s.schedule.to;
  f.also_at.value = s.schedule.also_at.join(", ");
  f.prune_at.value = s.schedule.prune_at;
  f.sources.value = s.sources.join("\n");
  f.use_fs_snapshot.checked = s.use_fs_snapshot;
  f.share_path.value = s.share.path;
  f.share_user.value = s.share.user;
  f.share_password_file.value = s.share.password_file;
  f.share_by_ip.checked = s.share.by_ip;
  $("#backup-excludes").textContent = data.excludes_file || "none set";
  summarize();
  $("#backup-log").textContent = data.log.length ? data.log.join("\n") : "No backup run by restic-machine yet.";
}

function values() {
  const f = form().elements;
  return {
    sources: f.sources.value,
    use_fs_snapshot: f.use_fs_snapshot.checked,
    share: { path: f.share_path.value.trim(), user: f.share_user.value.trim(),
      password_file: f.share_password_file.value.trim(), by_ip: f.share_by_ip.checked },
    schedule: { every_hours: Number(f.every_hours.value || 0), from: f.from.value, to: f.to.value,
      also_at: f.also_at.value, prune_at: f.prune_at.value },
  };
}

function summarize() {
  const s = values().schedule;
  const parts = [];
  if (s.every_hours) parts.push(`${s.every_hours === 1 ? "every hour" : `every ${s.every_hours} hours`} from ${s.from} to ${s.to}`);
  const also = s.also_at.split(/[,\s]+/).filter(Boolean);
  if (also.length) parts.push(`at ${also.join(", ")}`);
  $("#schedule-summary").textContent = parts.length
    ? `Backups ${parts.join(", and ")}; the one at ${s.prune_at.slice(0, 2)}:xx also forgets and prunes (or the next one, after 30 hours without).`
    : "No backups at all with this schedule.";
}

function renderState() {
  const box = $("#backup-state");
  const { ours, others, supported } = data.tasks;
  const parts = [];
  if (!supported) {
    parts.push("Scheduling backups is only handled on Windows so far: run `restic-machine backup` from your own scheduler.");
  } else if (ours) {
    parts.push(el("strong", {}, ours.state === "Disabled" ? "Scheduled, but the task is disabled. " : "Scheduled. "),
      `Task "${data.task_name}": last run ${ours.last_run ? formatDate(new Date(ours.last_run)) : "—"}`,
      ours.last_run ? ` (${ours.last_result === 0 ? "OK" : `result ${ours.last_result}`})` : "",
      `, next ${ours.next_run ? formatDate(new Date(ours.next_run)) : "—"}.`);
  } else {
    parts.push(el("strong", {}, "Not scheduled by restic-machine yet. "),
      "“Save and schedule” creates a Windows task (it asks for administrator rights once).");
  }
  if (data.tasks_error) parts.push(el("span", { class: "danger block" }, data.tasks_error));
  const list = $("#backup-others");
  list.replaceChildren(...others.map((task) => el("label", { class: "check" },
    el("input", { type: "checkbox", name: "disable", value: task.name, checked: task.state !== "Disabled" }),
    el("span", {}, `Disable the task “${task.name}” when scheduling (${task.state.toLowerCase()}, `,
      `next ${task.next_run ? formatDate(new Date(task.next_run)) : "—"}): it runs restic too — `,
      el("code", {}, task.action), ". It stays, so you can turn it back on."))));
  list.hidden = !others.length;
  box.classList.toggle("warning", !ours && supported);
  box.replaceChildren(...parts);
  $("#backup-install").disabled = !supported;
  $("#backup-run").disabled = !ours || ours.state === "Disabled";
}

async function save(install) {
  try {
    data = await post("/api/backup/settings", { settings: values() });
  } catch (error) {
    result(error.message, true);
    return;
  }
  fill();
  renderState();
  if (!install) {
    result("Saved. The schedule itself changes when you press “Save and schedule”.");
    return;
  }
  const disable = [...document.querySelectorAll("#backup-others input[name=disable]:checked")].map((x) => x.value);
  result("Windows is asking for administrator rights: answer its prompt…");
  try {
    data = await post("/api/backup/install", { disable });
  } catch (error) {
    result(error.message, true);
    return;
  }
  fill();
  renderState();
  result(data.ok ? "Scheduled." : data.error, !data.ok);
}

async function runNow() {
  try {
    const answer = await post("/api/backup/run");
    result(answer.ok ? "Started: the log below fills in as it goes (reload this page to follow it)." : answer.error, !answer.ok);
  } catch (error) {
    result(error.message, true);
  }
}

function result(text, error = false) {
  const box = $("#backup-result");
  box.textContent = text;
  box.classList.toggle("error", error);
}
