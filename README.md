# restic-machine

Browse the history of a [restic](https://restic.net) repository: a timeline of your
snapshots and, for any of them, the tree of files as it was then, with sizes and
modification times.

> **Status: early development.** Browsing works (timeline, tree, sizes over time);
> downloading and restoring files comes next.

In your browser you get:

- **a timeline** of the snapshots, each bar as tall as the number of changes it brought,
  so an unusual one (thousands of files changed at once) stands out;
- **the folder tree** as it was in the chosen snapshot, with the size and file count of
  every folder, and what is new, modified or removed since the previous snapshot;
- **the size of any file or folder over time**, and the dates on which it changed.

## How it works

- **It only reads your repository.** restic-machine runs restic's own command line
  (`restic ... --json`), so it works with every backend restic supports. Every call is
  read-only and uses `--no-lock`: browsing never writes a lock into the repository and
  can't make your `forget --prune` fail. If a read happens to collide with a prune, the
  repository is just reported as temporarily unavailable.
- **It keeps a local index** of what each snapshot contains (SQLite). A file that doesn't
  change is stored once for all the snapshots it appears in, so the index grows with what
  changes, not with the number of snapshots. New snapshots are read incrementally.
- **Snapshots you forget** disappear from the timeline but stay in the index for a while,
  so you can look into what happened.
- **It runs on your computer only.** The web interface listens on 127.0.0.1 and asks for a
  random key that changes every time it starts (it is in the link it opens), so other web
  pages and other users can't use it.
- **No dependencies:** Python 3.11+ standard library only.

## Requirements

- Python 3.11 or newer
- restic 0.17 or newer

## Usage

Configure the repository (see below), then start it from a clone, without installing
anything: double-click **`restic-machine.pyw`** on Windows, or run **`./restic-machine.sh`**
on Linux and macOS. It reads any new snapshots in the background and opens your browser.
Or install it (`pip install .`) and run `restic-machine-app` (or `restic-machine serve` in a
terminal).

On Windows it runs without any window of its own: just the browser tab. Starting it again
while it runs only opens the tab again. It stops by itself ten minutes after you close its
last tab, or right away with the **Stop** button. If it can't start, a dialog says why, and
everything it would have printed goes to `~/.cache/restic-machine/restic-machine.log`.

The first run reads every snapshot (about half a minute per snapshot for a home folder of
several hundred thousand files) and you can browse what is already read meanwhile; later
runs only read the new ones. There is a command line too:

```sh
restic-machine index                  # read new snapshots (e.g. right after each backup)
restic-machine snapshots              # list them
restic-machine ls latest /home/me     # a folder as it was in a snapshot
restic-machine info                   # where the index is and what it holds
```

(From a clone without installing: `PYTHONPATH=src python -m restic_machine ...`.)

## Configuration

restic-machine uses restic's environment variables (`RESTIC_REPOSITORY`,
`RESTIC_PASSWORD_FILE`, `RESTIC_PASSWORD_COMMAND`, backend credentials...). To keep
settings in a file instead, copy [`config.example.toml`](config.example.toml) to
`~/.config/restic-machine/config.toml`.

| What | Default | Override |
|---|---|---|
| Config file | `~/.config/restic-machine/config.toml` | `RESTIC_MACHINE_CONFIG`, `--config` |
| Index | `~/.cache/restic-machine/` | `RESTIC_MACHINE_DATA`, `data_dir` |

The index lists every file name in your backups. It never goes inside the package or a
git checkout; keep it private. When a new version changes its format, the index is upgraded
in place on first use (a copy of the old one is kept next to it), never rebuilt.

## Development

```sh
PYTHONPATH=src python -m unittest discover -s tests
```

The end-to-end tests build a throwaway repository with restic; they run when `restic` is on
`PATH` (or `RESTIC_MACHINE_TEST_RESTIC` points at it) and are skipped otherwise.

## License

Copyright (C) 2026 David Maluenda.

restic-machine is free software under the [GNU Affero General Public License v3](LICENSE)
or any later version: you may use, study, share and modify it, and modified versions you
distribute or offer over a network must be released under the same license. As an
additional term (section 7(b)), derived works must keep the attribution
"restic-machine by David Maluenda" in the notices of their user interface and source
code; see [NOTICE](NOTICE).
