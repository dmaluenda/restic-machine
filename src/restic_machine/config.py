# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""User configuration. It lives outside the package, in the user's own directories.

Without a config file restic-machine relies on restic's own environment variables
(RESTIC_REPOSITORY, RESTIC_PASSWORD_FILE, ...). It never stores a password itself.
"""

import os
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
    source: Path | None = None

    def _effective(self, var: str) -> str | None:
        if var in self.restic_env:
            return self.restic_env[var]
        return os.environ.get(var)

    def repository_label(self) -> str:
        """What identifies the configured repository (to find its index while it's offline)."""
        if self._effective("RESTIC_REPOSITORY"):
            return self._effective("RESTIC_REPOSITORY")
        if self._effective("RESTIC_REPOSITORY_FILE"):
            return "file:" + self._effective("RESTIC_REPOSITORY_FILE")
        where = self.source or config_file()
        raise ConfigError(f"no repository configured: set RESTIC_REPOSITORY or `repository` in {where}")

    def check_password_source(self) -> None:
        # Without one, restic would wait for a password on stdin.
        if not any(self._effective(var) for var in _PASSWORD_VARS):
            where = self.source or config_file()
            raise ConfigError(
                "no password source configured: set RESTIC_PASSWORD_FILE or RESTIC_PASSWORD_COMMAND,"
                f" or `password_file` / `password_command` in {where}")


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
    return cfg
