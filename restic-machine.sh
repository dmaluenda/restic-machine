#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Start restic-machine from this folder and open it in the browser. Ctrl+C stops it.
cd "$(dirname "$0")" || exit 1
PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m restic_machine serve "$@"
