# restic-machine

A visual interface for [restic](https://restic.net), running on the computer you back up.
Browse the history of your repository: a timeline of your snapshots and, for any of them,
the tree of files as it was then, with sizes and modification times. Manage its retention
policy and see what it would keep before it deletes anything.

> **Status: early development.** Browsing, settings and the retention policy work.
> Next: restic's commands from the interface, downloading and restoring files, and the
> backup itself.

In your browser you get:

- **Browse:** a timeline of the snapshots, each bar as tall as the number of changes it
  brought, so an unusual one (thousands of files changed at once) stands out; the folder
  tree as it was in the chosen snapshot, with the size and file count of every folder, and
  what is new, modified or removed since the previous snapshot; the size of any file or
  folder over time; and a button to leave a file or folder out of the next backups (it adds
  the path to your backup's exclude file).
- **Retention:** the retention policy (which snapshots to keep), a calendar of what it
  would leave on any day from today on (with the backups still to come), and a check with
  restic itself of what it would keep and remove right now, and why.
- **Repository:** restic's commands (coming next).
- **Settings:** where the repository is and how restic gets its password. When something
  is missing or wrong, restic-machine opens on this page.

## How it works

- **It runs restic's own command line** (`restic ... --json`), so it works with every
  backend restic supports.
- **Browsing only reads.** Every read uses `--no-lock`, so it never writes a lock into the
  repository and can't make a `forget --prune` fail. If a read happens to collide with a
  prune, the repository is just reported as temporarily unavailable.
- **Changes are explicit.** restic itself keeps no retention policy: it is whatever
  `restic forget` is given. restic-machine keeps yours in its config file; the web page
  shows and simulates it (a dry run), and `restic-machine forget` applies it, typically from
  your backup job, exactly like `restic forget --keep-… --prune` would.
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

Start it from a clone, without installing anything: double-click **`restic-machine.pyw`**
on Windows, or run **`./restic-machine.sh`** on Linux and macOS. It opens your browser
(on the Settings page if the repository isn't configured yet) and reads any new snapshots
in the background.
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
restic-machine forget --dry-run       # what the retention policy would keep and remove
restic-machine forget                 # apply it (this deletes snapshots: run it from your backup job)
```

(From a clone without installing: `PYTHONPATH=src python -m restic_machine ...`.)

### Retention from your backup job

Set the policy in the Retention page, then replace the `restic forget --keep-… --prune`
line of your backup script with `restic-machine forget`. It uses the same repository
settings (or `-r REPOSITORY`), prints restic's output and returns restic's exit code.

## Configuration

Use the Settings page, or restic's environment variables (`RESTIC_REPOSITORY`,
`RESTIC_PASSWORD_FILE`, `RESTIC_PASSWORD_COMMAND`, backend credentials...), or edit
`~/.config/restic-machine/config.toml` by hand (see [`config.example.toml`](config.example.toml)).
restic-machine never stores your password, only where restic finds it.

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
