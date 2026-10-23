# restic-machine

Browse the history of a [restic](https://restic.net) repository: a timeline of your
snapshots and, for any of them, the tree of files as it was then, with sizes and
modification times.

> **Status: early development.** The indexer and a command line work; the web interface
> (timeline + tree, download and restore) comes next.

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
- **No dependencies:** Python 3.11+ standard library only.

## Requirements

- Python 3.11 or newer
- restic 0.17 or newer

## Usage

From a clone, without installing anything:

```sh
PYTHONPATH=src python -m restic_machine index        # read new snapshots into the index
PYTHONPATH=src python -m restic_machine snapshots    # list them
PYTHONPATH=src python -m restic_machine ls latest /home/me
```

Or install it (`pip install .`) and use the `restic-machine` command.

The first `index` reads every snapshot (about half a minute per snapshot for a home folder
of several hundred thousand files); later runs only read the new ones, so it can run right
after each backup.

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
git checkout; keep it private.

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
