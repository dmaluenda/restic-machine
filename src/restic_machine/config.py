# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""User configuration. It lives outside the package, in the user's own directories.

Without a config file restic-machine relies on restic's own environment variables
(RESTIC_REPOSITORY, RESTIC_PASSWORD_FILE, ...). It never stores a password itself: only
where restic finds it (a file or a command).

The [retention] table is the snapshot retention policy. restic itself keeps none: a policy
is just what `restic forget` is given each time. restic-machine keeps it here, so the web
interface can show and simulate it and `restic-machine forget` (run by the backup job)
applies it.
"""

import copy
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "restic-machine"

# [restic] key in config.toml -> environment variable understood by restic
_RESTIC_KEYS = {
    "repository": "RESTIC_REPOSITORY",
    "repository_file": "RESTIC_REPOSITORY_FILE",
    "password_file": "RESTIC_PASSWORD_FILE",
    "password_command": "RESTIC_PASSWORD_COMMAND",
}
_PATH_KEYS = {"repository_file", "password_file"}
_PASSWORD_VARS = ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND")

# Retention rules, as `keep_<rule>` keys in [retention] and `--keep-<rule>` for restic forget.
RETENTION_COUNTS = ("last", "hourly", "daily", "weekly", "monthly", "yearly")
RETENTION_DURATIONS = ("within", "within_hourly", "within_daily", "within_weekly",
                       "within_monthly", "within_yearly")
_DURATION = re.compile(r"(\d+y)?(\d+m)?(\d+d)?(\d+h)?")
_GROUP_BY = {"host", "paths", "tags"}


class ConfigError(Exception):
    pass


def _user_dir(xdg_var: str, fallback: str) -> Path:
    value = os.environ.get(xdg_var)
    return Path(value) if value else Path.home() / fallback


def config_file() -> Path:
    """$RESTIC_MACHINE_CONFIG, else $XDG_CONFIG_HOME/restic-machine/config.toml
    (~/.config/restic-machine/config.toml when unset, on every OS)."""
    explicit = os.environ.get("RESTIC_MACHINE_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return _user_dir("XDG_CONFIG_HOME", ".config") / APP_NAME / "config.toml"


def default_data_dir() -> Path:
    """$RESTIC_MACHINE_DATA, else $XDG_CACHE_HOME/restic-machine (~/.cache/restic-machine)."""
    explicit = os.environ.get("RESTIC_MACHINE_DATA")
    if explicit:
        return Path(explicit).expanduser()
    return _user_dir("XDG_CACHE_HOME", ".cache") / APP_NAME


@dataclass
class Config:
    restic_binary: str = "restic"
    # Passed to restic on top of the current environment; None removes a variable.
    restic_env: dict[str, str | None] = field(default_factory=dict)
    data_dir: Path = field(default_factory=default_data_dir)
    forgotten_retention_days: int = 30
    retention: dict = field(default_factory=dict)  # see normalize_policy()
    excludes_file: Path | None = None  # the --exclude-file of the backup job
    name: str = ""  # what the user calls the repository (shown with its id)
    source: Path | None = None

    def _effective(self, var: str) -> str | None:
        if var in self.restic_env:
            return self.restic_env[var]
        return os.environ.get(var)

    @property
    def path(self) -> Path:
        return self.source or config_file()

    def repository_label(self) -> str:
        """What identifies the configured repository (to find its index while it's offline)."""
        if self._effective("RESTIC_REPOSITORY"):
            return self._effective("RESTIC_REPOSITORY")
        if self._effective("RESTIC_REPOSITORY_FILE"):
            return "file:" + self._effective("RESTIC_REPOSITORY_FILE")
        raise ConfigError(f"no repository configured: set RESTIC_REPOSITORY or `repository` in {self.path}")

    def check_password_source(self) -> None:
        # Without one, restic would wait for a password on stdin.
        if not any(self._effective(var) for var in _PASSWORD_VARS):
            raise ConfigError(
                "no password source configured: set RESTIC_PASSWORD_FILE or RESTIC_PASSWORD_COMMAND,"
                f" or `password_file` / `password_command` in {self.path}")

    def describe(self) -> dict:
        """The settings as the web page shows them. Values that come from the environment
        rather than the file are given apart, and no variable value from [restic.env]
        (it may hold backend credentials) ever leaves this process."""
        return {
            "file": str(self.path),
            "file_exists": self.path.exists(),
            "repository": self.restic_env.get("RESTIC_REPOSITORY") or "",
            "repository_from_env": os.environ.get("RESTIC_REPOSITORY") or "",
            "password_file": self.restic_env.get("RESTIC_PASSWORD_FILE") or "",
            "password_file_from_env": os.environ.get("RESTIC_PASSWORD_FILE") or "",
            "password_command": self.restic_env.get("RESTIC_PASSWORD_COMMAND") or "",
            "password_command_from_env": os.environ.get("RESTIC_PASSWORD_COMMAND") or "",
            "restic_binary": self.restic_binary,
            "data_dir": str(self.data_dir),
            "forgotten_retention_days": self.forgotten_retention_days,
            "excludes_file": str(self.excludes_file) if self.excludes_file else "",
            "name": self.name,
            "env_names": sorted(k for k, v in self.restic_env.items()
                                if v is not None and k not in _RESTIC_KEYS.values()),
        }

    def with_settings(self, values: dict) -> "Config":
        """A copy with the settings the web page changes. Empty means "not in the file"
        (restic's environment variable, if any, applies). The page runs on the same computer
        as restic and only for this user, so it may set what the command line can, including
        the restic program and the password command (both run programs)."""
        new = copy.deepcopy(self)
        for key in ("repository", "password_file", "password_command"):
            if key in values:
                var = _RESTIC_KEYS[key]
                value = str(values[key] or "").strip()
                if value:
                    new.restic_env[var] = os.path.expanduser(value) if key in _PATH_KEYS else value
                else:
                    new.restic_env.pop(var, None)
        if "restic_binary" in values:
            new.restic_binary = os.path.expanduser(str(values["restic_binary"] or "").strip()) or "restic"
        if "data_dir" in values:
            value = str(values["data_dir"] or "").strip()
            new.data_dir = Path(value).expanduser() if value else default_data_dir()
        if "name" in values:
            new.name = str(values["name"] or "").strip()
        if "excludes_file" in values:
            value = str(values["excludes_file"] or "").strip()
            new.excludes_file = Path(value).expanduser() if value else None
        if "forgotten_retention_days" in values:
            days = values["forgotten_retention_days"]
            if not isinstance(days, int) or isinstance(days, bool) or days < 0:
                raise ConfigError("forgotten_retention_days must be a whole number of days")
            new.forgotten_retention_days = days
        return new


def normalize_policy(raw: dict, where: str = "retention policy") -> dict:
    """Check a retention policy (the [retention] table, or what the web page sends) and keep
    only the rules that are set. `prune` defaults to true: forgetting alone frees nothing."""
    policy = {}
    for rule in RETENTION_COUNTS:
        value = raw.get(f"keep_{rule}")
        if value in (None, "", 0):
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{where}: keep_{rule} must be a whole number")
        policy[f"keep_{rule}"] = value
    for rule in RETENTION_DURATIONS:
        value = raw.get(f"keep_{rule}")
        if not value:
            continue
        if not isinstance(value, str) or not _DURATION.fullmatch(value.strip()):
            raise ConfigError(f"{where}: keep_{rule} must be a duration like 1y6m or 7d12h")
        policy[f"keep_{rule}"] = value.strip()
    tags = raw.get("keep_tag") or []
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
        raise ConfigError(f"{where}: keep_tag must be a list of tags")
    if tags:
        policy["keep_tag"] = tags
    group_by = raw.get("group_by") or ""
    if not isinstance(group_by, str) or (group_by.strip() and not set(group_by.strip().split(",")) <= _GROUP_BY):
        raise ConfigError(f"{where}: group_by must be made of host, paths and tags (e.g. host,paths)")
    if group_by.strip():
        policy["group_by"] = group_by.strip()
    prune = raw.get("prune", True)
    if not isinstance(prune, bool):
        raise ConfigError(f"{where}: prune must be true or false")
    policy["prune"] = prune
    return policy


def has_rules(policy: dict) -> bool:
    return any(key.startswith("keep_") for key in policy)


def forget_args(policy: dict) -> list[str]:
    """The `restic forget` options for a policy (without --prune, which is separate)."""
    args = []
    for rule in RETENTION_COUNTS + RETENTION_DURATIONS:
        if f"keep_{rule}" in policy:
            args += [f"--keep-{rule.replace('_', '-')}", str(policy[f"keep_{rule}"])]
    for tag in policy.get("keep_tag", []):
        args += ["--keep-tag", tag]
    if "group_by" in policy:
        args += ["--group-by", policy["group_by"]]
    return args


def load(path: Path | None = None) -> Config:
    """Read the config file; a missing default file just means "use the defaults"."""
    explicit = path is not None
    path = Path(path).expanduser() if explicit else config_file()
    if not path.exists():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return Config()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None

    cfg = Config(source=path)
    restic = raw.get("restic", {})
    if "binary" in restic:
        cfg.restic_binary = os.path.expanduser(str(restic["binary"]))
    for key, var in _RESTIC_KEYS.items():
        if key in restic:
            value = str(restic[key])
            cfg.restic_env[var] = os.path.expanduser(value) if key in _PATH_KEYS else value
    extra = restic.get("env", {})
    if not isinstance(extra, dict):
        raise ConfigError(f"{path}: [restic.env] must be a table of NAME = \"value\"")
    cfg.restic_env.update({str(k): str(v) for k, v in extra.items()})

    index = raw.get("index", {})
    if "data_dir" in index:
        cfg.data_dir = Path(str(index["data_dir"])).expanduser()
    if "forgotten_retention_days" in index:
        days = index["forgotten_retention_days"]
        if not isinstance(days, int) or days < 0:
            raise ConfigError(f"{path}: forgotten_retention_days must be a whole number of days")
        cfg.forgotten_retention_days = days
    if "retention" in raw:
        cfg.retention = normalize_policy(raw["retention"], f"{path} [retention]")
    cfg.name = str(raw.get("repository", {}).get("name", "")).strip()
    backup = raw.get("backup", {})
    if backup.get("excludes_file"):
        cfg.excludes_file = Path(str(backup["excludes_file"])).expanduser()
    return cfg


def _toml(value) -> str:
    # JSON strings, numbers, booleans and arrays of strings are valid TOML values too.
    return json.dumps(value, ensure_ascii=False)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml(key)


def dump(cfg: Config) -> str:
    """The config file for `cfg`, with only what differs from the defaults."""
    lines = ["# restic-machine configuration. Written by restic-machine (its Settings page);",
             "# you may edit it by hand too. See config.example.toml for every setting.", ""]
    if cfg.name:
        lines += ["[repository]", f"name = {_toml(cfg.name)}", ""]
    restic = []
    if cfg.restic_binary != "restic":
        restic.append(f"binary = {_toml(cfg.restic_binary)}")
    for key, var in _RESTIC_KEYS.items():
        if cfg.restic_env.get(var):
            restic.append(f"{key} = {_toml(cfg.restic_env[var])}")
    extra = {k: v for k, v in cfg.restic_env.items() if v is not None and k not in _RESTIC_KEYS.values()}
    if restic or extra:
        lines += ["[restic]", *restic, ""]
    if extra:
        lines += ["[restic.env]", *(f"{_toml_key(k)} = {_toml(v)}" for k, v in sorted(extra.items())), ""]
    index = []
    if cfg.data_dir != default_data_dir():
        index.append(f"data_dir = {_toml(str(cfg.data_dir))}")
    if cfg.forgotten_retention_days != Config.forgotten_retention_days:
        index.append(f"forgotten_retention_days = {cfg.forgotten_retention_days}")
    if index:
        lines += ["[index]", *index, ""]
    if cfg.excludes_file:
        lines += ["[backup]", f"excludes_file = {_toml(str(cfg.excludes_file))}", ""]
    if cfg.retention:
        lines += ["# Applied by `restic-machine forget` (e.g. from the backup job)", "[retention]",
                  *(f"{key} = {_toml(value)}" for key, value in cfg.retention.items()), ""]
    return "\n".join(lines)


def save(cfg: Config) -> Path:
    path = cfg.path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dump(cfg), encoding="utf-8")
    os.replace(tmp, path)
    cfg.source = path
    return path
