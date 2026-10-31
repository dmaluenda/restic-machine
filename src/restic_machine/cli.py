# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Command line: `restic-machine <command>` (or `python -m restic_machine <command>`)."""

import argparse
import json
import logging
import sqlite3
import sys
import webbrowser
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from . import __version__, backup, config, copies, db, query, schedule, server
from .indexer import IndexBusy, Indexer
from .restic import RepositoryUnavailable, Restic, ResticError

EX_TEMPFAIL = 75  # sysexits.h: temporary failure, try again later
EX_CONFIG = 78

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # not a plain file when embedded or tested
            stream.reconfigure(errors="backslashreplace")
    logging.basicConfig(format="restic-machine: %(message)s", level=logging.INFO)
    try:
        cfg = config.load(args.config)
        if args.repo:
            cfg.restic_env["RESTIC_REPOSITORY"] = args.repo
            cfg.restic_env["RESTIC_REPOSITORY_FILE"] = None
        return args.command(cfg, args)
    except RepositoryUnavailable as e:
        _err(f"repository temporarily unavailable ({e}); try again later")
        return EX_TEMPFAIL
    except IndexBusy as e:
        _err(f"{e}; try again later")
        return EX_TEMPFAIL
    except (config.ConfigError, ResticError, db.IndexSchemaError, schedule.ScheduleError, backup.BackupError) as e:
        _err(str(e))
        return EX_CONFIG
    except LookupError as e:
        _err(e.args[0] if e.args else str(e))
        return 1
    except KeyboardInterrupt:
        _err("interrupted")
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="restic-machine", description="Browse the history of a restic repository.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help=f"config file (default: {config.config_file()})")
    parser.add_argument("-r", "--repo", metavar="REPOSITORY",
                        help="repository location (default: RESTIC_REPOSITORY or the config file)")
    commands = parser.add_subparsers(title="commands", required=True, metavar="COMMAND")

    index = commands.add_parser("index", help="read new snapshots into the index")
    index.add_argument("--limit", type=int, metavar="N", help="read at most N snapshots")
    index.set_defaults(command=cmd_index)

    snaps = commands.add_parser("snapshots", help="list the snapshots in the index")
    snaps.add_argument("--all", action="store_true", help="include snapshots restic no longer has")
    snaps.set_defaults(command=cmd_snapshots)

    ls = commands.add_parser("ls", help="list a directory as it was in a snapshot")
    ls.add_argument("snapshot", help="snapshot id (or a prefix), or `latest`")
    ls.add_argument("path", nargs="?", default="/", help="restic path, e.g. /home/me or /C/Users/me")
    ls.set_defaults(command=cmd_ls)

    info = commands.add_parser("info", help="show where the index is and what it holds")
    info.set_defaults(command=cmd_info)

    serve = commands.add_parser("serve", help="open the browser interface (and update the index)")
    serve.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free one)")
    serve.add_argument("--no-browser", action="store_true", help="don't open a browser, just print the address")
    serve.add_argument("--no-index", action="store_true", help="don't read new snapshots on start")
    serve.add_argument("--idle-timeout", type=float, default=0, metavar="MINUTES",
                       help="stop this long after the last browser tab is closed (default: never)")
    serve.set_defaults(command=cmd_serve)

    forget = commands.add_parser(
        "forget", help="apply the retention policy of the config file (removes snapshots!)",
        description="Apply the [retention] policy of the config file with `restic forget` (and"
                    " --prune if the policy says so). This changes the repository: run it from"
                    " the backup job, like restic's own forget. Edit the policy in the web page.")
    forget.add_argument("--dry-run", action="store_true", help="only show what it would keep and remove")
    forget.set_defaults(command=cmd_forget)

    backup = commands.add_parser(
        "backup", help="one backup run, as the schedule starts it (and forget + prune when due)",
        description="Back up the [backup] sources, logging into the repository's Windows share first if one"
                    " is set, and apply the retention policy on the run at the prune time (or after 30 h"
                    " without one). The log is backup.log next to the index.")
    backup.add_argument("--prune", choices=("auto", "always", "never"), default="auto",
                        help="apply the retention policy: when due (default), always, or not")
    backup.set_defaults(command=cmd_backup)

    check = commands.add_parser(
        "check", help="check a copy of the repository and record which snapshots it holds",
        description="For a copy of the repository ([[copies]] in the config file: a mirror of its files on"
                    " another disk), record which snapshots it holds and run `restic check` on it, without a"
                    " lock (it only reads). Meant for the job that makes the copy, right after copying. Exit"
                    " code: 0 the copy is sound, 1 restic check found problems, 75 the copy can't be reached"
                    " now, 78 not set up (no such copy, no location, or not a copy of this repository)."
                    " The log is check.log next to the index.")
    check.add_argument("--source", required=True, metavar="COPY", help="the copy's name")
    check.add_argument("--read-data-subset", metavar="N/T",
                       help="also read this part of its data, as restic check takes it (e.g. 11/12)")
    check.set_defaults(command=cmd_check)

    sched = commands.add_parser("schedule", help="show the backup schedule, or install it (Windows, asks for administrator)")
    sched.add_argument("--install", action="store_true", help="create or update the scheduled task (UAC prompt)")
    sched.add_argument("--disable", action="append", default=[], metavar="TASK",
                       help="also disable this other scheduled task (e.g. an older backup script)")
    sched.set_defaults(command=cmd_schedule)
    return parser


