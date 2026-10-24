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
  tree as it was in the chosen snapshot (open folders in place, right-click one to expand a
  level, everything below it, or collapse it), with the size and file count of every folder,
  and what is new, modified or removed since the previous snapshot; Ctrl+click two snapshots
  and Compare to see only what changed between them, with the change in size; the size of
  any file or folder over time; and a button to leave a file or folder out of the next
  backups (it adds the path to your backup's exclude file).
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
- restic 0.17 or newer (see below)

## Getting started

### 1. Install restic

restic-machine drives restic; it doesn't replace it. restic is a single program file:

- **Windows:** download `restic_<version>_windows_amd64.zip` from
  [restic's releases](https://github.com/restic/restic/releases), unzip it and put
  `restic.exe` in a folder of your choice (for instance `C:\Tools\restic\`). Either add that
  folder to your `PATH` or give its full path in Settings → *restic program*. (`winget install
  restic.restic` and `scoop install restic` work too.)
- **Linux:** your distribution's package (`apt install restic`, `dnf install restic`…) if it
  is recent enough (`restic version`), otherwise the release binary as above.
- **macOS:** `brew install restic`.

Check it with `restic version`, and keep it current with `restic self-update`.

### 2. Choose where the backups go

A restic repository is a folder (or a bucket in the cloud). Keep it on **another disk** than the
data it protects, and better on another machine. restic encrypts everything it writes with the
repository password: keep that password in a file only you can read (restic-machine never
stores it, only where to find it), and keep a copy somewhere safe — without it the backups can't
be read.

- **A second internal disk.** Create a folder on it, say `D:\restic-repo` (Windows) or
  `/srv/restic-repo` (Linux), and use that path as the repository.
- **An external USB disk.** The same, with a folder on the USB disk. The backups only run while
  it is plugged in: a backup started without it fails and the next one catches up. On Windows,
  give the disk a fixed drive letter (Disk Management → *Change Drive Letter and Paths*) so the
  path stays the same; on Linux, mount it by label or UUID (an `/etc/fstab` line with `nofail`).
- **A network share (SMB), e.g. a NAS or a server running Samba.** Create a share and a user that
  can write to it on the server (setting up Samba itself is outside restic-machine), then use
  `\\server\share\restic-repo` as the repository. If the share needs its own login, give it in
  Retention → *Backups* → *Repository on a Windows share*: the share, the user, and a **file**
  with that user's password. restic-machine logs in before each backup, by IP address if you
  ask it to (Windows allows only one login per server name, and you may already be browsing that
  server as someone else). On Linux, mount the share instead (`mount -t cifs` or an `/etc/fstab`
  line with a credentials file) and use the mounted path.
- restic also writes to SFTP servers, its REST server, S3-compatible storage, Backblaze B2 and
  more (see restic's documentation): those work in restic-machine as they do in restic.

Then create the repository once, from a terminal: `restic -r <path> init` (it asks for the new
password), and put that password in its file.

### 3. Point restic-machine at it

Start restic-machine (below). With no repository configured it opens on **Settings**: give the
repository, the password file, and press *Test connection*, then *Save*. Then, in **Retention**:

- **Backups:** the folders to back up, when (every N hours between two times, plus fixed times),
  and *Save and schedule*. On Windows this creates a Task Scheduler task that runs
  `restic-machine backup` on its own, with restic-machine closed and nobody logged in; Windows
  asks for administrator rights to create it (the task runs with them, for VSS snapshots of files
  in use) and keeps **no password** for it.
- **Retention policy:** which snapshots to keep. The backup run at the prune time applies it.

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
restic-machine backup                 # one backup run, as the schedule starts it
restic-machine schedule [--install]   # the Windows task that runs the backups
```

(From a clone without installing: `PYTHONPATH=src python -m restic_machine ...`.)

### Keeping your own backup script

If you'd rather keep your own scheduled script, let it call `restic-machine backup` (or just
replace its `restic forget --keep-… --prune` line with `restic-machine forget`): the policy
then lives in restic-machine and its pages show and simulate it. Both use restic's exit code.

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
