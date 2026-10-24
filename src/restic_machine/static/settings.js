// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 David Maluenda
// The Settings page: where the repository is, how restic gets its password, where the index lives.

import { $, api, post } from "./common.js";

const FIELDS = ["name", "repository", "password_file", "password_command", "restic_binary", "data_dir",
  "forgotten_retention_days", "excludes_file"];
const FROM_ENV = { repository: "RESTIC_REPOSITORY", password_file: "RESTIC_PASSWORD_FILE",
  password_command: "RESTIC_PASSWORD_COMMAND" };
let onReady = () => {};

const form = () => $("#settings-form");

export function init(options) {
  onReady = options.ready;
  form().addEventListener("submit", (event) => {
    event.preventDefault();
    save();
  });
  $("#settings-test").addEventListener("click", test);
}

export async function load() {
  try {
    fill(await api("/api/settings"));
  } catch (error) {
    result(error.message, true);
  }
}

function fill(data) {
  const fields = form().elements;
  for (const name of FIELDS) fields[name].value = data[name] ?? "";
  if (data.restic_binary === "restic") fields.restic_binary.value = "";
  for (const [name, variable] of Object.entries(FROM_ENV)) {
    const fromEnv = data[`${name}_from_env`];
    fields[name].placeholder = fromEnv || fields[name].dataset.placeholder || "";
    $(`[data-env="${name}"]`).textContent = fromEnv && !data[name]
      ? `Empty here, so restic uses the ${variable} environment variable: ${fromEnv}` : "";
  }
  $("#settings-file").textContent = data.file + (data.file_exists ? "" : " (not created yet)");
  $("#settings-env").textContent = data.env_names.length
    ? `; it also gives restic ${data.env_names.join(", ")} (values not shown)` : "";

  const status = $("#settings-state");
  status.classList.toggle("warning", !data.ready);
  status.textContent = data.ready
    ? `Ready: repository ${data.repository_id.slice(0, 8)}.`
    : `restic-machine can't open the repository yet: ${data.problem || "it isn't configured"}. ` +
      "Say where it is and how restic gets its password, then Test and Save.";
}

function values() {
  const fields = form().elements;
  const out = {};
  for (const name of FIELDS) out[name] = fields[name].value.trim();
  out.forgotten_retention_days = out.forgotten_retention_days === "" ? 30 : Number(out.forgotten_retention_days);
  return out;
}

async function test() {
  result("Testing…");
  try {
    const answer = await post("/api/settings/test", values());
    result(answer.ok ? `It works: repository ${answer.repository_id.slice(0, 8)}.` : answer.error, !answer.ok);
  } catch (error) {
    result(error.message, true);
  }
}

async function save() {
  result("Saving…");
  try {
    const data = await post("/api/settings", values());
    fill(data);
    result(data.ready ? "Saved." : "Saved, but the repository can't be opened with these settings.", !data.ready);
    if (data.ready) onReady();
  } catch (error) {
    result(error.message, true);
  }
}

function result(text, error = false) {
  const box = $("#settings-result");
  box.textContent = text;
  box.classList.toggle("error", error);
}