class Opened(NamedTuple):
    con: sqlite3.Connection
    restic: Restic
    repository_id: str
    available: bool  # False: the repository can't be read now, only its index


def _open(cfg: config.Config, offline_ok: bool) -> Opened:
    """The index of the configured repository. With offline_ok, an unreachable repository
    still gives access to its index as last updated."""
    label = cfg.repository_label()
    cfg.check_password_source()
    restic = Restic(cfg.restic_binary, cfg.restic_env)
    try:
        repository_id = restic.repository_id()
        db.remember_repository(cfg.data_dir, label, repository_id)
        available = True
    except RepositoryUnavailable as e:
        repository_id = db.known_repository(cfg.data_dir, label) if offline_ok else None
        if repository_id is None:
            raise
        _err(f"warning: repository unavailable ({e}); showing the index as last updated")
        available = False
    return Opened(db.open_index(cfg.data_dir, repository_id), restic, repository_id, available)


def cmd_index(cfg: config.Config, args) -> int:
    con, restic, _, _ = _open(cfg, offline_ok=False)
    with closing(con):
        Indexer(con, restic, progress=_print_progress).run(limit=args.limit)
    return 0


def cmd_snapshots(cfg: config.Config, args) -> int:
    con = _open(cfg, offline_ok=True).con
    with closing(con):
        rows = query.snapshots(con, include_forgotten=args.all)
    several = len({row["series"] for row in rows}) > 1
    for row in rows:
        if row["forgotten_at"]:
            state = "forgotten"
        elif row["seq"] is None:
            state = "not indexed yet"
        else:
            state = f"{row['n_entries']:>10,}  {_changes(row['n_added'], row['n_modified'], row['n_removed'])}"
        host = f"  {row['hostname']} {', '.join(json.loads(row['paths']))}" if several else ""
        print(f"{row['restic_id'][:8]}  {_when(row['time'])}{host}  {state}")
    if not rows:
        print("no snapshots indexed yet: run `restic-machine index`")
    return 0


def cmd_ls(cfg: config.Config, args) -> int:
    con = _open(cfg, offline_ok=True).con
    with closing(con):
        snap = query.find_snapshot(con, args.snapshot)
        entries = query.list_dir(con, snap, args.path, query.previous_snapshot(con, snap))
    marks = {"added": "+", "modified": "~", "changed": "*"}
    for entry in entries:
        is_dir = entry["type"] == "dir"
        files = f"{entry['files']:,} files" if is_dir else ""
        name = entry["name"] + ("/" if is_dir else "")
        print(f"{marks.get(entry['status'], ' ')} {entry['type'][0]}  {_size(entry['size']):>10}"
              f"  {files:>13}  {_mtime(entry['mtime_us'])}  {name}")
    return 0


def cmd_info(cfg: config.Config, args) -> int:
    con = _open(cfg, offline_ok=True).con
    with closing(con):
        repository_id = con.execute("SELECT value FROM meta WHERE key = 'repository_id'").fetchone()[0]
        (path,) = [row["file"] for row in con.execute("PRAGMA database_list") if row["name"] == "main"]
        counts = con.execute(
            "SELECT count(*) FILTER (WHERE seq IS NOT NULL AND forgotten_at IS NULL) AS indexed,"
            " count(*) FILTER (WHERE seq IS NULL AND forgotten_at IS NULL) AS pending,"
            " count(*) FILTER (WHERE forgotten_at IS NOT NULL) AS forgotten FROM snapshot").fetchone()
        versions = con.execute("SELECT count(*) FROM version").fetchone()[0]
        paths = con.execute("SELECT count(*) FROM path").fetchone()[0]
    print(f"repository  {repository_id}")
    print(f"index       {path} ({_size(Path(path).stat().st_size)})")
    print(f"snapshots   {counts['indexed']} indexed, {counts['pending']} not indexed yet,"
          f" {counts['forgotten']} forgotten")
    print(f"contents    {paths:,} paths, {versions:,} versions")
    if cfg.source:
        print(f"config      {cfg.source}")
    return 0


