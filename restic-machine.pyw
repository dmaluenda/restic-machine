# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""Double-click to open restic-machine in your browser (on Windows, without a console window).

It stops by itself ten minutes after you close its last browser tab, or with its Stop button.
The log is in ~/.cache/restic-machine/restic-machine.log.
"""

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep the checkout clean (it may live in a synced folder)
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from restic_machine.desktop import main  # noqa: E402

sys.exit(main())
