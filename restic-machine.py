# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""restic-machine's command line, from a clone, without installing it:

    python restic-machine.py check --source offline-disk --read-data-subset 3/12

(The same as `restic-machine ...` once installed. `python restic-machine.py --help` lists the
commands.) Handy over SSH, e.g. from the job that makes a copy of the repository.
"""

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep the checkout clean (it may live in a synced folder)
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from restic_machine.cli import main  # noqa: E402

sys.exit(main())