def cmd_serve(cfg: config.Config, args) -> int:
    # One server per user, found through a runtime file in the data directory.
    runtime = config.default_data_dir() / "server.json"
    running = server.running_instance(runtime)
    if running:
        print(f"restic-machine is already running at {running}", flush=True)
        if not args.no_browser:
            webbrowser.open(running)
        return 0

    idle_timeout = args.idle_timeout * 60 if args.idle_timeout else None
    app = server.App(cfg, idle_timeout)  # with no usable repository, it opens on the setup page
    if app.problem:
        _err(f"not ready yet ({app.problem}): set it up in the page that opens")
    httpd = server.make_server(app, args.port)
    url = server.url_of(httpd.server_address[1], app.token)
    server.write_runtime(runtime, httpd.server_address[1], app.token)
    if not app.indexing["unavailable"] and not args.no_index:
        app.start_indexing()
    print(f"restic-machine is running at {url}\n"
          "(the link is the key: keep it private). Press Ctrl+C or the Stop button to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        server.remove_runtime(runtime, app.token)
    print("restic-machine stopped", flush=True)
    return 0


def cmd_forget(cfg: config.Config, args) -> int:
    policy = cfg.retention
    if not config.has_rules(policy):
        raise config.ConfigError(f"no retention policy in {cfg.path}: set one in the web page (Retention)"
                                 " or under [retention]")
    cfg.repository_label()
    cfg.check_password_source()
    restic = Restic(cfg.restic_binary, cfg.restic_env)
    policy_args = config.forget_args(policy)
    if args.dry_run:
        kept = removed = 0
        for group in restic.forget_preview(policy_args):
            reasons = {r["snapshot"]["id"]: r["matches"] for r in group.get("reasons") or []}
            for snap in group.get("keep") or []:
                kept += 1
                print(f"keep    {_when(snap['time'])}  {snap['id'][:8]}  {', '.join(reasons.get(snap['id'], []))}")
            for snap in group.get("remove") or []:
                removed += 1
                print(f"remove  {_when(snap['time'])}  {snap['id'][:8]}")
        print(f"would keep {kept} snapshot(s) and remove {removed}"
              f"{', then prune' if policy.get('prune') and removed else ''}")
        return 0
    print(f"restic-machine: applying the retention policy of {cfg.path}", flush=True)
    return restic.forget(policy_args, policy.get("prune", True))


def cmd_backup(cfg: config.Config, args) -> int:
    return backup.run(cfg, prune=args.prune)


def cmd_check(cfg: config.Config, args) -> int:
    return copies.check(cfg, args.source, args.read_data_subset)


def cmd_schedule(cfg: config.Config, args) -> int:
    if args.install:
        result = schedule.install(cfg, args.disable)
        if not result.get("ok"):
            raise schedule.ScheduleError(result.get("error", "could not install the schedule"))
        print(f"installed: {schedule.TASK_NAME}, {schedule.describe(cfg.backup['schedule'])}")
    current = schedule.state()
    if not current["supported"]:
        print("scheduled backups are only handled on Windows so far")
        return 0
    for task in ([current["ours"]] if current["ours"] else []) + current["others"]:
        print(f"{task['name']}: {task['state']}, last run {task['last_run'] or '-'} (result {task['last_result']}),"
              f" next {task['next_run'] or '-'}  [{task['action']}]")
    if not current["ours"]:
        print(f"{schedule.TASK_NAME}: not installed (restic-machine schedule --install)")
    return 0


def _print_progress(event: str, **info) -> None:
    if event == "start":
        _err(f"reading {info['total']} snapshot(s)..." if info["total"] else "index is up to date")
    elif event == "snapshot":
        snap = info["snapshot"]
        _err(f"[{info['n']}/{info['total']}] {snap['restic_id'][:8]}  {_when(snap['time'])}"
             f"  {info['entries']:>10,} entries"
             f"  {_changes(info['added'], info['modified'], info['removed'])}  {info['seconds']:.1f} s")
    elif event == "forgotten":
        _err(f"{info['count']} snapshot(s) no longer in the repository: hidden, kept in the index for now")
    elif event == "adopted":
        _err(f"snapshot {info['snapshot'][:8]} replaces {info['replaces'][:8]} (same content)")
    elif event == "rewind":
        lost = f"; {info['lost']} of them no longer exist and lose their contents" if info["lost"] else ""
        _err(f"a snapshot arrived out of order: re-reading {info['snapshots']} later snapshot(s){lost}")


def _changes(added: int | None, modified: int | None, removed: int | None) -> str:
    if added is None:
        return "(first)"
    return f"+{added:,} ~{modified:,} -{removed:,}"


def _when(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M")


def _mtime(us: int | None) -> str:
    if us is None:
        return " " * 16
    try:
        return (_EPOCH + timedelta(microseconds=us)).astimezone().strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?".ljust(16)


def _size(n: int | None) -> str:
    if n is None:
        return ""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _err(message: str) -> None:
    print(f"restic-machine: {message}", file=sys.stderr, flush=True)
